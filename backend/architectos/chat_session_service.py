"""Chat-session finalization, idle handling and per-turn memory capture.

Extracted from ``service.py``. Covers session activation/finalization, the idle
loop, session summarisation, the "keeper" candidate-capture helpers that turn
chat turns into memory candidates, plus the chat context pack, assistant-message
favorites, and the keeper retry entry point. Depends only on leaf modules,
never on ``service`` itself, so it introduces no import cycle. Repository
access, providers and council orchestration are reached through ``self`` via
the MRO on :class:`ArchitectOSService`.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeGuard

from .adapters_base import is_local_memory_stub_text
from .candidate_identity import candidate_origin_key
from .chat_memory import (
    collapse_similar_facts,
    evaluate_chat_turn,
    facts_from_markdown_summary,
    infer_fact_subject,
)
from .mcp_server import MCP_BIND_FILENAME, read_mcp_bind_project_id
from .memory_capture import annotate_fact, auto_threshold, filter_facts_for_policy
from .memory_dedup import DedupDecision
from .models import MemoryNode, owned_by, stable_id, utc_now
from .usage import estimate_tokens_from_text

_LOG = logging.getLogger("architectos.service")
AGENT_TURN_RETENTION_DAYS = 30


def nearest_rank(ordered: list[float], q: float) -> float:
    """Nearest-rank percentile of an already sorted list (0.0 when empty)."""
    if not ordered:
        return 0.0
    return round(ordered[max(0, math.ceil(q * len(ordered)) - 1)], 1)


def _latency(turns: list[dict[str, Any]]) -> dict[str, float]:
    ordered = sorted(float(turn["total_ms"]) for turn in turns)
    return {"p50": nearest_rank(ordered, 0.5), "p95": nearest_rank(ordered, 0.95)}


class ChatSessionServiceMixin:
    """Chat-session lifecycle finalization and per-turn memory capture."""

    def finalize_chat_session(
        self,
        chat_id: str,
        *,
        force: bool = False,
        trigger: str = "manual",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Close an active chat session and enqueue per-fact review candidates (summary is context-only)."""
        chat = self.repository.get_chat(chat_id)
        if not chat:
            raise ValueError("chat not found")
        chat = dict(chat)
        caller_project_id = str(chat.get("project_id") or project_id or "").strip() or None
        actual_project_id = caller_project_id or "architectos"
        life = self.memory_lifecycle.settings()
        policy = self.memory_capture_settings()
        mode = policy["chat_memory_mode"]
        if mode == "off" and not force and trigger != "manual":
            return {"chat": chat, "candidate": None, "skipped": True, "reason": "memory_capture=off"}

        all_messages = list(chat.get("messages") or [])
        start_index = max(0, min(int(chat.get("session_start_message_index") or 0), len(all_messages)))
        session_messages = all_messages[start_index:]
        messages = [m for m in session_messages if str(m.get("text") or "").strip()]
        if len(messages) < 2:
            self._record_keeper_event(
                actual_project_id,
                chat_id,
                "session_keeper",
                "skipped",
                "Skipped session summary: chat has no complete turn yet.",
                {"trigger": trigger},
            )
            return {"chat": chat, "candidate": None, "skipped": True, "reason": "empty_chat"}

        status = str(chat.get("session_status") or "active")
        if status == "complete" and not force:
            return {"chat": chat, "candidate": None, "skipped": True, "reason": "already_complete"}

        with self._chat_finalize_lock:
            if chat_id in self._chat_finalize_inflight:
                return {"chat": chat, "candidate": None, "skipped": True, "reason": "in_progress"}
            self._chat_finalize_inflight.add(chat_id)

        try:
            chat["session_status"] = "finalizing"
            chat = self.repository.upsert_chat(chat)
            revision = max(1, int(chat.get("session_revision") or 1))
            summary = self._summarize_chat_session(chat, session_messages=session_messages)
            if not summary.get("has_facts"):
                current = self.repository.get_chat(chat_id) or chat
                if int(current.get("session_revision") or 1) == revision and str(current.get("session_status") or "") == "finalizing":
                    current["session_status"] = "complete"
                    current["session_finalized_at"] = utc_now()
                    current["session_finalize_trigger"] = trigger
                    current["session_finalized_message_index"] = len(all_messages)
                    chat = self.repository.upsert_chat(current)
                else:
                    chat = current
                self.repository.upsert_chat_context_summary({
                    "chat_id": chat_id,
                    "project_id": actual_project_id,
                    "summary_type": "session_summary",
                    "summary_text": str(summary.get("text") or ""),
                    "covered_until": utc_now(),
                    "metadata": {"trigger": trigger, "revision": revision, "empty": True, "start_message_index": start_index, "end_message_index": len(all_messages)},
                })
                self.repository.upsert_chat_context_summary({
                    "chat_id": chat_id,
                    "project_id": actual_project_id,
                    "summary_type": f"session_summary:{revision}",
                    "summary_text": str(summary.get("text") or ""),
                    "covered_until": utc_now(),
                    "metadata": {"trigger": trigger, "revision": revision, "empty": True, "start_message_index": start_index, "end_message_index": len(all_messages)},
                })
                self._record_keeper_event(
                    actual_project_id,
                    chat_id,
                    "session_keeper",
                    "skipped",
                    "Session closed without durable facts for the candidate queue.",
                    {"trigger": trigger, "revision": revision},
                )
                return {"chat": chat, "candidate": None, "skipped": True, "reason": "no_durable_facts", "summary": summary}

            facts = list(summary.get("facts") or [])
            skip_ask_card = self._facts_already_captured_as_mcp(actual_project_id, facts)
            saved = None
            atoms: list[dict[str, Any]] = []
            # mode=off: the session still closes with a context-only summary,
            # but nothing enters memory or the Review Queue without an explicit save.
            if not skip_ask_card and policy["mode"] != "off":
                atoms = self._enqueue_session_fact_atoms(
                    actual_project_id,
                    chat_id,
                    revision,
                    facts,
                    mode=mode,
                    trigger=trigger,
                    caller_project_id=caller_project_id,
                )
                if not atoms and summary.get("has_facts"):
                    candidate = {
                        "id": stable_id("candidate", actual_project_id, "chat_session_summary", chat_id, str(revision)),
                        "project_id": actual_project_id,
                        "source_type": "chat",
                        "source_ref": chat_id,
                        "label": f"Chat session {revision}: {str(chat.get('title') or chat_id)[:64]}",
                        "type": str(summary.get("type") or "Lesson"),
                        "scope": "project",
                        "text": str(summary.get("text") or "").strip(),
                        "confidence": float(summary.get("confidence") or 0.72),
                        "metadata": {
                            "chat_id": chat_id,
                            "template": "chat_session_summary",
                            "source": "session_keeper",
                            "source_type": "chat",
                            "trigger": trigger,
                            "session_revision": revision,
                            "session_start_message_index": start_index,
                            "session_end_message_index": len(all_messages),
                            "message_count": len(messages),
                            "facts": facts[:12],
                            "chat_memory_mode": mode,
                        },
                    }
                    prepared = self.ingestion_engine.prepare_candidates(actual_project_id, [candidate], 1)
                    saved = self.repository.upsert_memory_candidate(prepared[0]) if prepared else None
            if skip_ask_card or saved or atoms:
                self.ingestion_engine.retire_autoscan_chat_dumps(actual_project_id, chat_id)
            if skip_ask_card:
                self._record_keeper_event(
                    actual_project_id,
                    chat_id,
                    "session_keeper",
                    "skipped",
                    "Skipped Ask summary: the same facts are already queued as MCP.",
                    {"trigger": trigger, "revision": revision},
                )
            elif saved:
                promoted = self._maybe_auto_accept_chat_candidate(saved, policy, caller_project_id=caller_project_id)
                if self._auto_saved(promoted):
                    self._record_keeper_event(
                        actual_project_id,
                        chat_id,
                        "session_keeper",
                        "auto_accepted",
                        f"Auto-accepted session summary: {saved.get('label')}",
                        {"candidate_id": saved.get("id"), "node_id": (promoted.get("memory") or {}).get("id"), "trigger": trigger},
                    )
                elif not promoted:
                    self._record_keeper_event(
                        actual_project_id,
                        chat_id,
                        "session_keeper",
                        "candidate_created",
                        f"Created session summary candidate: {saved.get('label')}",
                        {"candidate_id": saved.get("id"), "trigger": trigger, "revision": revision},
                    )
            elif not atoms:
                self._record_keeper_event(
                    actual_project_id,
                    chat_id,
                    "session_keeper",
                    "skipped",
                    "Skipped session facts: duplicate or low-value candidate.",
                    {"trigger": trigger, "revision": revision},
                )

            self.repository.upsert_chat_context_summary({
                "chat_id": chat_id,
                "project_id": actual_project_id,
                "summary_type": "session_summary",
                "summary_text": str(summary.get("text") or ""),
                "covered_until": utc_now(),
                "metadata": {"trigger": trigger, "revision": revision, "candidate_id": (saved or {}).get("id")},
            })
            self.repository.upsert_chat_context_summary({
                "chat_id": chat_id,
                "project_id": actual_project_id,
                "summary_type": f"session_summary:{revision}",
                "summary_text": str(summary.get("text") or ""),
                "covered_until": utc_now(),
                "metadata": {
                    "trigger": trigger,
                    "revision": revision,
                    "candidate_id": (saved or {}).get("id"),
                    "start_message_index": start_index,
                    "end_message_index": len(all_messages),
                },
            })
            current = self.repository.get_chat(chat_id) or chat
            if int(current.get("session_revision") or 1) == revision and str(current.get("session_status") or "") == "finalizing":
                current["session_status"] = "complete"
                current["session_finalized_at"] = utc_now()
                current["session_finalize_trigger"] = trigger
                current["session_finalized_message_index"] = len(all_messages)
                chat = self.repository.upsert_chat(current)
            else:
                # A new message reopened the chat while the previous revision was being summarized.
                chat = current
            return {"chat": chat, "candidate": saved, "atoms": atoms, "skipped": saved is None and not atoms, "summary": summary, "trigger": trigger}
        except Exception as exc:  # noqa: BLE001 - keep chat usable if summarization fails
            _LOG.warning("chat session finalize failed for %s: %s", chat_id, exc)
            chat["session_status"] = "active"
            chat = self.repository.upsert_chat(chat)
            self._record_keeper_event(
                actual_project_id,
                chat_id,
                "session_keeper",
                "error",
                f"Session finalize failed: {exc}",
                {"trigger": trigger},
            )
            raise
        finally:
            with self._chat_finalize_lock:
                self._chat_finalize_inflight.discard(chat_id)

    def _facts_already_captured_as_mcp(self, project_id: str, facts: list[dict[str, Any]]) -> bool:
        """True when every durable fact is already a pending/promoted MCP candidate."""
        keyed = [str(fact.get("text") or "").strip() for fact in facts if len(str(fact.get("text") or "").strip()) >= 18]
        if not keyed:
            return False
        owned = 0
        for text in keyed:
            origin = candidate_origin_key({
                "source_type": "mcp",
                "metadata": {"template": "mcp_turn_atom", "fact_key": text},
            })
            existing = self.repository.find_memory_candidate_by_origin(project_id, origin) if origin else None
            if existing and str(existing.get("source_type") or "") == "mcp":
                owned += 1
        return owned == len(keyed)

    def _enqueue_session_fact_atoms(
        self,
        project_id: str,
        chat_id: str,
        revision: int,
        facts: list[dict[str, Any]],
        *,
        mode: str,
        trigger: str,
        source_type: str = "chat",
        template: str = "chat_session_atom",
        id_kind: str = "chat_session_atom",
        caller_project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """One candidate per durable fact, not one blob for the whole chat.

        The capture policy decides whether each atom is written at once
        (``mode=auto``) or waits in the Review Queue (``mode=review``).
        """
        policy = self.memory_capture_settings()
        if policy["mode"] == "off":
            return []
        facts = collapse_similar_facts(filter_facts_for_policy(list(facts or []), policy), limit=12)
        saved_atoms: list[dict[str, Any]] = []
        origin = "mcp" if source_type == "mcp" else "session_keeper"
        from_mcp = source_type == "mcp"
        label_prefix = "MCP fact" if from_mcp else "Chat fact"
        body_prefix = (
            "Durable fact captured from an MCP agent turn (not a full transcript)."
            if from_mcp
            else "Durable fact extracted from Ask (not a full transcript)."
        )
        for raw_fact in facts[:12]:
            fact = annotate_fact(raw_fact)
            fact_text = str(fact.get("text") or "").strip()
            if len(fact_text) < 18:
                continue
            label_seed = fact_text[:72]
            fact_subject = str(fact.get("subject") or "").strip() or infer_fact_subject(fact_text)
            candidate = {
                "id": stable_id("candidate", project_id, id_kind, chat_id, str(revision), fact_text[:240]),
                "project_id": project_id,
                "source_type": source_type,
                "source_ref": chat_id,
                "label": f"{label_prefix}: {label_seed}",
                "type": str(fact.get("type") or "Lesson"),
                "scope": str(fact.get("scope") or "project"),
                "text": f"{body_prefix}\n\n{fact_text}".strip(),
                "confidence": float(fact.get("confidence") or 0.74),
                "metadata": {
                    "chat_id": chat_id,
                    "template": template,
                    "source": origin,
                    "source_type": source_type,
                    "fact_key": fact_text[:240],
                    "fact_source": fact.get("source") or "session",
                    "kind": fact.get("kind") or "project",
                    "evidence": fact.get("evidence") or fact_text[:240],
                    "capture_signals": list(fact.get("signals") or []),
                    "trigger": trigger,
                    "session_revision": revision,
                    "chat_memory_mode": mode,
                    **({"fact_subject": fact_subject} if fact_subject else {}),
                },
            }
            prepared = self.ingestion_engine.prepare_candidates(project_id, [candidate], 1)
            if not prepared:
                continue
            saved = self.repository.upsert_memory_candidate(prepared[0])
            if not saved:
                continue
            promoted = self._maybe_auto_accept_chat_candidate(saved, policy, caller_project_id=caller_project_id)
            if promoted:
                saved = self.repository.get_memory_candidate(str(saved.get("id") or "")) or saved
            if self._auto_saved(promoted):
                self._record_keeper_event(
                    project_id,
                    chat_id,
                    "session_keeper",
                    "auto_accepted",
                    f"Auto-accepted session fact: {saved.get('label')}",
                    {
                        "candidate_id": saved.get("id"),
                        "node_id": (promoted.get("memory") or {}).get("id"),
                        "trigger": trigger,
                        "revision": revision,
                        "template": str((saved.get("metadata") or {}).get("template") or "chat_session_atom"),
                    },
                )
            elif not promoted:
                self._record_keeper_event(
                    project_id,
                    chat_id,
                    "session_keeper",
                    "candidate_created",
                    f"Created session fact candidate: {saved.get('label')}",
                    {"candidate_id": saved.get("id"), "trigger": trigger, "revision": revision, "template": template},
                )
            saved_atoms.append(saved)
        if saved_atoms:
            self.ingestion_engine.refresh_candidate_duplicate_flags(project_id)
            saved_atoms = [
                self.repository.get_memory_candidate(str(item.get("id") or "")) or item
                for item in saved_atoms
            ]
        return saved_atoms

    def _activate_chat_session(self, chat: dict[str, Any]) -> dict[str, Any]:
        """Reopen a finalized/idle chat when the user continues the conversation."""
        chat = dict(chat)
        status = str(chat.get("session_status") or "active")
        chat.setdefault("session_revision", 1)
        chat.setdefault("last_activity_at", chat.get("updated_at") or chat.get("created_at") or utc_now())
        if status in {"complete", "finalizing"}:
            finalized_index = int(chat.get("session_finalized_message_index") or len(chat.get("messages") or []))
            chat["session_status"] = "active"
            chat["session_revision"] = max(1, int(chat.get("session_revision") or 1)) + 1
            chat["session_start_message_index"] = max(0, min(finalized_index, len(chat.get("messages") or [])))
            chat.pop("session_finalized_at", None)
            chat.pop("session_finalize_trigger", None)
        else:
            chat["session_status"] = "active"
        return chat

    def _chat_session_idle_loop(self, *, initial_delay_s: float = 90.0, interval_s: float = 60.0) -> None:
        """Finalize idle chat sessions so memory candidates are created after the conversation ends."""
        if self._chat_session_stop.wait(initial_delay_s):
            return
        while not self._chat_session_stop.is_set():
            try:
                self._finalize_idle_chat_sessions()
            except Exception as exc:  # noqa: BLE001 - background worker must not crash the app
                _LOG.warning("chat session idle sweep failed: %s", exc)
            if self._chat_session_stop.wait(interval_s):
                return

    def _finalize_idle_chat_sessions(self) -> dict[str, Any]:
        life = self.memory_lifecycle.settings()
        idle_minutes = int(life.get("chat_session_idle_minutes") or 0)
        if idle_minutes <= 0:
            return {"finalized": 0, "idle_minutes": idle_minutes, "disabled": True}
        if self.memory_capture_settings()["chat_memory_mode"] == "off":
            return {"finalized": 0, "idle_minutes": idle_minutes, "disabled": True}
        now = datetime.now(timezone.utc)
        finalized = 0
        for chat in self.repository.list_chats(None)[:200]:
            chat_id = str(chat.get("id") or "")
            if not chat_id:
                continue
            if str(chat.get("session_status") or "active") != "active":
                continue
            messages = [m for m in (chat.get("messages") or []) if str(m.get("text") or "").strip()]
            if len(messages) < 2:
                continue
            raw = str(chat.get("last_activity_at") or chat.get("updated_at") or "")
            then = self.memory_lifecycle._parse_time(raw) if raw else now
            age_minutes = max(0.0, (now - then).total_seconds() / 60.0)
            if age_minutes < idle_minutes:
                continue
            try:
                result = self.finalize_chat_session(chat_id, trigger="idle")
                if not result.get("skipped") or result.get("candidate"):
                    finalized += 1
            except Exception as exc:  # noqa: BLE001 - continue sweeping other chats
                _LOG.warning("idle finalize failed for %s: %s", chat_id, exc)
        return {"finalized": finalized, "idle_minutes": idle_minutes}

    def _summarize_chat_session(self, chat: dict[str, Any], *, session_messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Build a factual session summary for the memory candidate queue."""
        source_messages = list(session_messages) if session_messages is not None else list(chat.get("messages") or [])
        messages = [m for m in source_messages if str(m.get("text") or "").strip()]
        facts: list[dict[str, Any]] = []
        decisions: list[str] = []
        for index in range(1, len(messages)):
            if messages[index].get("role") != "assistant":
                continue
            user_text = self._nearest_user_message_before(messages, index)
            assistant_text = str(messages[index].get("text") or "")
            verdict = evaluate_chat_turn(user_text, assistant_text, mode="aggressive")
            for fact in list(verdict.facts or [])[:5]:
                text = str(fact.get("text") or "").strip()
                if not text:
                    continue
                # Keep who said it: the capture policy scores a person's own
                # line higher than text mined from a reply.
                entry = {"text": text, "type": str(fact.get("type") or "Lesson"), "source": str(fact.get("source") or "session")}
                if fact.get("subject"):
                    entry["subject"] = str(fact.get("subject") or "")
                facts.append(entry)
            decision = self._decision_log_line(user_text, assistant_text)
            if decision:
                decisions.append(decision.lstrip("- ").strip())

        # Deduplicate while preserving order.
        seen_facts: set[str] = set()
        unique_facts: list[dict[str, Any]] = []
        for fact in facts:
            key = str(fact.get("text") or "").lower()
            if key in seen_facts:
                continue
            seen_facts.add(key)
            unique_facts.append(fact)
        extraction: dict[str, Any] = {"extractor": "heuristic"}
        policy = self.memory_capture_settings()
        if policy.get("mode") != "off" and messages and self.llm_extractor_active(policy):
            # One call for the whole session: the transcript as the dialog, the
            # retrieval pack for the first question as "already known".
            user_part = "\n".join(str(m.get("text") or "").strip() for m in messages if m.get("role") == "user")
            assistant_part = "\n".join(str(m.get("text") or "").strip() for m in messages if m.get("role") != "user")
            first_user = next((str(m.get("text") or "") for m in messages if m.get("role") == "user"), "")
            try:
                known = self.context(first_user or user_part[:400], project_id=str(chat.get("project_id") or "architectos"), limit=8).get("hits") or []
            except Exception:  # noqa: BLE001 - the known block is advisory
                known = []
            llm_facts, extraction = self.extract_capture_facts(
                user_part, assistant_part, project_id=str(chat.get("project_id") or "architectos"), heuristic_facts=unique_facts,
                known_hits=known, chat_id=str(chat.get("id") or ""), trigger="session_summary", policy=policy,
            )
            if extraction.get("extractor") == "llm":
                unique_facts = llm_facts
        facts = collapse_similar_facts(unique_facts, limit=12)
        decisions = list(dict.fromkeys(decisions))[:8]

        previous_summaries = [
            item for item in self.repository.list_chat_context_summaries(str(chat.get("id") or ""))
            if str(item.get("summary_type") or "").startswith("session_summary:")
        ]
        previous_summaries.sort(key=lambda item: int((item.get("metadata") or {}).get("revision") or 0))
        previous_context = "\n\n".join(
            str(item.get("summary_text") or "")[:1800]
            for item in previous_summaries[-3:]
            if str(item.get("summary_text") or "").strip()
        )

        transcript_lines: list[str] = []
        for msg in messages[-24:]:
            role = "User" if msg.get("role") == "user" else "Assistant"
            transcript_lines.append(f"{role}: {str(msg.get('text') or '').strip()[:1200]}")
        transcript = "\n".join(transcript_lines)

        llm_summary = ""
        if facts or decisions or len(messages) >= 4:
            prompt = (
                "Summarize this finished chat into durable project memory facts only.\n"
                "Return plain Markdown with these sections when applicable:\n"
                "## Facts\n"
                "## Decisions\n"
                "## Constraints / Rules\n"
                "## Open questions\n"
                "Omit chitchat, process talk, and tool names. Prefer short bullets.\n"
                "Summarize ONLY the new session transcript. Earlier summaries are context for continuity and "
                "deduplication: do not repeat their facts unless needed briefly to make a new fact understandable.\n"
                "Make the new summary understandable on its own.\n"
                "If nothing durable was decided, reply with exactly: NO_DURABLE_FACTS\n\n"
                f"Chat title: {chat.get('title') or ''}\n\n"
                f"Earlier session summaries (context only):\n{previous_context or '(none)'}\n\n"
                "New session transcript:\n"
                f"{transcript}"
            )
            try:
                res = self.run_ai({
                    "message": prompt,
                    "provider_id": "auto",
                    "role": "review",
                    "limit": 1,
                    "enable_tools": False,
                })
                llm_summary = str(res.get("text") or "").strip()
            except Exception as exc:  # noqa: BLE001 - fall back to heuristic summary
                _LOG.warning("session LLM summary failed: %s", exc)

        if llm_summary:
            for extra in facts_from_markdown_summary(llm_summary, limit=12):
                key = str(extra.get("text") or "").lower()
                if not key or key in seen_facts:
                    continue
                seen_facts.add(key)
                facts.append(extra)
            facts = collapse_similar_facts(facts, limit=12)

        if llm_summary and "NO_DURABLE_FACTS" in llm_summary.upper() and not facts and not decisions:
            return {"text": "No durable project facts found in this chat session.", "has_facts": False, "facts": [], "type": "Lesson", "confidence": 0.4}

        llm_has_structure = bool(
            llm_summary
            and "NO_DURABLE_FACTS" not in llm_summary.upper()
            and any(marker in llm_summary for marker in ("## Facts", "## Decisions", "## Constraints", "- "))
        )
        # Without extracted facts/decisions, only trust an explicitly structured LLM summary.
        if not facts and not decisions and llm_summary and not llm_has_structure:
            llm_summary = ""

        sections: list[str] = [
            f"Session {int(chat.get('session_revision') or 1)} summary for chat: {chat.get('title') or chat.get('id')}",
            "",
        ]
        if llm_summary and "NO_DURABLE_FACTS" not in llm_summary.upper():
            sections.append(llm_summary.strip())
        else:
            if facts:
                sections.append("## Facts")
                sections.extend(f"- {item['text']}" for item in facts)
            if decisions:
                sections.append("## Decisions")
                sections.extend(f"- {item}" for item in decisions)
            if not facts and not decisions:
                # Keep a compact heuristic digest so manual finalize still produces a reviewable candidate
                # when the chat is substantive but fact extraction is sparse.
                bullets = []
                for msg in messages:
                    if msg.get("role") != "user":
                        continue
                    text = " ".join(str(msg.get("text") or "").split())
                    if len(text) < 40:
                        continue
                    bullets.append(f"- {text[:220]}")
                if not bullets:
                    return {"text": "No durable project facts found in this chat session.", "has_facts": False, "facts": [], "type": "Lesson", "confidence": 0.4}
                sections.append("## Topics discussed")
                sections.extend(bullets[:8])

        summary_text = "\n".join(sections).strip()
        memory_type = "Decision" if decisions or any(item.get("type") == "Decision" for item in facts) else (
            "Constraint" if any(item.get("type") == "Constraint" for item in facts) else "Lesson"
        )
        has_facts = bool(facts or decisions or llm_has_structure)
        # Heuristic topic digests count as reviewable session candidates too.
        if not has_facts and "## Topics discussed" in summary_text:
            has_facts = True
        return {
            "text": summary_text,
            "has_facts": has_facts,
            "facts": facts,
            "extractor": extraction.get("extractor"),
            "type": memory_type,
            "confidence": 0.78 if facts or decisions else 0.62,
        }

    def _chat_for_agent_turn(self, project_id: str, chat_id: str, message: str) -> dict[str, Any]:
        chat = self.repository.get_chat(chat_id) if chat_id else None
        if chat:
            return chat
        now = utc_now()
        return {
            "id": chat_id or stable_id("chat", project_id, message[:60], utc_now()),
            "project_id": project_id,
            "title": message[:60] or "New council dialog",
            "messages": [],
            "favorite": False,
            "created_at": now,
            "last_activity_at": now,
            "session_status": "active",
            "session_revision": 1,
        }

    def _council_assistant_text(self, result: dict[str, Any]) -> str:
        synthesis = str(result.get("synthesis") or "").strip()
        if is_local_memory_stub_text(synthesis):
            synthesis = ""
        answers = result.get("answers") or []
        if synthesis:
            details = []
            for answer in answers:
                if not str(answer.get("text") or "").strip():
                    continue
                label = answer.get("label") or answer.get("provider_id") or "Model"
                details.append(f"<summary>{label}</summary>\n\n{answer.get('text')}")
            if len(details) > 1:
                blocks = "\n\n".join(f"<details>\n{d}\n</details>" for d in details)
                return f"{synthesis}\n\n---\n**Individual model answers**\n\n{blocks}".strip()
            return synthesis
        parts = []
        for answer in answers:
            label = answer.get("label") or answer.get("provider_id") or "Model"
            parts.append(f"\n\n— {label} —\n{answer.get('text') or '(no answer)'}")
        return "".join(parts).strip()

    # Fact types the extractor produces. Rules-keeper cards ("Rule", template
    # rules_keeper) and raw turn dumps are not fact atoms and always go to review.
    CHAT_AUTO_ACCEPT_TYPES = {"Lesson", "Decision", "Constraint", "Requirement", "Preference"}
    CHAT_AUTO_ACCEPT_TEMPLATES = {"chat_session_atom", "chat_fact_keeper", "mcp_turn_atom"}

    def undo_auto_capture(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Take back a fact the ``auto`` policy wrote: archive the node, reject the candidate."""
        candidate_id = str(payload.get("candidate_id") or "").strip()
        candidate = self.repository.get_memory_candidate(candidate_id) if candidate_id else None
        if not candidate:
            raise ValueError("memory candidate not found")
        meta = dict(candidate.get("metadata") or {})
        if not meta.get("auto_accepted") or candidate.get("status") != "promoted":
            raise ValueError("candidate was not auto-captured")
        node = self.repository.get_node(str(candidate.get("promoted_node_id") or ""))
        # The project the auto-accept ran under (None: unbound capture, which may change any node).
        caller_project_id = meta.get("auto_accepted_by") or None
        if node is not None and node.status == "active" and not owned_by(node, caller_project_id):
            # A fold into a node the candidate's project does not own: never archive it.
            _LOG.info("undo of candidate %s leaves node %s (author=%s) outside project=%s", candidate_id, node.id, node.author_project_id, caller_project_id)
        elif node is not None and node.status == "active":
            node.status = "archived"
            node.metadata["archived_at"] = utc_now()
            node.metadata["archived_reason"] = "auto_capture_undo"
            node = self.repository.upsert_node(node)
            try:
                self.repository.record_memory_event(node.id, "archived", actor="ui", details={"reason": "auto_capture_undo", "candidate_id": candidate_id})
            except Exception as exc:  # history is best-effort
                _LOG.warning("undo event skipped: %s", exc)
        candidate["status"] = "rejected"
        candidate["rejected_at"] = utc_now()
        candidate["reject_reason"] = "auto capture undone"
        candidate["metadata"] = {**meta, "auto_capture_undone": True}
        candidate = self.repository.update_memory_candidate(candidate)
        self._record_keeper_event(
            str(candidate.get("project_id") or "architectos"),
            str(candidate.get("source_ref") or ""),
            "session_keeper",
            "undone",
            f"Undid auto-captured fact: {candidate.get('label')}",
            {"candidate_id": candidate_id, "node_id": getattr(node, "id", "")},
        )
        return {"candidate": candidate, "memory": node.to_dict() if node else None}

    def memory_capture_stats(self, days: int = 7, project_id: str | None = None) -> dict[str, Any]:
        """Counters behind the capture setting: what the policy did lately."""
        since = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%dT%H:%M:%SZ")
        counts = self.repository.count_keeper_events_since(since, project_id)
        turns = self.repository.list_agent_turn_metrics_since(since, project_id)
        prompts = [turn for turn in turns if turn["turn"] == "prompt"]
        responses = [turn for turn in turns if turn["turn"] != "prompt"]
        # Only a pack with hits can be injected; an empty one keeps the hook silent.
        packed = [turn["pack_tokens"] for turn in prompts if turn["hits"]]
        return {
            "days": int(days),
            "auto_saved": int(counts.get("auto_accepted", 0)),
            "queued": int(counts.get("candidate_created", 0)),
            "undone": int(counts.get("undone", 0)),
            "skipped": int(counts.get("skipped", 0)),
            "llm_calls": int(counts.get("llm_ok", 0)),
            "llm_fallbacks": int(counts.get("llm_fallback", 0)),
            # Prompt turns pay for retrieval before the answer; response turns
            # (Stop / afterAgentResponse) only capture, so their latency is apart.
            "agent_turns": {
                "count": len(turns),
                "sources": {source: sum(1 for turn in turns if turn["source"] == source) for source in ("hook", "mcp")},
                "prompt": {
                    "count": len(prompts),
                    "latency_ms": _latency(prompts),
                    "pack_tokens_avg": round(sum(packed) / len(packed), 1) if packed else 0.0,
                    "empty_pack_share": round(1 - len(packed) / len(prompts), 3) if prompts else 0.0,
                },
                "response": {"count": len(responses), "latency_ms": _latency(responses)},
            },
        }

    def prune_agent_turn_events(self, days: int = AGENT_TURN_RETENTION_DAYS) -> int:
        """Drop ``agent_turn`` counters older than ``days``; other keeper events stay."""
        before = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%dT%H:%M:%SZ")
        removed = self.repository.delete_keeper_events_before("agent_turn", before)
        _LOG.info("agent turn events pruned · removed=%s older_than_days=%s", removed, days)
        return removed

    def project_id_for_path(self, path: str | None) -> str | None:
        """Map a working directory to the project that owns it.

        Agent hooks only know the cwd they fired in, so the caller cannot supply a
        project id. The MCP server's folder bind (``.architectos.json`` walked up
        from the path) wins, so a hook and the MCP server in one folder always
        agree; otherwise the longest matching project root wins. Returns None when
        neither applies.
        """
        raw = str(path or "").strip()
        if not raw:
            return None
        try:
            target = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        bound_id = read_mcp_bind_project_id(target)
        if bound_id:
            _LOG.debug("project for %s: %s from %s", target, bound_id, MCP_BIND_FILENAME)
            return bound_id
        best_id: str | None = None
        best_depth = -1
        for project in self.repository.list_projects():
            root = str(getattr(project, "root_path", "") or "").strip()
            if not root:
                continue
            try:
                candidate = Path(root).expanduser().resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            if candidate != target and candidate not in target.parents:
                continue
            depth = len(candidate.parts)
            if depth > best_depth:
                best_id = str(project.id)
                best_depth = depth
        _LOG.debug("project for %s: %s by root_path (no project_id in %s)", target, best_id, MCP_BIND_FILENAME)
        return best_id

    def capture_memory_turn(
        self,
        user_text: str,
        assistant_text: str = "",
        *,
        project_id: str | None = None,
        scope: str | None = None,
        limit: int = 8,
        cwd: str | None = None,
        client: str | None = None,
    ) -> dict[str, Any]:
        """Per-turn hook for MCP agents and IDE hooks: pack memory, enqueue fact atoms.

        Atoms land in the review queue (TTL) unless they are low-risk Lessons,
        which auto-promote to short-term memory. Raw dialogue is not stored.
        ``cwd`` resolves the project when the caller is a hook that only knows a
        working directory; ``client`` is recorded on candidates for auditing.
        ``timing`` and ``pack_tokens`` are also kept on an ``agent_turn`` keeper
        event, which ``memory_capture_stats`` aggregates.
        """
        started = time.perf_counter()
        caller_project_id = str(project_id or self.project_id_for_path(cwd) or "").strip() or None
        actual_project_id = caller_project_id or "architectos"
        user = str(user_text or "").strip()
        assistant = str(assistant_text or "").strip()
        packed = self.context(user or assistant, project_id=actual_project_id, scope=scope, limit=limit)
        extract_started = time.perf_counter()
        policy = self.memory_capture_settings()
        mode = policy["chat_memory_mode"]
        verdict = evaluate_chat_turn(user, assistant, mode=mode)
        source_ref = stable_id("mcp_turn", actual_project_id, user[:240], assistant[:240])
        trigger = f"hook_turn:{client}" if client else "mcp_turn"
        facts, extraction = self._capture_facts_for_turn(
            user, assistant, verdict, project_id=actual_project_id, known_hits=packed.get("hits") or [],
            chat_id=source_ref, trigger=trigger, policy=policy,
        )
        kept = bool(facts) if extraction.get("extractor") == "llm" else bool(verdict.keep)
        reason = verdict.reason if extraction.get("extractor") != "llm" else ("llm_keep" if facts else "llm_nothing_durable")
        capture_started = time.perf_counter()
        atoms: list[dict[str, Any]] = []
        if kept:
            atoms = self._enqueue_session_fact_atoms(
                actual_project_id,
                source_ref,
                1,
                facts,
                mode=mode,
                trigger=trigger,
                source_type="mcp",
                template="mcp_turn_atom",
                id_kind="mcp_turn_atom",
                caller_project_id=caller_project_id,
            )
        queued = [item for item in atoms if str(item.get("status") or "") == "candidate"]
        accepted = [item for item in atoms if str(item.get("status") or "") == "promoted"]
        finished = time.perf_counter()
        context_timing = packed.get("timing") or {}
        timing = {
            "search_ms": float(context_timing.get("search_ms") or 0.0),
            "stable_ms": float(context_timing.get("stable_ms") or 0.0),
            "pack_ms": float(context_timing.get("pack_ms") or 0.0),
            "extractor_ms": round((capture_started - extract_started) * 1000.0, 1),
            "capture_ms": round((finished - capture_started) * 1000.0, 1),
            "total_ms": round((finished - started) * 1000.0, 1),
        }
        context = str(packed.get("context") or "")
        hits = packed.get("hits") or []
        pack_tokens = estimate_tokens_from_text(context)
        try:
            self._record_keeper_event(
                actual_project_id,
                source_ref,
                "agent_turn",
                "agent_turn",
                f"Agent turn: {len(hits)} hit(s), {pack_tokens} pack tokens, {timing['total_ms']} ms",
                {
                    "turn": "prompt" if user else "response",
                    "source": "hook" if client else "mcp",
                    "client": client or "",
                    "trigger": trigger,
                    "timing": timing,
                    "pack_tokens": pack_tokens,
                    "hits": len(hits),
                    "atoms": len(atoms),
                    "extractor": extraction.get("extractor"),
                },
            )
        except Exception as exc:  # noqa: BLE001 - counters must never break the turn
            _LOG.warning("agent turn event skipped for project %s: %s", actual_project_id, exc)
        _LOG.debug(
            "memory turn project=%s trigger=%s hits=%s atoms=%s pack_tokens=%s timing=%s",
            actual_project_id, trigger, len(hits), len(atoms), pack_tokens, timing,
        )
        return {
            "project_id": actual_project_id,
            "context": context,
            "hits": hits,
            "kept": kept,
            "reason": reason,
            "extractor": extraction.get("extractor"),
            "capture_mode": policy["mode"],
            "atoms": atoms,
            "queued": queued,
            "auto_accepted": accepted,
            "timing": timing,
            "pack_tokens": pack_tokens,
        }

    def _capture_facts_for_turn(
        self,
        user_text: str,
        assistant_text: str,
        verdict: Any,
        *,
        project_id: str,
        known_hits: list[dict[str, Any]],
        chat_id: str,
        trigger: str,
        policy: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Tier 1 decides whether a turn is worth a model call; tier 2 extracts.

        Chit-chat, empty turns and ``mode=off`` never reach the model. Short
        turns the heuristics already rejected are also skipped unless there is
        enough text for the model to find what keywords missed.
        """
        heuristic = list(getattr(verdict, "facts", None) or [])
        if policy.get("mode") == "off" or str(getattr(verdict, "reason", "")) in {"chitchat", "empty_turn", "chat_memory_mode=off"}:
            return heuristic, {"extractor": "heuristic"}
        if not getattr(verdict, "keep", False) and len(str(user_text or "")) + len(str(assistant_text or "")) < 120:
            return heuristic, {"extractor": "heuristic"}
        return self.extract_capture_facts(
            user_text, assistant_text, project_id=project_id, heuristic_facts=heuristic,
            known_hits=known_hits, chat_id=chat_id, trigger=trigger, policy=policy,
        )

    def _maybe_auto_accept_chat_candidate(
        self,
        candidate: dict[str, Any] | None,
        settings: dict[str, Any] | None = None,
        *,
        caller_project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Write an extracted fact at once when the capture policy is ``auto``.

        Requires a fact template, source_ref and confidence ≥
        ``memory_capture.min_confidence``. Duplicates, revisions (value
        updates) and near-duplicates/conflicts found by the dedup gate stay in
        review whatever the mode. The promoted node has
        metadata.auto_accepted=True so ``undo_auto_capture`` can reverse it.
        """
        policy = settings if settings and "mode" in settings else self.memory_capture_settings()
        if policy.get("mode") != "auto":
            return None
        if not candidate or str(candidate.get("status") or "candidate") != "candidate":
            return None
        meta_in = dict(candidate.get("metadata") or {})
        kind = str(meta_in.get("kind") or "project")
        if float(candidate.get("confidence") or 0.0) < auto_threshold(policy, kind, list(meta_in.get("capture_signals") or [])):
            return None
        if not str(candidate.get("source_ref") or "").strip():
            return None
        if str(candidate.get("type") or "") not in self.CHAT_AUTO_ACCEPT_TYPES:
            return None
        meta = dict(candidate.get("metadata") or {})
        if str(meta.get("template") or "") not in self.CHAT_AUTO_ACCEPT_TEMPLATES:
            return None
        if meta.get("duplicate"):
            return None
        if meta.get("revision"):
            # Human fact updates stay in Review for explicit accept → SUPERSEDES.
            return None
        blocked, held, decision = self._auto_accept_verdict(candidate, caller_project_id)
        if held is not None:
            # Another project already stored this fact: nothing to write or to review.
            return self._hold_memory_candidate(candidate, held)
        if blocked:
            return None
        candidate["metadata"] = {**meta, "auto_accepted": True, "auto_accepted_by": caller_project_id}
        self.repository.update_memory_candidate(candidate)
        candidate_id = str(candidate.get("id") or "")
        try:
            return self.promote_memory_candidate(
                candidate_id,
                caller_project_id=caller_project_id,
                gate_decision=(decision, None) if decision is not None else None,
            )
        except Exception as exc:  # noqa: BLE001 - auto-accept must never break the chat turn
            current = self.repository.get_memory_candidate(candidate_id) or candidate
            if str(current.get("status") or "") == "promoted":
                # The node is written; only a later step failed. It stays auto-accepted (undoable).
                _LOG.warning("chat auto-accept of %s wrote node %s, then failed: %s", candidate_id, current.get("promoted_node_id"), exc)
                node = self.repository.get_node(str(current.get("promoted_node_id") or ""))
                return {"candidate": current, "memory": node.to_dict() if node else None}
            _LOG.warning("chat auto-accept failed for %s, left in review: %s", candidate_id, exc)
            current["metadata"] = {
                key: value for key, value in dict(current.get("metadata") or {}).items()
                if key not in {"auto_accepted", "auto_accepted_by"}
            }
            self.repository.update_memory_candidate(current)
            return None

    @staticmethod
    def _auto_saved(promoted: dict[str, Any] | None) -> TypeGuard[dict[str, Any]]:
        """Whether auto-accept wrote memory: a held candidate is answered too, but rejected."""
        if not promoted or not promoted.get("memory"):
            return False
        return str((promoted.get("candidate") or {}).get("status") or "") == "promoted"

    def _auto_accept_near_duplicate(self, candidate: dict[str, Any], caller_project_id: str | None = None) -> bool:
        """Block auto-write when the atom restates or nearly restates an existing node."""
        return self._auto_accept_verdict(candidate, caller_project_id)[0]

    def _auto_accept_verdict(
        self, candidate: dict[str, Any], caller_project_id: str | None
    ) -> tuple[bool, MemoryNode | None, DedupDecision | None]:
        """``(blocked, held, decision)`` of the gate for an auto-accept candidate.

        ``blocked`` keeps the atom in review; ``held`` is a node another project
        owns that already states this very fact (only with a caller project): the
        atom is then neither written nor queued. ``decision`` (when the gate ran)
        is handed to promote so the atom is classified once.
        """
        meta = dict(candidate.get("metadata") or {})
        if meta.get("revision"):
            # Revisions must promote as a new node + SUPERSEDES, not be blocked as near-dups.
            return False, None, None
        scope = str(candidate.get("scope") or "project")
        try:
            decision, held = self._gate_decision(
                str(candidate.get("label") or ""),
                str(candidate.get("text") or ""),
                str(candidate.get("type") or "Lesson"),
                scope,
                self._memory_project_id_for_scope(scope, candidate.get("project_id")),
                caller_project_id,
                allow_llm=False,
            )
        except Exception:  # noqa: BLE001 - never fail a chat/MCP turn on dedup
            return False, None, None
        if held is not None:
            return True, held, decision
        # Duplicates, enrichments, value updates and contradictions stay in
        # review: a person (not an auto-accepted chat turn) decides whether the
        # stored fact changed. So do suggestions and close (lexical or semantic)
        # neighbours.
        if decision.action != "new" or decision.details.get("suggested_action"):
            return True, None, decision
        near = bool(decision.near_id) and decision.near_score >= self.memory_dedup.NEAR_DUPLICATE_SIMILARITY
        return near, None, decision

    def _capture_favorite_message_candidate(self, project_id: str, chat_id: str, message_index: int, user_text: str, assistant_text: str) -> dict[str, Any] | None:
        # Explicit ★ save always creates a candidate (opt-in), but still prefer facts when possible.
        verdict = evaluate_chat_turn(user_text, assistant_text, mode="aggressive")
        facts = list(verdict.facts or [])
        if facts:
            fact = facts[0]
            text = (
                "Favorited durable fact from assistant reply.\n\n"
                f"{fact.get('text')}\n\n"
                f"User:\n{user_text[:800]}\n\nAssistant excerpt:\n{assistant_text[:1200]}"
            ).strip()
            label = f"Favorite: {str(fact.get('text') or '')[:64]}"
            fact_type = str(fact.get("type") or self._chat_turn_memory_type(user_text, assistant_text))
        else:
            text = f"Favorited assistant reply.\n\nUser:\n{user_text[:1200]}\n\nAssistant:\n{assistant_text[:2600]}".strip()
            label = f"Favorite reply: {(user_text or assistant_text)[:72]}"
            fact_type = self._chat_turn_memory_type(user_text, assistant_text)
        candidate = {
            "id": stable_id("candidate", project_id, "assistant_favorite", chat_id, str(message_index), assistant_text[:240]),
            "project_id": project_id,
            "source_type": "chat_favorite",
            "source_ref": chat_id,
            "label": label,
            "type": fact_type,
            "scope": "project",
            "text": text,
            "confidence": 0.86,
            "metadata": {
                "chat_id": chat_id,
                "message_index": message_index,
                "template": "assistant_favorite",
                "explicit_save": True,
            },
        }
        prepared = self.ingestion_engine.prepare_candidates(project_id, [candidate], 1)
        return self.repository.upsert_memory_candidate(prepared[0]) if prepared else None

    def _after_chat_turn(self, project_id: str, chat_id: str, user_text: str, assistant_text: str, *, source: str) -> None:
        self._update_chat_summaries(project_id, chat_id, user_text, assistant_text)
        mode = self.memory_capture_settings()["chat_memory_mode"]
        if mode == "off":
            return
        # Rules keeper only when the turn already looks durable (avoid chitchat → rule spam).
        verdict = evaluate_chat_turn(user_text, assistant_text, mode=mode)
        if not verdict.keep:
            return
        rule_candidate = self._capture_rules_candidate(project_id, chat_id, user_text, assistant_text, source=source)
        if rule_candidate:
            self._record_keeper_event(project_id, chat_id, "rules_keeper", "candidate_created", f"Created rule candidate: {rule_candidate.get('label')}", {"candidate_id": rule_candidate.get("id")})

    def _chat_turn_worth_memory(self, user_text: str, assistant_text: str) -> bool:
        mode = self.memory_capture_settings()["chat_memory_mode"]
        return evaluate_chat_turn(user_text, assistant_text, mode=mode).keep

    def _chat_turn_memory_title(self, user_text: str) -> str:
        cleaned = " ".join(str(user_text or "").split())
        return f"Chat turn: {cleaned[:72] or 'memory candidate'}"

    def _chat_turn_memory_type(self, user_text: str, assistant_text: str) -> str:
        haystack = f"{user_text} {assistant_text}".lower()
        if any(word in haystack for word in ("decision", "решен", "adr", "architecture", "архитект")):
            return "Decision"
        if any(word in haystack for word in ("requirement", "требован", "must", "долж")):
            return "Requirement"
        if any(word in haystack for word in ("constraint", "огранич", "rule", "правил")):
            return "Constraint"
        return "Lesson"

    def _update_chat_summaries(self, project_id: str, chat_id: str, user_text: str, assistant_text: str) -> None:
        old = self.repository.get_chat_context_summary(chat_id, "rolling") or {}
        old_text = str(old.get("summary_text") or "")
        bullet = self._compact_turn_line(user_text, assistant_text)
        parts = [line for line in old_text.splitlines() if line.strip()]
        parts.append(f"- {bullet}")
        summary_text = "\n".join(parts[-12:])
        self.repository.upsert_chat_context_summary({
            "chat_id": chat_id,
            "project_id": project_id,
            "summary_type": "rolling",
            "summary_text": summary_text,
            "covered_until": utc_now(),
        })
        decision = self._decision_log_line(user_text, assistant_text)
        if decision:
            existing = self.repository.get_chat_context_summary(chat_id, "decision_log") or {}
            lines = [line for line in str(existing.get("summary_text") or "").splitlines() if line.strip()]
            if decision not in lines:
                lines.append(decision)
            self.repository.upsert_chat_context_summary({
                "chat_id": chat_id,
                "project_id": project_id,
                "summary_type": "decision_log",
                "summary_text": "\n".join(lines[-30:]),
                "covered_until": utc_now(),
            })

    def _capture_rules_candidate(self, project_id: str, chat_id: str, user_text: str, assistant_text: str, *, source: str) -> dict[str, Any] | None:
        haystack = f"{user_text}\n{assistant_text}".lower()
        if not any(word in haystack for word in ("rule", "rules", "constraint", "forbidden", "must", "never", "always", "правил", "огранич", "нельзя", "всегда", "долж")):
            return None
        if len(user_text.strip()) < 20:
            return None
        candidate = {
            "id": stable_id("candidate", project_id, "rules_keeper", chat_id, user_text[:240]),
            "project_id": project_id,
            "source_type": "rules_keeper",
            "source_ref": chat_id,
            "label": f"Rule: {self._chat_turn_memory_title(user_text).replace('Chat turn: ', '')}",
            "type": "Rule",
            "scope": "project",
            "text": f"Rule candidate from chat.\n\nUser:\n{user_text[:1800]}\n\nContext:\n{assistant_text[:1200]}",
            "confidence": 0.7,
            "metadata": {"chat_id": chat_id, "template": "rules_keeper", "source": source},
        }
        prepared = self.ingestion_engine.prepare_candidates(project_id, [candidate], 1)
        return self.repository.upsert_memory_candidate(prepared[0]) if prepared else None

    def _record_keeper_event(self, project_id: str, chat_id: str, kind: str, status: str, message: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.repository.add_keeper_event({
            "project_id": project_id,
            "chat_id": chat_id,
            "kind": kind,
            "status": status,
            "message": message,
            "metadata": dict(metadata or {}),
            "created_at": utc_now(),
        })

    def _compact_turn_line(self, user_text: str, assistant_text: str) -> str:
        user = " ".join(str(user_text or "").split())[:180]
        assistant = " ".join(str(assistant_text or "").split())[:180]
        return f"User asked: {user}. Assistant answered: {assistant}"

    def _decision_log_line(self, user_text: str, assistant_text: str) -> str:
        text = f"{user_text} {assistant_text}".lower()
        if not any(word in text for word in ("decision", "decided", "решен", "choose", "выбира", "долж", "must", "rule", "constraint", "огранич")):
            return ""
        source = " ".join(str(user_text or assistant_text or "").split())[:220]
        return f"- {utc_now()}: {source}"

    @staticmethod
    def _nearest_user_message_before(messages: list[dict[str, Any]], index: int) -> str:
        for item in reversed(messages[:index]):
            if item.get("role") == "user":
                return str(item.get("text") or "")
        return ""

    def chat_context_pack(self, chat_id: str, query: str = "", project_id: str | None = None) -> dict[str, Any]:
        chat = self.repository.get_chat(chat_id)
        if not chat:
            raise ValueError("chat not found")
        actual_project_id = str(chat.get("project_id") or project_id or "architectos")
        q = str(query or "").strip()
        if not q:
            messages = [m for m in chat.get("messages") or [] if str(m.get("text") or "").strip()]
            q = str(messages[-1].get("text") or "") if messages else str(chat.get("title") or "")
        summaries = self.repository.list_chat_context_summaries(chat_id)
        candidates = self.repository.list_memory_candidates(actual_project_id, "candidate", 20)
        relevant = self.search_memory(q or "memory", project_id=actual_project_id, limit=8, refresh=False)["hits"] if q else []
        events = self.repository.list_keeper_events(actual_project_id, chat_id, 20)
        return {
            "chat": chat,
            "summaries": summaries,
            "rolling_summary": next((s for s in summaries if s.get("summary_type") == "rolling"), None),
            "decision_log": next((s for s in summaries if s.get("summary_type") == "decision_log"), None),
            "relevant_memory": relevant,
            "pending_candidates": [c for c in candidates if str(c.get("source_ref") or "") == chat_id or str((c.get("metadata") or {}).get("chat_id") or "") == chat_id],
            "keeper_events": events,
        }

    def favorite_chat_message(self, chat_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        chat = self.repository.get_chat(chat_id)
        if not chat:
            raise ValueError("chat not found")
        raw_index: Any = payload.get("message_index")
        index = int(raw_index)
        messages = list(chat.get("messages") or [])
        if index < 0 or index >= len(messages):
            raise ValueError("message_index out of range")
        message = dict(messages[index])
        if message.get("role") != "assistant":
            raise ValueError("only assistant messages can be favorited into memory")
        favorite = bool(payload.get("favorite", True))
        message["favorite"] = favorite
        messages[index] = message
        chat["messages"] = messages
        chat = self.repository.upsert_chat(chat)
        candidate = None
        if favorite:
            user_text = self._nearest_user_message_before(messages, index)
            candidate = self._capture_favorite_message_candidate(str(chat.get("project_id") or "architectos"), chat_id, index, user_text, str(message.get("text") or ""))
        self._record_keeper_event(str(chat.get("project_id") or "architectos"), chat_id, "favorite", "candidate_created" if candidate else "updated", "Assistant favorite updated.", {"message_index": index, "candidate_id": (candidate or {}).get("id")})
        return {"chat": chat, "candidate": candidate}

    def retry_chat_keeper(self, chat_id: str) -> dict[str, Any]:
        return self.finalize_chat_session(chat_id, force=True, trigger="manual_retry")
