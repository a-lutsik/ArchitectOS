"""Graph memory model: graph read view and graph commands.

``GraphAutoLinker`` and source-hub constants live in ``graph_autolinker`` and
are re-exported here for backward-compatible imports. ``GraphServiceMixin``
groups the graph view (``graph()``), analytics, and edit commands.
Code-graph queries live in ``graph_code_service``; LLM link/consolidation
suggestions live in ``graph_suggest_service``. Repository, lifecycle and
provider access reach the service through ``self`` via the MRO.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .constants import DEFAULT_EXCLUDES, NOISE_FILE_SUFFIXES
from .embeddings import content_tokens
from .graph_analysis import compute_degrees, detect_communities
from .graph_autolinker import (
    GRAPH_STOPWORDS,
    SOURCE_HUB_ALIASES,
    SOURCE_HUB_CATALOG,
    GraphAutoLinker,
)
from .graph_code_service import GraphCodeServiceMixin
from .graph_suggest_service import GraphSuggestServiceMixin
from .models import stable_id, utc_now
from .storage import like_prefilter_supported

# Re-export for backward-compatible imports (service.py and tests).
__all__ = [
    "GRAPH_STOPWORDS",
    "SOURCE_HUB_ALIASES",
    "SOURCE_HUB_CATALOG",
    "GraphAutoLinker",
    "GraphServiceMixin",
]


class GraphServiceMixin(GraphCodeServiceMixin, GraphSuggestServiceMixin):
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

    def _graph_filter_facets(
        self, project_id: str | None
    ) -> tuple[list[str], list[str], list[dict[str, str]]]:
        """Filter-dropdown options for the whole store, cached against a revision token.

        Source keys are classified per node in Python, so this is the one part of
        the graph read that still has to walk every active node. It is recomputed
        only when nodes are added or touched; a filter interaction reuses the cache.
        Concurrent requests may each compute it once, which is harmless — the
        assignment is atomic and the value is derived, not authoritative.
        """
        revision = self.repository.nodes_revision(project_id or None, include_shared=bool(project_id))
        cache_key = (project_id or "", revision)
        cached = getattr(self, "_graph_facet_cache", None)
        if cached is not None and cached[0] == cache_key:
            return cached[1]
        nodes = self.repository.list_nodes(
            project_id=project_id or None,
            status="active",
            include_shared=bool(project_id),
        )
        types = sorted({node.type for node in nodes if node.type})
        scopes = sorted({node.scope for node in nodes if node.scope})
        sources = [
            {"id": key, "label": (SOURCE_HUB_CATALOG.get(key) or {}).get("label") or key}
            for key in sorted({self.graph_auto_linker._source_key_for_node(node) for node in nodes})
        ]
        facets = (types, scopes, sources)
        self._graph_facet_cache = (cache_key, facets)
        return facets

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
        # Prefer a scoped SQL fetch over a full-table scan. Shared/global nodes
        # stay visible for the selected project; status is filtered in SQL.
        #
        # type/scope/search are pushed into SQL so a narrowed view reads only the
        # rows it shows. The Python predicates below still run unchanged: the SQL
        # text filter is only a superset (see like_prefilter_supported), and any
        # query it cannot express simply falls through to the full scan.
        #
        # A SQL LIMIT would truncate before those Python filters run and silently
        # hide older matches, so it applies only to the unfiltered view, where
        # truncation is the intended density budget.
        narrowing = bool(search or node_type or scope or source or pinned or task_id or provider_id)
        fetch_limit = None if (limit is None or narrowing) else max(int(limit) * 8, 500)
        # Dropdown options describe the whole store, so they cannot be derived
        # from the narrowed set.
        types, scopes, sources = self._graph_filter_facets(project_id)
        all_nodes = self.repository.list_nodes(
            limit=fetch_limit,
            project_id=project_id or None,
            status="active",
            include_shared=bool(project_id),
            node_type=node_type or None,
            scope=scope or None,
            text_like=search if (search and like_prefilter_supported(search)) else None,
        )

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
        # Neighborhood fetch — avoid a full memory_edges table scan when the
        # visible set is small (typical graph view).
        if not allowed:
            table_edges = []
        elif len(allowed) <= 800:
            table_edges = self.repository.list_edges_touching(allowed)
        else:
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
        nodes = self.repository.list_nodes(
            project_id=project_id,
            status="active",
            include_shared=bool(project_id),
        ) if project_id else self.repository.list_nodes(status="active")

        def _apply_one(node: Any, reason: str) -> dict[str, Any]:
            record: dict[str, Any] = {"id": node.id, "label": node.label, "reason": reason, "type": node.type}
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
            return record

        victims: list[tuple[Any, str]] = []
        for node in nodes:
            scanned += 1
            reason = self._noise_artifact_reason(node)
            if not reason:
                continue
            victims.append((node, reason))
            if dry_run:
                items.append({"id": node.id, "label": node.label, "reason": reason, "type": node.type})

        if dry_run:
            return {"scanned": scanned, "purged": len(items), "dry_run": True, "hard": hard, "items": items[:200]}

        # One transaction for the whole batch so a mid-loop failure cannot leave
        # a half-purged graph (matches apply_graph_command hard-delete semantics).
        with self.repository.transaction():
            for node, reason in victims:
                items.append(_apply_one(node, reason))
        return {
            "scanned": scanned,
            "purged": len(items),
            "dry_run": False,
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

    def merge_graph_nodes(self, source_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        target_id = str(payload.get("target_id") or "").strip()
        if not target_id or target_id == source_id:
            raise ValueError("target_id is required")
        source = self.repository.get_node(source_id)
        target = self.repository.get_node(target_id)
        if not source or not target:
            raise ValueError("source and target graph nodes are required")
        # Fetch neighborhood outside the write lock — list_edges() under BEGIN IMMEDIATE
        # held the lock for a full table scan.
        touching = self.repository.list_edges_touching([source.id])
        # Edge relinks + node updates must commit as one unit (no half-merged graphs).
        with self.repository.transaction():
            merged_text = f"{target.text}\n\nMerged from {source.label}:\n{source.text}"
            target.text = merged_text[:6000]
            target.metadata["merged_from"] = list(dict.fromkeys([*(target.metadata.get("merged_from") or []), source.id]))
            target.metadata["pinned"] = bool(target.metadata.get("pinned") or source.metadata.get("pinned") or source.metadata.get("favorite"))
            for edge in touching:
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
