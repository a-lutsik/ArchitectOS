from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from .mcp import MCPError, MCPManager
from .rich_response import RESPONSE_FORMAT_POLICY

TOOL_RESULT_CHAR_CAP = 6000
MAX_TOOL_ROUNDS = 3

MEMORY_FIRST_POLICY = """Memory-first:
1) Prefer project memory / context already provided.
2) If a memory line is truncated or you need the full node, call memory_get with that id=… value.
3) Work-item comments and long description parts live as linked child memory nodes — memory_get or graph neighbors after the main card.
4) If memory has Azure Boards work item IDs or thin summaries, call boards_get_item / boards_list_comments for those IDs to refresh live details.
5) If memory has nothing useful for the question, call boards_search or boards_my_work, then boards_get_item as needed.
6) Prefer fs_* tools when the question needs files under the project folder.
7) For meetings / “what did X say” / call notes: if memory Meeting hits are missing or thin, call granola_list_meetings, then granola_get_meetings (and granola_get_transcript when notes are incomplete).
8) Do not call tools when memory already answers the question fully.
9) Emit tool_calls JSON only when a tool is needed; otherwise answer normally."""

MEMORY_ONLY_POLICY = """Memory-first:
1) Prefer project memory / context already provided.
2) If a memory line is truncated or you need the full node, call memory_get with that id=… value.
3) Work-item comments and long description parts live as linked child memory nodes — memory_get after the main card.
4) Do not call tools when memory already answers the question fully.
5) Emit tool_calls JSON only when a tool is needed; otherwise answer normally."""

TOOL_PROTOCOL = """You may request tools by emitting a single JSON object (optionally in a ```json fence):
{"tool_calls":[{"name":"memory_get","arguments":{"id":"node_abc"}}]}
If no tool is needed, answer normally without that JSON.
After tool results are provided, answer the user; do not invent tool results."""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    server_id: str
    mcp_tool: str
    parameters_schema: dict[str, Any]
    kind: str = "mcp"  # mcp | native
    write: bool = False


BOARDS_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="boards_my_work",
        description="List Azure Boards work items assigned to me.",
        server_id="azure-devops",
        mcp_tool="wit_my_work_items",
        parameters_schema={
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "top": {"type": "integer"},
                "type": {"type": "string", "description": "assignedtome | followed | mentioned | etc."},
            },
        },
    ),
    ToolSpec(
        name="boards_search",
        description="Search Azure Boards work items by text.",
        server_id="azure-devops",
        mcp_tool="search_workitem",
        parameters_schema={
            "type": "object",
            "properties": {
                "searchText": {"type": "string"},
                "query": {"type": "string"},
                "project": {"type": "string"},
                "top": {"type": "integer"},
            },
            "required": [],
        },
    ),
    ToolSpec(
        name="boards_get_item",
        description="Get one Azure Boards work item by id.",
        server_id="azure-devops",
        mcp_tool="wit_get_work_item",
        parameters_schema={
            "type": "object",
            "properties": {
                "id": {"type": ["integer", "string"]},
                "project": {"type": "string"},
            },
            "required": ["id"],
        },
    ),
    ToolSpec(
        name="boards_list_comments",
        description="List comments for an Azure Boards work item.",
        server_id="azure-devops",
        mcp_tool="wit_list_work_item_comments",
        parameters_schema={
            "type": "object",
            "properties": {
                "workItemId": {"type": ["integer", "string"]},
                "id": {"type": ["integer", "string"]},
                "project": {"type": "string"},
                "top": {"type": "integer"},
            },
            "required": [],
        },
    ),
    ToolSpec(
        name="boards_query_wiql",
        description="Run a WIQL query against Azure Boards.",
        server_id="azure-devops",
        mcp_tool="wit_query_by_wiql",
        parameters_schema={
            "type": "object",
            "properties": {
                "wiql": {"type": "string"},
                "project": {"type": "string"},
            },
            "required": ["wiql"],
        },
    ),
]

