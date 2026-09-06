"""Tool-augmented AI runtime: single-provider run/stream and provider-run tracking.

Extracted from ``service.py``. Covers the tool-augmented single-provider paths
(``run_ai``/``stream_ai``/``_run_ai_with_tools``), request assembly
(``_provider_request`` and its retrieval/tool helpers), chat streaming, and the
provider-run tracking + routing helpers (``_start_provider_run``/
``_finish_provider_run``/``_route_response``/``_lookup_provider``). Depends only
on leaf modules; the repository, providers, tool gateway and embedding engine
are reached through ``self`` via the MRO on :class:`ArchitectOSService`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from .adapters import ProviderRequest
from .ask_advice import (
    ask_memory_advice_enabled,
    build_advice_judge_prompt,
    collect_advice_candidates,
    format_advice_context_block,
    merge_advice_into_structured,
    parse_advice_verdict,
    should_skip_advice_turn,
)
from .models import stable_id, utc_now
from .permissions import PERMISSION_WAIT_SECONDS
from .rich_response import RESPONSE_FORMAT_POLICY, split_rich_response
from .routing import classify_role
from .tool_gateway import (
    MAX_TOOL_ROUNDS,
    compact_tool_trace_label,
    format_tool_results_for_prompt,
    parse_tool_calls,
    strip_tool_call_json,
    tool_action_label,
)
from .usage import usage_from_result

_LOG = logging.getLogger("architectos.service")


class AiRuntimeServiceMixin:
    """Tool-augmented single-provider run/stream paths and provider-run tracking."""

    def run_ai(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id, message, context, request = self._provider_request(payload)
        self._sync_router_settings()
        provider_id = self._resolve_ai_provider_id(payload)
        include_writes = bool(payload.get("allow_cli") or payload.get("cli_approved") or payload.get("approved") or payload.get("allow_fs_writes"))
        memory_only = str(payload.get("ask_mode") or "").strip().lower() == "memory"
        tools_on = self._tools_enabled_for_payload(payload, include_writes=include_writes)
        if tools_on:
            result = {}
            tool_trace = []
            for event in self._run_ai_with_tools(
                project_id, message, context, request, provider_id,
                include_writes=include_writes, memory_only=memory_only,
            ):
                if event.get("type") == "result":
                    result = event["result"]
                    tool_trace = event["tool_trace"]
        else:
            result = self.provider_router.route(self.repository.list_providers(), request, provider_id)
            tool_trace = []
        result = self._stamp_token_economy(result, context)
        response = self._route_response(project_id, message, context["context"], result)
        run = self._start_provider_run(request.run_id, project_id, message, result)
        self._finish_provider_run(run["id"], str(result.get("status") or "unknown"), result)
        response["run_id"] = run["id"]
        response["memory_hit_ids"] = self._hit_ids_from_context(context)
        response["retrieval"] = context.get("retrieval") or {}
        response["advice"] = list(context.get("advice") or [])
        if tool_trace:
            response["tool_trace"] = tool_trace
            response["tool_trace_label"] = compact_tool_trace_label(tool_trace)
        return response

    def stream_ai(self, payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
        project_id = str(payload.get("project_id") or "architectos")
        raw_message = str(payload.get("message") or payload.get("query") or "").strip()
        run_id = str(payload.get("run_id") or stable_id("run", project_id, raw_message[:80], utc_now()))
        # Emit start immediately so Ask can render a live activity trace while context builds.
        yield {
            "type": "start",
            "run_id": run_id,
            "project_id": project_id,
            "message": raw_message,
            "provider": {
                "id": payload.get("provider_id") or "auto",
                "requested_id": payload.get("provider_id") or "auto",
                "selected": {},
                "status": "preparing",
            },
        }
        yield {
            "type": "progress",
            "phase": "context",
            "status": "Searching project memory…",
            "step_id": "prep:context",
            "kind": "status",
        }
        prepared = dict(payload)
        prepared["run_id"] = run_id
        ui = dict(self.repository.get_setting("ui") or {})
        advice_on = ask_memory_advice_enabled(ui)
        role_hint = str(prepared.get("role") or "").strip().lower()
        if (
            advice_on
            and "advice" not in prepared
            and role_hint not in {"council", "review"}
            and str(prepared.get("provider_id") or "").strip().lower() != "local-memory"
        ):
            yield {
                "type": "progress",
                "phase": "advice",
                "status": "Checking tickets, meetings, and decisions…",
                "step_id": "prep:advice",
                "kind": "status",
            }
        project_id, message, context, request = self._provider_request(prepared)
        hit_ids = self._hit_ids_from_context(context)
        hit_count = len(hit_ids)
        yield {
            "type": "progress",
            "phase": "context_done",
            "status": f"Loaded {hit_count} memory hit{'s' if hit_count != 1 else ''}" if hit_count else "No memory hits",
            "step_id": "prep:context",
            "ok": True,
            "kind": "status",
            "memory_hit_ids": hit_ids,
        }
        if context.get("advice_checked"):
            advice_n = len(context.get("advice") or [])
            yield {
                "type": "progress",
                "phase": "advice_done",
                "status": (
                    f"Memory advice: {advice_n} finding{'s' if advice_n != 1 else ''}"
                    if advice_n
                    else "Memory advice: no conflicts"
                ),
                "step_id": "prep:advice",
                "ok": True,
                "kind": "status",
                "advice": list(context.get("advice") or []),
            }
        self._sync_router_settings()
        provider_id = self._resolve_ai_provider_id(prepared)
        planned_provider = self._lookup_provider(provider_id)
        if planned_provider:
            provider_payload = self._provider_stream_payload(planned_provider, provider_id, status="streaming")
            yield {
                "type": "progress",
                "phase": "provider",
                "status": f"Using {provider_payload['selected']['label']}",
                "step_id": "prep:provider",
                "kind": "status",
                "ok": True,
                "provider": provider_payload,
            }
            yield {
                "type": "start",
                "run_id": run_id,
                "project_id": project_id,
                "message": message,
                "provider": provider_payload,
            }
        elif str(provider_id or "auto") == "auto":
            auto_payload = self._provider_stream_payload(
                {"id": "auto", "label": "Auto", "model": ""},
                "auto",
                status="routing",
            )
            yield {
                "type": "progress",
                "phase": "provider",
                "status": "Routing to best available model…",
                "step_id": "prep:provider",
                "kind": "status",
                "ok": True,
                "provider": auto_payload,
            }
            yield {
                "type": "start",
                "run_id": run_id,
                "project_id": project_id,
                "message": message,
                "provider": auto_payload,
            }
        include_writes = bool(prepared.get("allow_cli") or prepared.get("cli_approved") or prepared.get("approved") or prepared.get("allow_fs_writes"))
        memory_only = str(prepared.get("ask_mode") or "").strip().lower() == "memory"
        tools_on = self._tools_enabled_for_payload(prepared, include_writes=include_writes)
        # When tools are enabled, resolve TOOL_CALL rounds non-stream first, then stream only the final prose pass.
        if tools_on:
            result: dict[str, Any] = {}
            tool_trace: list[dict[str, Any]] = []
            for event in self._run_ai_with_tools(
                project_id, message, context, request, provider_id,
                include_writes=include_writes, memory_only=memory_only,
                interactive=True, chat_id=str(prepared.get("chat_id") or ""),
            ):
                if event.get("type") in {"progress", "permission"}:
                    yield event
                elif event.get("type") == "result":
                    result = event["result"]
                    tool_trace = event["tool_trace"]

            selected = dict(result.get("selected_provider") or {})
            self._start_provider_run(run_id, project_id, message, {"provider_id": result.get("provider_id") or provider_id, "selected_provider": selected, "status": "running", "raw": {"approved": request.approved}})

            final_text = str(result.get("text") or "")
            if final_text:
                yield {"type": "delta", "text": final_text}
            result = self._stamp_token_economy(result, context)
            response = self._route_response(project_id, message, context["context"], result)
            response["run_id"] = run_id
            response["memory_hit_ids"] = hit_ids
            response["retrieval"] = context.get("retrieval") or {}
            response["advice"] = list(context.get("advice") or [])
            if tool_trace:
                response["tool_trace"] = tool_trace
                response["tool_trace_label"] = compact_tool_trace_label(tool_trace)
            self._finish_provider_run(run_id, str(result.get("status") or "unknown"), result)
            yield {"type": "done", "result": response}
            return

        audit_started = False
        for event in self.provider_router.stream(self.repository.list_providers(), request, provider_id):
            if event.get("type") == "start":
                selected = dict(event.get("selected_provider") or {})
                self._start_provider_run(run_id, project_id, message, {"provider_id": event.get("provider_id"), "selected_provider": selected, "status": "running", "raw": {"approved": request.approved}})
                audit_started = True
                yield {
                    "type": "progress",
                    "phase": "thinking",
                    "status": f"Calling {selected.get('label') or event.get('provider_id') or 'model'}…",
                    "step_id": "prep:model",
                    "kind": "status",
                    "provider": self._provider_stream_payload(selected, str(event.get("provider_id") or provider_id or "")),
                }
                yield {
                    "type": "start",
                    "run_id": run_id,
                    "project_id": project_id,
                    "message": message,
                    "context": context["context"],
                    "provider": self._provider_stream_payload(
                        selected,
                        str(event.get("provider_id") or provider_id or ""),
                        status="streaming",
                    ),
                }
            elif event.get("type") == "done":
                result = self._stamp_token_economy(dict(event.get("result") or {}), context)
                response = self._route_response(project_id, message, context["context"], result)
                response["run_id"] = run_id
                response["memory_hit_ids"] = hit_ids
                response["retrieval"] = context.get("retrieval") or {}
                response["advice"] = list(context.get("advice") or [])
                if audit_started:
                    self._finish_provider_run(run_id, str(result.get("status") or "unknown"), result)
                yield {
                    "type": "progress",
                    "phase": "thinking_done",
                    "status": "Answer ready",
                    "step_id": "prep:model",
                    "ok": True,
                    "kind": "status",
                }
                yield {"type": "done", "result": response}
            else:
                yield event

    def stream_chat_message(self, payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
        project_id = str(payload.get("project_id") or "architectos")
        raw_message = str(payload.get("message") or "").strip()
        message_security = self.security_policy.inspect_text(raw_message)
        message = str(message_security["text"])
        if not message:
            raise ValueError("message is required")
        chat = self.repository.get_chat(str(payload.get("chat_id") or "")) if payload.get("chat_id") else None
        if not chat:
            now = utc_now()
            chat = {"id": stable_id("chat", project_id, message[:60], now), "project_id": project_id, "title": message[:60] or "New dialog", "messages": [], "favorite": False, "created_at": now, "last_activity_at": now, "session_status": "active", "session_revision": 1}
        chat = self._activate_chat_session(chat)
        user_message: dict[str, Any] = {"role": "user", "text": message, "created_at": utc_now()}
        if message_security["redacted"]:
            user_message["security"] = {"redacted": True, "findings": message_security["findings"]}
        chat["messages"].append(user_message)
        response_chunks: list[str] = []
        final_result: dict[str, Any] | None = None
        stream_payload = {
            "project_id": project_id,
            "chat_id": chat["id"],
            "message": message,
            "provider_id": payload.get("provider_id") or "auto",
            "limit": payload.get("limit") or 8,
            "allow_cli": payload.get("allow_cli"),
            "allow_fs_writes": payload.get("allow_fs_writes"),
            "attachments": payload.get("attachments"),
            "ask_mode": payload.get("ask_mode"),
            "enable_tools": payload.get("enable_tools"),
        }
        if "advice" in payload:
            stream_payload["advice"] = payload.get("advice")
        for event in self.stream_ai(stream_payload):
            if event.get("type") == "delta":
                response_chunks.append(str(event.get("text") or ""))
                yield event
            elif event.get("type") == "start":
                yield event
            elif event.get("type") == "progress":
                yield event
            elif event.get("type") == "done":
                final_result = dict(event.get("result") or {})
        if final_result is None:
            final_result = {"text": "".join(response_chunks), "context": "", "provider": {"id": "unknown", "status": "error"}}
        response = str(final_result.get("text") or "".join(response_chunks))
        display_text, structured = split_rich_response(response)
        structured = merge_advice_into_structured(structured, final_result.get("advice"))
        security = dict(final_result.get("security") or {})
        if message_security["redacted"]:
            security["redacted"] = True
            security["message_redacted"] = True
            security["message_findings"] = message_security["findings"]
        chat["messages"].append({
            "role": "assistant",
            "text": display_text or response,
            "raw_text": response,
            "structured": structured,
            "created_at": utc_now(),
            "context": final_result.get("context") or "",
            "provider": final_result.get("provider") or {},
            "usage": final_result.get("usage") or {},
            "token_economy": final_result.get("token_economy") or {},
            "tool_trace": final_result.get("tool_trace") or [],
            "tool_trace_label": final_result.get("tool_trace_label") or "",
            "memory_hit_ids": list(final_result.get("memory_hit_ids") or []),
            "run_id": final_result.get("run_id") or "",
            "query": message,
            "security": security,
        })
        chat["last_activity_at"] = utc_now()
        chat = self.repository.upsert_chat(chat)
        if bool(payload.get("remember", False)):
            self.add_memory({"project_id": project_id, "type": "Lesson", "label": f"Dialog note: {message[:48]}", "scope": "project", "text": f"User asked: {message}. Local response: {response[:300]}", "source": "chat"})
        yield {
            "type": "done",
            "run_id": final_result.get("run_id") or "",
            "chat": chat,
            "response": display_text or response,
            "raw_response": response,
            "structured": structured,
            "context": final_result.get("context") or "",
            "provider": final_result.get("provider") or {},
            "usage": final_result.get("usage") or {},
            "token_economy": final_result.get("token_economy") or {},
            "raw": final_result.get("raw"),
            "tool_trace": final_result.get("tool_trace") or [],
            "tool_trace_label": final_result.get("tool_trace_label") or "",
            "memory_hit_ids": list(final_result.get("memory_hit_ids") or []),
            "advice": list(final_result.get("advice") or []),
            "security": security,
        }

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        if not run_id:
            raise ValueError("run id is required")
        with self._cancelled_runs_lock:
            self.cancelled_runs.add(run_id)
        if hasattr(self, "permissions"):
            self.permissions.deny_run(run_id)
        existing = next((run for run in self.repository.list_provider_runs(limit=100) if run["id"] == run_id), None)
        if existing:
            existing["status"] = "cancel_requested"
            existing["cancel_requested_at"] = utc_now()
            self.repository.upsert_provider_run(existing)
        return {"run_id": run_id, "cancel_requested": True}

    def decide_run_permission(self, run_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        request_id = str(payload.get("request_id") or "").strip()
        if not request_id:
            raise ValueError("request_id is required")
        if str(run_id or "").strip() and hasattr(self, "permissions"):
            self.permissions.bind_run(str(run_id), chat_id=str(payload.get("chat_id") or ""))
        allow = bool(payload.get("allow") or payload.get("approved"))
        scope = str(payload.get("scope") or "once").strip().lower()
        if scope not in {"once", "session", "run"}:
            scope = "once"
        decision = self.permissions.decide(request_id, allow=allow, scope=scope)
        return {"run_id": run_id, "request_id": request_id, **decision}

    def _is_run_cancelled(self, run_id: str) -> bool:
        with self._cancelled_runs_lock:
            return run_id in self.cancelled_runs

    def provider_runs(self, project_id: str | None = None, limit: int = 25) -> dict[str, Any]:
        return {"runs": self.repository.list_provider_runs(project_id, limit)}

    def _summarize_older_messages(self, messages: list[dict[str, Any]]) -> str:
        log_lines = []
        for msg in messages:
            role = "User" if msg.get("role") == "user" else "Assistant"
            text = str(msg.get("text") or "").strip()
            log_lines.append(f"{role}: {text[:1000]}")
        log_text = "\n".join(log_lines)
        
        prompt = (
            "Write a very brief, single-sentence or single-paragraph summary of the key topics, decisions, "
            "and user requests discussed in this conversation segment. Be concise and dense:\n\n"
            f"{log_text}"
        )
        try:
            res = self.run_ai({
                "message": prompt,
                "provider_id": "auto",
                "role": "review",
                "limit": 1,
            })
            summary = str(res.get("text") or "").strip()
            if summary:
                return f"[Summary of older conversation: {summary}]"
        except Exception:
            pass
        return "[Older conversation history omitted]"

    def _format_and_compress_chat_history(self, messages: list[dict[str, Any]], max_chars: int = 4000) -> str:
        if not messages:
            return ""
        formatted = []
        for msg in messages:
            role = "User" if msg.get("role") == "user" else "Assistant"
            text = str(msg.get("text") or "").strip()
            formatted.append((role, text))
            
        total_len = sum(len(text) for _, text in formatted)
        if total_len <= max_chars:
            lines = [f"{role}: {text}" for role, text in formatted]
            return "Conversation history:\n" + "\n".join(lines)
            
        keep_full_count = 4
        older_messages = formatted[:-keep_full_count]
        recent_messages = formatted[-keep_full_count:]
        
        compressed_lines = []
        if older_messages:
            older_struct = [{"role": "user" if r == "User" else "assistant", "text": t} for r, t in older_messages]
            summary = self._summarize_older_messages(older_struct)
            compressed_lines.append(summary)
                
        for role, text in recent_messages:
            compressed_lines.append(f"{role}: {text}")
            
        return "Conversation history:\n" + "\n".join(compressed_lines)

    def _provider_request(self, payload: dict[str, Any]) -> tuple[str, str, dict[str, Any], ProviderRequest]:
        project_id = str(payload.get("project_id") or "architectos")
        raw_message = str(payload.get("message") or payload.get("query") or "").strip()
        inspected_message = self.security_policy.inspect_text(raw_message)
        message = str(inspected_message["text"])
        if not message:
            raise ValueError("message is required")
        include_writes = bool(payload.get("allow_cli") or payload.get("cli_approved") or payload.get("approved") or payload.get("allow_fs_writes"))
        memory_only = str(payload.get("ask_mode") or "").strip().lower() == "memory"
        tools_on = self._tools_enabled_for_payload(payload, include_writes=include_writes)

        chat_id = str(payload.get("chat_id") or "").strip()
        # Prevent infinite recursion when calling run_ai inside _summarize_older_messages
        if payload.get("role") == "review":
            chat_id = ""

        history_text = ""
        past_messages: list[dict[str, Any]] = []
        if chat_id:
            chat = self.repository.get_chat(chat_id)
            if chat and chat.get("messages"):
                messages = list(chat.get("messages") or [])
                if messages and messages[-1]["role"] == "user" and messages[-1]["text"].strip() == message:
                    past_messages = messages[:-1]
                else:
                    past_messages = messages
                if past_messages:
                    history_text = self._format_and_compress_chat_history(past_messages)

        retrieval_query = self._retrieval_query_for_message(message, past_messages)
        context = self.context(retrieval_query, project_id=project_id, limit=int(payload.get("limit") or 8), tools_available=tools_on)
        context_text, context_redacted = self.security_policy.redact_text(context["context"])
        context = dict(context)
        context["context"] = context_text
        context["retrieval_query"] = retrieval_query
        self._apply_ask_advice(payload, message, context, project_id=project_id)
        if history_text:
            context["context"] = f"{history_text}\n\n{context['context']}".strip()
        attachments = [str(item) for item in (payload.get("attachments") or []) if str(item).strip()]
        images: list[dict[str, Any]] = []
        if attachments:
            index = {str(item.get("id")): item for item in self.files.list_files(project_id)}
            image_ids = [fid for fid in attachments if self.files.is_image(index.get(fid, {}))]
            text_ids = [fid for fid in attachments if fid not in image_ids]
            files_text = self.files.context_for(project_id, text_ids)
            if files_text:
                clean_files, files_redacted = self.security_policy.redact_text(files_text)
                context["context"] = f"{context['context']}\n\nAttached Files:\n{clean_files}".strip()
                context_redacted = context_redacted or files_redacted
            images = self.files.image_payload(project_id, image_ids)
            if images:
                names = ", ".join(str(img.get("name") or "image") for img in images)
                context["context"] = f"{context['context']}\n\nAttached Images (sent to vision-capable models): {names}".strip()
        tool_schemas: list[dict[str, Any]] = []
        if tools_on:
            tool_section = self.tool_gateway.tool_prompt_section(include_writes=include_writes, memory_only=memory_only)
            if tool_section:
                context["context"] = f"{context['context']}\n\n{tool_section}".strip()
            # Providers with native tool calling get the schemas too; the text protocol above
            # stays for CLI/local providers that only see the prompt.
            tool_schemas = self.tool_gateway.tool_schemas(include_writes=include_writes, memory_only=memory_only)
        elif str(payload.get("ask_mode") or "").strip().lower() != "review":
            # Even without tools, ask the model for Markdown / actions / mermaid.
            context["context"] = f"{context['context']}\n\n{RESPONSE_FORMAT_POLICY}".strip()
        if inspected_message["redacted"] or context_redacted:
            context["security"] = {"redacted": True, "message_findings": inspected_message["findings"], "context_redacted": context_redacted}
        run_id = str(payload.get("run_id") or stable_id("run", project_id, message[:80], utc_now()))
        approved = bool(payload.get("allow_cli") or payload.get("cli_approved") or payload.get("approved"))
        role = str(payload.get("role") or "") or classify_role(message)
        request = ProviderRequest(message=message, context=context["context"], project_id=project_id, run_id=run_id, approved=approved, role=role, images=images, tools=tool_schemas, cancel_requested=lambda: self._is_run_cancelled(run_id))
        return project_id, message, context, request

    @staticmethod
    def _is_thin_followup_message(message: str) -> bool:
        text = str(message or "").strip()
        if not text:
            return True
        from .embeddings import content_tokens
        content = content_tokens(text)
        # After stopword stripping, pure follow-ups like "подтяни данные" have no content terms.
        if not content:
            return True
        thin_content = {
            "details", "detail", "more", "continue", "full", "yes", "ok", "дальше", "продолжи",
            "продолжай", "полностью", "да", "ок",
        }
        return len(text) <= 64 and all(token in thin_content for token in content)

    def _retrieval_query_for_message(self, message: str, past_messages: list[dict[str, Any]] | None = None) -> str:
        """Build the memory-search query, blending prior user turns for short follow-ups."""
        current = str(message or "").strip()
        if not self._is_thin_followup_message(current):
            return current
        priors: list[str] = []
        for item in reversed(list(past_messages or [])):
            if str(item.get("role") or "") != "user":
                continue
            text = str(item.get("text") or "").strip()
            if not text or self._is_thin_followup_message(text):
                continue
            priors.append(text)
            if len(priors) >= 2:
                break
        if not priors:
            return current
        blended = " | ".join([*reversed(priors), current])
        return blended[:500]

    def _apply_ask_advice(
        self,
        payload: dict[str, Any],
        message: str,
        context: dict[str, Any],
        *,
        project_id: str,
    ) -> None:
        """Optionally run the memory-advice judge and inject findings into context."""
        ui = dict(self.repository.get_setting("ui") or {})
        enabled = ask_memory_advice_enabled(ui)
        precomputed = payload.get("advice") if isinstance(payload.get("advice"), list) else None
        if "advice" in payload and precomputed is None:
            # Explicit empty / non-list: treat as already handled (council reuse).
            precomputed = []
        role = str(payload.get("role") or "").strip().lower()
        provider_id = str(payload.get("provider_id") or "").strip()
        if should_skip_advice_turn(
            message=message,
            role=role,
            provider_id=provider_id,
            precomputed=precomputed,
            enabled=enabled,
        ):
            advice = list(precomputed or [])
            context["advice"] = advice
            context["advice_checked"] = precomputed is not None and enabled
            if advice:
                block = format_advice_context_block(advice)
                if block:
                    context["context"] = f"{context['context']}\n\n{block}".strip()
            return

        candidates = collect_advice_candidates(
            context.get("hits") if isinstance(context.get("hits"), list) else [],
            context.get("stable_hits") if isinstance(context.get("stable_hits"), list) else [],
            context.get("rule_layer_hits") if isinstance(context.get("rule_layer_hits"), list) else [],
        )
        if not candidates:
            context["advice"] = []
            context["advice_checked"] = True
            return

        advice = self._run_ask_advice_judge(message, candidates, project_id=project_id, provider_id=provider_id or None)
        context["advice"] = advice
        context["advice_checked"] = True
        if advice:
            block = format_advice_context_block(advice)
            if block:
                context["context"] = f"{context['context']}\n\n{block}".strip()

    def _run_ask_advice_judge(
        self,
        message: str,
        candidates: list[dict[str, Any]],
        *,
        project_id: str,
        provider_id: str | None = None,
    ) -> list[dict[str, Any]]:
        prompt = build_advice_judge_prompt(message, candidates)
        request = ProviderRequest(
            message=prompt,
            context="",
            project_id=project_id,
            role="review",
        )
        try:
            result = self.provider_router.route(
                self.repository.list_providers(),
                request,
                provider_id if provider_id and provider_id not in {"", "auto", "local-memory"} else "auto",
            )
        except Exception as exc:  # noqa: BLE001 - advice is best-effort
            _LOG.warning("ask memory advice judge failed: %s", exc)
            return []
        text = str(result.get("text") or "")
        if not text.strip():
            return []
        return parse_advice_verdict(text, candidates=candidates)

    def _tools_enabled_for_payload(self, payload: dict[str, Any], *, include_writes: bool = False) -> bool:
        if payload.get("enable_tools") is False:
            return False
        ask_mode = str(payload.get("ask_mode") or "").strip().lower()
        # Pure Memory mode: only memory_get (no MCP boards/fs).
        if ask_mode == "memory":
            return self.tool_gateway.tools_available(include_writes=False, memory_only=True)
        if payload.get("enable_tools") is True or ask_mode in {"", "quick", "multi", "memory-mcp", "memory_mcp"}:
            return self.tool_gateway.tools_available(include_writes=include_writes)
        return self.tool_gateway.tools_available(include_writes=include_writes)

    def _resolve_ai_provider_id(self, payload: dict[str, Any]) -> str:
        provider_id = str(payload.get("provider_id") or "auto").strip() or "auto"
        ask_mode = str(payload.get("ask_mode") or "").strip().lower()
        include_writes = bool(payload.get("allow_cli") or payload.get("cli_approved") or payload.get("approved") or payload.get("allow_fs_writes"))
        tools_on = self._tools_enabled_for_payload(payload, include_writes=include_writes)
        if ask_mode == "memory":
            # memory_get needs a model that can emit tool_calls; use auto when tools are on.
            if tools_on:
                return "auto" if provider_id in {"", "local-memory", "auto"} else provider_id
            return "local-memory"
        if ask_mode in {"memory-mcp", "memory_mcp"}:
            # local-memory cannot emit TOOL_CALL JSON — use auto when tools are live.
            if tools_on:
                return "auto" if provider_id in {"", "local-memory", "auto"} else provider_id
            return "local-memory"
        if tools_on and provider_id == "local-memory":
            return "auto"
        return provider_id

    def _run_ai_with_tools(
        self,
        project_id: str,
        message: str,
        context: dict[str, Any],
        request: ProviderRequest,
        provider_id: str,
        *,
        include_writes: bool = False,
        memory_only: bool = False,
        interactive: bool = False,
        chat_id: str = "",
    ) -> Iterator[dict[str, Any]]:
        tool_trace: list[dict[str, Any]] = []
        run_step_seq = 0
        tool_schemas = list(request.tools or [])
        working = ProviderRequest(
            message=request.message,
            context=request.context,
            project_id=request.project_id,
            run_id=request.run_id,
            approved=request.approved,
            role=request.role,
            images=list(request.images or []),
            tools=tool_schemas,
            cancel_requested=request.cancel_requested,
        )
        result: dict[str, Any] = {}
        writes_allowed = bool(include_writes)
        if hasattr(self, "permissions"):
            self.permissions.bind_run(str(working.run_id or ""), chat_id=str(chat_id or ""))
        for round_idx in range(MAX_TOOL_ROUNDS + 1):
            if working.cancel_requested and working.cancel_requested():
                break
            force_final = round_idx >= MAX_TOOL_ROUNDS
            think_id = f"think:{round_idx}"
            planned = self._lookup_provider(provider_id)
            planned_label = ""
            if planned:
                planned_label = self._provider_stream_payload(planned, provider_id)["selected"]["label"]
            thinking_status = "Composing final answer…" if force_final else ("Thinking…" if round_idx == 0 else "Continuing with tool results…")
            if planned_label:
                thinking_status = f"{thinking_status} · {planned_label}"
            yield {
                "type": "progress",
                "phase": "thinking",
                "status": thinking_status,
                "step_id": think_id,
                "kind": "status",
                "round": round_idx + 1,
                "provider": self._provider_stream_payload(planned, provider_id) if planned else None,
            }
            if force_final:
                working = ProviderRequest(
                    message=(
                        f"{message}\n\n"
                        "No more lookups are available this turn. Answer the user now using memory and the TOOL_RESULT "
                        "blocks already provided. Do not emit tool_calls, do not ask for permission to continue, and do "
                        "not describe what you would read next. State your conclusion, then list in one short line what "
                        "remains unverified."
                    ),
                    context=working.context,
                    project_id=working.project_id,
                    run_id=working.run_id,
                    approved=working.approved,
                    role=working.role,
                    images=list(working.images or []),
                    # No tools on the closing pass: the model must answer from what it already has.
                    tools=[],
                    cancel_requested=working.cancel_requested,
                )
            result = self.provider_router.route(self.repository.list_providers(), working, provider_id)
            selected_provider = dict(result.get("selected_provider") or {})
            provider_payload = self._provider_stream_payload(
                selected_provider,
                str(result.get("provider_id") or provider_id or ""),
                status="streaming",
            )
            yield {
                "type": "progress",
                "phase": "provider",
                "status": f"Using {provider_payload['selected']['label']}",
                "step_id": "prep:provider",
                "kind": "status",
                "ok": True,
                "provider": provider_payload,
                "round": round_idx + 1,
            }
            text = str(result.get("text") or "")
            calls = [] if force_final else parse_tool_calls(text)
            if not calls:
                cleaned = strip_tool_call_json(text)
                if cleaned != text.strip():
                    result = dict(result)
                    result["text"] = cleaned or text
                yield {
                    "type": "progress",
                    "phase": "thinking_done",
                    "status": f"Composing answer · {provider_payload['selected']['label']}",
                    "step_id": think_id,
                    "ok": True,
                    "kind": "status",
                    "round": round_idx + 1,
                    "provider": provider_payload,
                }
                break
            yield {
                "type": "progress",
                "phase": "thinking_done",
                "status": f"Planned {len(calls[:5])} lookup{'s' if len(calls[:5]) != 1 else ''} · {provider_payload['selected']['label']}",
                "step_id": think_id,
                "ok": True,
                "kind": "status",
                "round": round_idx + 1,
                "provider": provider_payload,
            }
            round_trace: list[dict[str, Any]] = []
            for call in calls[:5]:
                tool_name = str(call.get("name") or "")
                call_args = dict(call.get("arguments") or {})
                step_id = f"{run_step_seq}"
                run_step_seq += 1
                action_label = tool_action_label(tool_name, call_args)
                yield {
                    "type": "progress",
                    "phase": "tool_start",
                    "status": f"{action_label}…",
                    "tool_name": tool_name,
                    "arguments": call_args,
                    "step_id": step_id,
                    "round": round_idx + 1,
                    "tool_trace": tool_trace + round_trace + [{
                        "step_id": step_id,
                        "name": tool_name,
                        "arguments": call_args,
                        "ok": True,
                        "pending": True,
                        "summary": "Executing...",
                        "error": "",
                        "round": round_idx + 1,
                    }]
                }
                executed = self.tool_gateway.execute(
                    tool_name,
                    call_args,
                    include_writes=writes_allowed,
                    memory_only=memory_only,
                    project_id=project_id,
                )
                attempts = 0
                while executed.get("needs_permission") and attempts < 4:
                    attempts += 1
                    perm = dict(executed.get("permission") or {})
                    kind = str(perm.get("kind") or "sandbox")
                    action = str(perm.get("action") or "read")
                    target = str(perm.get("target") or "")
                    reason = str(perm.get("reason") or executed.get("error") or "Permission required")
                    broker = getattr(self, "permissions", None)
                    if broker is not None and broker.is_allowed(kind, action, target):
                        if kind == "write":
                            writes_allowed = True
                        executed = self.tool_gateway.execute(
                            tool_name,
                            call_args,
                            include_writes=writes_allowed,
                            memory_only=memory_only,
                            project_id=project_id,
                        )
                        continue
                    if not interactive or broker is None:
                        executed = {
                            "ok": False,
                            "name": tool_name,
                            "error": f"{reason} Approve this in Ask to continue.",
                            "summary": "",
                        }
                        break
                    request = broker.create_request(kind, action, target, reason, run_id=str(working.run_id or ""))
                    yield {
                        "type": "progress",
                        "phase": "permission",
                        "status": "Waiting for your permission…",
                        "tool_name": tool_name,
                        "arguments": call_args,
                        "step_id": step_id,
                        "round": round_idx + 1,
                        "kind": "status",
                    }
                    yield {"type": "permission", **request}
                    decision = broker.wait(
                        request["request_id"],
                        timeout=PERMISSION_WAIT_SECONDS,
                        cancel_requested=working.cancel_requested,
                    )
                    if not decision.get("allow"):
                        denied = "Permission request timed out." if decision.get("timed_out") else "User denied permission."
                        executed = {
                            "ok": False,
                            "name": tool_name,
                            "error": f"{denied} {reason}".strip(),
                            "summary": "",
                        }
                        break
                    if kind == "write":
                        writes_allowed = True
                    executed = self.tool_gateway.execute(
                        tool_name,
                        call_args,
                        include_writes=writes_allowed,
                        memory_only=memory_only,
                        project_id=project_id,
                    )
                entry = {
                    "step_id": step_id,
                    "name": executed.get("name") or tool_name,
                    "arguments": call_args,
                    "ok": bool(executed.get("ok")),
                    "pending": False,
                    "summary": executed.get("summary") or "",
                    "error": executed.get("error") or "",
                    "round": round_idx + 1,
                }
                round_trace.append(entry)
                tool_trace.append(entry)
                yield {
                    "type": "progress",
                    "phase": "tool_finish",
                    "status": f"Done: {action_label}" if entry["ok"] else f"Failed: {action_label}",
                    "tool_name": tool_name,
                    "arguments": call_args,
                    "step_id": step_id,
                    "ok": entry["ok"],
                    "round": round_idx + 1,
                    "tool_trace": list(tool_trace)
                }
            working = ProviderRequest(
                message=(
                    f"{message}\n\n"
                    f"{format_tool_results_for_prompt(round_trace)}\n\n"
                    "Continue on your own — do not ask the user for permission and do not announce a plan. "
                    "If you still need data, emit tool_calls JSON now (batch up to 5 related lookups); "
                    "otherwise give the final answer."
                ),
                context=working.context,
                project_id=working.project_id,
                run_id=working.run_id,
                approved=working.approved,
                role=working.role,
                images=list(working.images or []),
                tools=tool_schemas,
                cancel_requested=working.cancel_requested,
            )
        clean = dict(result)
        clean["text"] = strip_tool_call_json(str(clean.get("text") or "")) or str(clean.get("text") or "")
        yield {"type": "result", "result": clean, "tool_trace": tool_trace}

    @staticmethod
    def _hit_ids_from_context(context: dict[str, Any] | None) -> list[str]:
        ids: list[str] = []
        for hit in (context or {}).get("hits") or []:
            if not isinstance(hit, dict):
                continue
            node = hit.get("node") if isinstance(hit.get("node"), dict) else hit
            node_id = str((node or {}).get("id") or "").strip()
            if node_id and node_id not in ids:
                ids.append(node_id)
        return ids

    @staticmethod
    def _provider_stream_payload(provider: dict[str, Any] | None, provider_id: str = "", *, status: str = "streaming") -> dict[str, Any]:
        selected = dict(provider or {})
        pid = str(selected.get("id") or provider_id or "auto")
        label = str(selected.get("label") or pid)
        model = str(selected.get("model") or "").strip()
        display = label
        if model and model.lower() not in label.lower():
            display = f"{label} · {model}"
        return {
            "id": pid,
            "label": label,
            "model": model,
            "status": status,
            "selected": {
                "id": pid,
                "label": display,
                "model": model,
            },
        }

    def _lookup_provider(self, provider_id: str) -> dict[str, Any] | None:
        needle = str(provider_id or "").strip()
        if not needle or needle == "auto":
            return None
        return next((item for item in self.repository.list_providers() if str(item.get("id") or "") == needle), None)

    def _start_provider_run(self, run_id: str, project_id: str, message: str, result: dict[str, Any]) -> dict[str, Any]:
        provider = dict(result.get("selected_provider") or {})
        raw = dict(result.get("raw") or {}) if isinstance(result.get("raw"), dict) else {}
        clean_message = self.security_policy.redact_text(message)[0]
        command, _command_redacted = self.security_policy.redact_payload(raw.get("command") or provider.get("command") or [])
        workdir, _workdir_redacted = self.security_policy.redact_text(str(raw.get("workdir") or provider.get("workdir") or self.project_root))
        return self.repository.upsert_provider_run({
            "id": run_id,
            "project_id": project_id,
            "provider_id": str(result.get("provider_id") or provider.get("id") or ""),
            "provider_label": str(provider.get("label") or provider.get("id") or ""),
            "status": str(result.get("status") or "running"),
            "message_preview": clean_message[:180],
            "command": command,
            "workdir": workdir,
            "approval_required": bool(provider.get("approval_required", provider.get("provider_type") == "cli")),
            "approved": bool(raw.get("approved", False)),
            "started_at": utc_now(),
        })

    def _finish_provider_run(self, run_id: str, status: str, result: dict[str, Any]) -> dict[str, Any] | None:
        runs = self.repository.list_provider_runs(limit=100)
        run = next((item for item in runs if item["id"] == run_id), None)
        if not run:
            return None
        raw = dict(result.get("raw") or {}) if isinstance(result.get("raw"), dict) else {}
        raw_selected: Any = result.get("selected_provider")
        selected = raw_selected if isinstance(raw_selected, dict) else {}
        usage = usage_from_result(result) or (result.get("usage") if isinstance(result.get("usage"), dict) else None)
        run["status"] = status
        run["finished_at"] = utc_now()
        run["returncode"] = raw.get("returncode")
        run["stderr_preview"] = self.security_policy.redact_text(str(raw.get("stderr") or ""))[0][:500]
        run["model"] = str(selected.get("model") or (usage or {}).get("model") or run.get("model") or "")
        if usage:
            run["usage"] = usage
            if usage.get("cost_usd") is not None:
                run["cost_usd"] = usage.get("cost_usd")
        economy = result.get("token_economy") if isinstance(result.get("token_economy"), dict) else None
        if economy:
            run["token_economy"] = economy
        run["selected_provider"] = selected or run.get("selected_provider") or {}
        run["routing"] = result.get("routing") or run.get("routing") or {}
        remember = getattr(self, "remember_provider_runtime_status", None)
        if callable(remember):
            remember(result)
        return self.repository.upsert_provider_run(run)

    def _route_response(self, project_id: str, message: str, context: str, result: dict[str, Any]) -> dict[str, Any]:
        clean_message, message_redacted = self.security_policy.redact_text(message)
        clean_context, context_redacted = self.security_policy.redact_text(context)
        clean_text, text_redacted = self.security_policy.redact_text(str(result.get("text") or ""))
        clean_raw, raw_redacted = self.security_policy.redact_payload(result.get("raw"))
        clean_selected, provider_redacted = self.security_policy.redact_payload(result.get("selected_provider"))
        usage = usage_from_result(result)
        economy = result.get("token_economy") if isinstance(result.get("token_economy"), dict) else {}
        return {
            "project_id": project_id,
            "message": clean_message,
            "context": clean_context,
            "provider": {
                "id": result.get("provider_id"),
                "requested_id": result.get("requested_provider_id"),
                "selected": clean_selected,
                "status": result.get("status"),
                "routing": result.get("routing") or {},
            },
            "routing": result.get("routing") or {},
            "usage": usage or {},
            "token_economy": economy or {},
            "text": clean_text,
            "raw": None if clean_raw is None else clean_raw,
            "security": {
                "redacted": bool(message_redacted or context_redacted or text_redacted or raw_redacted or provider_redacted),
                "message_redacted": message_redacted,
                "context_redacted": context_redacted,
                "result_redacted": text_redacted,
                "raw_redacted": raw_redacted,
            },
        }

    @staticmethod
    def _stamp_token_economy(result: dict[str, Any] | None, context: dict[str, Any] | None) -> dict[str, Any]:
        stamped = dict(result or {})
        economy = context.get("token_economy") if isinstance(context, dict) else None
        if isinstance(economy, dict) and economy:
            stamped["token_economy"] = economy
        return stamped

