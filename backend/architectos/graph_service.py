"""Graph intelligence read-layer, extracted from ``service.py``.

``GraphServiceMixin`` groups the graph-analytics and code-graph query methods
so ``ArchitectOSService`` stays smaller and this cohesive slice can evolve on
its own. It is a pure mixin: every method operates on ``self`` (``repository``,
``graph``, ...) supplied by the concrete service, so behavior is unchanged.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .embeddings import content_tokens
from .graph_analysis import compute_degrees, detect_communities


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
            for token in seen:
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
                for token in set(content_tokens(f"{node.get('label') or ''} {node.get('text') or ''}")):
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