GRANOLA_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="granola_list_meetings",
        description="List recent Granola meetings (titles/dates/ids). Use for meeting/person questions when memory is thin.",
        server_id="granola",
        mcp_tool="list_meetings",
        parameters_schema={"type": "object", "properties": {}},
    ),
    ToolSpec(
        name="granola_get_meetings",
        description="Fetch Granola meeting details/notes by meeting id(s).",
        server_id="granola",
        mcp_tool="get_meetings",
        parameters_schema={
            "type": "object",
            "properties": {
                "meeting_ids": {"type": "array", "items": {"type": "string"}},
                "meeting_id": {"type": "string"},
                "id": {"type": "string"},
            },
            "required": [],
        },
    ),
    ToolSpec(
        name="granola_get_transcript",
        description="Fetch a Granola meeting transcript by meeting id when notes are incomplete.",
        server_id="granola",
        mcp_tool="get_meeting_transcript",
        parameters_schema={
            "type": "object",
            "properties": {
                "meeting_id": {"type": "string"},
                "id": {"type": "string"},
            },
            "required": [],
        },
    ),
]

MEMORY_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="memory_get",
        description="Fetch the full ArchitectOS memory node by id (use when context shows id=… and the excerpt is truncated or incomplete).",
        server_id="local-memory",
        mcp_tool="memory_get",
        parameters_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Memory node id from context (id=…)"},
                "node_id": {"type": "string"},
                "include_neighbors": {"type": "boolean", "description": "Include linked comment/chunk neighbors"},
            },
            "required": ["id"],
        },
        kind="native",
    ),
]

FS_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="fs_read",
        description="Read a text file under the current project folder.",
        server_id="filesystem",
        mcp_tool="read_file",
        parameters_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        kind="native",
    ),
    ToolSpec(
        name="fs_list",
        description="List files and folders under the current project folder.",
        server_id="filesystem",
        mcp_tool="list_directory",
        parameters_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative directory; empty or . for root"},
                "limit": {"type": "integer"},
            },
        },
        kind="native",
    ),
    ToolSpec(
        name="fs_search",
        description="Search project files by name or content.",
        server_id="filesystem",
        mcp_tool="search_files",
        parameters_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "mode": {"type": "string", "description": "name | content"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
        kind="native",
    ),
    ToolSpec(
        name="fs_write",
        description="Create or overwrite a text file under the project folder. Requires write approval.",
        server_id="filesystem",
        mcp_tool="write_file",
        parameters_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "text": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path"],
        },
        kind="native",
        write=True,
    ),
]

_ALL_SPECS = {spec.name: spec for spec in [*MEMORY_TOOLS, *BOARDS_TOOLS, *GRANOLA_TOOLS, *FS_TOOLS]}

_TOOL_CALLS_RE = re.compile(
    r"```(?:json)?\s*(\{[\s\S]*?\}\s*)```|(\{[^{}]*\"tool_calls\"[\s\S]*\})",
    re.I,
)


