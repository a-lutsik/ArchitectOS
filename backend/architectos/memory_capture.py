"""Memory capture policy: what happens to a fact the extractor pulled from a dialog.

One extractor (``chat_memory.evaluate_chat_turn``) serves Ask, MCP ``memory_turn``
and the IDE hooks. This policy decides the *fate* of what it extracts:

- ``auto``   — like Claude Code's auto memory: facts above ``min_confidence`` are
  written at once (through the dedup gate); only conflicts, value updates,
  duplicates and low-confidence facts wait in the Review Queue. Every
  auto-written fact can be undone (``undo_auto_capture``).
- ``review`` — the same extraction, but every fact becomes a candidate a person
  accepts or rejects (TTL ``chat_candidate_ttl_days``).
- ``off``    — only explicit saves: "Remember …", ★ on a reply, MCP ``memory_add``.

``aggressiveness`` tunes how much the extractor keeps, independent of the fate:
``conservative`` keeps only what the user said (no facts mined from assistant
replies), ``normal`` is the former ``strict`` mode, ``eager`` the former
``aggressive`` mode.

The policy supersedes ``memory_lifecycle.chat_memory_mode`` / ``chat_auto_accept``.
Both are kept as mirrors so older clients and stored settings keep working:
reading an installation without ``memory_capture`` derives it from them, and a
write to either side updates the other (see ``settings_router_service``).
"""
from __future__ import annotations

from typing import Any

from .chat_memory import classify_fact_type, normalize_chat_memory_mode
from .fact_signals import FACT_KINDS, describe_fact

CAPTURE_MODES = ("auto", "review", "off")
AGGRESSIVENESS_LEVELS = ("conservative", "normal", "eager")

DEFAULT_MEMORY_CAPTURE: dict[str, Any] = {
    "mode": "auto",
    "aggressiveness": "normal",
    "min_confidence": 0.7,
    # project facts (decisions, constraints shared by a team) cost more when
    # wrong than a personal preference or a correction, so auto needs more.
    "auto_project_min_confidence": 0.85,
    # auto: ask the model when a non-local provider is enabled; heuristic: never; llm: always try.
    "extractor": "auto",
    "llm_provider_id": "",
    "llm_max_facts": 8,
}

EXTRACTORS = ("auto", "heuristic", "llm")

# Kind → scope and node type. ``user`` facts describe the person, not the
# project, so they live in the global scope and keep the Preference type.
KIND_SCOPE = {"user": "global", "feedback": "project", "project": "project", "reference": "project"}

# Fact sources (``chat_memory.extract_durable_facts``) that come from the user's
# own words. ``conservative`` keeps only these.
USER_FACT_SOURCES = frozenset({"user_line", "user_statement", "user_explicit", "memory_add_split", "llm_user"})


def normalize_capture_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    settings = dict(DEFAULT_MEMORY_CAPTURE)
    settings.update({key: value for key, value in dict(raw or {}).items() if key in DEFAULT_MEMORY_CAPTURE})
    mode = str(settings["mode"] or "").strip().lower()
    settings["mode"] = mode if mode in CAPTURE_MODES else DEFAULT_MEMORY_CAPTURE["mode"]
    level = str(settings["aggressiveness"] or "").strip().lower()
    settings["aggressiveness"] = level if level in AGGRESSIVENESS_LEVELS else DEFAULT_MEMORY_CAPTURE["aggressiveness"]
    extractor = str(settings["extractor"] or "").strip().lower()
    settings["extractor"] = extractor if extractor in EXTRACTORS else DEFAULT_MEMORY_CAPTURE["extractor"]
    settings["llm_provider_id"] = str(settings["llm_provider_id"] or "").strip()
    try:
        settings["llm_max_facts"] = max(1, min(int(settings["llm_max_facts"]), 20))
    except (TypeError, ValueError):
        settings["llm_max_facts"] = DEFAULT_MEMORY_CAPTURE["llm_max_facts"]
    for key in ("min_confidence", "auto_project_min_confidence"):
        try:
            settings[key] = max(0.0, min(float(settings[key]), 1.0))
        except (TypeError, ValueError):
            settings[key] = DEFAULT_MEMORY_CAPTURE[key]
    settings["chat_memory_mode"] = legacy_chat_memory_mode(settings)
    return settings


def capture_from_legacy(lifecycle: dict[str, Any] | None) -> dict[str, Any]:
    """Policy implied by pre-policy settings (``chat_memory_mode`` + ``chat_auto_accept``)."""
    life = dict(lifecycle or {})
    legacy_mode = normalize_chat_memory_mode(life.get("chat_memory_mode"))
    if legacy_mode == "off":
        mode = "off"
    elif life.get("chat_auto_accept", True) is False:
        mode = "review"
    else:
        mode = "auto"
    return normalize_capture_settings({
        "mode": mode,
        "aggressiveness": "eager" if legacy_mode == "aggressive" else "normal",
    })


def legacy_chat_memory_mode(settings: dict[str, Any]) -> str:
    """``chat_memory_mode`` value the extractor understands for this policy."""
    if settings.get("mode") == "off":
        return "off"
    return "aggressive" if settings.get("aggressiveness") == "eager" else "strict"


def legacy_mirror(settings: dict[str, Any]) -> dict[str, Any]:
    """``memory_lifecycle`` keys that must agree with the policy."""
    return {
        "chat_memory_mode": legacy_chat_memory_mode(settings),
        "chat_auto_accept": settings.get("mode") == "auto",
    }


def auto_threshold(settings: dict[str, Any], kind: str, signals: list[str] | tuple[str, ...] = ()) -> float:
    """Confidence an extracted fact needs before ``auto`` writes it at once.

    Shared project facts need ``auto_project_min_confidence`` unless the person
    asked for the save outright ("Remember: …").
    """
    base = float(settings.get("min_confidence") or 0.0)
    if str(kind or "project") == "project" and "explicit_save" not in set(signals or ()):
        return max(base, float(settings.get("auto_project_min_confidence") or 0.0))
    return base


def annotate_fact(fact: dict[str, Any]) -> dict[str, Any]:
    """Return the fact with kind, confidence, scope, type and signals filled in.

    ``kind`` follows ``fact_signals.classify_fact_kind``; a ``user`` fact becomes
    a global-scope ``Preference``. Values already set by the caller (an explicit
    kind from a future LLM extractor) are kept.
    """
    annotated = dict(fact or {})
    text = str(annotated.get("text") or "")
    described = describe_fact(text, str(annotated.get("source") or ""))
    kind = str(annotated.get("kind") or described["kind"])
    annotated["kind"] = kind if kind in FACT_KINDS else "project"
    annotated.setdefault("confidence", described["confidence"])
    annotated["signals"] = described["signals"]
    annotated["scope"] = KIND_SCOPE.get(annotated["kind"], "project")
    if annotated["kind"] == "user":
        annotated["type"] = "Preference"
    elif not annotated.get("type"):
        annotated["type"] = classify_fact_type(text)
    annotated.setdefault("evidence", text[:240])
    return annotated


def filter_facts_for_policy(facts: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply ``aggressiveness`` to extracted facts (``conservative`` drops assistant-mined ones)."""
    if settings.get("aggressiveness") != "conservative":
        return list(facts or [])
    return [fact for fact in facts or [] if str(fact.get("source") or "") in USER_FACT_SOURCES]
