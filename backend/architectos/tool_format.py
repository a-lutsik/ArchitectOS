"""Tool-result summarization and tool_calls parsing for the agent loop.

Split out of ``tool_gateway`` so the runtime can format traces without loading
the Azure DevOps operation map. Re-exported from ``tool_gateway`` for
backward-compatible imports.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .tool_ado import _pull_request_summary

TOOL_RESULT_CHAR_CAP = 6000


_TOOL_CALLS_RE = re.compile(
    r"```(?:json)?\s*(\{[\s\S]*?\}\s*)```|(\{[^{}]*\"tool_calls\"[\s\S]*\})",
    re.I,
)


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
