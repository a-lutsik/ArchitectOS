from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from .mcp import MCPError, MCPManager
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
- Only ask the user something when it is a product decision you cannot make, or when a write/CLI action needs approval.
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
11) Emit tool_calls JSON only when a tool is needed; otherwise answer normally."""

MEMORY_ONLY_POLICY = f"""{AUTONOMY_POLICY}

Memory-first:
1) Prefer project memory / context already provided.
2) If a memory line is truncated or you need the full node, call memory_get with that id=… value immediately.
3) Work-item comments and long description parts live as linked child memory nodes — memory_get after the main card.
4) If the pack is thin or off-topic for the question, call memory_search with a sharper query before answering.
5) Do not call tools when memory already answers the question fully.
6) Emit tool_calls JSON only when a tool is needed; otherwise answer normally.
7) File access is off in this mode: if the answer needs source files, say so in one line instead of asking for approval."""

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
BOARDS_EXPAND_LEVELS = {
    "none": "None",
    "fields": "Fields",
    "relations": "Relations",
    "links": "Links",
    "all": "All",
}

_MISSING_TOOL_RE = re.compile(
    r"tool\s+\S+\s+(?:is\s+)?(?:not found|unknown|not supported|does not exist)"
    r"|unknown tool|no such tool|method not found",
    re.I,
)


_COMMIT_HASH_RE = re.compile(r"^[0-9a-f]{7,40}$", re.I)


def _as_str_list(value: Any) -> list[str]:
    """Accept a scalar or a list and return a clean list of strings."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    return [str(item).strip() for item in items if str(item).strip()]


_PR_STATUS_NAMES = {1: "Active", 2: "Abandoned", 3: "Completed"}
_CHANGE_TYPE_NAMES = {1: "add", 2: "edit", 8: "rename", 16: "delete", 18: "delete"}


def _pull_request_summary(item: dict[str, Any]) -> str:
    """One pull request as a few lines: identity, branches, changed files, linked work items."""
    status: Any = item.get("status")
    status_text = _PR_STATUS_NAMES.get(status, str(status)) if not isinstance(status, str) else status
    raw_repo: Any = item.get("repository")
    repo = raw_repo if isinstance(raw_repo, dict) else {}
    raw_author: Any = item.get("createdBy")
    author = raw_author if isinstance(raw_author, dict) else {}
    source = str(item.get("sourceRefName") or "").replace("refs/heads/", "")
    target = str(item.get("targetRefName") or "").replace("refs/heads/", "")
    raw_merge: Any = item.get("lastMergeCommit")
    merge = raw_merge if isinstance(raw_merge, dict) else {}
    lines = [
        f"- PR !{item.get('pullRequestId')} [{status_text}] {item.get('title') or ''}".rstrip(),
        f"  repo={repo.get('name') or ''} {source} -> {target} by {author.get('displayName') or ''}".rstrip(),
    ]
    if merge.get("commitId"):
        lines.append(f"  merge commit {str(merge['commitId'])[:12]} at {item.get('closedDate') or ''}".rstrip())
    raw_refs: Any = item.get("workItemRefs")
    refs = raw_refs if isinstance(raw_refs, list) else []
    ids = [str(ref.get("id")) for ref in refs if isinstance(ref, dict) and ref.get("id")]
    if ids:
        lines.append(f"  work items: {', '.join(ids[:20])}")
    raw_summary: Any = item.get("changedFilesSummary")
    summary = raw_summary if isinstance(raw_summary, dict) else {}
    raw_entries: Any = summary.get("changeEntries")
    entries = raw_entries if isinstance(raw_entries, list) else []
    if entries:
        lines.append(f"  changed files ({summary.get('fileCount') or len(entries)}):")
        for entry in entries[:40]:
            if not isinstance(entry, dict):
                continue
            raw_file_item: Any = entry.get("item")
            file_item = raw_file_item if isinstance(raw_file_item, dict) else {}
            change_type: Any = entry.get("changeType")
            change = _CHANGE_TYPE_NAMES.get(change_type, str(change_type or ""))
            lines.append(f"    {change}: {file_item.get('path') or ''}")
        if len(entries) > 40:
            lines.append(f"    (+{len(entries) - 40} more)")
    return "\n".join(lines)


