"""Expose the ArchitectOS memory engine as an MCP server.

This is the mirror image of :mod:`architectos.mcp` (the MCP *client*): instead of
connecting ArchitectOS to external MCP servers, this module lets external MCP
clients (Cursor, GitHub Copilot, Claude Desktop, ...) connect *to* ArchitectOS
and read/write its durable memory.

Transport: MCP stdio — newline-delimited JSON-RPC 2.0 on stdin/stdout, matching
the framing used by :class:`architectos.mcp.MCPClient`. All logging goes to
stderr so it never corrupts the protocol stream on stdout.

Backed directly by :class:`architectos.service.ArchitectOSService`, so it shares
the exact same SQLite store as the desktop app (``<root>/data/architectos.db``).
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "architectos-memory", "version": "1.0.0"}

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _resolve_root() -> Path:
    # Shared with the HTTP server / desktop sidecar (ARCHITECTOS_ROOT + packaged defaults).
    from .paths import resolve_project_root

    return resolve_project_root()


def _log(message: str) -> None:
    sys.stderr.write(f"[architectos-mcp] {message}\n")
    sys.stderr.flush()


TOOLS: list[dict[str, Any]] = [
    {
        "name": "memory_search",
        "description": (
            "Search ArchitectOS durable project memory (lessons, decisions, concepts, "
            "artifacts) and return the most relevant scored hits. Use this to recall "
            "prior knowledge before answering."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language search query. May be empty when mode='list'."},
                "project_id": {"type": "string", "description": "Optional project id to scope the search."},
                "scope": {"type": "string", "description": "Optional scope filter (e.g. project, interface, shared, global)."},
                "limit": {"type": "integer", "description": "Max number of hits (default 8).", "minimum": 1, "maximum": 50},
                "mode": {"type": "string", "enum": ["search", "list"], "description": "'search' (default) ranks by relevance; 'list' returns nodes matching filters without scoring."},
                "as_of": {"type": "string", "description": "Optional ISO date/datetime — temporal point-in-time query: only memory created on/before this moment is returned (e.g. '2026-01-01')."},
                "include_communities": {"type": "boolean", "description": "Also return the coarse theme (graph community) each hit belongs to for a dual-level view (default false)."},
                "min_score": {"type": "number", "description": "Optional absolute score floor: drop hits below this score to suppress weak/irrelevant matches on noisy queries."},
                "filters": {
                    "type": "object",
                    "description": "Optional field/metadata filters. Values: exact match, '!=X' exclusion, 'a|b' alternatives, or a list. Example: {\"type\": \"Requirement\", \"work_item_state\": \"!=Done\"}.",
                    "additionalProperties": True,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_context",
        "description": (
            "Build a ready-to-inject context pack from ArchitectOS memory for a query: "
            "relevant memory, open tasks, and enabled providers, formatted as text. "
            "Prefer this when you want a compact briefing to paste into a prompt."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What the assistant is about to work on."},
                "project_id": {"type": "string", "description": "Optional project id to scope the context."},
                "scope": {"type": "string", "description": "Optional scope filter."},
                "limit": {"type": "integer", "description": "Max memory hits to include (default 8).", "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_add",
        "description": (
            "Store a new durable memory in ArchitectOS (a lesson, decision, concept, or "
            "artifact). Use this to persist knowledge learned during a session so it can "
            "be recalled later. Secrets are automatically redacted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Short title for the memory."},
                "text": {"type": "string", "description": "Full memory content (>= 4 non-secret characters)."},
                "type": {"type": "string", "description": "Memory type: Lesson, Decision, Concept, Artifact, etc. (default Lesson)."},
                "scope": {"type": "string", "description": "Scope: project, interface, shared, global (default project)."},
                "project_id": {"type": "string", "description": "Project id (default 'architectos')."},
                "confidence": {"type": "number", "description": "Confidence 0..1 (default 0.8).", "minimum": 0, "maximum": 1},
            },
            "required": ["label", "text"],
        },
    },
    {
        "name": "memory_get",
        "description": (
            "Fetch a single ArchitectOS memory node in full by id (untruncated text, "
            "metadata, evidence). Optionally include related nodes (graph neighbors). "
            "Use after memory_search when you need the complete record."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Memory node id (from memory_search hits)."},
                "include_neighbors": {"type": "boolean", "description": "Include related nodes from the memory graph (default true)."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "memory_feedback",
        "description": (
            "Rate how useful retrieved memory was. Call this after applying (or rejecting) "
            "memory hits so retrieval ranking improves over time."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "rating": {"type": "integer", "description": "1 = useful, -1 = not useful / stale / wrong.", "enum": [1, -1]},
                "hit_ids": {"type": "array", "items": {"type": "string"}, "description": "Memory node ids the rating applies to."},
                "query": {"type": "string", "description": "The query or task the memory was retrieved for."},
                "note": {"type": "string", "description": "Optional free-form note (e.g. what was stale or wrong)."},
                "project_id": {"type": "string", "description": "Project id (default 'architectos')."},
            },
            "required": ["rating"],
        },
    },
    {
        "name": "memory_list_projects",
        "description": "List ArchitectOS projects (id, name, root path) available for scoping memory.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory_themes",
        "description": (
            "List the high-level themes (graph communities) in ArchitectOS memory — a "
            "coarse map of the subsystems/topics the notes cluster into, each with its "
            "keywords and key member notes. Optionally rank themes by a query to get a "
            "global overview before drilling into memory_search."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Optional query to rank themes by relevance."},
                "project_id": {"type": "string", "description": "Optional project id to scope the themes."},
                "limit": {"type": "integer", "description": "Max themes to return (default 6).", "minimum": 1, "maximum": 20},
            },
        },
    },
    {
        "name": "memory_explain_path",
        "description": (
            "Explain how two memory nodes are connected: the shortest relationship path "
            "(sequence of nodes and edge types) between them in the memory graph. Use to "
            "justify why a fact is relevant or to trace provenance across decisions/lessons."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_id": {"type": "string", "description": "Start memory node id."},
                "target_id": {"type": "string", "description": "End memory node id."},
            },
            "required": ["source_id", "target_id"],
        },
    },
    {
        "name": "code_neighbors",
        "description": (
            "For a code symbol (function/method/class), list who calls it, what it "
            "calls, and the file that defines it — from ArchitectOS's persisted code "
            "graph. Accepts a Symbol node id, a 'path::qualname' label, a qualified "
            "name, or a bare symbol name. Use to understand a symbol before editing it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol id, 'path::qualname', qualname, or name."},
                "project_id": {"type": "string", "description": "Optional project id to scope resolution."},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "code_impact",
        "description": (
            "Estimate the blast radius of changing a code symbol: everything that "
            "transitively calls it (reverse CALLS up to a depth) plus files that import "
            "its defining file. Use to answer 'what could break if I change X'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol id, 'path::qualname', qualname, or name."},
                "depth": {"type": "integer", "description": "Reverse-call depth (default 3).", "minimum": 1, "maximum": 6},
                "project_id": {"type": "string", "description": "Optional project id to scope resolution."},
            },
            "required": ["symbol"],
        },
    },
]


class MemoryMCPServer:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._service: Any | None = None
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "memory_search": self._tool_search,
            "memory_context": self._tool_context,
            "memory_add": self._tool_add,
            "memory_get": self._tool_get,
            "memory_feedback": self._tool_feedback,
            "memory_list_projects": self._tool_list_projects,
            "memory_themes": self._tool_themes,
            "memory_explain_path": self._tool_explain_path,
            "code_neighbors": self._tool_code_neighbors,
            "code_impact": self._tool_code_impact,
        }

    # -- Service (lazy) ----------------------------------------------------
    @property
    def service(self) -> Any:
        if self._service is None:
            from .service import ArchitectOSService

            self._service = ArchitectOSService(self.root)
            _log(f"memory store ready at {self.root / 'data' / 'architectos.db'}")
        return self._service

    # -- Run loop ----------------------------------------------------------
    def serve(self, stdin=None, stdout=None) -> int:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        _log(f"serving on stdio (root={self.root})")
        for raw in stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._write(stdout, {"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": "Invalid JSON."}})
                continue
            response = self._dispatch(message)
            if response is not None:
                self._write(stdout, response)
        return 0

    def _write(self, stdout, message: dict[str, Any]) -> None:
        stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        stdout.flush()

    # -- Dispatch ----------------------------------------------------------
    def _dispatch(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "Expected JSON-RPC 2.0 object."}}
        method = str(message.get("method") or "")
        message_id = message.get("id")
        raw_params: Any = message.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        is_notification = "id" not in message

        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method in {"notifications/initialized", "notifications/cancelled", "initialized"}:
                return None  # notifications: no response
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self._call_tool(params)
            elif method in {"resources/list", "resources/templates/list"}:
                result = self._resources(method)
            elif method == "resources/read":
                result = self._resource_read(params)
            elif method == "prompts/list":
                result = {"prompts": []}
            else:
                if is_notification:
                    return None
                return {"jsonrpc": "2.0", "id": message_id, "error": {"code": METHOD_NOT_FOUND, "message": f"Unknown method: {method}"}}
        except _RpcError as exc:
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": message_id, "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:  # noqa: BLE001 - surface as JSON-RPC error, keep server alive
            _log("internal error:\n" + traceback.format_exc())
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": message_id, "error": {"code": INTERNAL_ERROR, "message": str(exc)}}

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = str(params.get("protocolVersion") or PROTOCOL_VERSION)
        return {
            "protocolVersion": requested or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": (
                "ArchitectOS memory engine. Use memory_search or memory_context to recall "
                "project knowledge, memory_get to fetch a full record by id, memory_add to "
                "persist new lessons and decisions, and memory_feedback to rate retrieved "
                "hits so ranking improves. Resources expose management views: "
                "memory://projects, memory://review, memory://nodes/{id}."
            ),
        }

    # -- Tools -------------------------------------------------------------
    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        raw_arguments: Any = params.get("arguments")
        arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
        handler = self._handlers.get(name)
        if handler is None:
            raise _RpcError(METHOD_NOT_FOUND, f"Unknown tool: {name}")
        try:
            return handler(arguments)
        except ValueError as exc:
            return _tool_error(str(exc))

    def _tool_search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        mode = str(args.get("mode") or "search").strip().lower() or "search"
        filters = args.get("filters") if isinstance(args.get("filters"), dict) else None
        if not query and mode != "list" and not filters:
            raise _RpcError(INVALID_PARAMS, "query is required")
        limit = _clamp_int(args.get("limit"), default=8, low=1, high=50)
        include_communities = bool(args.get("include_communities"))
        raw_min = args.get("min_score")
        try:
            min_score = float(raw_min) if raw_min is not None else None
        except (TypeError, ValueError):
            min_score = None
        result = self.service.search_memory(query, project_id=args.get("project_id"), scope=args.get("scope"), limit=limit, filters=filters, mode=mode, as_of=args.get("as_of"), include_communities=include_communities, min_score=min_score)
        hits = [_compact_hit(hit) for hit in result.get("hits") or []]
        if mode == "list":
            summary = f"{len(hits)} memory node(s) match the filters." if hits else "No memory nodes match the filters."
            return _tool_result(summary, {"mode": mode, "filters": filters or {}, "hits": hits})
        if hits:
            lines = [f'{i + 1}. [{h["type"]}] {h["label"]} — {_truncate(h["text"], 240)} (score {h["score"]}, conf {h["confidence"]})' for i, h in enumerate(hits)]
            summary = f'Found {len(hits)} memory hit(s) for "{query}":\n' + "\n".join(lines)
        else:
            summary = f'No memory found for "{query}".'
        data: dict[str, Any] = {"query": query, "hits": hits}
        if include_communities and result.get("communities") is not None:
            data["communities"] = result.get("communities")
        return _tool_result(summary, data)

    def _tool_context(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            raise _RpcError(INVALID_PARAMS, "query is required")
        limit = _clamp_int(args.get("limit"), default=8, low=1, high=50)
        result = self.service.context(query, project_id=args.get("project_id"), scope=args.get("scope"), limit=limit)
        return _tool_result(str(result.get("context") or ""), {"query": query, "context": result.get("context") or "", "hits": [_compact_hit(hit) for hit in result.get("hits") or []]})

    def _tool_add(self, args: dict[str, Any]) -> dict[str, Any]:
        node = self.service.add_memory({
            "label": args.get("label"),
            "text": args.get("text"),
            "type": args.get("type") or "Lesson",
            "scope": args.get("scope") or "project",
            "project_id": args.get("project_id") or "architectos",
            "confidence": args.get("confidence"),
            "source": "mcp",
        })
        summary = f'Stored memory [{node.get("type")}] "{node.get("label")}" (id {node.get("id")}).'
        return _tool_result(summary, {"id": node.get("id"), "label": node.get("label"), "type": node.get("type"), "scope": node.get("scope"), "project_id": node.get("project_id")})

    def _tool_themes(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = _clamp_int(args.get("limit"), default=6, low=1, high=20)
        result = self.service.community_summaries(project_id=args.get("project_id"), query=args.get("query"), limit=limit)
        themes = result.get("communities") or []
        if themes:
            lines = [
                f'{i + 1}. {t["label"]} — {t["size"]} notes; key: {", ".join((t.get("keywords") or [])[:5]) or "n/a"}'
                for i, t in enumerate(themes)
            ]
            summary = f"{len(themes)} memory theme(s):\n" + "\n".join(lines)
        else:
            summary = "No memory themes found (graph too small or unclustered)."
        return _tool_result(summary, {"query": result.get("query") or "", "themes": themes})

    def _tool_explain_path(self, args: dict[str, Any]) -> dict[str, Any]:
        source_id = str(args.get("source_id") or "").strip()
        target_id = str(args.get("target_id") or "").strip()
        if not source_id or not target_id:
            raise _RpcError(INVALID_PARAMS, "source_id and target_id are required")
        result = self.service.explain_graph_path(source_id, target_id)
        summary = str(result.get("explanation") or "No connected path found between selected nodes.")
        return _tool_result(summary, result)

    def _tool_code_neighbors(self, args: dict[str, Any]) -> dict[str, Any]:
        symbol = str(args.get("symbol") or "").strip()
        if not symbol:
            raise _RpcError(INVALID_PARAMS, "symbol is required")
        result = self.service.code_neighbors(symbol, project_id=args.get("project_id"))
        if not result.get("found"):
            return _tool_result(f"No code symbol found for '{symbol}'.", result)
        sym = result.get("symbol") or {}
        callers = result.get("callers") or []
        callees = result.get("callees") or []
        defined_in = (result.get("defined_in") or {}).get("label") or "?"
        summary = (
            f'{sym.get("kind") or "Symbol"} {sym.get("label")} — defined in {defined_in}; '
            f"{len(callers)} caller(s), {len(callees)} callee(s)."
        )
        if callers:
            summary += "\nCalled by: " + ", ".join(str(c.get("label")) for c in callers[:8])
        if callees:
            summary += "\nCalls: " + ", ".join(str(c.get("label")) for c in callees[:8])
        return _tool_result(summary, result)

    def _tool_code_impact(self, args: dict[str, Any]) -> dict[str, Any]:
        symbol = str(args.get("symbol") or "").strip()
        if not symbol:
            raise _RpcError(INVALID_PARAMS, "symbol is required")
        depth = _clamp_int(args.get("depth"), default=3, low=1, high=6)
        result = self.service.code_impact(symbol, depth=depth, project_id=args.get("project_id"))
        if not result.get("found"):
            return _tool_result(f"No code symbol found for '{symbol}'.", result)
        impacted = result.get("impacted") or []
        sym = result.get("symbol") or {}
        if impacted:
            lines = [
                f'{i + 1}. {item.get("label")} (d{item.get("distance")}, via {item.get("via")})'
                for i, item in enumerate(impacted[:15])
            ]
            summary = f'{len(impacted)} node(s) may be affected by changing {sym.get("label")}:\n' + "\n".join(lines)
        else:
            summary = f'Nothing in the code graph depends on {sym.get("label")} (leaf symbol).'
        return _tool_result(summary, result)

    def _tool_list_projects(self, _args: dict[str, Any]) -> dict[str, Any]:
        projects = self.service.projects()
        rows = [{"id": p.get("id"), "name": p.get("name"), "root_path": p.get("root_path")} for p in projects]
        summary = "Projects:\n" + "\n".join(f'- {r["id"]}: {r["name"]}' for r in rows) if rows else "No projects yet."
        return _tool_result(summary, {"projects": rows})

    def _tool_get(self, args: dict[str, Any]) -> dict[str, Any]:
        node_id = str(args.get("id") or "").strip()
        if not node_id:
            raise _RpcError(INVALID_PARAMS, "id is required")
        payload = self.service._tool_memory_get({
            "id": node_id,
            "include_neighbors": bool(args.get("include_neighbors", True)),
        })
        summary = f'[{payload.get("type")}] {payload.get("label")} (id {payload.get("id")})\n{payload.get("text") or ""}'
        neighbors = payload.get("neighbors") or []
        if neighbors:
            lines = [f'- ({n.get("edge_type")}) [{n.get("type")}] {n.get("label")} (id {n.get("id")})' for n in neighbors]
            summary += "\n\nRelated:\n" + "\n".join(lines)
        return _tool_result(summary, payload)

    def _tool_feedback(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            raw_rating: Any = args.get("rating")
            rating = int(raw_rating)
        except (TypeError, ValueError):
            raise _RpcError(INVALID_PARAMS, "rating is required (1 or -1)")
        if rating not in {1, -1}:
            raise _RpcError(INVALID_PARAMS, "rating must be 1 (useful) or -1 (not useful)")
        result = self.service.record_retrieval_feedback({
            "project_id": args.get("project_id") or "architectos",
            "rating": rating,
            "hit_ids": args.get("hit_ids") or [],
            "query": args.get("query") or "",
            "note": args.get("note") or "",
        })
        verdict = "useful" if rating == 1 else "not useful"
        summary = f"Recorded feedback: {verdict} for {result.get('hit_count', 0)} memory hit(s)."
        return _tool_result(summary, {"rating": rating, "hit_count": result.get("hit_count", 0)})

    # -- Resources (management views) ---------------------------------------
    def _resources(self, method: str) -> dict[str, Any]:
        if method == "resources/templates/list":
            return {"resourceTemplates": [{
                "uriTemplate": "memory://nodes/{id}",
                "name": "Memory node",
                "description": "Full memory node (text, metadata, evidence, neighbors) by id.",
                "mimeType": "application/json",
            }]}
        resources = [
            {
                "uri": "memory://projects",
                "name": "Projects",
                "description": "ArchitectOS projects available for scoping memory.",
                "mimeType": "application/json",
            },
            {
                "uri": "memory://review",
                "name": "Review queue",
                "description": "Memory candidates awaiting review (promote/reject).",
                "mimeType": "application/json",
            },
        ]
        return {"resources": resources}

    def _resource_read(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = str(params.get("uri") or "").strip()
        if uri == "memory://projects":
            rows = [{"id": p.get("id"), "name": p.get("name"), "root_path": p.get("root_path")} for p in self.service.projects()]
            return _resource_contents(uri, {"projects": rows})
        if uri == "memory://review":
            result = self.service.list_memory_candidates(status="candidate", limit=100)
            candidates = [
                {
                    "id": c.get("id"),
                    "label": c.get("label"),
                    "type": c.get("type"),
                    "status": c.get("status"),
                    "source": c.get("source"),
                    "text": _truncate(c.get("text") or "", 400),
                }
                for c in (result.get("candidates") or [])
            ]
            return _resource_contents(uri, {"count": len(candidates), "candidates": candidates})
        if uri.startswith("memory://nodes/"):
            node_id = uri.removeprefix("memory://nodes/").strip()
            if not node_id:
                raise _RpcError(INVALID_PARAMS, "node id is missing in uri")
            try:
                payload = self.service._tool_memory_get({"id": node_id, "include_neighbors": True})
            except ValueError as exc:
                raise _RpcError(INVALID_PARAMS, str(exc))
            return _resource_contents(uri, payload)
        raise _RpcError(INVALID_PARAMS, f"Unknown resource: {uri}")


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _tool_result(summary: str, data: dict[str, Any]) -> dict[str, Any]:
    text = summary if not data else f"{summary}\n\n```json\n{json.dumps(data, ensure_ascii=False, indent=2)}\n```"
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {message}"}], "isError": True}


def _resource_contents(uri: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"contents": [{
        "uri": uri,
        "mimeType": "application/json",
        "text": json.dumps(data, ensure_ascii=False, indent=2),
    }]}


def _compact_hit(hit: dict[str, Any]) -> dict[str, Any]:
    raw_node: Any = hit.get("node")
    node = raw_node if isinstance(raw_node, dict) else {}
    return {
        "id": node.get("id"),
        "type": node.get("type"),
        "label": node.get("label"),
        "text": node.get("text"),
        "scope": node.get("scope"),
        "project_id": node.get("project_id"),
        "confidence": round(float(node.get("confidence") or 0.0), 3),
        "score": round(float(hit.get("score") or 0.0), 3),
        "evidence": node.get("evidence") or [],
    }


def _truncate(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clamp_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def main(argv: list[str] | None = None) -> int:
    root = _resolve_root()
    if getattr(sys, "frozen", False) and not os.environ.get("ARCHITECTOS_ROOT"):
        _log(
            f"ARCHITECTOS_ROOT unset; using {root} "
            "(set ARCHITECTOS_ROOT to the full-app data root to share architectos.db)"
        )
    return MemoryMCPServer(root).serve()


if __name__ == "__main__":
    raise SystemExit(main())
