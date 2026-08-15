"""Graph auto-linker: source hubs, similarity edges, and hub pruning.

Standalone class instantiated by ``ArchitectOSService`` as
``self.graph_auto_linker``. Constants and the linker itself are re-exported
from ``graph_service`` for backward-compatible imports.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

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
