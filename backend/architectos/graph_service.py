"""Graph memory model: auto-linker engine, graph read view, and graph commands.

Extracted from ``service.py``. ``GraphAutoLinker`` is a standalone class
(instantiated in ``ArchitectOSService.__init__`` as ``self.graph_auto_linker``);
``GraphServiceMixin`` groups the graph view (``graph()``), the graph-analytics
and code-graph query methods, LLM-assisted link/consolidation suggestions, and
the graph edit commands (merge/rename/relink/delete). Token helpers are shared
with the ingestion engine (imported from ``ingestion_service``); repository,
lifecycle and provider access reaches the service through ``self`` via the MRO,
so behavior is unchanged.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import re
from typing import Any

from .adapters import ProviderRequest
from .constants import DEFAULT_EXCLUDES, NOISE_FILE_SUFFIXES
from .embeddings import content_tokens
from .graph_analysis import compute_degrees, detect_communities
from .ingestion_service import _token_set, _token_similarity
from .models import stable_id, utc_now
from .storage import SQLiteMemoryRepository


GRAPH_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "will", "shall",
    "should", "have", "has", "are", "was", "were", "been", "being",
}

SOURCE_HUB_CATALOG: dict[str, dict[str, str]] = {
    "docs": {"label": "Repo docs", "text": "Local repository documentation and markdown memory for this scope."},
    "code": {"label": "Code", "text": "Source code and configuration artifacts for this scope."},
    "git": {"label": "Git", "text": "Commit history and git-derived memory for this scope."},
    "adr": {"label": "ADRs", "text": "Architecture decision records for this scope."},
    "issues": {"label": "Issue files", "text": "Local issue/bug markdown files for this scope (not Azure Boards)."},
    "prs": {"label": "PR notes", "text": "Local pull-request notes and templates for this scope (not remote PRs)."},
    "meetings": {"label": "Meeting notes", "text": "Local meeting note files for this scope (prefer Granola for live meetings)."},
    "chat": {"label": "App chat", "text": "ArchitectOS chat-derived memory for this scope."},
    "inbox": {"label": "Inbox", "text": "Files dropped into the inbox folder for this scope."},
    "granola": {"label": "Granola", "text": "Granola meeting memory for this scope."},
    "azure-boards": {"label": "Azure Boards", "text": "Azure Boards work items for this scope."},
    "azure-wiki": {"label": "Azure Wiki", "text": "Azure Wiki pages for this scope."},
    "azure-git": {"label": "Azure Git", "text": "Azure Repos repositories and pull requests for this scope."},
    "teams-meetings": {"label": "Teams", "text": "Microsoft Teams transcripts and AI/Facilitator insights for this scope."},
    "project_scan": {"label": "Project scan", "text": "Files imported by project scan for this scope."},
    "manual": {"label": "Manual", "text": "Manually captured memory for this scope."},
    "other": {"label": "Other", "text": "Ungrouped memory for this scope."},
}
SOURCE_HUB_ALIASES = {
    "doc": "docs",
    "documentation": "docs",
    "project_scan": "project_scan",
    "ui": "manual",
    "memory_candidate": "manual",
    "promoted_candidate": "manual",
    "azure_boards": "azure-boards",
    "boards": "azure-boards",
    "azure_wiki": "azure-wiki",
    "wiki": "azure-wiki",
    "azure_git": "azure-git",
    "azure_repos": "azure-git",
    "ado_git": "azure-git",
    "ado_repos": "azure-git",
    "teams": "teams-meetings",
    "teams_meetings": "teams-meetings",
    "ms_teams": "teams-meetings",
    "facilitator": "teams-meetings",
    "pr": "prs",
    "pull_request": "prs",
    "pull_requests": "prs",
    "issue": "issues",
    "meeting": "meetings",
    "commit": "git",
    "commits": "git",
    "git_cluster": "git",
}


class GraphAutoLinker:
    def __init__(self, repository: SQLiteMemoryRepository) -> None:
        self.repository = repository

    def link_node(
        self,
        node: Any,
        project_id: str | None = None,
        *,
        hub_cache: dict[tuple[str, str | None, str], Any] | None = None,
        existing_edge_ids: set[str] | None = None,
        nodes_snapshot: list[Any] | None = None,
        project_root_cache: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._is_structural_node(node):
            return {"created": 0, "edges": [], "linked_nodes": 0}
        scope = str(getattr(node, "scope", None) or "project")
        resolved_project_id = project_id or getattr(node, "project_id", None)
        if scope == "project":
            resolved_project_id = resolved_project_id or "architectos"
        else:
            resolved_project_id = None
        source_key = self._source_key_for_node(node)
        hub = self._ensure_source_hub(
            scope,
            resolved_project_id,
            source_key,
            hub_cache=hub_cache,
            nodes_snapshot=nodes_snapshot,
        )
        planned: list[tuple[str, str, str, str, float, str]] = [
            (hub.id, node.id, self._hub_leaf_edge_type(node, source_key), scope, 0.8, "source_hub"),
        ]
        if scope == "project":
            root = self._project_root_node(
                resolved_project_id or "architectos",
                nodes_snapshot=nodes_snapshot,
                root_cache=project_root_cache,
            )
            if root and root.id != hub.id:
                planned.append((root.id, hub.id, "HAS_MEMORY", "project", 0.9, "project_source_hub"))
        else:
            scope_root = self._ensure_scope_root(scope, nodes_snapshot=nodes_snapshot, hub_cache=hub_cache)
            if scope_root.id != hub.id:
                planned.append((scope_root.id, hub.id, "HAS_MEMORY", scope, 0.9, "scope_source_hub"))
        return self._write_edges(planned, existing_edge_ids=existing_edge_ids)

    def rebuild(self, project_id: str | None = None, similarity_limit: int = 3) -> dict[str, Any]:
        nodes = [node for node in self.repository.list_nodes() if node.status == "active"]
        if project_id:
            nodes = [
                node for node in nodes
                if node.project_id in {project_id, None} or str(getattr(node, "scope", "") or "") in {"shared", "global"}
            ]
        hub_cache: dict[tuple[str, str | None, str], Any] = {}
        project_root_cache: dict[str, Any] = {}
        existing_edge_ids = self.repository.list_edge_ids()
        root = self._project_root_node(project_id or "architectos", nodes_snapshot=nodes, root_cache=project_root_cache)

        planned: list[tuple[str, str, str, str, float, str]] = []
        content_nodes = [node for node in nodes if not self._is_structural_node(node)]
        by_id = {node.id: node for node in content_nodes}
        for node in content_nodes:
            scope = str(getattr(node, "scope", None) or "project")
            node_project_id = getattr(node, "project_id", None)
            if scope == "project":
                node_project_id = node_project_id or project_id or "architectos"
            else:
                node_project_id = None
            source_key = self._source_key_for_node(node)
            hub = self._ensure_source_hub(
                scope,
                node_project_id,
                source_key,
                hub_cache=hub_cache,
                nodes_snapshot=nodes,
            )
            planned.append((hub.id, node.id, self._hub_leaf_edge_type(node, source_key), scope, 0.8, "source_hub"))
            if scope == "project" and root and root.id != hub.id:
                planned.append((root.id, hub.id, "HAS_MEMORY", "project", 0.9, "project_source_hub"))
            elif scope != "project":
                scope_root = self._ensure_scope_root(scope, nodes_snapshot=nodes, hub_cache=hub_cache)
                if scope_root.id != hub.id:
                    planned.append((scope_root.id, hub.id, "HAS_MEMORY", scope, 0.9, "scope_source_hub"))

        # Inverted-index similarity: avoid O(n²) pairwise Jaccard over the full corpus.
        # Code artifacts are excluded: path/token overlap between source files created
        # tens of thousands of meaningless RELATED_TO edges (33k on the live corpus).
        similarity_nodes = [node for node in content_nodes if not self._is_code_artifact(node)]
        token_map = {node.id: self._similarity_tokens(node) for node in similarity_nodes}
        inverted: dict[str, list[str]] = {}
        for node_id, tokens in token_map.items():
            for token in tokens:
                inverted.setdefault(token, []).append(node_id)
        # Drop ultra-common tokens so one frequent word cannot explode candidates back toward O(n²).
        max_df = max(48, len(similarity_nodes) // 40)
        inverted = {token: ids for token, ids in inverted.items() if 1 < len(ids) <= max_df}
        candidate_cap = max(24, similarity_limit * 16)
        max_expansions = 1800

        for node in similarity_nodes:
            left = token_map.get(node.id) or set()
            if not left:
                continue
            # Prefer rare tokens; bound posting-list walks so large corpora stay interactive.
            ranked_tokens = sorted(
                (token for token in left if token in inverted),
                key=lambda token: len(inverted[token]),
            )
            shares: Counter[str] = Counter()
            expansions = 0
            for token in ranked_tokens:
                postings = inverted[token]
                for other_id in postings:
                    if other_id != node.id:
                        shares[other_id] += 1
                expansions += len(postings)
                if expansions >= max_expansions and len(shares) >= candidate_cap:
                    break
            scored: list[tuple[float, Any]] = []
            for other_id, _inter in shares.most_common(candidate_cap):
                other = by_id.get(other_id)
                if other is None:
                    continue
                score = self._similarity(left, token_map.get(other_id) or set())
                if score >= 0.32:
                    scored.append((score, other))
            for score, other in sorted(scored, key=lambda item: (-item[0], item[1].label))[:similarity_limit]:
                source, target = sorted([node.id, other.id])
                planned.append(
                    (source, target, "RELATED_TO", node.scope or other.scope or "project", min(0.88, 0.48 + score), "similarity")
                )

        written = self._write_edges(planned, existing_edge_ids=existing_edge_ids)
        pruned = self.prune_empty_source_hubs(project_id)
        return {**written, "root": root.id if root else "", "candidate_edges": len(planned), "pruned_hubs": pruned}

    def prune_empty_source_hubs(self, project_id: str | None = None) -> dict[str, Any]:
        """Remove source hubs and scope roots that have no content children."""
        nodes = self._active_nodes_for_project(project_id)
        by_id = {node.id: node for node in nodes}
        content_ids = {node.id for node in nodes if not self._is_structural_node(node)}
        child_targets = self._outgoing_targets(by_id)

        pruned_hubs = 0
        for node in list(nodes):
            meta = dict(node.metadata or {})
            if not meta.get("hub"):
                continue
            children = child_targets.get(node.id) or set()
            if any(child_id in content_ids for child_id in children):
                continue
            node.status = "deleted"
            meta["deleted_at"] = utc_now()
            meta["delete_reason"] = "empty_source_hub"
            node.metadata = meta
            self.repository.upsert_node(node)
            pruned_hubs += 1

        nodes = self._active_nodes_for_project(project_id)
        by_id = {node.id: node for node in nodes}
        child_targets = self._outgoing_targets(by_id)
        active_hub_ids = {node.id for node in nodes if dict(node.metadata or {}).get("hub")}
        pruned_scope_roots = 0
        for node in nodes:
            meta = dict(node.metadata or {})
            if not meta.get("scope_root"):
                continue
            children = child_targets.get(node.id) or set()
            if any(child_id in active_hub_ids for child_id in children):
                continue
            node.status = "deleted"
            meta["deleted_at"] = utc_now()
            meta["delete_reason"] = "empty_scope_root"
            node.metadata = meta
            self.repository.upsert_node(node)
            pruned_scope_roots += 1
        return {"hubs": pruned_hubs, "scope_roots": pruned_scope_roots}

    def _is_code_artifact(self, node: Any) -> bool:
        if str(getattr(node, "type", "") or "") != "Artifact":
            return False
        return self._source_key_for_node(node) == "code"

    def prune_code_artifact_similarity_edges(self, project_id: str | None = None, dry_run: bool = True) -> dict[str, Any]:
        """Drop RELATED_TO edges that touch code artifacts (legacy noise from before
        code artifacts were excluded from similarity linking). Dry-run by default."""
        nodes = self._active_nodes_for_project(project_id)
        by_id = {node.id: node for node in nodes}
        code_ids = {node.id for node in nodes if self._is_code_artifact(node)}
        if not code_ids:
            return {"dry_run": dry_run, "pruned": 0, "items": []}
        pruned = 0
        items: list[dict[str, Any]] = []
        for edge in self.repository.list_edges():
            if str(getattr(edge, "type", "") or "") != "RELATED_TO":
                continue
            if edge.source not in code_ids and edge.target not in code_ids:
                continue
            if edge.source not in by_id and edge.target not in by_id:
                continue
            pruned += 1
            if len(items) < 100:
                items.append({"id": edge.id, "source": edge.source, "target": edge.target})
            if not dry_run:
                self.repository.delete_edge(edge.id)
        return {"dry_run": dry_run, "pruned": pruned, "items": items}

    def _active_nodes_for_project(self, project_id: str | None = None) -> list[Any]:
        nodes = [node for node in self.repository.list_nodes() if node.status == "active"]
        if not project_id:
            return nodes
        return [
            node for node in nodes
            if node.project_id in {project_id, None} or str(getattr(node, "scope", "") or "") in {"shared", "global", "interface"}
        ]

    def _outgoing_targets(self, by_id: dict[str, Any]) -> dict[str, set[str]]:
        child_targets: dict[str, set[str]] = {}
        for edge in self.repository.list_edges():
            if edge.status != "active":
                continue
            if edge.source not in by_id or edge.target not in by_id:
                continue
            child_targets.setdefault(edge.source, set()).add(edge.target)
        return child_targets

    def _ensure_source_hub(
        self,
        scope: str,
        project_id: str | None,
        source_key: str,
        *,
        hub_cache: dict[tuple[str, str | None, str], Any] | None = None,
        nodes_snapshot: list[Any] | None = None,
    ) -> Any:
        source_key = self._normalize_source_key(source_key)
        catalog = SOURCE_HUB_CATALOG.get(source_key) or SOURCE_HUB_CATALOG["other"]
        label = f"Source: {catalog['label']}"
        hub_project_id = project_id if scope == "project" else None
        cache_key = (scope, hub_project_id or None, source_key)
        if hub_cache is not None and cache_key in hub_cache:
            return hub_cache[cache_key]
        nodes = nodes_snapshot if nodes_snapshot is not None else self.repository.list_nodes()
        for node in nodes:
            if node.status != "active":
                continue
            meta = dict(node.metadata or {})
            if (
                meta.get("hub")
                and str(meta.get("source_key") or "") == source_key
                and str(node.scope or "") == scope
                and (node.project_id or None) == (hub_project_id or None)
            ):
                if hub_cache is not None:
                    hub_cache[cache_key] = node
                return node
        text = f"{catalog['text']} Scope: {scope}."
        if hub_project_id:
            text += f" Project: {hub_project_id}."
        # Stable label includes source_key so ids never collide across hubs.
        hub = self.repository.add_node(
            "Concept",
            label,
            scope,
            text,
            hub_project_id,
            confidence=0.95,
            metadata={
                "hub": True,
                "source_key": source_key,
                "source": "source_hub",
                "source_type": source_key,
                "structural": True,
            },
        )
        if hub_cache is not None:
            hub_cache[cache_key] = hub
        if nodes_snapshot is not None:
            nodes_snapshot.append(hub)
        return hub

    def _ensure_scope_root(
        self,
        scope: str,
        *,
        nodes_snapshot: list[Any] | None = None,
        hub_cache: dict[tuple[str, str | None, str], Any] | None = None,
    ) -> Any:
        cache_key = (scope, None, f"__scope_root__:{scope}")
        if hub_cache is not None and cache_key in hub_cache:
            return hub_cache[cache_key]
        label = f"Scope: {scope}"
        nodes = nodes_snapshot if nodes_snapshot is not None else self.repository.list_nodes()
        for node in nodes:
            meta = dict(node.metadata or {})
            if node.status == "active" and meta.get("scope_root") and str(node.scope or "") == scope and node.project_id is None:
                if hub_cache is not None:
                    hub_cache[cache_key] = node
                return node
        root = self.repository.add_node(
            "Concept",
            label,
            scope,
            f"Root container for {scope}-scoped memory source groups.",
            None,
            confidence=0.98,
            metadata={"scope_root": True, "structural": True, "source": "scope_root"},
        )
        if hub_cache is not None:
            hub_cache[cache_key] = root
        if nodes_snapshot is not None:
            nodes_snapshot.append(root)
        return root

    def _project_root_node(
        self,
        project_id: str,
        *,
        nodes_snapshot: list[Any] | None = None,
        root_cache: dict[str, Any] | None = None,
    ) -> Any | None:
        if root_cache is not None and project_id in root_cache:
            return root_cache[project_id]
        nodes = nodes_snapshot if nodes_snapshot is not None else self.repository.list_nodes()
        found = None
        for node in nodes:
            if node.type == "Project" and node.project_id == project_id:
                found = node
                break
        if found is None:
            for node in nodes:
                if node.type == "Project" and node.label.lower() == project_id.lower():
                    found = node
                    break
        if found is None:
            for node in nodes:
                if node.type == "Project" and node.label == "ArchitectOS":
                    found = node
                    break
        if root_cache is not None:
            root_cache[project_id] = found
        return found

    def _source_key_for_node(self, node: Any) -> str:
        metadata = dict(getattr(node, "metadata", {}) or {})
        raw = str(
            metadata.get("source_type")
            or metadata.get("source")
            or metadata.get("template")
            or ""
        ).strip().lower()
        if raw in {"source_hub", "scope_root", "project_profile"}:
            return "other"
        key = self._normalize_source_key(raw)
        if key in SOURCE_HUB_CATALOG and key not in {"other", "project_scan"}:
            return key
        if key == "project_scan" or raw == "project_scan":
            path = str(metadata.get("path") or getattr(node, "label", "") or "").lower()
            if path.endswith((".md", ".txt", ".rst", ".adoc")):
                return "docs"
            return "code"
        path = str(metadata.get("path") or metadata.get("extension") or getattr(node, "label", "") or "").lower()
        if any(path.endswith(ext) for ext in (".md", ".txt", ".rst", ".adoc")):
            return "docs"
        if any(ext in path for ext in (".py", ".java", ".js", ".ts", ".tsx", ".jsx", ".css", ".json", ".yml", ".yaml", ".xml")):
            return "code"
        node_type = str(getattr(node, "type", "") or "").lower()
        if node_type == "meeting":
            return "meetings"
        if node_type == "doc":
            return "docs"
        if node_type in {"artifact", "feature", "requirement", "constraint"} and "azure" in raw:
            if "git" in raw or "repo" in raw or "pull" in raw or "pr" in raw:
                return "azure-git"
            return "azure-boards" if "board" in raw or "work" in raw else key
        if not raw or raw in {"ui", "manual", "memory_candidate"}:
            return "manual"
        return key if key in SOURCE_HUB_CATALOG else "other"

    def _normalize_source_key(self, value: str) -> str:
        key = str(value or "").strip().lower().replace("_", "-")
        key = SOURCE_HUB_ALIASES.get(key, key)
        key = SOURCE_HUB_ALIASES.get(key.replace("-", "_"), key)
        if key in SOURCE_HUB_CATALOG:
            return key
        compact = key.replace("-", "")
        for candidate in SOURCE_HUB_CATALOG:
            if candidate.replace("-", "") == compact:
                return candidate
        return "other"

    def _hub_leaf_edge_type(self, node: Any, source_key: str) -> str:
        if source_key in {"docs", "adr", "meetings", "azure-wiki", "granola", "chat", "teams-meetings", "inbox"}:
            return "DOCUMENTED_IN"
        if source_key in {"code", "project_scan"}:
            return "IMPLEMENTS"
        if source_key == "azure-boards":
            return "HAS_MEMORY"
        return self._root_edge_type(node)

    @staticmethod
    def _is_structural_node(node: Any) -> bool:
        if str(getattr(node, "type", "") or "") == "Project":
            return True
        metadata = dict(getattr(node, "metadata", {}) or {})
        return bool(metadata.get("hub") or metadata.get("scope_root") or metadata.get("structural"))

    def _root_edge_type(self, node: Any) -> str:
        metadata = dict(getattr(node, "metadata", {}) or {})
        source = " ".join(str(metadata.get(key) or "") for key in ("source", "source_type", "template", "path")).lower()
        label = str(getattr(node, "label", "") or "").lower()
        node_type = str(getattr(node, "type", "") or "").lower()
        if node_type in {"doc", "decision", "meeting"} or any(item in source for item in ("docs", "adr", "meeting", ".md", ".txt", ".rst")):
            return "DOCUMENTED_IN"
        if node_type == "artifact" or any(item in source for item in (".py", ".js", ".ts", ".tsx", ".json", ".yaml", ".yml")) or label.endswith((".py", ".js", ".ts", ".tsx", ".json")):
            return "IMPLEMENTS"
        return "HAS_MEMORY"

    def _metadata_text(self, node: Any) -> str:
        metadata = dict(getattr(node, "metadata", {}) or {})
        return " ".join(str(value) for value in metadata.values() if isinstance(value, (str, int, float)))

    def _tokens(self, text: str) -> set[str]:
        return _token_set(text, GRAPH_STOPWORDS, min_len=3, strip_chars="._-/#")

    def _similarity(self, left: set[str], right: set[str]) -> float:
        return _token_similarity(left, right, containment_weight=0.72)

    def _similarity_tokens(self, node: Any) -> set[str]:
        # Keep structural/lifecycle metadata out of RELATED_TO scoring — it creates
        # huge false-positive cliques (timestamps, TTL, paths) on large corpora.
        return self._tokens(f"{getattr(node, 'label', '')} {getattr(node, 'text', '')} {getattr(node, 'type', '')}")

    def _write_edges(
        self,
        planned: list[tuple[str, str, str, str, float, str]],
        *,
        existing_edge_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        shared = existing_edge_ids is not None
        existing: set[str] = existing_edge_ids if existing_edge_ids is not None else {edge.id for edge in self.repository.list_edges()}
        created = 0
        linked_nodes: set[str] = set()
        edges: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for source, target, edge_type, scope, confidence, reason in planned:
            if not source or not target or source == target:
                continue
            key = (source, target, edge_type, scope or "project")
            if key in seen:
                continue
            seen.add(key)
            edge_id = stable_id("edge", edge_type, source, target, scope or "project")
            # Batch promote passes a shared set and can skip redundant writes.
            if shared and edge_id in existing:
                linked_nodes.update({source, target})
                continue
            edge = self.repository.add_edge(source, target, edge_type, scope or "project", confidence)
            if edge.id not in existing:
                created += 1
            existing.add(edge.id)
            linked_nodes.update({source, target})
            payload = edge.to_dict()
            payload["reason"] = reason
            edges.append(payload)
        return {"created": created, "edges": edges, "linked_nodes": len(linked_nodes)}


class GraphServiceMixin:
    """Graph analytics + code-graph queries for :class:`ArchitectOSService`."""

    # Edge origins that mean "a machine inferred this link" rather than "it was
    # stated in the source" — used to tag edge provenance for the UI (dashed =
    # inferred) and, later, for honesty filters in retrieval.
    _INFERRED_EDGE_ORIGINS = {"llm", "suggested", "auto", "similarity", "auto_link", "consolidation"}
    _INFERRED_EDGE_TYPES = {"RELATED_TO"}

    def _edge_provenance(self, edge: dict[str, Any]) -> str:
        meta = dict(edge.get("metadata") or {})
        origin = str(meta.get("origin") or meta.get("source") or "").lower()
        if meta.get("suggested") or meta.get("inferred") or origin in self._INFERRED_EDGE_ORIGINS:
            return "INFERRED"
        if meta.get("extracted") or origin:
            return "EXTRACTED"
        # No explicit provenance: similarity/auto links are the untyped default the
        # auto-linker emits, so treat bare RELATED_TO as inferred, everything else
        # (DEPENDS_ON, PART_OF, HAS_MEMORY, ...) as an explicit relationship.
        return "INFERRED" if str(edge.get("type") or "") in self._INFERRED_EDGE_TYPES else "EXTRACTED"

    def _annotate_graph_analytics(self, graph_nodes: list[dict[str, Any]], graph_edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Attach community + degree to nodes and provenance to edges in place.

        Returns the community metadata (id, label, size) so the UI can render a
        theme legend. Communities come from deterministic label propagation over
        the returned subgraph; labels are the most distinctive content tokens of
        each cluster. Singleton clusters are reported with size 1 so the UI can
        grey them out instead of spending a palette color.
        """
        node_ids = [str(node.get("id")) for node in graph_nodes if node.get("id")]
        edge_pairs = [
            (str(edge.get("source")), str(edge.get("target")))
            for edge in graph_edges
            if edge.get("source") and edge.get("target")
        ]
        communities = detect_communities(node_ids, edge_pairs)
        degrees = compute_degrees(node_ids, edge_pairs)

        members: dict[int, list[dict[str, Any]]] = {}
        for node in graph_nodes:
            cid = communities.get(str(node.get("id")), -1)
            node["community"] = cid
            node["degree"] = degrees.get(str(node.get("id")), 0)
            node["superseded"] = bool((node.get("metadata") or {}).get("invalid_at"))
            members.setdefault(cid, []).append(node)

        for edge in graph_edges:
            edge["provenance"] = self._edge_provenance(edge)

        community_meta: list[dict[str, Any]] = []
        for cid, group in sorted(members.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            community_meta.append({
                "id": cid,
                "size": len(group),
                "label": self._community_label(group),
            })
        return community_meta

    def _community_label(self, nodes: list[dict[str, Any]]) -> str:
        """Pick the most distinctive content tokens shared inside a community."""
        if len(nodes) == 1:
            return str(nodes[0].get("label") or "").strip() or "Note"
        counts: Counter[str] = Counter()
        for node in nodes:
            seen = set(content_tokens(f"{node.get('label') or ''} {node.get('text') or ''}"))
            # Iterate deterministically: Counter.most_common breaks count ties by
            # insertion order, so raw set iteration would leak PYTHONHASHSEED into
            # the chosen label keywords.
            for token in sorted(seen):
                if len(token) > 2:
                    counts[token] += 1
        if not counts:
            return f"Cluster of {len(nodes)}"
        top = [token for token, _count in counts.most_common(3)]
        return " / ".join(top)

    # -- Dual-level retrieval: communities as a coarse "theme" layer -----------

    def _project_communities(
        self, project_id: str | None = None
    ) -> tuple[dict[int, list[dict[str, Any]]], dict[str, int], dict[int, dict[str, Any]]]:
        """Compute the community partition of the project graph once.

        Reuses ``graph()`` so the clusters, labels and degrees match exactly what
        the UI renders. Returns ``(members_by_community, node_to_community,
        meta_by_community)``.
        """
        payload = self.graph(project_id)
        members: dict[int, list[dict[str, Any]]] = {}
        node_to_community: dict[str, int] = {}
        for node in payload.get("nodes") or []:
            cid = node.get("community")
            if not isinstance(cid, int) or cid < 0:
                continue
            node_to_community[str(node.get("id"))] = cid
            members.setdefault(cid, []).append(node)
        meta_by_community = {int(c["id"]): c for c in (payload.get("communities") or []) if "id" in c}
        return members, node_to_community, meta_by_community

    @staticmethod
    def _is_synthetic_graph_node(node: dict[str, Any]) -> bool:
        meta = node.get("metadata") or {}
        return bool(meta.get("synthetic") or meta.get("hub") or meta.get("scope_root")) or node.get("type") in {"Project", "Task", "Provider"}

    def community_summaries(
        self,
        project_id: str | None = None,
        query: str | None = None,
        limit: int = 6,
        members_per: int = 5,
    ) -> dict[str, Any]:
        """A coarse map of the memory: themes (graph communities) instead of notes.

        This is the "global" half of dual-level retrieval — GraphRAG-style. Each
        theme carries its distinctive keywords and highest-degree member notes so
        an agent can orient before drilling into ``search_memory``. With a query,
        themes are ranked by keyword overlap so a broad question ("how does
        retrieval work?") returns the relevant subsystems rather than scattered
        lines.
        """
        members, _node_to_community, meta_by_community = self._project_communities(project_id)
        query_tokens = {t for t in content_tokens(str(query or "")) if len(t) > 2} if query else set()
        summaries: list[dict[str, Any]] = []
        for cid, group in members.items():
            real = [node for node in group if not self._is_synthetic_graph_node(node)]
            if len(real) < 2:
                continue  # a single note is not a theme
            token_counts: Counter[str] = Counter()
            for node in real:
                # Sorted iteration keeps Counter insertion order deterministic:
                # most_common(8) breaks ties by insertion order, and raw set
                # iteration order varies with PYTHONHASHSEED (flaky keywords).
                for token in sorted(set(content_tokens(f"{node.get('label') or ''} {node.get('text') or ''}"))):
                    if len(token) > 2:
                        token_counts[token] += 1
            keywords = [token for token, _count in token_counts.most_common(8)]
            types = sorted({str(node.get("type") or "") for node in real if node.get("type")})
            ranked = sorted(real, key=lambda n: (-(n.get("degree") or 0), str(n.get("label") or "")))
            top_members = [
                {"id": n.get("id"), "label": n.get("label"), "type": n.get("type"), "degree": n.get("degree") or 0}
                for n in ranked[: max(1, members_per)]
            ]
            label = str((meta_by_community.get(cid) or {}).get("label") or self._community_label(real))
            score = (sum(1 for token in query_tokens if token in token_counts) / len(query_tokens)) if query_tokens else 0.0
            summaries.append({
                "id": cid,
                "label": label,
                "size": len(real),
                "types": types,
                "keywords": keywords,
                "members": top_members,
                "score": round(score, 3),
                "summary": f"{label}: {len(real)} linked notes ({', '.join(types) or 'mixed'}). Key: {', '.join(keywords[:5]) or 'n/a'}.",
            })
        if query_tokens:
            summaries = [item for item in summaries if item["score"] > 0]
            summaries.sort(key=lambda item: (-item["score"], -item["size"]))
        else:
            summaries.sort(key=lambda item: -item["size"])
        return {
            "project_id": project_id or "architectos",
            "query": str(query or ""),
            "communities": summaries[: max(1, int(limit or 6))],
        }

    def _communities_for_hits(self, project_id: str | None, hit_ids: list[str]) -> list[dict[str, Any]]:
        """The themes the returned hits belong to — the coarse layer of a search.

        Links the two retrieval levels: each theme reports which of the current
        hit ids fall inside it, so an agent sees both the specific notes and the
        subsystem they cluster into.
        """
        members, node_to_community, meta_by_community = self._project_communities(project_id)
        by_community: dict[int, list[str]] = {}
        for hid in hit_ids:
            cid = node_to_community.get(hid)
            if cid is None:
                continue
            by_community.setdefault(cid, []).append(hid)
        out: list[dict[str, Any]] = []
        for cid, ids in by_community.items():
            real = [node for node in members.get(cid, []) if not self._is_synthetic_graph_node(node)]
            if len(real) < 2:
                continue
            out.append({
                "id": cid,
                "label": str((meta_by_community.get(cid) or {}).get("label") or self._community_label(real)),
                "size": len(real),
                "hit_ids": ids,
            })
        out.sort(key=lambda item: (-len(item["hit_ids"]), -item["size"]))
        return out


    def explain_graph_path(self, source_id: str, target_id: str) -> dict[str, Any]:
        if not source_id or not target_id:
            raise ValueError("source and target are required")
        nodes = {node.id: node for node in self.repository.list_nodes() if node.status == "active"}
        if source_id not in nodes or target_id not in nodes:
            raise ValueError("source and target graph nodes are required")
        adjacency: dict[str, list[tuple[str, str]]] = {node_id: [] for node_id in nodes}
        for edge in self.repository.list_edges():
            if edge.source in nodes and edge.target in nodes:
                adjacency.setdefault(edge.source, []).append((edge.target, edge.type))
                adjacency.setdefault(edge.target, []).append((edge.source, edge.type))
        queue: list[tuple[str, list[str], list[str]]] = [(source_id, [source_id], [])]
        seen = {source_id}
        while queue:
            current, path, edge_types = queue.pop(0)
            if current == target_id:
                labels = [nodes[node_id].label for node_id in path]
                explanation = " -> ".join(f"{label}{f' ({edge_types[index - 1]})' if index else ''}" for index, label in enumerate(labels))
                return {"found": True, "path": path, "labels": labels, "edge_types": edge_types, "explanation": explanation}
            for next_id, edge_type in adjacency.get(current, []):
                if next_id in seen:
                    continue
                seen.add(next_id)
                queue.append((next_id, [*path, next_id], [*edge_types, edge_type]))
        return {"found": False, "path": [], "labels": [], "edge_types": [], "explanation": "No connected path found between selected nodes."}

    # -- Code graph queries -------------------------------------------------
    @staticmethod
    def _code_node_summary(node: Any) -> dict[str, Any]:
        metadata = node.metadata or {}
        return {
            "id": node.id,
            "label": node.label,
            "type": node.type,
            "kind": metadata.get("kind"),
            "path": metadata.get("path") or metadata.get("cg_file"),
            "line": metadata.get("line"),
            "language": metadata.get("language"),
        }

    def _resolve_code_symbol(self, identifier: str, project_id: str | None) -> Any | None:
        """Resolve a Symbol node by id, ``path::qual`` label, qualname, or name."""
        identifier = str(identifier or "").strip()
        if not identifier:
            return None
        direct = self.repository.get_node(identifier)
        if direct is not None and direct.type == "Symbol" and direct.status == "active":
            return direct
        matches: list[Any] = []
        for node in self.repository.list_nodes():
            if node.type != "Symbol" or node.status != "active":
                continue
            if project_id and node.project_id not in {project_id, None}:
                continue
            metadata = node.metadata or {}
            qualname = str(metadata.get("symbol") or "")
            simple = qualname.rsplit(".", 1)[-1] if qualname else ""
            if identifier in (node.label, qualname, simple) or node.label.endswith(f"::{identifier}"):
                matches.append(node)
        if not matches:
            return None
        # Prefer an exact label/qualname hit; otherwise return the first match.
        exact = [n for n in matches if identifier in (n.label, str((n.metadata or {}).get("symbol") or ""))]
        return (exact or matches)[0]

    def code_neighbors(self, identifier: str, project_id: str | None = None) -> dict[str, Any]:
        """Callers / callees / definition file for a code symbol.

        ``identifier`` may be a Symbol node id, a ``path::qualname`` label, a
        qualified name, or a bare symbol name.
        """
        symbol = self._resolve_code_symbol(identifier, project_id)
        if symbol is None:
            return {"found": False, "query": identifier, "callers": [], "callees": [], "defined_in": None}
        nodes = {node.id: node for node in self.repository.list_nodes() if node.status == "active"}
        callers: list[dict[str, Any]] = []
        callees: list[dict[str, Any]] = []
        contains: list[dict[str, Any]] = []
        defined_in: dict[str, Any] | None = None
        for edge in self.repository.list_edges_touching([symbol.id]):
            if edge.status != "active":
                continue
            if edge.type == "CALLS" and edge.target == symbol.id and edge.source in nodes:
                callers.append(self._code_node_summary(nodes[edge.source]))
            elif edge.type == "CALLS" and edge.source == symbol.id and edge.target in nodes:
                callees.append(self._code_node_summary(nodes[edge.target]))
            elif edge.type == "CONTAINS" and edge.source == symbol.id and edge.target in nodes:
                contains.append(self._code_node_summary(nodes[edge.target]))
            elif edge.type == "DEFINES" and edge.target == symbol.id and edge.source in nodes:
                defined_in = self._code_node_summary(nodes[edge.source])
        callers.sort(key=lambda item: str(item.get("label") or ""))
        callees.sort(key=lambda item: str(item.get("label") or ""))
        return {
            "found": True,
            "query": identifier,
            "symbol": self._code_node_summary(symbol),
            "defined_in": defined_in,
            "callers": callers,
            "callees": callees,
            "contains": contains,
        }

    def code_impact(self, identifier: str, depth: int = 3, project_id: str | None = None) -> dict[str, Any]:
        """What transitively depends on a symbol: reverse CALLS + file importers.

        Answers "what could break if I change X" by walking CALLS edges backwards
        (callers of callers, up to ``depth``) and adding files that import the
        symbol's defining file.
        """
        symbol = self._resolve_code_symbol(identifier, project_id)
        if symbol is None:
            return {"found": False, "query": identifier, "impacted": [], "counts": {}}
        depth = max(1, min(int(depth or 3), 6))
        nodes = {node.id: node for node in self.repository.list_nodes() if node.status == "active"}
        callers_of: dict[str, list[str]] = {}
        importers_of: dict[str, list[str]] = {}
        defines_file: dict[str, str] = {}  # symbol_id -> file_id
        for edge in self.repository.list_edges():
            if edge.status != "active":
                continue
            if edge.source not in nodes or edge.target not in nodes:
                continue
            if edge.type == "CALLS":
                callers_of.setdefault(edge.target, []).append(edge.source)
            elif edge.type == "IMPORTS":
                importers_of.setdefault(edge.target, []).append(edge.source)
            elif edge.type == "DEFINES":
                defines_file[edge.target] = edge.source

        impacted: dict[str, dict[str, Any]] = {}
        frontier = [(symbol.id, 0)]
        visited = {symbol.id}
        while frontier:
            current, dist = frontier.pop(0)
            if dist >= depth:
                continue
            for caller in callers_of.get(current, []):
                if caller in visited:
                    continue
                visited.add(caller)
                impacted[caller] = {**self._code_node_summary(nodes[caller]), "distance": dist + 1, "via": "CALLS"}
                frontier.append((caller, dist + 1))

        # File-level blast radius: files importing the symbol's defining file.
        file_id = defines_file.get(symbol.id)
        if file_id:
            for importer in importers_of.get(file_id, []):
                if importer in impacted or importer == symbol.id:
                    continue
                impacted[importer] = {**self._code_node_summary(nodes[importer]), "distance": 1, "via": "IMPORTS"}

        ranked = sorted(
            impacted.values(),
            key=lambda item: (int(item.get("distance") or 0), str(item.get("label") or "")),
        )
        return {
            "found": True,
            "query": identifier,
            "symbol": self._code_node_summary(symbol),
            "depth": depth,
            "impacted": ranked,
            "counts": {
                "total": len(ranked),
                "by_calls": sum(1 for item in ranked if item.get("via") == "CALLS"),
                "by_imports": sum(1 for item in ranked if item.get("via") == "IMPORTS"),
            },
        }

    def graph(
        self,
        project_id: str | None = None,
        task_id: str | None = None,
        provider_id: str | None = None,
        pinned: bool = False,
        node_type: str | None = None,
        source: str | None = None,
        scope: str | None = None,
        limit: int | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        self.memory_lifecycle.annotate_nodes(project_id)
        all_nodes = [node for node in self.repository.list_nodes() if node.status == "active"]
        if project_id:
            all_nodes = [node for node in all_nodes if node.project_id in {project_id, None} or node.scope in {"shared", "global"}]
        
        # Unique types/scopes/sources for filter dropdowns (before filtering)
        types = sorted(list({node.type for node in all_nodes if node.type}))
        scopes = sorted(list({node.scope for node in all_nodes if node.scope}))
        source_keys = sorted({self.graph_auto_linker._source_key_for_node(node) for node in all_nodes})
        sources = [
            {
                "id": key,
                "label": (SOURCE_HUB_CATALOG.get(key) or {}).get("label") or key,
            }
            for key in source_keys
        ]
        
        if pinned:
            all_nodes = [node for node in all_nodes if bool(node.metadata.get("favorite") or node.metadata.get("pinned"))]
            
        task = self.repository.get_task(task_id) if task_id else None
        task_node: dict[str, Any] | None = None
        if task:
            linked_ids = set(task.get("linked_memory_ids") or [])
            all_nodes = [node for node in all_nodes if node.id in linked_ids]
            task_node = {"id": f"task:{task['id']}", "type": "Task", "label": task["title"], "scope": "project", "text": task.get("detail") or "", "project_id": task.get("project_id"), "metadata": {"synthetic": True, "task_id": task["id"]}}
            
        provider_node: dict[str, Any] | None = None
        if provider_id:
            provider = next((item for item in self.repository.list_providers() if item["id"] == provider_id), None)
            needle = " ".join(str(item or "").lower() for item in [provider_id, provider.get("label") if provider else ""])
            all_nodes = [node for node in all_nodes if provider_id.lower() in (node.label + " " + node.text).lower() or (provider and str(provider.get("label") or "").lower() in (node.label + " " + node.text).lower())]
            provider_node = {"id": f"provider:{provider_id}", "type": "Provider", "label": (provider or {}).get("label") or provider_id, "scope": "project", "text": (provider or {}).get("notes") or f"Provider filter for {provider_id}.", "project_id": project_id, "metadata": {"synthetic": True, "provider_id": provider_id, "needle": needle.strip()}}
            
        # Apply source/type/scope filters
        filtered_nodes = all_nodes
        source_key = self.graph_auto_linker._normalize_source_key(source) if source else ""
        if source_key:
            # Keep Project roots visible so Groups → Projects and the graph spine still work.
            filtered_nodes = [
                node
                for node in filtered_nodes
                if node.type == "Project"
                or self.graph_auto_linker._source_key_for_node(node) == source_key
            ]
        if node_type:
            filtered_nodes = [node for node in filtered_nodes if node.type == node_type]
        if scope:
            filtered_nodes = [node for node in filtered_nodes if node.scope == scope]

        query = str(search or "").strip().lower()
        exact_id_hits: set[str] = set()
        if query:
            matched: list[Any] = []
            for node in filtered_nodes:
                meta = dict(node.metadata or {})
                meta_bits = " ".join(
                    str(meta.get(key) or "")
                    for key in (
                        "source",
                        "work_item_id",
                        "wiki_page_key",
                        "meeting_id",
                        "repo_id",
                        "pull_request_id",
                    )
                )
                haystack = " ".join(
                    [
                        str(node.id or ""),
                        str(node.label or ""),
                        str(node.text or ""),
                        str(node.type or ""),
                        str(node.scope or ""),
                        meta_bits,
                    ]
                ).lower()
                node_id = str(node.id or "").lower()
                if node_id == query or node_id.startswith(query) or query in haystack:
                    matched.append(node)
                    if node_id == query or (query.startswith("node_") and node_id.startswith(query)):
                        exact_id_hits.add(node.id)
            filtered_nodes = matched
            
        allowed = {node.id for node in filtered_nodes}
        # Fetch the edge table once; the per-view subsets are derived in memory.
        table_edges = self.repository.list_edges()
        all_edges = [edge for edge in table_edges if edge.source in allowed and edge.target in allowed]

        # Exact ID search: pull in 1-hop neighbors so the hit isn't an isolated dot.
        if exact_id_hits:
            neighbor_ids: set[str] = set()
            for edge in table_edges:
                if edge.source in exact_id_hits:
                    neighbor_ids.add(edge.target)
                if edge.target in exact_id_hits:
                    neighbor_ids.add(edge.source)
            if neighbor_ids:
                by_id = {node.id: node for node in all_nodes}
                for node_id in neighbor_ids:
                    if node_id in allowed:
                        continue
                    neighbor = by_id.get(node_id)
                    if neighbor is None:
                        continue
                    filtered_nodes.append(neighbor)
                    allowed.add(node_id)
                all_edges = [
                    edge
                    for edge in table_edges
                    if edge.source in allowed and edge.target in allowed
                ]
        
        total_count = len(filtered_nodes)
        
        # Apply density budget on the backend to keep it fast
        if limit is None:
            if query:
                limit = 400
            elif not node_type and not source_key and not scope and total_count > 200:
                limit = 200
            else:
                limit = 600
                
        allowed_selected = allowed
        if limit and len(filtered_nodes) > limit:
            must_ids = set(exact_id_hits)
            filtered_nodes = self._select_graph_nodes_by_source_quota(
                filtered_nodes,
                all_edges,
                limit,
                must_ids=must_ids,
            )
            allowed_selected = {node.id for node in filtered_nodes}
            all_edges = [edge for edge in all_edges if edge.source in allowed_selected and edge.target in allowed_selected]
            
        graph_nodes = [node.to_dict() for node in filtered_nodes]
        graph_edges = [edge.to_dict() for edge in all_edges]
        
        if task_node:
            graph_nodes.append(task_node)
            for node_id in allowed:
                if node_id in allowed_selected:
                    graph_edges.append({"id": stable_id("edge", "TASK_LINK", task_node["id"], node_id, "project"), "source": task_node["id"], "target": node_id, "type": "TASK_LINK", "scope": "project", "status": "active", "confidence": 0.8, "metadata": {"synthetic": True}})
        if provider_node:
            graph_nodes.append(provider_node)
            for node_id in allowed:
                if node_id in allowed_selected:
                    graph_edges.append({"id": stable_id("edge", "PROVIDER_RELATED", provider_node["id"], node_id, "project"), "source": provider_node["id"], "target": node_id, "type": "PROVIDER_RELATED", "scope": "project", "status": "active", "confidence": 0.62, "metadata": {"synthetic": True}})
                    
        communities = self._annotate_graph_analytics(graph_nodes, graph_edges)

        return {
            "nodes": graph_nodes,
            "edges": graph_edges,
            "types": types,
            "scopes": scopes,
            "sources": sources,
            "communities": communities,
            "total_nodes": total_count,
            "filters": {
                "task_id": task_id or "",
                "provider_id": provider_id or "",
                "pinned": pinned,
                "type": node_type or "",
                "source": source_key or "",
                "scope": scope or "",
                "search": query,
            }
        }

    def pin_graph_node(self, node_id: str) -> dict[str, Any]:
        node = self.repository.get_node(node_id)
        if not node:
            raise ValueError("graph node not found")
        node.metadata["pinned"] = not bool(node.metadata.get("pinned") or node.metadata.get("favorite"))
        node.metadata["favorite"] = node.metadata["pinned"]
        node = self.repository.upsert_node(node)
        if node.metadata.get("pinned"):
            return self.memory_lifecycle.promote_long_term(node.id, "pinned")
        return {"node": node.to_dict()}

    def rebuild_graph_links(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        project_id = str(payload.get("project_id") or "architectos")
        return {"project_id": project_id, **self.graph_auto_linker.rebuild(project_id)}

    def _noise_artifact_reason(self, node: Any) -> str:
        """Return a reason string if a node is an accidentally-indexed file artifact.

        Only file-import artifacts (project scan / code|docs ingestion) are eligible —
        curated Decisions/Lessons/Constraints are never touched. A node is noise when
        its underlying path is a build/runtime artifact (``.log``, lockfile, sourcemap)
        or lives under a tooling scratch dir (``.playwright-mcp/``, ``test-results/`` …)
        that should have been excluded at ingest.
        """
        if node is None or getattr(node, "status", "") != "active":
            return ""
        meta = dict(getattr(node, "metadata", None) or {})
        if meta.get("favorite") or meta.get("pinned"):
            return ""  # user-curated: never auto-purge
        source = str(meta.get("source") or meta.get("source_type") or "").lower()
        template = str(meta.get("template") or "").lower()
        file_backed = (
            source in {"project_scan", "code", "docs"}
            or template in {"generic", "inbox"}
            or bool(meta.get("path"))
        )
        if not file_backed:
            return ""
        label = str(getattr(node, "label", "") or "")
        raw_path = str(meta.get("path") or meta.get("source_ref") or "")
        if not raw_path:
            # Fall back to the label ("Code: <rel>" / "Doc: <rel>" / bare relative path).
            raw_path = re.sub(r"^(?:Code|Doc|Inbox):\s*", "", label).strip()
        rel = raw_path.replace("\\", "/").strip()
        name = rel.rsplit("/", 1)[-1]
        if self._is_noise_file_name(name):
            return f"artifact file: {name}"
        parts = {segment for segment in rel.split("/") if segment}
        hit = parts & DEFAULT_EXCLUDES
        if hit:
            return f"excluded dir: {sorted(hit)[0]}"
        ext = str(meta.get("extension") or "").lower()
        if ext in NOISE_FILE_SUFFIXES:
            return f"artifact ext: {ext}"
        return ""

    def purge_noise_nodes(
        self, project_id: str | None = None, dry_run: bool = False, hard: bool = False
    ) -> dict[str, Any]:
        """Clean up accidentally-indexed artifacts (e.g. ``.playwright-mcp/*.log``).

        Archives (default) or hard-deletes file-backed nodes that never should have
        been ingested. Reversible by default: archived nodes drop out of active search
        but stay in the store; ``hard=True`` also strips their edges and marks deleted.
        """
        items: list[dict[str, Any]] = []
        scanned = 0
        for node in self.repository.list_nodes():
            if node.status != "active":
                continue
            if project_id and node.project_id not in {project_id, None}:
                continue
            scanned += 1
            reason = self._noise_artifact_reason(node)
            if not reason:
                continue
            record: dict[str, Any] = {"id": node.id, "label": node.label, "reason": reason, "type": node.type}
            items.append(record)
            if dry_run:
                continue
            metadata = dict(node.metadata or {})
            deleted_edges = 0
            if hard:
                deleted_edges = self.repository.delete_node_edges(node.id)
                node.status = "deleted"
                metadata["deleted_at"] = utc_now()
            else:
                node.status = "archived"
                metadata["archived_at"] = metadata.get("archived_at") or utc_now()
            metadata["archived_reason"] = "noise_artifact"
            metadata["noise_reason"] = reason
            node.metadata = metadata
            self.repository.upsert_node(node)
            record["deleted_edges"] = deleted_edges
        return {
            "scanned": scanned,
            "purged": len(items),
            "dry_run": dry_run,
            "hard": hard,
            "items": items[:200],
        }

    def create_graph_edge(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = str(payload.get("source") or "").strip()
        target = str(payload.get("target") or "").strip()
        if not source or not target or source == target:
            raise ValueError("source and target graph nodes are required")
        source_node = self.repository.get_node(source)
        target_node = self.repository.get_node(target)
        if not source_node or not target_node:
            raise ValueError("source and target must be durable memory nodes")
        edge_type = str(payload.get("type") or "RELATED_TO")
        scope = str(payload.get("scope") or source_node.scope or target_node.scope or "project")
        edge = self.repository.add_edge(source, target, edge_type, scope, float(payload.get("confidence") or 0.72))
        if edge_type == "SUPERSEDES":
            # source SUPERSEDES target → target stopped being true when source began.
            self._apply_supersede(source_node, target_node)
        return {"edge": edge.to_dict()}

    def _apply_supersede(self, source_node: Any, target_node: Any) -> None:
        """Mark the superseded fact invalid from the moment the new fact began.

        Bi-temporal, non-destructive: the retired node stays in the store (so
        as_of queries can still surface it as historical truth) but carries an
        ``invalid_at`` timestamp and a ``superseded_by`` back-reference. The
        earliest retirement instant wins if it is superseded more than once.
        """
        when = str(getattr(source_node, "created_at", "") or "") or utc_now()
        meta = dict(getattr(target_node, "metadata", {}) or {})
        existing = str(meta.get("invalid_at") or "")
        if existing and existing <= when:
            return
        meta["invalid_at"] = when
        meta["superseded_by"] = getattr(source_node, "id", "")
        target_node.metadata = meta
        self.repository.upsert_node(target_node)

    LINK_EDGE_TYPES = {"RELATED_TO", "DEPENDS_ON", "PART_OF", "SUPPORTS", "CONTRADICTS", "SUPERSEDES", "EXAMPLE_OF"}

    def suggest_memory_links(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """LLM-assisted linking of contextually related but unlinked memory nodes.

        Individually added memories rarely share direct references. This pass
        1) picks a pool of low-degree (least linked) active content nodes,
        2) finds candidate pairs by token similarity that have no edge yet,
        3) asks the configured LLM whether each pair is related and how,
        4) optionally (``apply=true``) writes the accepted pairs as graph edges.

        Default is a dry run: suggestions are returned without writes.
        """
        payload = payload or {}
        project_id = str(payload.get("project_id") or "architectos")
        limit = max(2, min(int(payload.get("limit") or 24), 200))
        pair_limit = max(1, min(int(payload.get("pair_limit") or 12), 60))
        min_similarity = max(0.05, min(float(payload.get("min_similarity") or 0.22), 0.95))
        apply = bool(payload.get("apply") or False)
        provider_id = str(payload.get("provider_id") or "").strip() or None

        linker = self.graph_auto_linker
        nodes = [n for n in self.repository.list_nodes() if n.status == "active"]
        nodes = [n for n in nodes if not linker._is_structural_node(n) and not linker._is_code_artifact(n)]
        if str(payload.get("scope_all") or "") != "1" and not payload.get("all_projects"):
            nodes = [
                n for n in nodes
                if n.project_id in {project_id, None} or str(getattr(n, "scope", "") or "") in {"shared", "global"}
            ]

        edges = self.repository.list_edges()
        degree: Counter[str] = Counter()
        linked_pairs: set[tuple[str, ...]] = set()
        for edge in edges:
            degree[edge.source] += 1
            degree[edge.target] += 1
            linked_pairs.add(tuple(sorted((edge.source, edge.target))))

        # Least-linked nodes first — exactly the isolated items this pass targets.
        nodes.sort(key=lambda n: (degree[n.id], str(getattr(n, "created_at", "") or "")))
        pool = nodes[:limit]

        token_map = {node.id: linker._similarity_tokens(node) for node in pool}
        pair_candidates: list[tuple[float, Any, Any]] = []
        for index, left in enumerate(pool):
            left_tokens = token_map.get(left.id) or set()
            if not left_tokens:
                continue
            for right in pool[index + 1:]:
                if tuple(sorted((left.id, right.id))) in linked_pairs:
                    continue
                score = linker._similarity(left_tokens, token_map.get(right.id) or set())
                if score >= min_similarity:
                    pair_candidates.append((score, left, right))
        pair_candidates.sort(key=lambda item: -item[0])
        pair_candidates = pair_candidates[:pair_limit]

        # Judge pairs concurrently: each verdict is an independent LLM call and
        # sequential judging made the UI look dead (10 pairs × ~5 s each).
        # Providers are listed once here so worker threads never touch the repository.
        providers = self.repository.list_providers()
        workers = max(1, min(4, len(pair_candidates)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            verdicts = list(executor.map(
                lambda pair: self._judge_link_with_llm(pair[1], pair[2], project_id, provider_id, providers),
                pair_candidates,
            ))
        suggestions: list[dict[str, Any]] = []
        for (score, left, right), verdict in zip(pair_candidates, verdicts):
            suggestion = {
                "source_id": left.id,
                "target_id": right.id,
                "source_label": left.label,
                "target_label": right.label,
                "similarity": round(score, 3),
                "related": bool(verdict.get("related")),
                "edge_type": verdict.get("edge_type") or "RELATED_TO",
                "reason": str(verdict.get("reason") or ""),
                "confidence": round(float(verdict.get("confidence") or 0.6), 3),
                "llm_error": str(verdict.get("error") or ""),
            }
            suggestions.append(suggestion)

        created: list[dict[str, Any]] = []
        if apply:
            for suggestion in suggestions:
                if not suggestion["related"] or suggestion["llm_error"]:
                    continue
                edge_type = suggestion["edge_type"] if suggestion["edge_type"] in self.LINK_EDGE_TYPES else "RELATED_TO"
                try:
                    created_edge = self.create_graph_edge({
                        "source": suggestion["source_id"],
                        "target": suggestion["target_id"],
                        "type": edge_type,
                        "confidence": suggestion["confidence"],
                    })
                    created.append(created_edge["edge"])
                except ValueError:
                    continue
        return {
            "project_id": project_id,
            "applied": apply,
            "pool_size": len(pool),
            "pairs_considered": len(pair_candidates),
            "suggestions": suggestions,
            "created": created,
        }

    def _judge_link_with_llm(self, left: Any, right: Any, project_id: str, provider_id: str | None, providers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        prompt = (
            "You maintain a project memory graph. Decide whether the two memory nodes below are "
            "contextually related (same decision/topic/cause, one explains or depends on the other, "
            "one supersedes or contradicts the other). Answer with a single JSON object only:\n"
            '{"related": true|false, "edge_type": "RELATED_TO|DEPENDS_ON|PART_OF|SUPPORTS|CONTRADICTS|SUPERSEDES|EXAMPLE_OF", '
            '"reason": "<short>", "confidence": 0.0-1.0}\n\n'
            f"Node A [{left.type}] {left.label}:\n{(left.text or '')[:900]}\n\n"
            f"Node B [{right.type}] {right.label}:\n{(right.text or '')[:900]}"
        )
        request = ProviderRequest(message=prompt, context="", project_id=project_id, role="review")
        result = self.provider_router.route(providers if providers is not None else self.repository.list_providers(), request, provider_id)
        text = str(result.get("text") or "")
        if not text.strip():
            return {"related": False, "error": str(result.get("status") or "empty response")}
        return self._parse_link_verdict(text)

    @staticmethod
    def _parse_link_verdict(text: str) -> dict[str, Any]:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {"related": False, "error": "no JSON in LLM response"}
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return {"related": False, "error": "invalid JSON in LLM response"}
        if not isinstance(data, dict):
            return {"related": False, "error": "unexpected LLM response shape"}
        edge_type = str(data.get("edge_type") or "RELATED_TO").strip().upper()
        try:
            confidence = float(data.get("confidence") or 0.6)
        except (TypeError, ValueError):
            confidence = 0.6
        return {
            "related": bool(data.get("related")),
            "edge_type": edge_type if edge_type in GraphServiceMixin.LINK_EDGE_TYPES else "RELATED_TO",
            "reason": str(data.get("reason") or "")[:300],
            "confidence": max(0.0, min(confidence, 1.0)),
        }

    CONSOLIDATION_ACTIONS = {"merge", "contradicts", "keep"}

    def suggest_consolidations(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """LLM-assisted consolidation: find duplicate/conflicting memory nodes.

        Complements :meth:`suggest_memory_links` (which links *related* nodes):
        this pass targets *redundant* and *conflicting* knowledge, like mem0's
        UPDATE/DELETE consolidation. Pairs above a high similarity threshold are
        judged by the LLM: ``merge`` (same fact twice — pick a keeper),
        ``contradicts`` (statements in conflict — link with CONTRADICTS), or
        ``keep`` (distinct facts). Dry-run by default; ``apply=true`` merges via
        :meth:`merge_graph_nodes` or creates CONTRADICTS edges.
        """
        payload = payload or {}
        project_id = str(payload.get("project_id") or "architectos")
        limit = max(2, min(int(payload.get("limit") or 40), 300))
        pair_limit = max(1, min(int(payload.get("pair_limit") or 10), 40))
        min_similarity = max(0.2, min(float(payload.get("min_similarity") or 0.45), 0.98))
        apply = bool(payload.get("apply") or False)
        provider_id = str(payload.get("provider_id") or "").strip() or None

        linker = self.graph_auto_linker
        nodes = [n for n in self.repository.list_nodes() if n.status == "active"]
        nodes = [n for n in nodes if not linker._is_structural_node(n) and not linker._is_code_artifact(n)]
        if not payload.get("all_projects"):
            nodes = [
                n for n in nodes
                if n.project_id in {project_id, None} or str(getattr(n, "scope", "") or "") in {"shared", "global"}
            ]
        # Recent first: duplicates mostly arrive from repeated ingestion.
        nodes.sort(key=lambda n: str(getattr(n, "created_at", "") or ""), reverse=True)
        pool = nodes[:limit]

        contradicts_pairs: set[tuple[str, ...]] = set()
        for edge in self.repository.list_edges():
            if edge.type == "CONTRADICTS":
                contradicts_pairs.add(tuple(sorted((edge.source, edge.target))))

        token_map = {node.id: linker._similarity_tokens(node) for node in pool}
        pair_candidates: list[tuple[float, Any, Any]] = []
        for index, left in enumerate(pool):
            left_tokens = token_map.get(left.id) or set()
            if not left_tokens:
                continue
            for right in pool[index + 1:]:
                if tuple(sorted((left.id, right.id))) in contradicts_pairs:
                    continue
                score = linker._similarity(left_tokens, token_map.get(right.id) or set())
                if score >= min_similarity:
                    pair_candidates.append((score, left, right))
        pair_candidates.sort(key=lambda item: -item[0])
        pair_candidates = pair_candidates[:pair_limit]

        # Concurrent judging, same rationale as suggest_memory_links.
        providers = self.repository.list_providers()
        workers = max(1, min(4, len(pair_candidates)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            verdicts = list(executor.map(
                lambda pair: self._judge_consolidation_with_llm(pair[1], pair[2], project_id, provider_id, providers),
                pair_candidates,
            ))
        suggestions: list[dict[str, Any]] = []
        for (score, left, right), verdict in zip(pair_candidates, verdicts):
            suggestions.append({
                "source_id": left.id,
                "target_id": right.id,
                "source_label": left.label,
                "target_label": right.label,
                "similarity": round(score, 3),
                "action": verdict.get("action") or "keep",
                "keep": verdict.get("keep") or "",
                "reason": str(verdict.get("reason") or ""),
                "confidence": round(float(verdict.get("confidence") or 0.6), 3),
                "llm_error": str(verdict.get("error") or ""),
            })

        merged: list[dict[str, Any]] = []
        contradictions: list[dict[str, Any]] = []
        if apply:
            merged_away: set[str] = set()
            # Merges first: later pairs referencing a merged-away node are skipped.
            for suggestion in suggestions:
                if suggestion["llm_error"] or suggestion["action"] != "merge":
                    continue
                if suggestion["source_id"] in merged_away or suggestion["target_id"] in merged_away:
                    continue
                try:
                    if suggestion["keep"] == "A":
                        result = self.merge_graph_nodes(suggestion["target_id"], {"target_id": suggestion["source_id"]})
                    else:
                        result = self.merge_graph_nodes(suggestion["source_id"], {"target_id": suggestion["target_id"]})
                    merged.append({"kept": result["target"]["id"], "merged": result["source"]["id"], "labels": [suggestion["source_label"], suggestion["target_label"]]})
                    merged_away.add(result["source"]["id"])
                except ValueError:
                    continue
            for suggestion in suggestions:
                if suggestion["llm_error"] or suggestion["action"] != "contradicts":
                    continue
                if suggestion["source_id"] in merged_away or suggestion["target_id"] in merged_away:
                    continue
                try:
                    created_edge = self.create_graph_edge({
                        "source": suggestion["source_id"],
                        "target": suggestion["target_id"],
                        "type": "CONTRADICTS",
                        "confidence": suggestion["confidence"],
                    })
                    contradictions.append(created_edge["edge"])
                except ValueError:
                    continue
        return {
            "project_id": project_id,
            "applied": apply,
            "pool_size": len(pool),
            "pairs_considered": len(pair_candidates),
            "suggestions": suggestions,
            "merged": merged,
            "contradictions": contradictions,
        }

    def _judge_consolidation_with_llm(self, left: Any, right: Any, project_id: str, provider_id: str | None, providers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        prompt = (
            "You maintain a project memory graph. Two memory nodes are lexically similar. Decide:\n"
            '- "merge": they state the same fact twice (duplicates). Pick which node to KEEP '
            '("A" = first, "B" = second — prefer the more complete/precise one).\n'
            '- "contradicts": they make conflicting statements about the same subject '
            "(different values, dates, decisions).\n"
            '- "keep": they are distinct facts, no action needed.\n'
            "Answer with a single JSON object only:\n"
            '{"action": "merge"|"contradicts"|"keep", "keep": "A"|"B", "reason": "<short>", "confidence": 0.0-1.0}\n\n'
            f"Node A [{left.type}] {left.label}:\n{(left.text or '')[:900]}\n\n"
            f"Node B [{right.type}] {right.label}:\n{(right.text or '')[:900]}"
        )
        request = ProviderRequest(message=prompt, context="", project_id=project_id, role="review")
        result = self.provider_router.route(providers if providers is not None else self.repository.list_providers(), request, provider_id)
        text = str(result.get("text") or "")
        if not text.strip():
            return {"action": "keep", "error": str(result.get("status") or "empty response")}
        return self._parse_consolidation_verdict(text)

    @staticmethod
    def _parse_consolidation_verdict(text: str) -> dict[str, Any]:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {"action": "keep", "error": "no JSON in LLM response"}
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return {"action": "keep", "error": "invalid JSON in LLM response"}
        if not isinstance(data, dict):
            return {"action": "keep", "error": "unexpected LLM response shape"}
        action = str(data.get("action") or "keep").strip().lower()
        if action not in GraphServiceMixin.CONSOLIDATION_ACTIONS:
            action = "keep"
        keep = str(data.get("keep") or "").strip().upper()
        try:
            confidence = float(data.get("confidence") or 0.6)
        except (TypeError, ValueError):
            confidence = 0.6
        return {
            "action": action,
            "keep": keep if keep in {"A", "B"} else ("A" if action == "merge" else ""),
            "reason": str(data.get("reason") or "")[:300],
            "confidence": max(0.0, min(confidence, 1.0)),
        }

    def merge_graph_nodes(self, source_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        target_id = str(payload.get("target_id") or "").strip()
        if not target_id or target_id == source_id:
            raise ValueError("target_id is required")
        source = self.repository.get_node(source_id)
        target = self.repository.get_node(target_id)
        if not source or not target:
            raise ValueError("source and target graph nodes are required")
        # Edge relinks + node updates must commit as one unit (no half-merged graphs).
        with self.repository.transaction():
            merged_text = f"{target.text}\n\nMerged from {source.label}:\n{source.text}"
            target.text = merged_text[:6000]
            target.metadata["merged_from"] = list(dict.fromkeys([*(target.metadata.get("merged_from") or []), source.id]))
            target.metadata["pinned"] = bool(target.metadata.get("pinned") or source.metadata.get("pinned") or source.metadata.get("favorite"))
            for edge in self.repository.list_edges():
                if edge.source == source.id and edge.target != target.id:
                    self.repository.add_edge(target.id, edge.target, edge.type, edge.scope, edge.confidence)
                elif edge.target == source.id and edge.source != target.id:
                    self.repository.add_edge(edge.source, target.id, edge.type, edge.scope, edge.confidence)
            source.status = "merged"
            source.metadata["merged_into"] = target.id
            source.metadata["merged_at"] = utc_now()
            target = self.repository.upsert_node(target)
            source = self.repository.upsert_node(source)
        return {"target": target.to_dict(), "source": source.to_dict()}

    def apply_graph_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action") or payload.get("command") or "").strip().lower()
        if action == "merge":
            source_id = str(payload.get("source_id") or "").strip()
            if not source_id:
                raise ValueError("source_id is required")
            return {"action": "merge", **self.merge_graph_nodes(source_id, payload)}
        if action == "rename":
            node_id = str(payload.get("node_id") or "").strip()
            label = str(payload.get("label") or "").strip()
            if not node_id or not label:
                raise ValueError("node_id and label are required")
            node = self.repository.get_node(node_id)
            if not node:
                raise ValueError("graph node not found")
            old_label = node.label
            node.label = label[:180]
            node.metadata["renamed_from"] = list(dict.fromkeys([*(node.metadata.get("renamed_from") or []), old_label]))
            node.metadata["renamed_at"] = utc_now()
            return {"action": "rename", "node": self.repository.upsert_node(node).to_dict()}
        if action == "relink":
            edge_id = str(payload.get("edge_id") or "").strip()
            source_id = str(payload.get("source_id") or payload.get("source") or "").strip()
            target_id = str(payload.get("target_id") or payload.get("target") or "").strip()
            if not source_id or not target_id:
                raise ValueError("source_id and target_id are required")
            source = self.repository.get_node(source_id)
            target = self.repository.get_node(target_id)
            if not source or not target:
                raise ValueError("source and target graph nodes are required")
            edge_type = str(payload.get("type") or "RELATED_TO")
            scope = str(payload.get("scope") or source.scope or target.scope or "project")
            # Delete + re-add as one unit so a failure cannot drop the old edge alone.
            with self.repository.transaction():
                if edge_id:
                    self.repository.delete_edge(edge_id)
                edge = self.repository.add_edge(source_id, target_id, edge_type, scope, float(payload.get("confidence") or 0.72))
            return {"action": "relink", "edge": edge.to_dict(), "deleted_edge_id": edge_id}
        if action == "delete":
            node_id = str(payload.get("node_id") or "").strip()
            edge_id = str(payload.get("edge_id") or "").strip()
            hard = bool(payload.get("hard"))
            if edge_id:
                return {"action": "delete", "edge_id": edge_id, "deleted": self.repository.delete_edge(edge_id)}
            if not node_id:
                raise ValueError("node_id or edge_id is required")
            node = self.repository.get_node(node_id)
            if not node:
                raise ValueError("graph node not found")
            if hard:
                # Edge removal + tombstone update as one unit (no half-deleted graphs).
                with self.repository.transaction():
                    edge_count = self.repository.delete_node_edges(node_id)
                    node.status = "deleted"
                    node.metadata["deleted_at"] = utc_now()
                    self.repository.upsert_node(node)
                return {"action": "delete", "node": node.to_dict(), "deleted_edges": edge_count}
            node.status = "archived"
            node.metadata["archived_at"] = utc_now()
            node.metadata["archived_reason"] = str(payload.get("reason") or "graph_command_delete")
            return {"action": "delete", "node": self.repository.upsert_node(node).to_dict(), "deleted_edges": 0}
        raise ValueError("action must be merge, rename, relink, or delete")