def _normalize_version_type(requested: Any, version: str) -> str:
    """Azure DevOps expects Branch/Tag/Commit; infer it when the model omits it."""
    raw = str(requested or "").strip().lower()
    known = {"branch": "Branch", "tag": "Tag", "commit": "Commit"}
    if raw in known:
        return known[raw]
    return "Commit" if _COMMIT_HASH_RE.match(str(version or "").strip()) else "Branch"


def is_empty_mcp_payload(result: Any) -> bool:
    """True when an MCP tool answered with nothing usable (Azure DevOps does this for a bad project)."""
    payload = result.get("result") if isinstance(result, dict) and "result" in result else result
    if payload is None:
        return True
    if not isinstance(payload, dict):
        return not str(payload).strip()
    content = payload.get("content")
    if isinstance(content, list) and content:
        for item in content:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if text and text.lower() not in {"null", "none", "[]", "{}"}:
                return False
        return True
    return not any(key for key in payload if key not in {"content", "isError"})


def mcp_tool_error_text(result: Any) -> str:
    """Return the error text when an MCP tool result carries isError, else an empty string."""
    payload = result.get("result") if isinstance(result, dict) and "result" in result else result
    if not isinstance(payload, dict) or not payload.get("isError"):
        return ""
    content = payload.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and str(item.get("type") or "") == "text":
                text = str(item.get("text") or "").strip()
                if text:
                    return text
    return str(payload.get("message") or payload.get("error") or "").strip() or "MCP tool call failed."


