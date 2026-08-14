"""Parse ArchitectOS rich assistant responses (Markdown + structured actions)."""

from __future__ import annotations

import json
import re
from typing import Any

ARCHITECTOS_FENCE_RE = re.compile(r"```architectos\s*([\s\S]*?)```", re.IGNORECASE)

RESPONSE_FORMAT_POLICY = """Response format (final user answer):
1) Prefer Markdown: headings, bullet lists, and GFM tables when comparing items.
2) Link Azure Boards work items as AB#1234 (or [title](ab://1234)).
3) When a diagram helps (architecture, flow, ownership), include a ```mermaid fence.
4) When you can propose concrete next steps, end with one ```architectos JSON fence:
```architectos
{"actions":[{"type":"open_work_item","id":"1234","label":"Open AB#1234"},{"type":"ask","prompt":"Compare AB#1234 and AB#1288","label":"Compare these items"},{"type":"memory_get","id":"node_abc","label":"Open memory node"}],"links":[{"kind":"work_item","id":"1234","title":"Login timeout"}]}
```
Supported action types: open_work_item, boards_get_item, boards_search, memory_get, ask.
5) Do not put tool_calls JSON in the final answer. Keep the architectos block valid JSON.
6) If there are no actions, omit the architectos fence.
7) Actions must offer new work the user may want next. Never use them to ask for permission to read files or memory,
   to “continue the scan”, or to confirm a plan — do that work before answering.
8) Write action labels in plain language, without internal tool names."""


def split_rich_response(text: str) -> tuple[str, dict[str, Any]]:
    """Return (display_markdown, structured) extracted from an assistant reply."""
    source = str(text or "")
    match = ARCHITECTOS_FENCE_RE.search(source)
    if not match:
        return source.strip(), {"actions": [], "links": []}
    structured: dict[str, Any] = {"actions": [], "links": []}
    raw = match.group(1).strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            actions = parsed.get("actions") if isinstance(parsed.get("actions"), list) else []
            links = parsed.get("links") if isinstance(parsed.get("links"), list) else []
            structured = {
                **parsed,
                "actions": [item for item in actions if isinstance(item, dict)],
                "links": [item for item in links if isinstance(item, dict)],
            }
    except json.JSONDecodeError:
        structured = {"actions": [], "links": [], "parse_error": True}
    display = (source[: match.start()] + source[match.end() :]).strip()
    return display, structured
