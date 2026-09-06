"""Generic MCP ingest: list/get tools and flatten to memory candidates."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

TEXT_FIELD_NAMES = (
    "title", "name", "summary", "description", "body", "text", "content",
    "notes", "markdown", "overview", "transcript", "state", "status",
)

ID_FIELD_NAMES = ("id", "issue_id", "meeting_id", "work_item_id", "number", "url", "href", "web_url")


def _payloads_from_result(result: Any) -> list[Any]:
    if result is None:
        return []
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        if isinstance(result.get("result"), (list, dict)):
            inner = result["result"]
            return inner if isinstance(inner, list) else [inner]
        for key in ("value", "items", "meetings", "issues", "data", "content"):
            val = result.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                return [val]
        return [result]
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            return _payloads_from_result(parsed)
        except json.JSONDecodeError:
            return [{"text": result}]
    return []


def _first_value(item: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        val = item.get(name)
        if val not in (None, "", [], {}):
            return str(val).strip()[:500]
    return ""


def _flatten_item_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for name in TEXT_FIELD_NAMES:
        val = item.get(name)
        if isinstance(val, str) and val.strip():
            parts.append(f"{name}: {val.strip()}")
    if not parts:
        parts.append(json.dumps(item, ensure_ascii=False)[:3000])
    return "\n\n".join(parts)[:8000]


def _node_type_for_kind(kind: str) -> str:
    return {
        "meetings": "Meeting",
        "issues": "Requirement",
        "pull_requests": "Artifact",
        "wiki": "Doc",
        "docs": "Doc",
    }.get(kind, "Artifact")


def ingest_generic_mcp_candidates(
    project_id: str,
    source: dict[str, Any],
    *,
    call_tool: Callable[[str, str, dict[str, Any], float | None], Any],
    stable_id: Callable[..., str],
    item_timeout: float = 25.0,
    deadline_expired: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    cfg = dict(source.get("config") or {})
    server_id = str(cfg.get("mcp_server_id") or "").strip()
    tool_map = dict(cfg.get("tool_map") or {})
    list_tool = str(tool_map.get("list") or "").strip()
    get_tool = str(tool_map.get("get") or "").strip()
    kind = str(source.get("kind") or "docs")
    source_id = str(source.get("id") or "")
    source_name = str(source.get("name") or "")
    if not server_id or not list_tool:
        return []
    listed = call_tool(server_id, list_tool, {}, item_timeout)
    items: list[dict[str, Any]] = []
    for payload in _payloads_from_result(listed):
        if isinstance(payload, dict):
            items.append(payload)
        elif isinstance(payload, str):
            items.append({"text": payload})
    candidates: list[dict[str, Any]] = []
    max_items = int(cfg.get("max_items") or 0)
    for index, item in enumerate(items):
        if deadline_expired and deadline_expired():
            break
        if max_items and index >= max_items:
            break
        detailed = item
        item_id = _first_value(item, ID_FIELD_NAMES) or f"item-{index}"
        if get_tool and item_id and not str(item_id).startswith("item-"):
            try:
                args = {"id": item_id}
                if "meeting" in get_tool:
                    args = {"meeting_ids": [item_id]}
                got = call_tool(server_id, get_tool, args, item_timeout)
                payloads = _payloads_from_result(got)
                if payloads and isinstance(payloads[0], dict):
                    detailed = {**item, **payloads[0]}
            except Exception:
                pass
        title = _first_value(detailed, ("title", "name", "summary")) or item_id
        text = _flatten_item_text(detailed)
        if len(text.strip()) < 4:
            continue
        candidates.append({
            "id": stable_id("candidate", project_id, source_id, item_id),
            "project_id": project_id,
            "source_type": kind,
            "source_ref": item_id,
            "label": f"{source_name}: {title}"[:180],
            "type": _node_type_for_kind(kind),
            "scope": "project",
            "text": text[:12000],
            "confidence": 0.65,
            "metadata": {
                "template": "generic_mcp",
                "source_id": source_id,
                "source_name": source_name,
                "mcp_server_id": server_id,
            },
        })
    return candidates
