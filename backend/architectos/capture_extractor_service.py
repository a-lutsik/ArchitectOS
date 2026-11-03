"""LLM fact extractor for memory capture (tier 2 of the capture pipeline).

Tier 1 is the deterministic extractor in ``chat_memory`` / ``fact_signals``:
free, offline, keyword-driven. When a real provider is configured, this mixin
asks it once per turn (MCP, hooks) or once per session (Ask) to pull the facts
worth remembering, with the same brief Claude Code gives itself: four kinds,
"useful in a future conversation", skip what the code or the existing memory
already says. A block of what is *already known* comes from the retrieval pack,
so the "already recorded" criterion is applied before anything is written.

The model's answer is data, not authority: it is parsed strictly, each fact is
re-scored by ``fact_signals`` (code-derivable or transient wording still lowers
confidence), text is redacted again before storage, and any failure, empty or
malformed reply falls back to the tier-1 facts. ``memory_capture.extractor``
picks ``auto`` (LLM when a non-local provider is enabled), ``heuristic`` or
``llm``.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from .adapters import ProviderRequest
from .adapters_base import is_local_memory_stub_text
from .chat_memory import classify_fact_type
from .fact_signals import FACT_KINDS, classify_fact_kind, detect_signals, signal_names
from .storage import sanitize_text

_LOG = logging.getLogger(__name__)

FACT_TYPES = ("Decision", "Constraint", "Requirement", "Lesson", "Preference")
# After a failed provider call hooks must not pay the timeout again on every turn.
LLM_EXTRACTOR_BACKOFF_S = 600.0
MAX_TRANSCRIPT_CHARS = 6000
MAX_KNOWN_ITEMS = 8

_PROMPT = """You maintain the long-term memory of a software project for an AI assistant.
From the DIALOG below, list the facts that would be useful in a FUTURE conversation about this project.

Kinds:
- "user": the person's role, expertise, working preferences, how they want answers.
- "feedback": a correction the person gave, or an approach they explicitly confirmed.
- "project": a decision, constraint, deadline or status that cannot be derived from the code, git history or docs.
- "reference": where to find information outside the repository (tracker, dashboard, wiki, channel).

Do NOT list: anything derivable from the codebase (architecture, file paths, what a function does, debugging details),
anything already in ALREADY KNOWN, secrets or tokens, transient details of the current task, greetings or chit-chat,
or a restatement of the assistant's answer that adds no new information.

Answer with JSON only, no prose:
{{"facts": [{{"kind": "user|feedback|project|reference", "type": "Decision|Constraint|Requirement|Lesson|Preference",
  "text": "<one self-contained sentence, no pronouns>", "confidence": 0.0-1.0, "evidence": "<exact quote from the dialog>"}}]}}
Return {{"facts": []}} when nothing qualifies. At most {max_facts} facts.

ALREADY KNOWN:
{known}

