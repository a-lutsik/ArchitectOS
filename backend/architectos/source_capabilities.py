"""Capability signatures: match MCP tools to canonical source kinds."""

from __future__ import annotations

import re
from typing import Any

# tool name patterns per kind (lowercase)
KIND_TOOL_PATTERNS: dict[str, tuple[str, ...]] = {
    "meetings": (
        r"list_.*meeting",
        r"get_.*meeting",
        r"meeting.*list",
        r"search_.*meeting",
        r"list_meetings",
        r"get_meetings",
    ),
    "issues": (
        r"list_.*issue",
        r"get_.*issue",
        r"search_.*issue",
        r"work.?item",
        r"wit_",
        r"create_issue",
    ),
    "pull_requests": (
        r"pull.?request",
        r"merge.?request",
        r"list_.*pr",
        r"get_.*pr",
        r"repo_pull_request",
    ),
    "wiki": (
        r"wiki",
        r"list_.*page",
        r"get_.*page",
        r"confluence",
    ),
    "docs": (
        r"list_.*file",
        r"read_.*file",
        r"get_.*content",
        r"repository.*content",
    ),
}

# MCP server id -> named adapter + kinds
NAMED_MCP_PRESETS: dict[str, list[tuple[str, str]]] = {
    "granola": [("granola", "meetings")],
    "azure-devops": [
        ("azure-boards", "issues"),
        ("azure-wiki", "wiki"),
    ],
    "azure-devops-git": [("azure-git", "pull_requests")],
    "github": [
        ("github", "issues"),
        ("github", "pull_requests"),
    ],
    "gitlab": [
        ("gitlab", "issues"),
        ("gitlab", "pull_requests"),
        ("gitlab", "wiki"),
    ],
}


def _tool_names(tools: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for tool in tools:
        name = str(tool.get("name") or "").strip().lower()
        if name:
            names.append(name)
    return names


def match_tool_kind(tool_name: str) -> str | None:
    lower = tool_name.lower()
    for kind, patterns in KIND_TOOL_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, lower):
                return kind
    return None


GITHUB_TOOL_MAPS: dict[str, dict[str, str]] = {
    "issues": {"list": "list_issues", "get": "get_issue"},
    "pull_requests": {"list": "list_pull_requests", "get": "get_pull_request"},
}

GITLAB_TOOL_MAPS: dict[str, dict[str, str]] = {
    "issues": {"list": "list_issues", "get": "get_issue"},
    "pull_requests": {"list": "list_merge_requests", "get": "get_merge_request"},
    "wiki": {"list": "list_wiki_pages", "get": "get_wiki_page"},
}


def preset_tool_map(adapter: str, kind: str) -> dict[str, str]:
    if adapter == "github":
        return dict(GITHUB_TOOL_MAPS.get(kind) or {})
    if adapter == "gitlab":
        return dict(GITLAB_TOOL_MAPS.get(kind) or {})
    return {}


def discover_mcp_capabilities(server_id: str, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return draft binding specs from MCP tool list."""
    presets = NAMED_MCP_PRESETS.get(server_id)
    if presets:
        return [
            {"adapter": adapter, "kind": kind, "confidence": 0.95, "needs_mapping": False}
            for adapter, kind in presets
        ]
    names = _tool_names(tools)
    found: dict[str, float] = {}
    for name in names:
        kind = match_tool_kind(name)
        if kind:
            found[kind] = max(found.get(kind, 0.0), 0.72)
    if not found:
        return [{"adapter": "generic", "kind": "docs", "confidence": 0.35, "needs_mapping": True, "tools": names[:40]}]
    return [
        {"adapter": "generic", "kind": kind, "confidence": score, "needs_mapping": score < 0.7}
        for kind, score in sorted(found.items(), key=lambda item: (-item[1], item[0]))
    ]


def guess_tool_map(kind: str, tools: list[dict[str, Any]]) -> dict[str, str]:
    names = _tool_names(tools)
    tool_map: dict[str, str] = {}
    list_candidates = [n for n in names if re.search(r"list|search", n) and match_tool_kind(n) == kind]
    get_candidates = [n for n in names if re.search(r"get|read|fetch", n) and match_tool_kind(n) == kind]
    if list_candidates:
        tool_map["list"] = list_candidates[0]
    if get_candidates:
        tool_map["get"] = get_candidates[0]
    return tool_map
