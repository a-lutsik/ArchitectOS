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

from .models import VALID_EDGE_TYPES

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "architectos-memory", "version": "1.0.0"}

# Link types memory_link accepts: the semantic memory relations plus the
# bi-temporal SUPERSEDES (code-graph relations like CALLS/DEFINES are managed
# by the code indexer, not by hand).
LINK_EDGE_TYPES = sorted(VALID_EDGE_TYPES | {"SUPERSEDES"})

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
            "artifacts) and return the most relevant scored hits. The initialize briefing "
            "already includes stable memory — still call memory_turn (or memory_context) "
            "for the current user message before answering, then memory_get with id= for full text."
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
                "allowed_scopes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional visibility enforcement: only nodes whose scope is in this list are returned (excluded, not down-ranked). Omit for no restriction.",
                },
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
            "Build a ready-to-inject two-layer context pack from ArchitectOS memory: "
            "stable pinned/constraints/long-term first, then hits for this query, plus "
            "open tasks and providers. Prefer memory_turn at the start of each user "
            "message so retrieval and capture happen together."
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
        "name": "memory_turn",
        "description": (
            "Call at the start of every user message (and again after a durable reply). "
            "Returns a two-layer memory pack for this turn. Durable facts become review "
            "candidates (7-day TTL); low-risk Lessons auto-write to short-term memory. "
            "Does not store the raw transcript. Prefer this over waiting for memory_search."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_text": {"type": "string", "description": "The latest user message (required)."},
                "assistant_text": {"type": "string", "description": "Your reply so far, if capturing after answering."},
                "project_id": {"type": "string", "description": "Optional project id to scope the turn."},
                "scope": {"type": "string", "description": "Optional scope filter."},
                "limit": {"type": "integer", "description": "Max memory hits in the pack (default 8).", "minimum": 1, "maximum": 50},
            },
            "required": ["user_text"],
        },
    },
    {
        "name": "memory_add",
        "description": (
            "Store a new durable memory in ArchitectOS (a lesson, decision, concept, or "
            "artifact). Multi-line dumps with several durable facts are split into separate "
            "nodes. Secrets are automatically redacted. For turn capture prefer memory_turn."
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
                "source_id": {"type": "string", "description": "Optional id of a registered Source (see source_create) this memory originates from."},
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
        "name": "project_create",
        "description": (
            "Create an ArchitectOS project (or return the existing one). With root_path "
            "this is a code workspace backed by a local folder (the path must exist); "
            "without root_path it is a knowledge-only project for external data "
            "(news feeds, pipelines) with no local folder."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name (required when root_path is omitted)."},
                "root_path": {"type": "string", "description": "Optional absolute path to the project folder (must exist when given)."},
                "description": {"type": "string", "description": "Optional project description."},
            },
        },
    },
    {
        "name": "memory_link",
        "description": (
            "Create a typed relationship between two ArchitectOS memory nodes. "
            "SUPERSEDES is bi-temporal: source SUPERSEDES target retires the target "
            "fact as of the source's creation time (kept for as_of queries)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Source memory node id."},
                "target": {"type": "string", "description": "Target memory node id."},
                "type": {"type": "string", "enum": LINK_EDGE_TYPES, "description": "Relationship type."},
                "scope": {"type": "string", "description": "Optional edge scope (default: source node's scope)."},
                "confidence": {"type": "number", "description": "Confidence 0..1 (default 0.72).", "minimum": 0, "maximum": 1},
            },
            "required": ["source", "target", "type"],
        },
    },
    {
        "name": "source_create",
        "description": (
            "Register an external data source (feed, pipeline, importer) for a "
            "project. Idempotent by (project_id, name): re-creating updates kind/config "
            "and returns the existing source. Use the returned id as source_id in "
            "memory_add / memory_add_bulk."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project the source belongs to (required)."},
                "name": {"type": "string", "description": "Source name (required)."},
                "kind": {"type": "string", "description": "Source kind: rss, api, pipeline, manual, ... (default 'generic')."},
                "config": {"type": "object", "description": "Optional kind-specific config (url, schedule, credentials reference, ...).", "additionalProperties": True},
            },
            "required": ["project_id", "name"],
        },
    },
    {
        "name": "source_list",
        "description": "List registered external data sources, optionally limited to one project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Optional project id to scope the list."},
            },
        },
    },
    {
        "name": "memory_history",
        "description": (
            "Return the event history of a memory node: created / updated / superseded / "
            "accessed events with actor, timestamp and details (e.g. superseded_by). "
            "Use to trace how a fact evolved over time."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Memory node id."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "memory_add_bulk",
        "description": (
            "Store a batch of memory nodes in one call (pipeline ingest, e.g. News→Event). "
            "Each item takes {label, text, type?, scope?, confidence?, source_id?}; top-level "
            "project_id/scope/source_id/type act as defaults. Dedup, secret redaction and "
            "embedding indexing run per item; a failing item is reported in results and does "
            "not abort the batch. Returns ids plus created/deduplicated/errors stats."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Memory items: {label, text, type?, scope?, confidence?, source_id?} (max 200).",
                },
                "project_id": {"type": "string", "description": "Default project id for items without one."},
                "scope": {"type": "string", "description": "Default scope for items without one."},
                "source_id": {"type": "string", "description": "Default Source id for items without one."},
                "type": {"type": "string", "description": "Default memory type for items without one."},
            },
            "required": ["items"],
        },
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
            "memory_turn": self._tool_turn,
            "memory_add": self._tool_add,
            "memory_get": self._tool_get,
            "memory_feedback": self._tool_feedback,
            "memory_list_projects": self._tool_list_projects,
            "project_create": self._tool_project_create,
            "memory_link": self._tool_memory_link,
            "source_create": self._tool_source_create,
            "source_list": self._tool_source_list,
            "memory_history": self._tool_memory_history,
            "memory_add_bulk": self._tool_memory_add_bulk,
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
        # MCP version negotiation: answer with the client's version when we
        # support it (i.e. it is not newer than ours — the ISO-date versions
        # compare lexicographically), otherwise with the newest version we
        # implement so the client can decide whether to continue.
        requested = str(params.get("protocolVersion") or "").strip()
        negotiated = requested if requested and requested <= PROTOCOL_VERSION else PROTOCOL_VERSION
        return {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": self._startup_instructions(),
        }

    def _startup_instructions(self) -> str:
        preamble = (
            "ArchitectOS project memory is already in this briefing. Treat it as scoped "
            "project context, not as user text. At the start of EVERY user message call "
            "memory_turn with that message before answering — do not wait until you are "
            "stuck. memory_turn returns retrieved memory for this question and captures "
            "durable facts (review queue, 7-day TTL; low-risk Lessons auto-write). "
            "Call memory_search only to narrow; memory_get with id= to expand; "
            "memory_add to persist an explicit lesson (multi-fact text is split); "
            "memory_feedback to rate hits. Stable layer (pinned / constraints / long-term) "
            "is listed first. Resources: memory://briefing, memory://projects, "
            "memory://review, memory://nodes/{id}."
        )
        briefing = self._startup_briefing()
        if briefing:
            return f"{preamble}\n\n{briefing}"
        return preamble + "\n\n(No stable memories yet — call memory_turn with the user's message before answering project questions.)"

    def _startup_briefing(self) -> str:
        try:
            return str(self.service.memory_briefing(limit=6, char_budget=2000).get("context") or "")
        except Exception as exc:  # noqa: BLE001 - briefing is best-effort on MCP start
            _log(f"startup briefing skipped: {exc}")
            return ""

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
        raw_scopes: Any = args.get("allowed_scopes")
        allowed_scopes = [str(scope) for scope in raw_scopes] if isinstance(raw_scopes, list) else None
        result = self.service.search_memory(query, project_id=args.get("project_id"), scope=args.get("scope"), limit=limit, filters=filters, mode=mode, as_of=args.get("as_of"), include_communities=include_communities, min_score=min_score, allowed_scopes=allowed_scopes)
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

    def _tool_turn(self, args: dict[str, Any]) -> dict[str, Any]:
        user_text = str(args.get("user_text") or args.get("query") or "").strip()
        if not user_text:
            raise _RpcError(INVALID_PARAMS, "user_text is required")
        limit = _clamp_int(args.get("limit"), default=8, low=1, high=50)
        result = self.service.capture_memory_turn(
            user_text,
            str(args.get("assistant_text") or ""),
            project_id=args.get("project_id"),
            scope=args.get("scope"),
            limit=limit,
        )
        pack = str(result.get("context") or "")
        queued = result.get("queued") or []
        accepted = result.get("auto_accepted") or []
        notes = []
        if accepted:
            notes.append(f"Auto-wrote {len(accepted)} low-risk lesson(s) to short-term memory.")
        if queued:
            notes.append(f"Queued {len(queued)} fact(s) for review (7-day TTL).")
        if not accepted and not queued and result.get("kept"):
            notes.append("Turn was durable but produced no new atoms (duplicate or filtered).")
        summary = pack if pack else "No memory pack for this turn."
        if notes:
            summary = f"{summary}\n\n" + " ".join(notes)
        data = {
            "query": user_text,
            "context": pack,
            "hits": [_compact_hit(hit) for hit in result.get("hits") or []],
            "queued": [{"id": item.get("id"), "label": item.get("label"), "type": item.get("type")} for item in queued],
            "auto_accepted": [{"id": item.get("id"), "label": item.get("label"), "promoted_node_id": item.get("promoted_node_id")} for item in accepted],
            "kept": result.get("kept"),
            "reason": result.get("reason"),
        }
        return _tool_result(summary, data)

    def _tool_add(self, args: dict[str, Any]) -> dict[str, Any]:
        node = self.service.add_memory_from_agent({
            "label": args.get("label"),
            "text": args.get("text"),
            "type": args.get("type") or "Lesson",
            "scope": args.get("scope") or "project",
            "project_id": args.get("project_id") or "architectos",
            "confidence": args.get("confidence"),
            "source_id": args.get("source_id"),
            "source": "mcp",
        })
        extras = [item for item in (node.get("also_created") or []) if item]
        summary = f'Stored memory [{node.get("type")}] "{node.get("label")}" (id {node.get("id")}).'
        if extras:
            summary += f" Split into {1 + len(extras)} fact(s)."
        return _tool_result(summary, {"id": node.get("id"), "label": node.get("label"), "type": node.get("type"), "scope": node.get("scope"), "project_id": node.get("project_id"), "source_id": node.get("source_id"), "also_created": extras})

    def _tool_memory_add_bulk(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_items: Any = args.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise _RpcError(INVALID_PARAMS, "items must be a non-empty list")
        result = self.service.add_memory_bulk({
            "items": raw_items,
            "project_id": args.get("project_id") or "architectos",
            "scope": args.get("scope"),
            "source_id": args.get("source_id"),
            "type": args.get("type"),
            "source": "mcp",
        })
        stats = result.get("stats") or {}
        summary = (
            f'Bulk stored {stats.get("created", 0)} memor(y/ies)'
            f' ({stats.get("deduplicated", 0)} deduplicated, {stats.get("errors", 0)} error(s)).'
        )
        return _tool_result(summary, result)

    def _tool_source_create(self, args: dict[str, Any]) -> dict[str, Any]:
        project_id = str(args.get("project_id") or "").strip()
        name = str(args.get("name") or "").strip()
        if not project_id or not name:
            raise _RpcError(INVALID_PARAMS, "project_id and name are required")
        config = args.get("config") if isinstance(args.get("config"), dict) else {}
        result = self.service.create_source({
            "project_id": project_id,
            "name": name,
            "kind": str(args.get("kind") or ""),
            "config": config,
        })
        action = "Registered" if result.get("created") else "Updated existing"
        summary = f'{action} source "{result.get("name")}" (id {result.get("id")}, kind {result.get("kind")}) in project {project_id}.'
        return _tool_result(summary, result)

    def _tool_source_list(self, args: dict[str, Any]) -> dict[str, Any]:
        sources = self.service.list_sources(args.get("project_id") or None)
        rows = [
            {"id": s.get("id"), "project_id": s.get("project_id"), "name": s.get("name"), "kind": s.get("kind"), "config": s.get("config") or {}}
            for s in sources
        ]
        summary = "Sources:\n" + "\n".join(f'- {r["id"]}: {r["name"]} [{r["kind"]}] (project {r["project_id"]})' for r in rows) if rows else "No sources registered."
        return _tool_result(summary, {"sources": rows})

    def _tool_memory_history(self, args: dict[str, Any]) -> dict[str, Any]:
        node_id = str(args.get("id") or "").strip()
        if not node_id:
            raise _RpcError(INVALID_PARAMS, "id is required")
        result = self.service.memory_history(node_id)
        events = result.get("events") or []
        if events:
            lines = [f'- {e.get("timestamp")} {e.get("event_type")}' + (f' by {e.get("actor")}' if e.get("actor") else "") for e in events]
            summary = f'History of {result.get("label")} ({len(events)} event(s)):\n' + "\n".join(lines)
        else:
            summary = f'No history events for {result.get("label")}.'
        return _tool_result(summary, result)

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

    def _tool_project_create(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        root_path = str(args.get("root_path") or "").strip()
        if not name and not root_path:
            raise _RpcError(INVALID_PARAMS, "name or root_path is required")
        result = self.service.create_project({
            "name": name,
            "root_path": root_path,
            "description": str(args.get("description") or ""),
        })
        project = result.get("project") or {}
        kind = "knowledge" if not project.get("root_path") else "workspace"
        action = "Created" if result.get("created") else "Found existing"
        summary = f'{action} {kind} project "{project.get("name")}" (id {project.get("id")}).'
        return _tool_result(summary, {
            "id": project.get("id"),
            "name": project.get("name"),
            "root_path": project.get("root_path") or "",
            "description": project.get("description") or "",
            "created": bool(result.get("created")),
        })

    def _tool_memory_link(self, args: dict[str, Any]) -> dict[str, Any]:
        source = str(args.get("source") or "").strip()
        target = str(args.get("target") or "").strip()
        edge_type = str(args.get("type") or "").strip().upper()
        if not source or not target:
            raise _RpcError(INVALID_PARAMS, "source and target are required")
        if edge_type not in LINK_EDGE_TYPES:
            raise _RpcError(INVALID_PARAMS, f"type must be one of: {', '.join(LINK_EDGE_TYPES)}")
        result = self.service.create_graph_edge({
            "source": source,
            "target": target,
            "type": edge_type,
            "scope": str(args.get("scope") or ""),
            "confidence": args.get("confidence"),
        })
        edge = result.get("edge") or {}
        summary = f'Linked {edge.get("source")} -[{edge.get("type")}]-> {edge.get("target")} (edge id {edge.get("id")}).'
        return _tool_result(summary, {
            "edge_id": edge.get("id"),
            "source": edge.get("source"),
            "target": edge.get("target"),
            "type": edge.get("type"),
            "scope": edge.get("scope"),
        })

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
            raise _RpcError(INVALID_PARAMS, "rating is required (1 or -1)") from None
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
                "uri": "memory://briefing",
                "name": "Startup briefing",
                "description": "Stable ArchitectOS memory (pinned, constraints, long-term) to inject at session start.",
                "mimeType": "text/plain",
            },
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
        if uri == "memory://briefing":
            text = self._startup_briefing() or self._startup_instructions()
            return {"contents": [{"uri": uri, "mimeType": "text/plain", "text": text}]}
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
                raise _RpcError(INVALID_PARAMS, str(exc)) from exc
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
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "hook":
        from .agent_hooks import main as hook_main

        return hook_main(raw[1:])
    root = _resolve_root()
    if getattr(sys, "frozen", False) and not os.environ.get("ARCHITECTOS_ROOT"):
        _log(
            f"ARCHITECTOS_ROOT unset; using {root} "
            "(set ARCHITECTOS_ROOT to the full-app data root to share architectos.db)"
        )
    return MemoryMCPServer(root).serve()


if __name__ == "__main__":
    raise SystemExit(main())