DIALOG:
{dialog}"""


class CaptureExtractorMixin:
    _capture_llm_backoff_until: float = 0.0

    # -- availability -----------------------------------------------------------
    def llm_extractor_active(self, policy: dict[str, Any] | None = None) -> bool:
        """Whether the next extraction should ask the model, per ``memory_capture.extractor``."""
        policy = policy or self.memory_capture_settings()  # type: ignore[attr-defined]
        choice = str(policy.get("extractor") or "auto")
        if choice == "heuristic":
            return False
        if time.monotonic() < self._capture_llm_backoff_until:
            return False
        if choice == "llm":
            return True
        wanted = str(policy.get("llm_provider_id") or "")
        for provider in self.repository.list_providers():  # type: ignore[attr-defined]
            if str(provider.get("id") or "") == "local-memory" or not provider.get("enabled"):
                continue
            if not wanted or str(provider.get("id") or "") == wanted:
                return True
        return False

    # -- extraction ---------------------------------------------------------------
    def extract_capture_facts(
        self,
        user_text: str,
        assistant_text: str,
        *,
        project_id: str,
        heuristic_facts: list[dict[str, Any]],
        known_hits: list[dict[str, Any]] | None = None,
        chat_id: str = "",
        trigger: str = "",
        policy: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Facts for the capture policy plus ``{"extractor": "llm"|"heuristic", ...}``.

        Falls back to ``heuristic_facts`` whenever the model is off, unreachable,
        answers with the local-memory stub or returns something unparsable.
        """
        policy = policy or self.memory_capture_settings()  # type: ignore[attr-defined]
        if not self.llm_extractor_active(policy):
            return list(heuristic_facts or []), {"extractor": "heuristic"}
        dialog = self._capture_dialog(user_text, assistant_text)
        if not dialog.strip():
            return list(heuristic_facts or []), {"extractor": "heuristic"}
        prompt = _PROMPT.format(
            max_facts=int(policy.get("llm_max_facts") or 8),
            known=self._known_block(known_hits) or "(nothing relevant stored yet)",
            dialog=dialog,
        )
        request = ProviderRequest(message=prompt, context="", project_id=project_id or "architectos", role="review")
        try:
            result = self.provider_router.route(  # type: ignore[attr-defined]
                self.repository.list_providers(), request, str(policy.get("llm_provider_id") or "") or None,  # type: ignore[attr-defined]
            )
            text = str(result.get("text") or "")
            provider = str((result.get("selected_provider") or {}).get("id") or "")
            if not text.strip() or is_local_memory_stub_text(text):
                raise RuntimeError(f"no language model answered ({provider or 'none'})")
            facts = self._parse_llm_facts(text, user_text, assistant_text, int(policy.get("llm_max_facts") or 8))
        except Exception as exc:  # noqa: BLE001 - extraction must never break a turn
            self._capture_llm_backoff_until = time.monotonic() + LLM_EXTRACTOR_BACKOFF_S
            _LOG.warning("llm fact extractor fell back to heuristics: %s", exc)
            self._extractor_event(project_id, chat_id, "llm_fallback", f"LLM extractor unavailable, used heuristics: {exc}", {"trigger": trigger, "error": str(exc)[:300]})
            return list(heuristic_facts or []), {"extractor": "heuristic", "llm_error": str(exc)[:300]}
        if facts is None:
            self._extractor_event(project_id, chat_id, "llm_fallback", "LLM extractor returned no JSON, used heuristics", {"trigger": trigger, "provider": provider})
            return list(heuristic_facts or []), {"extractor": "heuristic", "llm_error": "no JSON in reply", "provider": provider}
        self._capture_llm_backoff_until = 0.0
        self._extractor_event(
            project_id, chat_id, "llm_ok", f"LLM extractor returned {len(facts)} fact(s)",
            {"trigger": trigger, "provider": provider, "facts": len(facts), "heuristic_facts": len(heuristic_facts or [])},
        )
        return facts, {"extractor": "llm", "provider": provider}

    # -- helpers --------------------------------------------------------------------
    @staticmethod
    def _capture_dialog(user_text: str, assistant_text: str) -> str:
        user = sanitize_text(str(user_text or ""))[0].strip()
        assistant = sanitize_text(str(assistant_text or ""))[0].strip()
        parts = []
        if user:
            parts.append(f"USER:\n{user}")
        if assistant:
            parts.append(f"ASSISTANT:\n{assistant}")
        dialog = "\n\n".join(parts)
        if len(dialog) > MAX_TRANSCRIPT_CHARS:
            dialog = dialog[-MAX_TRANSCRIPT_CHARS:]
        return dialog

    @staticmethod
    def _known_block(hits: list[dict[str, Any]] | None) -> str:
        lines: list[str] = []
        for hit in list(hits or [])[:MAX_KNOWN_ITEMS]:
            node = hit.get("node") if isinstance(hit, dict) and isinstance(hit.get("node"), dict) else hit
            label = str((node or {}).get("label") or "").strip()
            text = " ".join(str((node or {}).get("text") or "").split())
            if not label and not text:
                continue
            lines.append(f"- {label}: {text[:160]}" if label else f"- {text[:160]}")
        return "\n".join(lines)

    @staticmethod
    def _parse_llm_facts(
        text: str, user_text: str, assistant_text: str, max_facts: int
    ) -> list[dict[str, Any]] | None:
        """Strict parse of the model's JSON; ``None`` when there is no JSON at all."""
        match = re.search(r"\{.*\}|\[.*\]", text or "", re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return None
        items = data.get("facts") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return None
        user_lower = " ".join(str(user_text or "").lower().split())
        facts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            fact_text = sanitize_text(" ".join(str(item.get("text") or "").split()))[0].strip()
            if len(fact_text) < 18 or fact_text.lower() in seen:
                continue
            seen.add(fact_text.lower())
            signals = detect_signals(fact_text)
            kind = str(item.get("kind") or "").strip().lower()
            if kind not in FACT_KINDS:
                kind = classify_fact_kind(fact_text, signals)
            fact_type = str(item.get("type") or "").strip()
            if fact_type not in FACT_TYPES:
                fact_type = "Preference" if kind == "user" else classify_fact_type(fact_text)
            raw_confidence = item.get("confidence")
            try:
                confidence = float(raw_confidence) if raw_confidence is not None else 0.7
            except (TypeError, ValueError):
                confidence = 0.7
            # The model is told to skip these; if it did not, the fact still waits for review.
            if signals["derivable"]:
                confidence -= 0.15
            if signals["transient"]:
                confidence -= 0.10
            confidence = round(max(0.5, min(0.95, confidence)), 2)
            evidence = sanitize_text(" ".join(str(item.get("evidence") or "").split()))[0].strip()[:240] or fact_text[:240]
            spoken_by_user = evidence.lower()[:80] in user_lower or fact_text.lower()[:80] in user_lower
            facts.append({
                "text": fact_text[:400],
                "type": fact_type,
                "kind": kind,
                "confidence": confidence,
                "evidence": evidence,
                "source": "llm_user" if spoken_by_user else "llm_assistant",
                "signals": signal_names(signals),
            })
            if len(facts) >= max(1, max_facts):
                break
        return facts

    def _extractor_event(self, project_id: str, chat_id: str, status: str, message: str, metadata: dict[str, Any]) -> None:
        try:
            self._record_keeper_event(project_id, chat_id, "capture_extractor", status, message, metadata)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - bookkeeping only
            _LOG.debug("extractor event skipped: %s", exc)