class ToolGateway:
    """Allowlisted agent tools backed by MCP and/or native project file APIs."""

    def __init__(
        self,
        mcp_manager: MCPManager,
        *,
        boards_project: Callable[[], str] | None = None,
        native_handlers: dict[str, Callable[[dict[str, Any]], Any]] | None = None,
    ) -> None:
        self.mcp_manager = mcp_manager
        self._boards_project = boards_project or (lambda: "E-AI")
        self._native_handlers = native_handlers or {}

    def server_enabled(self, server_id: str) -> bool:
        config = self.mcp_manager.get_server(server_id)
        return bool(config and config.enabled)

    def boards_enabled(self) -> bool:
        return self.server_enabled("azure-devops")

    def granola_enabled(self) -> bool:
        return self.server_enabled("granola")

    def filesystem_mcp_enabled(self) -> bool:
        return self.server_enabled("filesystem")

    def available_specs(self, *, include_writes: bool = False, memory_only: bool = False) -> list[ToolSpec]:
        specs: list[ToolSpec] = list(MEMORY_TOOLS)
        if memory_only:
            return specs
        if self.boards_enabled():
            specs.extend(BOARDS_TOOLS)
        if self.granola_enabled():
            specs.extend(GRANOLA_TOOLS)
        # Agent filesystem tools follow MCP Filesystem enablement; execution prefers native handlers.
        if self.filesystem_mcp_enabled():
            for spec in FS_TOOLS:
                if spec.write and not include_writes:
                    continue
                specs.append(spec)
        return specs

    def tools_available(self, *, include_writes: bool = False, memory_only: bool = False) -> bool:
        return bool(self.available_specs(include_writes=include_writes, memory_only=memory_only))

    def catalog_for_prompt(self, *, include_writes: bool = False, memory_only: bool = False) -> str:
        specs = self.available_specs(include_writes=include_writes, memory_only=memory_only)
        if not specs:
            return ""
        lines = ["Available tools:"]
        for spec in specs:
            params = json.dumps(spec.parameters_schema.get("properties") or {}, ensure_ascii=False)
            write_note = " [write]" if spec.write else ""
            lines.append(f"- {spec.name}{write_note}: {spec.description} params={params}")
        return "\n".join(lines)

    def tool_prompt_section(self, *, include_writes: bool = False, memory_only: bool = False) -> str:
        catalog = self.catalog_for_prompt(include_writes=include_writes, memory_only=memory_only)
        if not catalog:
            return ""
        policy = MEMORY_ONLY_POLICY if memory_only else MEMORY_FIRST_POLICY
        return f"{policy}\n\n{catalog}\n\n{TOOL_PROTOCOL}\n\n{RESPONSE_FORMAT_POLICY}"

    def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        include_writes: bool = False,
        memory_only: bool = False,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        spec = _ALL_SPECS.get(name)
        if not spec:
            return {"ok": False, "name": name, "error": f"Unknown or disallowed tool: {name}", "summary": ""}
        if spec.write and not include_writes:
            return {
                "ok": False,
                "name": name,
                "error": "Write tools require approval (enable CLI/write approval for this turn).",
                "summary": "",
            }
        allowed = {item.name for item in self.available_specs(include_writes=include_writes, memory_only=memory_only)}
        if name not in allowed:
            return {"ok": False, "name": name, "error": f"Tool not available: {name}", "summary": ""}

        args = dict(arguments or {})
        if project_id and "project_id" not in args:
            args["project_id"] = project_id

        try:
            if spec.name.startswith("memory_"):
                payload = self._execute_native(spec, args)
            elif spec.name.startswith("boards_"):
                payload = self._execute_boards(spec, args)
            elif spec.name.startswith("granola_"):
                payload = self._execute_granola(spec, args)
            elif spec.name.startswith("fs_"):
                payload = self._execute_fs(spec, args)
            else:
                return {"ok": False, "name": name, "error": "Unsupported tool kind.", "summary": ""}
        except MCPError as exc:
            return {"ok": False, "name": name, "error": str(exc), "summary": ""}
        except Exception as exc:  # noqa: BLE001 — surface to model as tool error
            return {"ok": False, "name": name, "error": str(exc), "summary": ""}

        summary = summarize_tool_payload(payload)
        return {"ok": True, "name": name, "error": "", "summary": summary, "result": payload}

    def _execute_native(self, spec: ToolSpec, args: dict[str, Any]) -> Any:
        handler = self._native_handlers.get(spec.name)
        if not handler:
            raise ValueError(f"Native handler not registered for {spec.name}")
        return handler(args)

    def _execute_boards(self, spec: ToolSpec, args: dict[str, Any]) -> Any:
        ado_project = str(args.get("project") or self._boards_project()).strip() or self._boards_project()
        mcp_args: dict[str, Any]
        if spec.name == "boards_my_work":
            mcp_args = {
                "project": ado_project,
                "type": str(args.get("type") or "assignedtome"),
                "top": int(args.get("top") or 20),
                "includeCompleted": bool(args.get("includeCompleted", True)),
            }
        elif spec.name == "boards_search":
            search_text = str(args.get("searchText") or args.get("query") or args.get("search_text") or "").strip()
            if not search_text:
                raise ValueError("boards_search requires searchText or query")
            mcp_args = {
                "searchText": search_text,
                "project": [ado_project],
                "top": min(int(args.get("top") or 25), 25),
                "skip": int(args.get("skip") or 0),
            }
            if args.get("workItemType") or args.get("work_item_type"):
                mcp_args["workItemType"] = [str(args.get("workItemType") or args.get("work_item_type"))]
        elif spec.name == "boards_get_item":
            work_id = args.get("id") or args.get("workItemId") or args.get("work_item_id")
            if work_id is None or str(work_id).strip() == "":
                raise ValueError("boards_get_item requires id")
            mcp_args = {"id": int(work_id), "project": ado_project, "expand": str(args.get("expand") or "all")}
        elif spec.name == "boards_list_comments":
            work_id = args.get("workItemId") or args.get("id") or args.get("work_item_id")
            if work_id is None or str(work_id).strip() == "":
                raise ValueError("boards_list_comments requires workItemId or id")
            mcp_args = {
                "project": ado_project,
                "workItemId": int(work_id),
                "top": int(args.get("top") or 20),
            }
        elif spec.name == "boards_query_wiql":
            wiql = str(args.get("wiql") or "").strip()
            if not wiql:
                raise ValueError("boards_query_wiql requires wiql")
            mcp_args = {"wiql": wiql, "project": ado_project}
        else:
            raise ValueError(f"Unsupported boards tool: {spec.name}")
        return self.mcp_manager.call_tool(spec.server_id, spec.mcp_tool, mcp_args)

    def _execute_granola(self, spec: ToolSpec, args: dict[str, Any]) -> Any:
        if not self.granola_enabled():
            raise MCPError("Granola tools are unavailable (MCP granola disabled).")
        if spec.name == "granola_list_meetings":
            return self.mcp_manager.call_tool("granola", "list_meetings", {})
        if spec.name == "granola_get_meetings":
            meeting_ids: list[str] = []
            raw_ids = args.get("meeting_ids") or args.get("ids")
            if isinstance(raw_ids, list):
                meeting_ids = [str(item).strip() for item in raw_ids if str(item).strip()]
            elif isinstance(raw_ids, str) and raw_ids.strip():
                meeting_ids = [part.strip() for part in raw_ids.split(",") if part.strip()]
            single = str(args.get("meeting_id") or args.get("id") or "").strip()
            if single and single not in meeting_ids:
                meeting_ids.insert(0, single)
            if not meeting_ids:
                raise ValueError("granola_get_meetings requires meeting_ids or meeting_id")
            meeting_ids = meeting_ids[:10]
            last_error: Exception | None = None
            for arguments in (
                {"meeting_ids": meeting_ids},
                {"ids": meeting_ids},
                {"meeting_id": meeting_ids[0]} if len(meeting_ids) == 1 else None,
                {"id": meeting_ids[0]} if len(meeting_ids) == 1 else None,
            ):
                if not arguments:
                    continue
                try:
                    return self.mcp_manager.call_tool("granola", "get_meetings", arguments)
                except MCPError as exc:
                    last_error = exc
                    continue
            raise last_error or MCPError("granola get_meetings failed")
        if spec.name == "granola_get_transcript":
            meeting_id = str(args.get("meeting_id") or args.get("id") or "").strip()
            if not meeting_id:
                raise ValueError("granola_get_transcript requires meeting_id")
            last_error = None
            for arguments in ({"meeting_id": meeting_id}, {"id": meeting_id}, {"meeting_ids": [meeting_id]}):
                try:
                    return self.mcp_manager.call_tool("granola", "get_meeting_transcript", arguments)
                except MCPError as exc:
                    last_error = exc
                    continue
            raise last_error or MCPError("granola get_meeting_transcript failed")
        raise ValueError(f"Unsupported granola tool: {spec.name}")

    def _execute_fs(self, spec: ToolSpec, args: dict[str, Any]) -> Any:
        handler = self._native_handlers.get(spec.name)
        if handler:
            return handler(args)
        if not self.filesystem_mcp_enabled():
            raise MCPError("Filesystem tools are unavailable (MCP filesystem disabled and no native handler).")
        path = str(args.get("path") or args.get("query") or ".").strip() or "."
        if spec.name == "fs_read":
            return self.mcp_manager.call_tool("filesystem", "read_file", {"path": path})
        if spec.name == "fs_list":
            return self.mcp_manager.call_tool("filesystem", "list_directory", {"path": path})
        if spec.name == "fs_search":
            query = str(args.get("query") or "").strip()
            if not query:
                raise ValueError("fs_search requires query")
            return self.mcp_manager.call_tool(
                "filesystem",
                "search_files",
                {"path": str(args.get("path") or "."), "pattern": query},
            )
        if spec.name == "fs_write":
            text = args.get("text")
            if text is None:
                text = args.get("content")
            return self.mcp_manager.call_tool(
                "filesystem",
                "write_file",
                {"path": path, "content": str(text if text is not None else "")},
            )
        raise ValueError(f"Unsupported fs tool: {spec.name}")


