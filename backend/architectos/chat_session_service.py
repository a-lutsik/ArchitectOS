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
from datetime import datetime, timezone
from typing import Any

from .chat_memory import evaluate_chat_turn, normalize_chat_memory_mode
from .models import stable_id, utc_now

_LOG = logging.getLogger("architectos.service")


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
        """Close an active chat session and enqueue one factual summary candidate."""
        chat = self.repository.get_chat(chat_id)
        if not chat:
            raise ValueError("chat not found")
        chat = dict(chat)
        actual_project_id = str(chat.get("project_id") or project_id or "architectos")
        life = self.memory_lifecycle.settings()
        mode = normalize_chat_memory_mode(life.get("chat_memory_mode"))
        if mode == "off" and not force and trigger != "manual":
            return {"chat": chat, "candidate": None, "skipped": True, "reason": "chat_memory_mode=off"}

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
                    "facts": list(summary.get("facts") or [])[:12],
                    "chat_memory_mode": mode,
                },
            }
            prepared = self.ingestion_engine.prepare_candidates(actual_project_id, [candidate], 1)
            saved = self.repository.upsert_memory_candidate(prepared[0]) if prepared else None
            if saved:
                promoted = self._maybe_auto_accept_chat_candidate(saved, life)
                if promoted and promoted.get("memory"):
                    self._record_keeper_event(
                        actual_project_id,
                        chat_id,
                        "session_keeper",
                        "auto_accepted",
                        f"Auto-accepted session summary: {saved.get('label')}",
                        {"candidate_id": saved.get("id"), "node_id": (promoted.get("memory") or {}).get("id"), "trigger": trigger},
                    )
                else:
                    self._record_keeper_event(
                        actual_project_id,
                        chat_id,
                        "session_keeper",
                        "candidate_created",
                        f"Created session summary candidate: {saved.get('label')}",
                        {"candidate_id": saved.get("id"), "trigger": trigger, "revision": revision},
                    )
            else:
                self._record_keeper_event(
                    actual_project_id,
                    chat_id,
                    "session_keeper",
                    "skipped",
                    "Skipped session summary: duplicate or low-value candidate.",
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
            return {"chat": chat, "candidate": saved, "skipped": saved is None, "summary": summary, "trigger": trigger}
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
        if normalize_chat_memory_mode(life.get("chat_memory_mode")) == "off":
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
            for fact in list(verdict.facts or [])[:3]:
                text = str(fact.get("text") or "").strip()
                if not text:
                    continue
                facts.append({"text": text, "type": str(fact.get("type") or "Lesson")})
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
        facts = unique_facts[:12]
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

    def _capture_chat_turn_memory_candidate(
        self,
        project_id: str,
        chat_id: str,
        user_text: str,
        assistant_text: str,
        *,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        user_text = str(user_text or "").strip()
        assistant_text = str(assistant_text or "").strip()
        life = self.memory_lifecycle.settings()
        mode = normalize_chat_memory_mode(life.get("chat_memory_mode"))
        verdict = evaluate_chat_turn(user_text, assistant_text, mode=mode)
        if not verdict.keep:
            self._record_keeper_event(
                project_id,
                chat_id,
                source,
                "skipped",
                f"Skipped: {verdict.reason} (mode={mode}, score={verdict.score:.2f}).",
                {**(metadata or {}), "chat_memory_mode": mode, "salience": verdict.score},
            )
            return None

        facts_only = bool(life.get("chat_store_facts_only", True))
        facts = list(verdict.facts or [])
        if facts_only and facts:
            saved_last = None
            for index, fact in enumerate(facts[:3]):
                fact_text = str(fact.get("text") or "").strip()
                if not fact_text:
                    continue
                label_seed = fact_text[:72]
                candidate = {
                    "id": stable_id("candidate", project_id, "chat_fact", chat_id, fact_text[:240], str(index)),
                    "project_id": project_id,
                    "source_type": "chat",
                    "source_ref": chat_id,
                    "label": f"Chat fact: {label_seed}",
                    "type": str(fact.get("type") or self._chat_turn_memory_type(user_text, assistant_text)),
                    "scope": "project",
                    "text": (
                        "Durable fact extracted from chat (not a full transcript).\n\n"
                        f"{fact_text}\n\n"
                        f"Context user prompt: {user_text[:400]}"
                    ).strip(),
                    "confidence": max(0.55, min(0.85, 0.55 + verdict.score * 0.3)),
                    "metadata": {
                        "chat_id": chat_id,
                        "template": "chat_fact_keeper",
                        "source": source,
                        "source_type": "chat",
                        "fact_source": fact.get("source"),
                        "salience": verdict.score,
                        "chat_memory_mode": mode,
                        **dict(metadata or {}),
                    },
                }
                prepared = self.ingestion_engine.prepare_candidates(project_id, [candidate], 1)
                if not prepared:
                    continue
                saved_last = self.repository.upsert_memory_candidate(prepared[0])
                promoted = self._maybe_auto_accept_chat_candidate(saved_last, life)
                if promoted and promoted.get("memory"):
                    self._record_keeper_event(
                        project_id,
                        chat_id,
                        source,
                        "auto_accepted",
                        f"Auto-accepted chat fact into memory: {saved_last.get('label')}",
                        {"candidate_id": saved_last.get("id"), "node_id": (promoted.get("memory") or {}).get("id"), **dict(metadata or {})},
                    )
                    continue
                self._record_keeper_event(
                    project_id,
                    chat_id,
                    source,
                    "candidate_created",
                    f"Created chat fact candidate: {saved_last.get('label')}",
                    {"candidate_id": saved_last.get("id"), "salience": verdict.score, **dict(metadata or {})},
                )
            if not saved_last:
                self._record_keeper_event(project_id, chat_id, source, "skipped", "Skipped: duplicate or low-value fact candidates.", dict(metadata or {}))
            return saved_last

        # Fallback / aggressive raw-turn path (still gated).
        title = self._chat_turn_memory_title(user_text)
        body = (
            "Post-turn chat memory candidate.\n\n"
            f"User:\n{user_text[:1800]}\n\n"
            f"Assistant:\n{assistant_text[:2200]}"
        ).strip()
        candidate = {
            "id": stable_id("candidate", project_id, "chat_turn", chat_id, user_text[:240], assistant_text[:240]),
            "project_id": project_id,
            "source_type": "chat",
            "source_ref": chat_id,
            "label": title,
            "type": self._chat_turn_memory_type(user_text, assistant_text),
            "scope": "project",
            "text": body,
            "confidence": max(0.55, min(0.8, 0.5 + verdict.score * 0.3)),
            "metadata": {
                "chat_id": chat_id,
                "template": "chat_turn_keeper",
                "source": source,
                "source_type": "chat",
                "salience": verdict.score,
                "chat_memory_mode": mode,
                **dict(metadata or {}),
            },
        }
        prepared = self.ingestion_engine.prepare_candidates(project_id, [candidate], 1)
        if not prepared:
            self._record_keeper_event(project_id, chat_id, source, "skipped", "Skipped: duplicate or low-value candidate.", dict(metadata or {}))
            return None
        saved = self.repository.upsert_memory_candidate(prepared[0])
        self._record_keeper_event(project_id, chat_id, source, "candidate_created", f"Created memory candidate: {saved.get('label')}", {"candidate_id": saved.get("id"), **dict(metadata or {})})
        return saved

    CHAT_AUTO_ACCEPT_TYPES = {"Decision", "Constraint", "Rule"}

    def _maybe_auto_accept_chat_candidate(self, candidate: dict[str, Any] | None, settings: dict[str, Any]) -> dict[str, Any] | None:
        """Promote high-confidence durable chat facts straight to memory (opt-in).

        Only facts that look like decisions/constraints/rules with a traceable
        source and confidence ≥ 0.75 qualify; everything else stays in the
        review queue. The promoted node carries metadata.auto_accepted=True.
        """
        if not settings.get("chat_auto_accept"):
            return None
        if not candidate or str(candidate.get("status") or "candidate") != "candidate":
            return None
        if float(candidate.get("confidence") or 0.0) < 0.75:
            return None
        if not str(candidate.get("source_ref") or "").strip():
            return None
        if str(candidate.get("type") or "") not in self.CHAT_AUTO_ACCEPT_TYPES:
            return None
        meta = dict(candidate.get("metadata") or {})
        meta["auto_accepted"] = True
        candidate["metadata"] = meta
        self.repository.update_memory_candidate(candidate)
        try:
            return self.promote_memory_candidate(str(candidate.get("id") or ""))
        except Exception as exc:  # noqa: BLE001 - auto-accept must never break the chat turn
            _LOG.warning("chat auto-accept failed for %s: %s", candidate.get("id"), exc)
            return None

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
        mode = normalize_chat_memory_mode(self.memory_lifecycle.settings().get("chat_memory_mode"))
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
        mode = normalize_chat_memory_mode(self.memory_lifecycle.settings().get("chat_memory_mode"))
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
