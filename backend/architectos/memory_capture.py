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

from .chat_memory import normalize_chat_memory_mode

CAPTURE_MODES = ("auto", "review", "off")
AGGRESSIVENESS_LEVELS = ("conservative", "normal", "eager")

DEFAULT_MEMORY_CAPTURE: dict[str, Any] = {
    "mode": "auto",
    "aggressiveness": "normal",
    "min_confidence": 0.7,
}

# Fact sources (``chat_memory.extract_durable_facts``) that come from the user's
# own words. ``conservative`` keeps only these.
USER_FACT_SOURCES = frozenset({"user_line", "user_statement", "user_explicit", "memory_add_split"})


def normalize_capture_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    settings = dict(DEFAULT_MEMORY_CAPTURE)
    settings.update({key: value for key, value in dict(raw or {}).items() if key in DEFAULT_MEMORY_CAPTURE})
    mode = str(settings["mode"] or "").strip().lower()
    settings["mode"] = mode if mode in CAPTURE_MODES else DEFAULT_MEMORY_CAPTURE["mode"]
    level = str(settings["aggressiveness"] or "").strip().lower()
    settings["aggressiveness"] = level if level in AGGRESSIVENESS_LEVELS else DEFAULT_MEMORY_CAPTURE["aggressiveness"]
    try:
        settings["min_confidence"] = max(0.0, min(float(settings["min_confidence"]), 1.0))
    except (TypeError, ValueError):
        settings["min_confidence"] = DEFAULT_MEMORY_CAPTURE["min_confidence"]
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


def filter_facts_for_policy(facts: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply ``aggressiveness`` to extracted facts (``conservative`` drops assistant-mined ones)."""
    if settings.get("aggressiveness") != "conservative":
        return list(facts or [])
    return [fact for fact in facts or [] if str(fact.get("source") or "") in USER_FACT_SOURCES]
