"""Parse ArchitectOS rich assistant responses (Markdown + structured actions)."""

from __future__ import annotations

import json
import re
from typing import Any

ARCHITECTOS_FENCE_RE = re.compile(r"```architectos\s*([\s\S]*?)```", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)]|[a-z][.)])\s+(.+?)\s*$", re.IGNORECASE)
_ASK_RE = re.compile(
    r"please|point me|tell me|choose|pick|select|one of these|one of the following|"
    r"which of|would you|should i|can you (?:point|tell|share)|"
    r"уточн|выбер|укажите|что из|какой из|какой вариант",
    re.IGNORECASE,
)

RESPONSE_FORMAT_POLICY = """Response format (final user answer):
1) Prefer Markdown: headings, bullet lists, and GFM tables when comparing items.
2) Link Azure Boards work items as AB#1234 (or [title](ab://1234)).
3) When a diagram helps (architecture, flow, ownership), include a ```mermaid fence.
   Use flowchart TB or flowchart LR. Group with subgraph. Do not use block-beta or ASCII boxes —
   those render as nested grey cards instead of a readable diagram.
   Do not set layout: elk (this app's Mermaid build cannot load ELK). Straight edges come from
   the renderer (curve: linear). Quote labels that contain (), /, or punctuation:
   Load["Load config (decrypt)"]. Use subgraph Id ["Title"] with a space before the title.
4) When you can propose concrete next steps, end with one ```architectos JSON fence:
```architectos
{"actions":[{"type":"open_work_item","id":"1234","label":"Open AB#1234"},{"type":"ask","prompt":"Compare AB#1234 and AB#1288","label":"Compare these items"},{"type":"memory_get","id":"node_abc","label":"Open memory node"}],"links":[{"kind":"work_item","id":"1234","title":"Login timeout"}],"advice":[{"kind":"contradicts","id":"node_abc","label":"API contract","text":"Conflicts with the decided response shape"}]}
```
Supported action types: open_work_item, boards_get_item, boards_search, memory_get, ask.
Optional advice array kinds: contradicts, limits, supports (server may also attach these — the UI renders them; do not restate them or cite node ids in the prose).
5) Do not put tool_calls JSON in the final answer. Keep the architectos block valid JSON.
6) If there are no actions and no advice, omit the architectos fence.
7) Actions must offer new work the user may want next. Never use them to ask for permission to read files or memory,
   to “continue the scan”, or to confirm a plan — do that work before answering.
8) Write action labels in plain language, without internal tool names.
9) If the user must choose between a few options, do not write a bullet list. Put a questions array in the architectos fence:
{"questions":[{"prompt":"Which should I use?","options":[{"id":"file","label":"The class or file path"},{"id":"endpoint","label":"A related endpoint"}]}]}
10) If the context includes a “Memory advice” block, comply with any CONTRADICTS/LIMITS it states, but do not mention the block, its markers, or node ids in the prose — the UI already renders it."""


def _is_choice_intro(line: str) -> bool:
    text = str(line or "").strip()
    if len(text) < 8 or len(text) > 320:
        return False
    if _ASK_RE.search(text) and re.search(r"[?:]\s*$", text):
        return True
    if re.search(r"\?\s*$", text) and re.search(
        r"which|what|who|where|how|какой|что|где|как", text, re.IGNORECASE
    ):
        return True
    return False


def _clean_option_label(text: str) -> str:
    return re.sub(r"[,;]?\s+or\s*$", "", str(text or "").strip(), flags=re.IGNORECASE).strip()


def extract_clarify_question(text: str) -> tuple[str, dict[str, Any] | None]:
    """Pull a Cursor-style multiple-choice prompt out of a markdown reply."""
    source = str(text or "").replace("\r\n", "\n")
    lines = source.split("\n")
    in_fence = False
    best: tuple[int, int, str, list[str]] | None = None
    for index, line in enumerate(lines):
        if re.match(r"^\s*```", line):
            in_fence = not in_fence
            continue
        if in_fence or not _is_choice_intro(line):
            continue
        cursor = index + 1
        while cursor < len(lines) and not str(lines[cursor]).strip():
            cursor += 1
        options: list[str] = []
        while cursor < len(lines):
            match = _BULLET_RE.match(lines[cursor])
            if not match:
                break
            label = _clean_option_label(match.group(1))
            if not label or len(label) > 180:
                options = []
                break
            options.append(label)
            cursor += 1
        if 2 <= len(options) <= 8:
            best = (index, cursor, line.strip(), options)
    if not best:
        return source.strip(), None
    start, end, prompt, options = best
    display = "\n".join([*lines[:start], *lines[end:]]).strip()
    prompt = re.sub(r"[:：]\s*$", "", prompt).strip()
    question = {
        "prompt": prompt,
        "options": [{"id": f"opt-{i}", "label": label} for i, label in enumerate(options)],
    }
    return display, question


def split_rich_response(text: str) -> tuple[str, dict[str, Any]]:
    """Return (display_markdown, structured) extracted from an assistant reply."""
    source = str(text or "")
    match = ARCHITECTOS_FENCE_RE.search(source)
    structured: dict[str, Any] = {"actions": [], "links": [], "advice": []}
    if match:
        raw = match.group(1).strip()
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                raw_actions: Any = parsed.get("actions")
                actions = raw_actions if isinstance(raw_actions, list) else []
                raw_links: Any = parsed.get("links")
                links = raw_links if isinstance(raw_links, list) else []
                raw_advice: Any = parsed.get("advice")
                advice = raw_advice if isinstance(raw_advice, list) else []
                structured = {
                    **parsed,
                    "actions": [item for item in actions if isinstance(item, dict)],
                    "links": [item for item in links if isinstance(item, dict)],
                    "advice": [item for item in advice if isinstance(item, dict)],
                }
        except json.JSONDecodeError:
            structured = {"actions": [], "links": [], "advice": [], "parse_error": True}
        source = (source[: match.start()] + source[match.end() :]).strip()
    else:
        source = source.strip()
        structured.setdefault("advice", [])
    questions = structured.get("questions")
    has_questions = isinstance(questions, list) and any(isinstance(item, dict) for item in questions)
    display, detected = extract_clarify_question(source)
    if detected:
        source = display
        if not has_questions:
            structured["questions"] = [detected]
    if "advice" not in structured or not isinstance(structured.get("advice"), list):
        structured["advice"] = []
    return source, structured