def summarize_tool_payload(payload: Any, *, cap: int = TOOL_RESULT_CHAR_CAP) -> str:
    if payload is None:
        return ""
    compact = _compact_mcp_tool_payload(payload)
    if compact is not None:
        text = compact
    elif isinstance(payload, str):
        text = payload
    else:
        try:
            text = json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(payload)
    text = text.strip()
    if len(text) <= cap:
        return text
    return text[: cap - 20] + "\n…[truncated]"


_UNTRUSTED_WIQL_RE = re.compile(
    r"<<[0-9a-f]+>>\s*\[UNTRUSTED WIQL QUERY RESULTS CONTENT[^\]]*\]\s*<<[0-9a-f]+>>\s*",
    re.I,
)


def _compact_mcp_tool_payload(payload: Any) -> str | None:
    """Shrink Azure DevOps MCP wrappers so WIQL workItems survive the summary cap."""
    if not isinstance(payload, dict):
        return None
    tool = str(payload.get("tool") or "").strip()
    inner = payload.get("result")
    text_blob = _mcp_content_text(inner if inner is not None else payload)
    if not text_blob:
        return None
    cleaned = _UNTRUSTED_WIQL_RE.sub("", text_blob).strip()
    parsed = _try_parse_json_object(cleaned) or _try_parse_json_object(text_blob)
    if not isinstance(parsed, dict):
        # Keep raw text but without wrapper noise when possible.
        return cleaned[:TOOL_RESULT_CHAR_CAP] if cleaned else None

    if tool in {"wit_query_by_wiql", "wit_get_query_results_by_id"} or "workItems" in parsed:
        work_items = parsed.get("workItems")
        if isinstance(work_items, list):
            ids: list[str] = []
            for item in work_items:
                if isinstance(item, dict) and item.get("id") is not None:
                    ids.append(str(item.get("id")))
                elif isinstance(item, (int, str)) and str(item).strip():
                    ids.append(str(item).strip())
            preview = ", ".join(ids[:80])
            more = f" (+{len(ids) - 80} more)" if len(ids) > 80 else ""
            return (
                f"WIQL ok · {len(ids)} work item(s)"
                + (f": {preview}{more}" if ids else " (empty)")
            )

    if tool == "search_workitem" or ("count" in parsed and "results" in parsed):
        results = parsed.get("results") if isinstance(parsed.get("results"), list) else []
        count = int(parsed.get("count") or len(results) or 0)
        lines: list[str] = [f"search_workitem · count={count}, showing={len(results)}"]
        for item in results[:20]:
            if not isinstance(item, dict):
                continue
            fields = item.get("fields") if isinstance(item.get("fields"), dict) else item
            wid = fields.get("system.id") or fields.get("System.Id") or fields.get("id") or ""
            title = fields.get("system.title") or fields.get("System.Title") or fields.get("title") or ""
            wtype = fields.get("system.workitemtype") or fields.get("System.WorkItemType") or ""
            lines.append(f"- {wtype} #{wid}: {title}".strip())
        return "\n".join(lines)

    if tool == "wit_get_work_item" or (parsed.get("id") and isinstance(parsed.get("fields"), dict)):
        fields = parsed.get("fields") if isinstance(parsed.get("fields"), dict) else {}
        wid = parsed.get("id") or fields.get("System.Id")
        title = fields.get("System.Title") or ""
        wtype = fields.get("System.WorkItemType") or ""
        state = fields.get("System.State") or ""
        iteration = fields.get("System.IterationPath") or ""
        parent = fields.get("System.Parent") or ""
        return (
            f"work item #{wid} · {wtype} · {state}\n"
            f"title: {title}\n"
            f"iteration: {iteration}"
            + (f"\nparent: {parent}" if parent else "")
        )

    granola_compact = _compact_granola_payload(tool, parsed, cleaned)
    if granola_compact:
        return granola_compact

    # Generic: return cleaned JSON without MCP envelope / untrusted banners.
    try:
        return json.dumps(parsed, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return cleaned or None


def _compact_granola_payload(tool: str, parsed: dict[str, Any], cleaned: str) -> str | None:
    """Keep meeting titles/ids readable under the tool summary cap."""
    meetings: list[Any] = []
    if isinstance(parsed.get("meetings"), list):
        meetings = list(parsed.get("meetings") or [])
    elif isinstance(parsed.get("data"), list):
        meetings = list(parsed.get("data") or [])
    elif isinstance(parsed.get("items"), list):
        meetings = list(parsed.get("items") or [])
    elif tool in {"list_meetings", "get_meetings"} and isinstance(parsed.get("id"), (str, int)):
        meetings = [parsed]

    if tool in {"list_meetings", "get_meetings"} or meetings:
        if not meetings and cleaned:
            # Fall back to truncated cleaned text for unstructured granola payloads.
            return f"granola {tool or 'meetings'}:\n{cleaned[:TOOL_RESULT_CHAR_CAP]}"
        lines = [f"granola {tool or 'meetings'} · {len(meetings)} item(s)"]
        for item in meetings[:12]:
            if not isinstance(item, dict):
                continue
            mid = item.get("id") or item.get("meeting_id") or ""
            title = item.get("title") or item.get("name") or item.get("summary") or ""
            when = item.get("date") or item.get("start_time") or item.get("created_at") or ""
            notes = str(item.get("notes") or item.get("summary") or item.get("text") or "").strip()
            line = f"- {title or '(untitled)'}".strip()
            if mid:
                line += f" · id={mid}"
            if when:
                line += f" · {when}"
            lines.append(line)
            if notes:
                lines.append(f"  notes: {notes[:900]}")
        return "\n".join(lines)

    if tool == "get_meeting_transcript" or "transcript" in parsed:
        transcript = str(parsed.get("transcript") or parsed.get("text") or parsed.get("content") or cleaned or "").strip()
        if transcript:
            return f"granola transcript:\n{transcript[:TOOL_RESULT_CHAR_CAP]}"
    return None


def _mcp_content_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    content = payload.get("content")
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                chunks.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                chunks.append(item)
        if chunks:
            return "\n".join(chunks)
    if isinstance(payload.get("text"), str):
        return str(payload.get("text") or "")
    return ""


def parse_tool_calls(text: str) -> list[dict[str, Any]]:
    """Tolerantly extract tool_calls from model text. Returns [] if none."""
    raw = str(text or "").strip()
    if not raw or "tool_calls" not in raw:
        return []

    candidates: list[str] = []
    for match in _TOOL_CALLS_RE.finditer(raw):
        chunk = match.group(1) or match.group(2)
        if chunk:
            candidates.append(chunk.strip())
    # Prefer last JSON-looking blob that contains tool_calls
    if not candidates:
        start = raw.rfind("{")
        while start >= 0:
            blob = raw[start:]
            if "tool_calls" in blob:
                candidates.append(blob)
                break
            start = raw.rfind("{", 0, start)

    for candidate in reversed(candidates):
        parsed = _try_parse_json_object(candidate)
        if not parsed:
            continue
        calls = parsed.get("tool_calls")
        if isinstance(calls, list):
            cleaned: list[dict[str, Any]] = []
            for item in calls:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("tool") or "").strip()
                if not name:
                    continue
                arguments = item.get("arguments") or item.get("args") or {}
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {"raw": arguments}
                if not isinstance(arguments, dict):
                    arguments = {"value": arguments}
                cleaned.append({"name": name, "arguments": arguments})
            if cleaned:
                return cleaned
    return []


