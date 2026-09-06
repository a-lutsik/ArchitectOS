"""Ask memory advice: ground a user turn against high-trust project memory.

Compares the user's Ask message to governing rules (Constraint / Decision /
Rule) and other high-trust memory, returning contradicts / limits / supports.
Pure helpers here; the LLM call lives in the Ask runtime.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .candidate_identity import CHAT_KEEPER_TEMPLATES
from .embeddings import tokens
from .search import RULE_LAYER_TYPES, parse_memory_identifiers

HIGH_TRUST_TYPES = frozenset({"Decision", "Constraint", "Requirement", "Meeting", "Rule"})
HIGH_TRUST_SOURCES = frozenset({
    "azure-boards",
    "azure_boards",
    "granola",
    "granola_mcp",
    "adr",
})
ADVICE_TYPE_PRIORITY = {
    "Constraint": 0,
    "Decision": 1,
    "Rule": 2,
    "Requirement": 3,
    "Meeting": 4,
}
ADVICE_KINDS = frozenset({"contradicts", "limits", "supports", "unrelated"})
KEEP_KINDS = frozenset({"contradicts", "limits", "supports"})
ADVICE_KIND_ORDER = {"contradicts": 0, "limits": 1, "supports": 2}
MAX_CANDIDATES = 8
MAX_ADVICE = 4
TEXT_CAP = 400
MIN_CONFIDENCE = 0.55

COUNCIL_WRAPPER_MARKERS = (
    "You are one independent expert on an ArchitectOS council.",
    "You are the judge of an ArchitectOS model council.",
)

# Retrieve/view action verbs (bare imperatives). For these the advice judge has
# nothing to govern — the user wants a fetch/load/view/open, not permission.
# Real permissibility phrasing ("можно ли…", "can I…", "should…") keeps the judge on.
ACTION_COMMAND_VERBS = frozenset({
    # Russian — load / download / fetch / open / view / read / search
    "загрузи", "загрузить", "загружай", "загрузите",
    "скачай", "скачать", "скачайте", "скачивай",
    "выгрузи", "выгрузить", "выгружай",
    "достань", "достать", "достаньте", "доставай",
    "подтяни", "подтянуть", "подтяните",
    "открой", "открыть", "откройте", "открывай",
    "покажи", "показать", "покажите", "показывай",
    "найди", "найти", "найдите", "находи",
    "прочитай", "прочитать", "прочитайте", "читай",
    "посмотри", "посмотреть", "посмотрите",
    "проверь", "проверить", "проверьте",
    "получи", "получить", "получите",
    "забери", "забрать", "вытащи", "вытащить",
    # English — download / load / fetch / pull / open / show / read / search
    "download", "downloads", "downloaded", "load", "loads", "loaded",
    "fetch", "fetches", "fetched", "pull", "pulls", "pulled",
    "open", "opens", "opened", "show", "shows", "showed",
    "get", "gets", "got", "retrieve", "retrieves", "retrieved",
    "read", "reads", "sync", "syncs", "synced",
    "refresh", "refreshes", "refreshed", "find", "finds",
    "view", "views", "list", "lists", "search", "searches",
})

ACTION_ADVISABILITY_MARKERS = frozenset({
    # Russian — the user is actually asking whether something is allowed
    "можно", "стоит", "стоило", "должен", "должна", "должны", "должно",
    "нужно", "надо", "нельзя", "разрешено", "запрещено", "допустимо",
    "позволено", "правильно", "законно",
    # English
    "can", "could", "may", "might", "should", "would", "allowed",
    "permitted", "advisable",
})


def ask_memory_advice_enabled(ui_settings: dict[str, Any] | None) -> bool:
    """Absent key means on (same pattern as ``ui.memory_enabled``)."""
    if not isinstance(ui_settings, dict):
        return True
    return ui_settings.get("ask_memory_advice") is not False


def should_skip_advice_turn(
    *,
    message: str,
    role: str = "",
    provider_id: str = "",
    precomputed: list[dict[str, Any]] | None = None,
    enabled: bool = True,
) -> bool:
    """Return True when the advice judge must not run for this turn."""
    if not enabled:
        return True
    if precomputed is not None:
        return True
    role_l = str(role or "").strip().lower()
    if role_l in {"council", "review"}:
        return True
    if str(provider_id or "").strip().lower() == "local-memory":
        return True
    text = str(message or "").strip()
    if not text:
        return True
    if any(marker in text for marker in COUNCIL_WRAPPER_MARKERS):
        return True
    idents = parse_memory_identifiers(text)
    if idents.identifier_only:
        return True
    # Bare load/fetch/view commands ("загрузи коммит", "скачай файл") carry no
    # permissibility question; the judge would only emit a meaningless supports card.
    if _is_action_command(text):
        return True
    return False


def _is_action_command(text: str) -> bool:
    """True for a short load/fetch/view command that needs no permission advice.

    Bare imperatives such as "загрузи коммит" / "скачай файл" carry no
    permissibility question, so running the advice judge only produces
    meaningless green "supports" cards. Genuine advisability phrasing
    ("можно ли…", "can I…", "should…") keeps the judge on.
    """
    raw = str(text or "").strip().lower()
    if not raw:
        return False
    toks = tokens(raw)
    if not toks or not any(tok in ACTION_COMMAND_VERBS for tok in toks):
        return False
    if any(tok in ACTION_ADVISABILITY_MARKERS for tok in toks):
        return False
    # The command verb must be leading, or the whole message a short command.
    return toks[0] in ACTION_COMMAND_VERBS or len(toks) <= 6


def _is_chat_derived_node(node: dict[str, Any]) -> bool:
    """True for nodes that came from a chat conversation, not durable governance.

    Session summaries / fact atoms keep their chat template and ``source_type``
    even after promotion (``source`` is rewritten to ``memory_candidate``).
    Conversation-derived notes must not act as governing rules in the advice
    judge: prefer Decision/Constraint/ADR/Boards facts over chat-derived notes.
    """
    meta = dict(node.get("metadata") or {})
    template = str(meta.get("template") or "").strip().lower()
    if template in CHAT_KEEPER_TEMPLATES:
        return True
    source_type = str(meta.get("source_type") or "").strip().lower()
    if source_type in {"chat", "chat_favorite"}:
        return True
    source = str(meta.get("source") or meta.get("source_key") or "").strip().lower()
    if source in {"chat", "chat_favorite", "session_keeper"}:
        return True
    # Any node that records an Ask/MCP conversation id is conversation-derived.
    if meta.get("chat_id"):
        return True
    return False


def _node_source(node: dict[str, Any]) -> str:
    meta = dict(node.get("metadata") or {})
    return str(meta.get("source") or meta.get("source_type") or "").strip().lower()


def _is_high_trust_node(node: dict[str, Any]) -> bool:
    # Chat-derived notes are never governing, even when the summarizer labelled
    # them Decision/Constraint (their template/source_type tell the real origin).
    if _is_chat_derived_node(node):
        return False
    node_type = str(node.get("type") or "").strip()
    if node_type in HIGH_TRUST_TYPES:
        return True
    source = _node_source(node)
    if source in HIGH_TRUST_SOURCES:
        return True
    if source.startswith("azure-boards") or source.startswith("azure_boards"):
        return True
    return False


def _is_rule_layer_candidate(item: dict[str, Any]) -> bool:
    return str(item.get("type") or "") in RULE_LAYER_TYPES


def _is_boards_fact_candidate(item: dict[str, Any]) -> bool:
    node_type = str(item.get("type") or "")
    source = str(item.get("source") or "").lower()
    if node_type == "Artifact":
        return True
    return source.startswith("azure-boards") or source.startswith("azure_boards")


def _truncate(text: str, cap: int = TEXT_CAP) -> str:
    raw = str(text or "").strip()
    if len(raw) <= cap:
        return raw
    return raw[: cap - 1].rstrip() + "…"


def collect_advice_candidates(
    hits: list[dict[str, Any]] | None,
    stable_hits: list[dict[str, Any]] | None = None,
    rule_layer_hits: list[dict[str, Any]] | None = None,
    *,
    limit: int = MAX_CANDIDATES,
) -> list[dict[str, Any]]:
    """Pick high-trust memory nodes for the advice judge.

    Governing rules (Constraint / Decision / Rule) always outrank tickets and
    other facts so limits are not crowded out by related work items.
    """
    limit = max(1, min(int(limit or MAX_CANDIDATES), 16))
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for hit in list(rule_layer_hits or []) + list(stable_hits or []) + list(hits or []):
        if not isinstance(hit, dict):
            continue
        node = hit.get("node") if isinstance(hit.get("node"), dict) else None
        if not node or not _is_high_trust_node(node):
            continue
        node_id = str(node.get("id") or "").strip()
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        source = _node_source(node) or "memory"
        out.append({
            "id": node_id,
            "type": str(node.get("type") or ""),
            "label": str(node.get("label") or "").strip()[:200],
            "text": _truncate(str(node.get("text") or "")),
            "source": source,
            "score": float(hit.get("score") or 0.0),
        })
    out.sort(key=lambda item: (
        ADVICE_TYPE_PRIORITY.get(str(item.get("type") or ""), 9),
        -float(item.get("score") or 0.0),
    ))
    return out[:limit]


def build_advice_judge_prompt(message: str, candidates: list[dict[str, Any]]) -> str:
    """JSON-only prompt: score each candidate vs the user message."""
    lines = [
        "You ground an ArchitectOS Ask turn against project memory.",
        "For EACH memory candidate, decide how the USER MESSAGE relates to that fact.",
        "",
        "Step 1 — classify USER FRAMING (field: framing):",
        '- "unrestricted_request": the user seeks the outcome while excluding, omitting, bypassing,',
        "  or not accepting a prerequisite, approval, transform, or intermediate step the rule requires.",
        "  Includes explicit waivers AND implicit default-path requests where the rule forbids that path.",
        '- "capability_inquiry": the user asks whether something is allowed, supported, or possible',
        "  without asserting that required prerequisites can be skipped.",
        '- "allowed": the user describes an action that satisfies the rule as written (includes prerequisites).',
        '- "unrelated": different topic.',
        "",
        "Step 2 — read the RULE: what does the candidate require, forbid, or permit only conditionally?",
        "",
        "Step 3 — assign kind from framing + rule:",
        '- "contradicts": unrestricted_request AND the rule requires a condition the user is not accepting.',
        '- "limits": capability_inquiry AND the rule permits the capability only with a stated condition.',
        '- "supports": allowed OR the rule confirms the asked action without extra conditions.',
        '- "unrelated": different topic.',
        "",
        "Important:",
        "- Treat questions the same as plans.",
        "- Rule and question may be in different languages — compare meaning, not wording.",
        "- Do NOT use limits when framing is unrestricted_request; use contradicts.",
        "- Do NOT use contradicts when framing is capability_inquiry; use limits.",
        "- Constraint / Decision / Rule outrank tickets and artifacts on the same topic.",
        "- A related ticket must not be supports when a governing rule limits the same capability.",
        "",
        "Answer with a single JSON object only — no markdown:",
        '{"items":[{"kind":"contradicts"|"limits"|"supports"|"unrelated",'
        '"framing":"unrestricted_request"|"capability_inquiry"|"allowed"|"unrelated",'
        '"id":"<node id>","source":"<source>","label":"<short>",'
        '"text":"<one sentence why>","confidence":0.0-1.0}]}',
        "",
        f"USER MESSAGE:\n{str(message or '').strip()[:2000]}",
        "",
        "MEMORY CANDIDATES:",
    ]
    for item in candidates:
        lines.append(
            f"- id={item['id']} [{item.get('type')}|{item.get('source')}] "
            f"{item.get('label')}: {item.get('text')}"
        )
    return "\n".join(lines)


def reconcile_advice_kinds(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Align kind with framing when the judge picked the wrong severity."""
    reconciled: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        framing = str(item.get("framing") or "").strip().lower()
        if framing == "unrestricted_request" and kind == "limits":
            item = {**item, "kind": "contradicts"}
        elif framing == "capability_inquiry" and kind == "contradicts":
            item = {**item, "kind": "limits"}
        elif framing in {"allowed", "unrelated"} and kind in {"contradicts", "limits"}:
            if framing == "allowed" and kind == "limits":
                item = {**item, "kind": "supports"}
        reconciled.append(item)
    return reconciled


