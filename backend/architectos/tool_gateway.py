from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .mcp import MCPError, MCPManager
from .permissions import PermissionRequired
from .rich_response import RESPONSE_FORMAT_POLICY

TOOL_RESULT_CHAR_CAP = 6000
# File reads are the payload, not a wrapper around it: give them room so a full
# read window survives instead of losing the continuation hint to truncation.
FILE_TOOL_RESULT_CHAR_CAP = 16_000
MAX_TOOL_ROUNDS = 6

AUTONOMY_POLICY = """Autonomy (overrides everything else):
- Read-only lookups (memory, files, work items, meetings) run without user approval. Never ask “may I scan?”, “разрешаешь?”, “shall I continue?” — just call the tools.
- Never promise work for a later turn and never answer with a plan of what you are about to read. Either emit tool_calls now or give the final answer.
- Batch related lookups in one round (up to 5 calls) instead of one file per turn.
- A partial result is not a dead end: when a result reports truncated / next_start_line / hasMore / more pages, call the tool again for the next window before answering. Keep going until you have what the answer needs or the round budget runs out.
- Never ask the user to paste content you can read yourself, and never end a turn with a list of sources you still need — fetch them now and answer with what you found.
- Only ask the user something when it is a product decision you cannot make.
- If a file path is outside the project folder, still call the tool with that path. ArchitectOS will pause and ask the user for access. Do not ask for permission in chat text.
- Answer in plain language and never name the tools listed below — write “read the file …”, “searched the project …”, “opened the work item …”."""

MEMORY_FIRST_POLICY = f"""{AUTONOMY_POLICY}

Memory-first:
1) Prefer project memory / context already provided.
2) If a memory line is truncated or you need the full node, call memory_get with that id=… value immediately — do not describe what you would open.
3) Work-item comments and long description parts live as linked child memory nodes — memory_get or graph neighbors after the main card.
4) If memory has Azure Boards work item IDs or thin summaries, call boards_get_item / boards_list_comments for those IDs to refresh live details.
5) If memory has nothing useful for the question, call memory_search with a sharper query, then boards_search (or boards_query_wiql for structured filters), then boards_get_item as needed. Boards lookups cover every work item in the project by default — never narrow them to the current user unless the user explicitly asked for their own items ("my tasks", "мои задачи", "assigned to me"); only then call boards_my_work.
6) Prefer fs_* tools when the question needs files under the project folder. For git history — a commit hash, "which change broke this", "what did that fix touch" — use repo_* tools: repo_pull_requests_for_commit or repo_search_commits to locate the change, repo_get_pull_request for its changed files and linked work items, repo_read_file_at to see a file as of that commit, then compare with fs_read on the working copy.
7) For meetings / “what did X say” / call notes: if memory Meeting hits are missing or thin, call granola_list_meetings, then granola_get_meetings (and granola_get_transcript when notes are incomplete). Do not answer about meeting tools when the user asked about something else (a work item, a file, or a cited memory node).
8) Do not call tools when memory already answers the question fully. When memory is truncated, thin, off-topic, or the question is about source code behaviour, fetch/search now instead of guessing.
9) For code questions (bugs, root cause, how X works) follow the call chain end-to-end: locate files, read them, then read their dependencies. Do not stop at search hits or file names.
10) Only cite a work item, file, or memory node when the context or a tool result clearly supports it. If the pack is chat noise or unrelated, call memory_search / boards_search / fs_search for the user’s topic now. Short follow-ups (“подтяни данные”, “open it”, “fetch”) mean continue the prior topic with memory_get / search — never switch topics.
11) If context includes a “Memory advice” block (contradicts / limits / supports), comply with its CONTRADICTS/LIMITS findings, but never quote the block, its markers, or node ids in the answer — the UI already renders it; do not invent extra conflicts beyond that block and tool results.
12) Emit tool_calls JSON only when a tool is needed; otherwise answer normally."""

MEMORY_ONLY_POLICY = f"""{AUTONOMY_POLICY}

Memory-first:
1) Prefer project memory / context already provided.
2) If a memory line is truncated or you need the full node, call memory_get with that id=… value immediately.
3) Work-item comments and long description parts live as linked child memory nodes — memory_get after the main card.
4) If the pack is thin or off-topic for the question, call memory_search with a sharper query before answering.
5) Do not call tools when memory already answers the question fully.
6) If context includes a “Memory advice” block, comply with its conflicts and limits, but do not mention the block, its markers, or node ids in the answer — the UI already renders it.
7) Emit tool_calls JSON only when a tool is needed; otherwise answer normally.
8) File access is off in this mode: if the answer needs source files, say so in one line instead of asking for approval."""

