"""Code-graph neighbor and impact queries for GraphServiceMixin."""
from __future__ import annotations

from typing import Any


class GraphCodeServiceMixin:
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
