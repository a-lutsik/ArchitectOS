"""Task & chat CRUD, extracted from ``service.py``.

``ChatServiceMixin`` owns the lightweight task and chat create/read/update
surface (listing, summaries, enrichment, non-streaming ``post_chat_message``).
Pure mixin: it runs on ``self`` (``repository``, ``security_policy``,
``run_ai``, ``add_memory``, ``_activate_chat_session``) from the concrete
service, so behavior is unchanged.
"""

from __future__ import annotations

from typing import Any

from .models import stable_id, utc_now
from .rich_response import split_rich_response


class ChatServiceMixin:
    """Task/chat CRUD helpers for :class:`ArchitectOSService`."""

    def list_tasks(self, project_id: str | None = None) -> dict[str, Any]:
        return {"tasks": self.repository.list_tasks(project_id)}

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = self.security_policy.redact_text(str(payload.get("title") or "").strip())[0]
        detail = self.security_policy.redact_text(str(payload.get("detail") or ""))[0]
        if not title:
            raise ValueError("task title is required")
        return self.repository.upsert_task({"id": stable_id("task", payload.get("project_id") or "architectos", title, utc_now()), "project_id": str(payload.get("project_id") or "architectos"), "title": title, "status": str(payload.get("status") or "todo"), "priority": str(payload.get("priority") or "medium"), "detail": detail, "linked_memory_ids": list(payload.get("linked_memory_ids") or []), "created_at": utc_now()})

    def update_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if not task:
            raise ValueError("task not found")
        for key in ("title", "status", "priority", "detail"):
            if key in payload:
                value = str(payload[key])
                task[key] = self.security_policy.redact_text(value)[0] if key in {"title", "detail"} else value
        if "linked_memory_ids" in payload:
            task["linked_memory_ids"] = list(payload["linked_memory_ids"] or [])
        return self.repository.upsert_task(task)

    def list_chats(self, project_id: str | None = None, limit: int = 80) -> dict[str, Any]:
        chats = []
        for chat in self.repository.list_chats(project_id)[: max(1, min(int(limit or 80), 500))]:
            chats.append(self._chat_summary(chat, project_id))
        return {"chats": chats, "lightweight": True}

    def get_chat(self, chat_id: str) -> dict[str, Any]:
        chat = self.repository.get_chat(chat_id)
        if not chat:
            raise ValueError("chat not found")
        return self._enrich_chat(chat)

    def _enrich_chat(self, chat: dict[str, Any]) -> dict[str, Any]:
        chat = dict(chat)
        project_id = str(chat.get("project_id") or "architectos")
        chat_id = str(chat.get("id") or "")
        events = self.repository.list_keeper_events(project_id, chat_id, 1)
        if events:
            chat["keeper_status"] = events[0]
        summaries = self.repository.list_chat_context_summaries(chat_id)
        if summaries:
            chat["context_summaries"] = summaries
        return chat

    def _chat_summary(self, chat: dict[str, Any], project_id: str | None = None) -> dict[str, Any]:
        chat = dict(chat)
        messages = list(chat.get("messages") or [])
        last_message = next((message for message in reversed(messages) if str(message.get("text") or "").strip()), {})
        summary = {
            "id": chat.get("id"),
            "project_id": chat.get("project_id") or project_id or "architectos",
            "title": chat.get("title") or "New dialog",
            "favorite": bool(chat.get("favorite")),
            "created_at": chat.get("created_at") or "",
            "updated_at": chat.get("updated_at") or "",
            "message_count": len(messages),
            "last_message_at": last_message.get("created_at") or chat.get("updated_at") or "",
            "preview": str(last_message.get("text") or "")[:240],
            "session_status": chat.get("session_status") or "active",
            "session_revision": int(chat.get("session_revision") or 1),
            "session_finalized_at": chat.get("session_finalized_at") or "",
        }
        enriched = self._enrich_chat({**chat, "messages": []})
        for key in ("keeper_status", "context_summaries"):
            if key in enriched:
                summary[key] = enriched[key]
        return summary

    def create_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title") or "New dialog").strip()
        now = utc_now()
        return self.repository.upsert_chat({"id": stable_id("chat", payload.get("project_id") or "architectos", title, now), "project_id": str(payload.get("project_id") or "architectos"), "title": title, "messages": [], "favorite": False, "created_at": now, "last_activity_at": now, "session_status": "active", "session_revision": 1})

    def post_chat_message(self, payload: dict[str, Any]) -> dict[str, Any]:
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
        user_message = {"role": "user", "text": message, "created_at": utc_now()}
        if message_security["redacted"]:
            user_message["security"] = {"redacted": True, "findings": message_security["findings"]}
        chat["messages"].append(user_message)
        route = self.run_ai({"project_id": project_id, "message": message, "provider_id": payload.get("provider_id") or "auto", "allow_cli": payload.get("allow_cli"), "attachments": payload.get("attachments")})
        response = self.security_policy.redact_text(str(route["text"]))[0]
        display_text, structured = split_rich_response(response)
        chat["messages"].append({
            "role": "assistant",
            "text": display_text or response,
            "raw_text": response,
            "structured": structured,
            "created_at": utc_now(),
            "context": route["context"],
            "provider": route["provider"],
            "usage": route.get("usage") or {},
            "security": route.get("security") or {},
        })
        chat["last_activity_at"] = utc_now()
        chat = self.repository.upsert_chat(chat)
        if bool(payload.get("remember", False)):
            self.add_memory({"project_id": project_id, "type": "Lesson", "label": f"Dialog note: {message[:48]}", "scope": "project", "text": f"User asked: {message}. Local response: {response[:300]}", "source": "chat"})
        return {"chat": chat, "response": display_text or response, "structured": structured, "context": route["context"], "usage": route.get("usage") or {}, "security": route.get("security") or {}}