def _advice_sort_key(item: dict[str, Any]) -> tuple[int, float]:
    kind = str(item.get("kind") or "").strip().lower()
    return (
        ADVICE_KIND_ORDER.get(kind, 9),
        -float(item.get("confidence") or 0.0),
    )


def postprocess_advice_verdict(
    items: list[dict[str, Any]],
    *,
    candidates: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Drop ticket supports when a governing rule already limits the same question."""
    if not items:
        return items
    by_id = {str(c.get("id") or ""): c for c in (candidates or []) if isinstance(c, dict)}
    limiting_rule_ids = {
        str(item.get("id") or "")
        for item in items
        if str(item.get("kind") or "") in {"contradicts", "limits"}
        and _is_rule_layer_candidate(by_id.get(str(item.get("id") or ""), {"type": item.get("type")}))
    }
    if not limiting_rule_ids:
        return items
    filtered: list[dict[str, Any]] = []
    for item in items:
        kind = str(item.get("kind") or "")
        if kind != "supports":
            filtered.append(item)
            continue
        candidate = by_id.get(str(item.get("id") or ""), item)
        if _is_boards_fact_candidate(candidate):
            continue
        filtered.append(item)
    return filtered


def parse_advice_verdict(text: str, *, candidates: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Parse the judge JSON into normalized contradicts/limits/supports items."""
    match = re.search(r"\{.*\}", str(text or ""), re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        return []
    by_id = {str(c.get("id") or ""): c for c in (candidates or []) if isinstance(c, dict)}
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in KEEP_KINDS:
            continue
        try:
            confidence = float(raw.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < MIN_CONFIDENCE:
            continue
        node_id = str(raw.get("id") or "").strip()
        if not node_id:
            continue
        key = (kind, node_id)
        if key in seen:
            continue
        seen.add(key)
        known = by_id.get(node_id) or {}
        source = str(raw.get("source") or known.get("source") or "memory").strip() or "memory"
        label = str(raw.get("label") or known.get("label") or node_id).strip()[:200]
        reason = str(raw.get("text") or raw.get("reason") or "").strip()[:300]
        if not reason:
            continue
        framing = str(raw.get("framing") or "").strip().lower()
        out.append({
            "kind": kind,
            "id": node_id,
            "source": source,
            "label": label,
            "text": reason,
            "confidence": max(0.0, min(confidence, 1.0)),
            "type": str(known.get("type") or raw.get("type") or ""),
            "framing": framing,
        })
        if len(out) >= MAX_ADVICE:
            break
    out = reconcile_advice_kinds(out)
    out = postprocess_advice_verdict(out, candidates=candidates)
    out.sort(key=_advice_sort_key)
    return out[:MAX_ADVICE]


def format_advice_context_block(advice: list[dict[str, Any]]) -> str:
    """Inject into the answering model's context so it must acknowledge findings."""
    if not advice:
        return ""
    lines = [
        "Memory advice (server-checked against governing rules, tickets, meetings, decisions).",
        "The UI already renders this block. Use it only to respect constraints:",
        "never mention 'Memory advice', its markers, or node ids in your answer text.",
        "CONTRADICTS: comply — do not say the action is allowed as requested.",
        "LIMITS: answer as supported only together with the rule's required condition.",
        "Do not declare as-is support when a LIMITS or CONTRADICTS item applies.",
    ]
    markers = {
        "contradicts": "CONTRADICTS",
        "limits": "LIMITS",
        "supports": "SUPPORTS",
    }
    for item in advice:
        marker = markers.get(str(item.get("kind") or ""), "SUPPORTS")
        label = str(item.get("label") or "memory note").strip()[:160]
        text = str(item.get("text") or "").strip()
        lines.append(f"- {marker}: {label} — {text}")
    return "\n".join(lines)


def _boards_id_from_advice(item: dict[str, Any]) -> str:
    source = str(item.get("source") or "").lower()
    label = str(item.get("label") or "")
    node_id = str(item.get("id") or "")
    if "azure" in source or "boards" in source:
        match = re.search(r"(\d{3,7})", label) or re.search(r"(\d{3,7})", node_id)
        if match:
            return match.group(1)
    match = re.search(r"(?:AB#|ADO#)(\d{3,7})", label, re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def advice_actions(advice: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chip actions for opening the cited ticket or memory node."""
    actions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in advice or []:
        if not isinstance(item, dict):
            continue
        boards_id = _boards_id_from_advice(item)
        if boards_id:
            key = ("open_work_item", boards_id)
            if key not in seen:
                seen.add(key)
                actions.append({
                    "type": "open_work_item",
                    "id": boards_id,
                    "label": str(item.get("label") or f"Open AB#{boards_id}"),
                })
            continue
        node_id = str(item.get("id") or "").strip()
        if node_id:
            key = ("memory_get", node_id)
            if key not in seen:
                seen.add(key)
                actions.append({
                    "type": "memory_get",
                    "id": node_id,
                    "label": str(item.get("label") or f"Open {node_id}")[:80],
                })
    return actions


def merge_advice_into_structured(
    structured: dict[str, Any] | None,
    advice: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Server advice wins on same id+kind; ensure open actions exist."""
    base = dict(structured or {})
    actions = [item for item in (base.get("actions") or []) if isinstance(item, dict)]
    links = [item for item in (base.get("links") or []) if isinstance(item, dict)]
    questions = base.get("questions") if isinstance(base.get("questions"), list) else []
    existing = [item for item in (base.get("advice") or []) if isinstance(item, dict)]
    server = [item for item in (advice or []) if isinstance(item, dict) and item.get("kind") in KEEP_KINDS]

    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    server_ids = {
        str(item.get("id") or "").strip()
        for item in server
        if str(item.get("kind") or "").strip().lower() in KEEP_KINDS and str(item.get("id") or "").strip()
    }
    for item in existing:
        kind = str(item.get("kind") or "").strip().lower()
        node_id = str(item.get("id") or "").strip()
        if kind in KEEP_KINDS and node_id and node_id not in server_ids:
            by_key[(kind, node_id)] = item
    for item in server:
        kind = str(item.get("kind") or "").strip().lower()
        node_id = str(item.get("id") or "").strip()
        if kind in KEEP_KINDS and node_id:
            by_key[(kind, node_id)] = item  # server wins

    merged_advice = sorted(by_key.values(), key=_advice_sort_key)[:MAX_ADVICE]

    for action in advice_actions(merged_advice):
        a_type = str(action.get("type") or "")
        a_id = str(action.get("id") or "")
        if not any(str(a.get("type")) == a_type and str(a.get("id")) == a_id for a in actions):
            actions.append(action)

    out = {**base, "actions": actions, "links": links, "advice": merged_advice}
    if questions:
        out["questions"] = questions
    return out
