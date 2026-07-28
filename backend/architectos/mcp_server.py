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
    override = os.environ.get("ARCHITECTOS_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


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
                "query": {"type": "string", "description": "Natural-language search query."},
                "project_id": {"type": "string", "description": "Optional project id to scope the search."},
                "scope": {"type": "string", "description": "Optional scope filter (e.g. project, interface, shared, global)."},
                "limit": {"type": "integer", "description": "Max number of hits (default 8).", "minimum": 1, "maximum": 50},
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
        "name": "memory_list_projects",
        "description": "List ArchitectOS projects (id, name, root path) available for scoping memory.",
        "inputSchema": {"type": "object", "properties": {}},
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
            "memory_list_projects": self._tool_list_projects,
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
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
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
                result = {"resources": [], "resourceTemplates": []}
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
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": (
                "ArchitectOS memory engine. Use memory_search or memory_context to recall "
                "project knowledge, and memory_add to persist new lessons and decisions."
            ),
        }

    # -- Tools -------------------------------------------------------------
    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        handler = self._handlers.get(name)
        if handler is None:
            raise _RpcError(METHOD_NOT_FOUND, f"Unknown tool: {name}")
        try:
            return handler(arguments)
        except ValueError as exc:
            return _tool_error(str(exc))

    def _tool_search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            raise _RpcError(INVALID_PARAMS, "query is required")
        limit = _clamp_int(args.get("limit"), default=8, low=1, high=50)
        result = self.service.search_memory(query, project_id=args.get("project_id"), scope=args.get("scope"), limit=limit)
        hits = [_compact_hit(hit) for hit in result.get("hits") or []]
        if hits:
            lines = [f'{i + 1}. [{h["type"]}] {h["label"]} — {_truncate(h["text"], 240)} (score {h["score"]}, conf {h["confidence"]})' for i, h in enumerate(hits)]
            summary = f'Found {len(hits)} memory hit(s) for "{query}":\n' + "\n".join(lines)
        else:
            summary = f'No memory found for "{query}".'
        return _tool_result(summary, {"query": query, "hits": hits})

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

    def _tool_list_projects(self, _args: dict[str, Any]) -> dict[str, Any]:
        projects = self.service.projects()
        rows = [{"id": p.get("id"), "name": p.get("name"), "root_path": p.get("root_path")} for p in projects]
        summary = "Projects:\n" + "\n".join(f'- {r["id"]}: {r["name"]}' for r in rows) if rows else "No projects yet."
        return _tool_result(summary, {"projects": rows})


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


def _compact_hit(hit: dict[str, Any]) -> dict[str, Any]:
    node = hit.get("node") if isinstance(hit.get("node"), dict) else {}
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
    return MemoryMCPServer(root).serve()


if __name__ == "__main__":
    raise SystemExit(main())