def ado_call_plan(op: str, args: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Map an Azure DevOps operation to MCP (tool, arguments) candidates, current release first."""
    data = {key: value for key, value in dict(args or {}).items() if value is not None}
    project = str(data.get("project") or "").strip()

    def scoped(candidates: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
        if not project:
            return candidates
        return [(tool, {**arguments, "project": project}) for tool, arguments in candidates]

    if op == "get_item":
        item_id = int(data.get("id") or data.get("workItemId") or 0)
        if item_id <= 0:
            raise ValueError("boards get_item requires a work item id")
        expand = str(data.get("expand") or "relations").lower()
        return scoped([
            ("wit_work_item", {"action": "get", "id": item_id, "expand": BOARDS_EXPAND_LEVELS.get(expand, "Relations")}),
            ("wit_get_work_item", {"id": item_id, "expand": expand}),
        ])
    if op == "list_comments":
        item_id = int(data.get("workItemId") or data.get("id") or 0)
        if item_id <= 0:
            raise ValueError("boards list_comments requires a work item id")
        top = max(1, int(data.get("top") or 20))
        return scoped([
            ("wit_work_item", {"action": "list_comments", "workItemId": item_id, "top": top}),
            ("wit_list_work_item_comments", {"workItemId": item_id, "top": top}),
        ])
    if op == "my_work":
        payload: dict[str, Any] = {
            "type": str(data.get("type") or "assignedtome"),
            "top": max(1, int(data.get("top") or 20)),
            "includeCompleted": bool(data.get("includeCompleted", True)),
        }
        return scoped([
            ("wit_work_item", {"action": "my", **payload}),
            ("wit_my_work_items", payload),
        ])
    if op == "wiql":
        wiql = str(data.get("wiql") or "").strip()
        if not wiql:
            raise ValueError("boards wiql requires a query")
        modern: dict[str, Any] = {"action": "wiql", "wiql": wiql}
        if data.get("top"):
            modern["top"] = int(data["top"])
        return scoped([
            ("wit_query", modern),
            ("wit_query_by_wiql", {"wiql": wiql}),
        ])
    if op == "search":
        search_text = str(data.get("searchText") or data.get("query") or "").strip()
        if not search_text:
            raise ValueError("boards search requires searchText")
        payload = {
            "searchText": search_text,
            "top": min(max(1, int(data.get("top") or 25)), 25),
            "skip": max(0, int(data.get("skip") or 0)),
        }
        if project:
            payload["project"] = [project]
        work_item_type = data.get("workItemType")
        if work_item_type:
            payload["workItemType"] = work_item_type if isinstance(work_item_type, list) else [str(work_item_type)]
        return [("search_workitem", payload)]
    if op == "list_repos":
        legacy: dict[str, Any] = {}
        modern = {"action": "list"}
        for key in ("top", "skip", "repoNameFilter"):
            if data.get(key) is not None:
                modern[key] = data[key]
        return scoped([("repo_repository", modern), ("repo_list_repos_by_project", legacy)])
    if op == "list_pull_requests":
        shared: dict[str, Any] = {
            key: data[key]
            for key in ("top", "skip", "status", "repositoryId", "repository", "targetRefName", "sourceRefName")
            if data.get(key) is not None
        }
        return scoped([
            ("repo_pull_request", {"action": "list", **shared}),
            ("repo_list_pull_requests_by_repo_or_project", dict(shared)),
        ])
    if op == "search_commits":
        search_text = str(data.get("searchText") or data.get("query") or "").strip()
        if not search_text:
            raise ValueError("commit search requires searchText")
        payload = {"searchText": search_text, "top": min(max(1, int(data.get("top") or 20)), 50)}
        # Commit search takes list filters even for a single repository/branch/author.
        for key in ("repository", "branch", "author"):
            values = _as_str_list(data.get(key))
            if values:
                payload[key] = values
        for key in ("commitStartDate", "commitEndDate", "skip"):
            if data.get(key) is not None:
                payload[key] = data[key]
        return scoped([("repo_search_commits", payload)])
    if op == "pull_requests_for_commit":
        commits = data.get("commits") or data.get("commitId") or data.get("commit")
        if isinstance(commits, str):
            commits = [commits.strip()]
        commits = [str(item).strip() for item in (commits or []) if str(item).strip()]
        if not commits:
            raise ValueError("pull_requests_for_commit requires a commit id")
        repository = str(data.get("repository") or data.get("repositoryId") or "").strip()
        if not repository:
            raise ValueError("pull_requests_for_commit requires a repository")
        # Azure DevOps matches only merge commits unless the query asks for member commits.
        shared = {
            "repository": repository,
            "commits": commits,
            "queryType": str(data.get("queryType") or "Commit"),
        }
        for key in ("top", "skip"):
            if data.get(key) is not None:
                shared[key] = data[key]
        return scoped([
            ("repo_pull_request", {"action": "list_by_commits", **shared}),
            ("repo_list_pull_requests_by_commits", dict(shared)),
        ])
    if op == "get_pull_request":
        pull_request_id = data.get("pullRequestId") or data.get("id")
        if pull_request_id is None or str(pull_request_id).strip() == "":
            raise ValueError("get_pull_request requires pullRequestId")
        shared = {
            "pullRequestId": int(pull_request_id),
            "includeChangedFiles": bool(data.get("includeChangedFiles", True)),
            "includeWorkItemRefs": bool(data.get("includeWorkItemRefs", True)),
        }
        for key in ("repositoryId", "repository", "includeLabels"):
            if data.get(key) is not None:
                shared[key] = data[key]
        return scoped([
            ("repo_pull_request", {"action": "get", **shared}),
            ("repo_get_pull_request_by_id", dict(shared)),
        ])
    if op == "pull_request_comments":
        pull_request_id = data.get("pullRequestId") or data.get("id")
        if pull_request_id is None or str(pull_request_id).strip() == "":
            raise ValueError("pull_request_comments requires pullRequestId")
        repository_id = str(data.get("repositoryId") or data.get("repository") or "").strip()
        if not repository_id:
            raise ValueError("pull_request_comments requires repositoryId")
        shared = {
            "repositoryId": repository_id,
            "pullRequestId": int(pull_request_id),
            "top": max(1, int(data.get("top") or 30)),
        }
        thread_id = data.get("threadId")
        if thread_id is None:
            # Without a thread id the API can only enumerate threads, which already carry their comments.
            return scoped([
                ("repo_pull_request_thread", {"action": "list", **shared}),
                ("repo_list_pull_request_threads", dict(shared)),
            ])
        shared["threadId"] = int(thread_id)
        return scoped([
            ("repo_pull_request_thread", {"action": "list_comments", **shared}),
            ("repo_list_pull_request_thread_comments", dict(shared)),
        ])
    if op == "repo_file":
        path = str(data.get("path") or "").strip()
        if not path:
            raise ValueError("repo_file requires path")
        repository_id = str(data.get("repositoryId") or data.get("repository") or "").strip()
        if not repository_id:
            raise ValueError("repo_file requires repositoryId")
        shared = {"path": path if path.startswith("/") else f"/{path}", "repositoryId": repository_id}
        version = str(data.get("version") or "").strip()
        if version:
            shared["version"] = version
            shared["versionType"] = _normalize_version_type(data.get("versionType"), version)
        return scoped([
            ("repo_file", {"action": "get_content", **shared}),
            ("repo_get_file_content", dict(shared)),
        ])
    if op == "list_wikis":
        return scoped([("wiki", {"action": "list_wikis"}), ("wiki_list_wikis", {})])
    if op == "list_wiki_pages":
        shared = {
            key: data[key]
            for key in ("wikiIdentifier", "top", "path", "recursionLevel", "continuationToken")
            if data.get(key) is not None
        }
        return scoped([
            ("wiki", {"action": "list_pages", **shared}),
            ("wiki_list_pages", dict(shared)),
        ])
    if op == "get_wiki_page_content":
        shared = {
            key: data[key]
            for key in ("wikiIdentifier", "path", "url")
            if data.get(key) is not None
        }
        if not shared:
            raise ValueError("wiki page content requires url or wikiIdentifier/path")
        # A direct page url already carries the project, so do not force one in.
        candidates = [("wiki", {"action": "get_page_content", **shared}), ("wiki_get_page_content", dict(shared))]
        return candidates if shared.get("url") else scoped(candidates)
    raise ValueError(f"Unsupported Azure DevOps operation: {op}")


def call_ado_tool(
    mcp_manager: MCPManager,
    op: str,
    args: dict[str, Any],
    *,
    server_id: str = "azure-devops",
    timeout: float | None = None,
) -> dict[str, Any]:
    """Run an Azure DevOps operation, retrying with legacy tool names on older MCP releases."""
    last_error = ""
    for tool, arguments in ado_call_plan(op, args):
        try:
            result = mcp_manager.call_tool(server_id, tool, arguments, timeout=timeout)
        except MCPError as exc:
            last_error = str(exc)
            if not _MISSING_TOOL_RE.search(last_error):
                raise
            continue
        error = mcp_tool_error_text(result)
        if not error:
            return result
        last_error = error
        # Only a missing tool is worth retrying: anything else is a real Azure DevOps failure.
        if not _MISSING_TOOL_RE.search(error):
            raise MCPError(error)
    raise MCPError(last_error or f"Azure DevOps {op} failed.")


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
            elif spec.name.startswith("repo_"):
                payload = self._execute_repo(spec, args)
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
    if compact is None and isinstance(payload, dict):
        # Cutting the serialized tail would drop the very fields that tell the model
        # how to continue (next_start_line, total_lines, …), so trim the body instead.
        trimmed = _trim_payload_body(payload, cap)
        if trimmed is not None:
            return trimmed
    return text[: cap - 20] + "\n…[truncated]"


def _trim_payload_body(payload: dict[str, Any], cap: int) -> str | None:
    """Shrink the largest text field of a structured payload, keeping its metadata."""
    body_key = ""
    if isinstance(payload.get("text"), str):
        body_key = "text"
    else:
        longest = 0
        for key, value in payload.items():
            if isinstance(value, str) and len(value) > longest:
                body_key, longest = key, len(value)
    if not body_key:
        return None
    full_body = str(payload.get(body_key) or "")
    allowance = cap
    # Escaping makes the rendered size unpredictable, so shrink until it actually fits.
    for _ in range(6):
        body = full_body[:allowance]
        if "\n" in body:
            # Cut at a line boundary so a numbered code window stays parseable.
            body = body[: body.rindex("\n")]
        if not body:
            return None
        trimmed = dict(payload)
        trimmed[body_key] = body
        last_line = _last_numbered_line(body)
        if last_line and isinstance(payload.get("total_lines"), int):
            total = int(payload["total_lines"])
            trimmed["end_line"] = last_line
            trimmed["truncated"] = last_line < total
            trimmed["next_start_line"] = last_line + 1 if last_line < total else 0
        trimmed["note"] = (
            "Output trimmed to fit the tool budget. Continue with "
            f"start_line={trimmed.get('next_start_line') or 'n/a'}."
        )
        try:
            rendered = json.dumps(trimmed, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return None
        if len(rendered) <= cap:
            return rendered
        allowance -= max(200, len(rendered) - cap + 200)
        if allowance < 400:
            return None
    return None


def _last_numbered_line(body: str) -> int:
    """Line number of the last `123| …` row in a read window, else 0."""
    for row in reversed(body.splitlines()):
        head = row.split("|", 1)[0].strip()
        if head.isdigit():
            return int(head)
    return 0


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
    if inner is None and not isinstance(payload.get("content"), list):
        # Native payloads (fs_read, memory_get, …) are already structured; compacting
        # them here would keep only the text body and drop path/line metadata.
        return None
    text_blob = _mcp_content_text(inner if inner is not None else payload)
    if not text_blob:
        return None
    cleaned = _UNTRUSTED_WIQL_RE.sub("", text_blob).strip()
    parsed = _try_parse_json_object(cleaned) or _try_parse_json_object(text_blob)
    if not isinstance(parsed, dict):
        # Keep raw text but without wrapper noise when possible.
        return cleaned[:TOOL_RESULT_CHAR_CAP] if cleaned else None

    if tool in {"wit_query", "wit_query_by_wiql", "wit_get_query_results_by_id"} or "workItems" in parsed:
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

    if tool in {"repo_pull_request", "repo_get_pull_request_by_id", "repo_list_pull_requests_by_commits"}:
        items = parsed if isinstance(parsed, list) else parsed.get("value") if isinstance(parsed.get("value"), list) else None
        if items is None and parsed.get("pullRequestId"):
            items = [parsed]
        if items is None and isinstance(parsed.get("results"), list):
            # A commit query answers as [{<commit hash>: [pull requests]}].
            items = [
                pull_request
                for bucket in parsed["results"]
                if isinstance(bucket, dict)
                for group in bucket.values()
                for pull_request in (group if isinstance(group, list) else [])
            ]
        if items is not None:
            if not items:
                return "Pull requests · none matched"
            lines = [f"Pull requests · {len(items)}"]
            for item in items[:10]:
                if isinstance(item, dict):
                    lines.append(_pull_request_summary(item))
            return "\n".join(lines)

    if tool == "repo_search_commits":
        results = parsed.get("results") if isinstance(parsed.get("results"), list) else []
        if not results:
            hint = "" if parsed.get("infoCode") in (None, 0) else " (code search index unavailable for this project)"
            return (
                f"Commit search · 0 matches{hint}. "
                "For a known hash use repo_pull_requests_for_commit; otherwise filter pull requests."
            )
        lines = [f"Commit search · {len(results)} match(es)"]
        for item in results[:20]:
            if not isinstance(item, dict):
                continue
            commit_id = str(item.get("commitId") or "")[:12]
            author = ((item.get("author") or {}) if isinstance(item.get("author"), dict) else {}).get("name") or ""
            repo = ((item.get("repository") or {}) if isinstance(item.get("repository"), dict) else {}).get("name") or ""
            comment = str(item.get("comment") or "").splitlines()[:1]
            lines.append(f"- {commit_id} {repo} {author}: {comment[0] if comment else ''}".strip())
        return "\n".join(lines)

    if tool == "search_workitem" or ("count" in parsed and "results" in parsed):
        raw_results: Any = parsed.get("results")
        workitem_results = raw_results if isinstance(raw_results, list) else []
        count = int(parsed.get("count") or len(workitem_results) or 0)
        lines = [f"search_workitem · count={count}, showing={len(workitem_results)}"]
        for item in workitem_results[:20]:
            if not isinstance(item, dict):
                continue
            raw_item_fields: Any = item.get("fields")
            fields = raw_item_fields if isinstance(raw_item_fields, dict) else item
            wid = fields.get("system.id") or fields.get("System.Id") or fields.get("id") or ""
            title = fields.get("system.title") or fields.get("System.Title") or fields.get("title") or ""
            wtype = fields.get("system.workitemtype") or fields.get("System.WorkItemType") or ""
            lines.append(f"- {wtype} #{wid}: {title}".strip())
        return "\n".join(lines)

    # wit_work_item also serves comments and "my work" lists, so require a field bag here.
    if isinstance(parsed.get("fields"), dict) and (parsed.get("id") or tool in {"wit_work_item", "wit_get_work_item"}):
        raw_parsed_fields: Any = parsed.get("fields")
        fields = raw_parsed_fields if isinstance(raw_parsed_fields, dict) else {}
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


_TOOL_ACTION_VERBS: dict[str, str] = {
    "memory_search": "Searching memory",
    "memory_get": "Reading memory note",
    "boards_search": "Searching Azure Boards",
    "boards_my_work": "Loading my work items",
    "boards_get_item": "Opening work item",
    "boards_list_comments": "Reading work item comments",
    "boards_query_wiql": "Querying Azure Boards",
    "repo_list_repositories": "Listing repositories",
    "repo_search_commits": "Searching commits",
    "repo_pull_requests_for_commit": "Finding pull requests for commit",
    "repo_get_pull_request": "Opening pull request",
    "repo_list_pull_request_comments": "Reading pull request comments",
    "repo_read_file_at": "Reading file at revision",
    "granola_list_meetings": "Listing meetings",
    "granola_get_meetings": "Reading meeting notes",
    "granola_get_transcript": "Reading meeting transcript",
    "fs_read": "Reading file",
    "fs_list": "Listing folder",
    "fs_search": "Searching project files",
    "fs_write": "Writing file",
}

_TOOL_ACTION_DETAIL_KEYS: tuple[str, ...] = ("path", "file", "query", "text", "id", "node_id", "work_item_id", "item_id")


def tool_action_label(name: str, arguments: dict[str, Any] | None = None) -> str:
    """Plain-language label for a tool call, safe to show to non-technical users."""
    tool = str(name or "").strip()
    verb = _TOOL_ACTION_VERBS.get(tool)
    if not verb:
        verb = tool.replace("boards_", "").replace("granola_", "").replace("fs_", "").replace("_", " ").strip() or "Working"
        verb = verb[:1].upper() + verb[1:]
    detail = ""
    args = arguments or {}
    for key in _TOOL_ACTION_DETAIL_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            detail = value.strip()
            break
    if not detail:
        detail = next((value.strip() for value in args.values() if isinstance(value, str) and value.strip()), "")
    detail = detail[:80]
    return f"{verb} {detail}".strip() if detail else verb


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
        elif name == "memory_search":
            bits.append("memory search")
        else:
            bits.append(name.replace("boards_", "").replace("repo_", "repo ").replace("_", " "))
    prefix = "Used tools"
    if any(str(item.get("name") or "").startswith(("boards_", "repo_")) for item in trace):
        prefix = "Used Azure DevOps / tools"
    return f"{prefix}: " + ", ".join(bits)