def strip_tool_call_json(text: str) -> str:
    """Remove trailing tool_calls JSON so the user sees prose only."""
    raw = str(text or "")
    if "tool_calls" not in raw:
        return raw.strip()
    match = _TOOL_CALLS_RE.search(raw)
    if match:
        start = match.start()
        return (raw[:start] + raw[match.end() :]).strip()
    idx = raw.rfind('{"tool_calls"')
    if idx < 0:
        idx = raw.rfind("{")
        if idx >= 0 and "tool_calls" in raw[idx:]:
            return raw[:idx].strip()
        return raw.strip()
    return raw[:idx].strip()


def _try_parse_json_object(text: str) -> dict[str, Any] | None:
    raw = text.strip()
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    # Balance braces from the first {
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for index in range(start, len(raw)):
        ch = raw[index]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(raw[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return value if isinstance(value, dict) else None
    return None


def format_tool_results_for_prompt(trace: list[dict[str, Any]]) -> str:
    lines = ["TOOL_RESULT"]
    for item in trace:
        name = item.get("name") or "tool"
        if item.get("ok"):
            lines.append(f"### {name}\n{item.get('summary') or ''}")
        else:
            lines.append(f"### {name}\nERROR: {item.get('error') or 'failed'}")
    return "\n\n".join(lines).strip()


def compact_tool_trace_label(trace: list[dict[str, Any]]) -> str:
    if not trace:
        return ""
    bits: list[str] = []
    for item in trace:
        name = str(item.get("name") or "tool")
        if not item.get("ok"):
            bits.append(f"{name} (failed)")
            continue
        if name == "boards_get_item":
            bits.append("get item")
        elif name == "boards_search":
            bits.append("search")
        elif name == "boards_my_work":
            bits.append("my work")
        elif name.startswith("fs_"):
            bits.append(name.replace("fs_", "fs "))
        elif name == "memory_get":
            bits.append("memory get")
        else:
            bits.append(name.replace("boards_", "").replace("_", " "))
    prefix = "Used tools"
    if any(str(item.get("name") or "").startswith("boards_") for item in trace):
        prefix = "Used Azure Boards / tools"
    return f"{prefix}: " + ", ".join(bits)
