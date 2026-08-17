"""Azure DevOps MCP call planning and payload helpers.

Split out of ``tool_gateway`` so Boards/Repos sync and the gateway can share
the same operation map without importing the full tool catalog. Re-exported
from ``tool_gateway`` for backward-compatible imports.
"""

from __future__ import annotations

import re
from typing import Any

from .mcp import MCPError, MCPManager

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