TOOL_PROTOCOL = """You may request tools by emitting a single JSON object (optionally in a ```json fence):
{"tool_calls":[{"name":"memory_search","arguments":{"query":"auth token expiry"}},{"name":"memory_get","arguments":{"id":"node_abc"}}]}
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


# @azure-devops/mcp folded the split wit_* tools into dispatchers driven by an "action"
# argument. Older releases still expose the split names, so every operation below keeps the
# legacy call as a fallback; mcp_tool names the current tool.

# Re-export the split surface so existing ``from .tool_gateway import X`` call sites
# (azure_sync, ai_runtime, tests) keep working after the split.
from .tool_ado import (  # noqa: F401
    BOARDS_EXPAND_LEVELS,
    ado_call_plan,
    call_ado_tool,
    is_empty_mcp_payload,
    mcp_tool_error_text,
)
from .tool_format import (
    compact_tool_trace_label,
    format_tool_results_for_prompt,
    parse_tool_calls,
    strip_tool_call_json,
    summarize_tool_payload,
    tool_action_label,
)

__all__ = [
    "AUTONOMY_POLICY",
    "BOARDS_EXPAND_LEVELS",
    "BOARDS_TOOLS",
    "FILE_TOOL_RESULT_CHAR_CAP",
    "FS_TOOLS",
    "GRANOLA_TOOLS",
    "MAX_TOOL_ROUNDS",
    "MEMORY_FIRST_POLICY",
    "MEMORY_ONLY_POLICY",
    "MEMORY_TOOLS",
    "REPO_TOOLS",
    "TOOL_PROTOCOL",
    "TOOL_RESULT_CHAR_CAP",
    "ToolGateway",
    "ToolSpec",
    "ado_call_plan",
    "call_ado_tool",
    "compact_tool_trace_label",
    "format_tool_results_for_prompt",
    "is_empty_mcp_payload",
    "mcp_tool_error_text",
    "parse_tool_calls",
    "strip_tool_call_json",
    "summarize_tool_payload",
    "tool_action_label",
]


BOARDS_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="boards_my_work",
        description=(
            "List Azure Boards work items assigned to the current user. Use only when the user explicitly "
            "asks for their own items; for any other lookup use boards_search."
        ),
        server_id="azure-devops",
        mcp_tool="wit_work_item",
        parameters_schema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Azure DevOps project name; omit it to use the configured project"},
                "top": {"type": "integer"},
                "type": {"type": "string", "description": "assignedtome | followed | mentioned | etc."},
            },
        },
    ),
    ToolSpec(
        name="boards_search",
        description=(
            "Search Azure Boards work items by text across the whole project, regardless of assignee. "
            "Default choice for any work-item lookup."
        ),
        server_id="azure-devops",
        mcp_tool="search_workitem",
        parameters_schema={
            "type": "object",
            "properties": {
                "searchText": {"type": "string"},
                "query": {"type": "string"},
                "project": {"type": "string", "description": "Azure DevOps project name; omit it to use the configured project"},
                "top": {"type": "integer"},
            },
            "required": [],
        },
    ),
    ToolSpec(
        name="boards_get_item",
        description="Get one Azure Boards work item by id.",
        server_id="azure-devops",
        mcp_tool="wit_work_item",
        parameters_schema={
            "type": "object",
            "properties": {
                "id": {"type": ["integer", "string"]},
                "project": {"type": "string", "description": "Azure DevOps project name; omit it to use the configured project"},
            },
            "required": ["id"],
        },
    ),
    ToolSpec(
        name="boards_list_comments",
        description="List comments for an Azure Boards work item.",
        server_id="azure-devops",
        mcp_tool="wit_work_item",
        parameters_schema={
            "type": "object",
            "properties": {
                "workItemId": {"type": ["integer", "string"]},
                "id": {"type": ["integer", "string"]},
                "project": {"type": "string", "description": "Azure DevOps project name; omit it to use the configured project"},
                "top": {"type": "integer"},
            },
            "required": [],
        },
    ),
    ToolSpec(
        name="boards_query_wiql",
        description=(
            "Run a WIQL query against Azure Boards for structured filters (state, iteration, type, dates). "
            "Query the whole project; add an assignee clause only when the user asked for it."
        ),
        server_id="azure-devops",
        mcp_tool="wit_query",
        parameters_schema={
            "type": "object",
            "properties": {
                "wiql": {"type": "string"},
                "project": {"type": "string", "description": "Azure DevOps project name; omit it to use the configured project"},
            },
            "required": ["wiql"],
        },
    ),
]

_REPO_ID_PARAM = {
    "type": "string",
    "description": "Repository name or id; omit to search across the project",
}
_REPO_PROJECT_PARAM = {
    "type": "string",
    "description": "Azure DevOps project name; omit it to use the configured project",
}

REPO_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="repo_list_repositories",
        description="List Azure Repos git repositories in the project.",
        server_id="azure-devops",
        mcp_tool="repo_repository",
        parameters_schema={
            "type": "object",
            "properties": {
                "project": _REPO_PROJECT_PARAM,
                "repoNameFilter": {"type": "string"},
                "top": {"type": "integer"},
            },
        },
    ),
    ToolSpec(
        name="repo_search_commits",
        description=(
            "Search Azure Repos commits by message text, author, branch, or date range. "
            "Use it to locate the commit behind a change before reading its pull request."
        ),
        server_id="azure-devops",
        mcp_tool="repo_search_commits",
        parameters_schema={
            "type": "object",
            "properties": {
                "searchText": {"type": "string", "description": "Text from the commit message, e.g. a work item id"},
                "repository": _REPO_ID_PARAM,
                "branch": {"type": "string"},
                "author": {"type": "string"},
                "commitStartDate": {"type": "string", "description": "ISO date lower bound"},
                "commitEndDate": {"type": "string", "description": "ISO date upper bound"},
                "project": _REPO_PROJECT_PARAM,
                "top": {"type": "integer"},
            },
            "required": ["searchText"],
        },
    ),
    ToolSpec(
        name="repo_pull_requests_for_commit",
        description=(
            "Find the pull requests that contain a commit hash. Start here when the user gives "
            "a commit id: the pull request carries the changed files, reviewers, and linked work items."
        ),
        server_id="azure-devops",
        mcp_tool="repo_pull_request",
        parameters_schema={
            "type": "object",
            "properties": {
                "commits": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Commit hashes (full or short)",
                },
                "repositoryId": {"type": "string", "description": "Repository name or id"},
                "project": _REPO_PROJECT_PARAM,
                "top": {"type": "integer"},
            },
            "required": ["commits", "repositoryId"],
        },
    ),
    ToolSpec(
        name="repo_get_pull_request",
        description=(
            "Open one pull request with its changed files and linked work items. "
            "The changed-file list is the closest thing to a diff summary for a merged change."
        ),
        server_id="azure-devops",
        mcp_tool="repo_pull_request",
        parameters_schema={
            "type": "object",
            "properties": {
                "pullRequestId": {"type": "integer"},
                "repositoryId": _REPO_ID_PARAM,
                "project": _REPO_PROJECT_PARAM,
                "includeChangedFiles": {"type": "boolean", "description": "Defaults to true"},
                "includeWorkItemRefs": {"type": "boolean", "description": "Defaults to true"},
            },
            "required": ["pullRequestId"],
        },
    ),
    ToolSpec(
        name="repo_list_pull_request_comments",
        description="Read review comments on a pull request (why a change was made or reverted).",
        server_id="azure-devops",
        mcp_tool="repo_pull_request_thread",
        parameters_schema={
            "type": "object",
            "properties": {
                "pullRequestId": {"type": "integer"},
                "repositoryId": {"type": "string", "description": "Repository name or id"},
                "project": _REPO_PROJECT_PARAM,
                "threadId": {"type": "integer", "description": "Narrow to one thread; omit to list all threads"},
                "top": {"type": "integer"},
            },
            "required": ["pullRequestId", "repositoryId"],
        },
    ),
    ToolSpec(
        name="repo_read_file_at",
        description=(
            "Read a file from Azure Repos at a specific commit, branch, or tag. Use it to compare "
            "the code as of a commit with the current workspace copy read by fs_read."
        ),
        server_id="azure-devops",
        mcp_tool="repo_file",
        parameters_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository-relative file path"},
                "repositoryId": {"type": "string", "description": "Repository name or id"},
                "version": {"type": "string", "description": "Commit hash, branch, or tag name"},
                "versionType": {
                    "type": "string",
                    "description": "Commit | Branch | Tag (inferred from version when omitted)",
                },
                "project": _REPO_PROJECT_PARAM,
            },
            "required": ["path", "repositoryId"],
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
        name="memory_search",
        description=(
            "Search ArchitectOS project memory with a sharper query when the provided context pack "
            "is thin, truncated, or off-topic. Prefer concrete terms from the user's topic "
            "(ids, titles, class/file names, error text) over conversational fillers."
        ),
        server_id="local-memory",
        mcp_tool="memory_search",
        parameters_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search text using specific terms from the user's topic",
                },
                "limit": {"type": "integer", "description": "Max hits (default 8)"},
            },
            "required": ["query"],
        },
        kind="native",
    ),
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
        description=(
            "Read a text file under the current project folder. Long files come back in windows: "
            "when the result has truncated=true, call this again with start_line=next_start_line "
            "until you reach the end instead of reporting a partial read."
        ),
        server_id="filesystem",
        mcp_tool="read_file",
        parameters_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Project-relative path, an absolute path inside the project folder, or a fully-qualified class name (com.acme.FooBuilder)",
                },
                "start_line": {"type": "integer", "description": "First line to return; defaults to 1"},
                "end_line": {"type": "integer", "description": "Last line to return"},
            },
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
        description=(
            "Search project files by name or content. Name mode accepts a class name or a "
            "fully-qualified name and matches the file that declares it."
        ),
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

_ALL_SPECS = {spec.name: spec for spec in [*MEMORY_TOOLS, *BOARDS_TOOLS, *REPO_TOOLS, *GRANOLA_TOOLS, *FS_TOOLS]}


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
            # Boards and Repos ride the same Azure DevOps MCP server.
            specs.extend(BOARDS_TOOLS)
            specs.extend(REPO_TOOLS)
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

    def tool_schemas(self, *, include_writes: bool = False, memory_only: bool = False) -> list[dict[str, Any]]:
        """Tool definitions for providers that support native tool calling."""
        return [
            {"name": spec.name, "description": spec.description, "parameters": spec.parameters_schema}
            for spec in self.available_specs(include_writes=include_writes, memory_only=memory_only)
        ]

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
        catalog = {item.name for item in self.available_specs(include_writes=True, memory_only=memory_only)}
        if name not in catalog:
            return {"ok": False, "name": name, "error": f"Tool not available: {name}", "summary": ""}
        args = dict(arguments or {})
        if spec.write and not include_writes:
            target = str(args.get("path") or args.get("file") or "")
            return PermissionRequired(
                "write",
                "write",
                target,
                "Writing files needs your approval.",
            ).as_payload(name)
        if project_id and "project_id" not in args:
            args["project_id"] = project_id

        try:
            if spec.name.startswith("memory_"):
                payload = self._execute_native(spec, args)
            elif spec.name.startswith("boards_"):
                payload = self._execute_boards(spec, args)
            elif spec.name.startswith("repo_"):
                payload = self._execute_repo(spec, args)
            elif spec.name.startswith("granola_"):
                payload = self._execute_granola(spec, args)
            elif spec.name.startswith("fs_"):
                payload = self._execute_fs(spec, args)
            else:
                return {"ok": False, "name": name, "error": "Unsupported tool kind.", "summary": ""}
        except PermissionRequired as exc:
            return exc.as_payload(name)
        except MCPError as exc:
            return {"ok": False, "name": name, "error": str(exc), "summary": ""}
        except Exception as exc:  # noqa: BLE001 — surface to model as tool error
            return {"ok": False, "name": name, "error": str(exc), "summary": ""}

        # An MCP server reports tool-level failures inside a successful response; without this
        # the model would read "Tool … not found" as data and conclude the source is offline.
        tool_error = mcp_tool_error_text(payload)
        if tool_error:
            return {"ok": False, "name": name, "error": tool_error, "summary": ""}

        cap = FILE_TOOL_RESULT_CHAR_CAP if name == "fs_read" else TOOL_RESULT_CHAR_CAP
        summary = summarize_tool_payload(payload, cap=cap)
        return {"ok": True, "name": name, "error": "", "summary": summary, "result": payload}

    def _execute_native(self, spec: ToolSpec, args: dict[str, Any]) -> Any:
        handler = self._native_handlers.get(spec.name)
        if not handler:
            raise ValueError(f"Native handler not registered for {spec.name}")
        return handler(args)

    def _boards_project_for(self, args: dict[str, Any]) -> str:
        """Pick the Azure DevOps project, ignoring local ids or the org name a model may echo back."""
        default = str(self._boards_project() or "").strip()
        requested = str(args.get("project") or "").strip()
        if not requested:
            return default
        rejected = {str(args.get("project_id") or "").strip().lower(), "architectos", "default", "current", "auto"}
        config = self.mcp_manager.get_server("azure-devops")
        if config:
            # The org is the first positional argument of the MCP command, not a project.
            rejected.update(str(part).strip().lower() for part in (config.command or [])[1:])
        return default if requested.lower() in rejected else requested

    def _execute_boards(self, spec: ToolSpec, args: dict[str, Any]) -> Any:
        ado_project = self._boards_project_for(args)
        op_args: dict[str, Any] = {"project": ado_project}
        if spec.name == "boards_my_work":
            op = "my_work"
            op_args.update({
                "type": args.get("type"),
                "top": args.get("top"),
                "includeCompleted": args.get("includeCompleted", True),
            })
        elif spec.name == "boards_search":
            op = "search"
            search_text = str(args.get("searchText") or args.get("query") or args.get("search_text") or "").strip()
            if not search_text:
                raise ValueError("boards_search requires searchText or query")
            op_args.update({
                "searchText": search_text,
                "top": args.get("top"),
                "skip": args.get("skip"),
                "workItemType": args.get("workItemType") or args.get("work_item_type"),
            })
        elif spec.name == "boards_get_item":
            op = "get_item"
            work_id = args.get("id") or args.get("workItemId") or args.get("work_item_id")
            if work_id is None or str(work_id).strip() == "":
                raise ValueError("boards_get_item requires id")
            op_args.update({"id": work_id, "expand": args.get("expand") or "all"})
        elif spec.name == "boards_list_comments":
            op = "list_comments"
            work_id = args.get("workItemId") or args.get("id") or args.get("work_item_id")
            if work_id is None or str(work_id).strip() == "":
                raise ValueError("boards_list_comments requires workItemId or id")
            op_args.update({"workItemId": work_id, "top": args.get("top")})
        elif spec.name == "boards_query_wiql":
            op = "wiql"
            wiql = str(args.get("wiql") or "").strip()
            if not wiql:
                raise ValueError("boards_query_wiql requires wiql")
            op_args.update({"wiql": wiql, "top": args.get("top")})
        else:
            raise ValueError(f"Unsupported boards tool: {spec.name}")
        result = call_ado_tool(self.mcp_manager, op, op_args)
        if is_empty_mcp_payload(result):
            raise MCPError(
                f"Azure Boards returned no data for {op} in project '{ado_project}'. "
                "Check the work item id, or omit project to use the configured one."
            )
        return result

    def _execute_repo(self, spec: ToolSpec, args: dict[str, Any]) -> Any:
        ado_project = self._boards_project_for(args)
        op_args: dict[str, Any] = {"project": ado_project}
        repository = args.get("repositoryId") or args.get("repository") or args.get("repo")
        if spec.name == "repo_list_repositories":
            op = "list_repos"
            op_args.update({"repoNameFilter": args.get("repoNameFilter"), "top": args.get("top")})
        elif spec.name == "repo_search_commits":
            op = "search_commits"
            search_text = str(args.get("searchText") or args.get("query") or "").strip()
            if not search_text:
                raise ValueError("repo_search_commits requires searchText")
            op_args.update({
                "searchText": search_text,
                "repository": repository,
                "branch": args.get("branch"),
                "author": args.get("author"),
                "commitStartDate": args.get("commitStartDate"),
                "commitEndDate": args.get("commitEndDate"),
                "top": args.get("top"),
            })
        elif spec.name == "repo_pull_requests_for_commit":
            op = "pull_requests_for_commit"
            op_args.update({
                "commits": args.get("commits") or args.get("commitId") or args.get("commit"),
                "repositoryId": repository,
                "top": args.get("top"),
            })
        elif spec.name == "repo_get_pull_request":
            op = "get_pull_request"
            op_args.update({
                "pullRequestId": args.get("pullRequestId") or args.get("id"),
                "repositoryId": repository,
                "includeChangedFiles": args.get("includeChangedFiles", True),
                "includeWorkItemRefs": args.get("includeWorkItemRefs", True),
            })
        elif spec.name == "repo_list_pull_request_comments":
            op = "pull_request_comments"
            op_args.update({
                "pullRequestId": args.get("pullRequestId") or args.get("id"),
                "repositoryId": repository,
                "threadId": args.get("threadId"),
                "top": args.get("top"),
            })
        elif spec.name == "repo_read_file_at":
            op = "repo_file"
            op_args.update({
                "path": args.get("path"),
                "repositoryId": repository,
                "version": args.get("version") or args.get("commit") or args.get("branch"),
                "versionType": args.get("versionType"),
            })
        else:
            raise ValueError(f"Unsupported repo tool: {spec.name}")
        result = call_ado_tool(self.mcp_manager, op, op_args)
        if is_empty_mcp_payload(result):
            raise MCPError(
                f"Azure Repos returned no data for {op} in project '{ado_project}'. "
                "Check the repository, commit id, or pull request id."
            )
        return result

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
