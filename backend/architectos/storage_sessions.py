"""Tasks, chats, providers, settings, analytics, and bundle I/O."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

from .models import MemoryEdge, MemoryNode, Project, stable_id, utc_now
from .storage_defaults import _default_provider_command


def _sanitize_payload(value: Any) -> tuple[Any, bool]:
    from .storage import sanitize_payload

    return sanitize_payload(value)


def _strip_sensitive_settings(value: Any) -> Any:
    from .storage import strip_sensitive_settings

    return strip_sensitive_settings(value)


class StorageSessionsMixin:
    def upsert_task(self, task: dict[str, Any]) -> dict[str, Any]:
        task, redacted = _sanitize_payload(dict(task))
        if redacted:
            task["security_redacted"] = True
        task.setdefault("id", stable_id("task", task.get("project_id", "architectos"), task.get("title", ""), utc_now()))
        task.setdefault("project_id", "architectos")
        task.setdefault("status", "todo")
        task.setdefault("priority", "medium")
        task.setdefault("detail", "")
        task.setdefault("linked_memory_ids", [])
        task.setdefault("created_at", utc_now())
        task["updated_at"] = utc_now()
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO tasks(id, project_id, status, priority, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?)", (task["id"], task["project_id"], task["status"], task["priority"], task["updated_at"], json.dumps(task, sort_keys=True)))
        return task

    def list_tasks(self, project_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT payload FROM tasks"
        params: tuple[str, ...] = ()
        if project_id:
            sql += " WHERE project_id = ?"
            params = (project_id,)
        sql += " ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def upsert_chat(self, chat: dict[str, Any]) -> dict[str, Any]:
        chat, redacted = _sanitize_payload(dict(chat))
        if redacted:
            chat["security_redacted"] = True
        chat.setdefault("id", stable_id("chat", chat.get("project_id", "architectos"), chat.get("title", ""), utc_now()))
        chat.setdefault("project_id", "architectos")
        chat.setdefault("title", "New dialog")
        chat.setdefault("messages", [])
        chat.setdefault("favorite", False)
        chat.setdefault("created_at", utc_now())
        chat["updated_at"] = utc_now()
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO chat_sessions(id, project_id, title, favorite, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?)", (chat["id"], chat["project_id"], chat["title"], int(chat["favorite"]), chat["updated_at"], json.dumps(chat, sort_keys=True)))
        return chat

    def list_chats(self, project_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT payload FROM chat_sessions"
        params: tuple[str, ...] = ()
        if project_id:
            sql += " WHERE project_id = ?"
            params = (project_id,)
        sql += " ORDER BY favorite DESC, updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def get_chat(self, chat_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM chat_sessions WHERE id = ?", (chat_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def upsert_chat_context_summary(self, summary: dict[str, Any]) -> dict[str, Any]:
        summary, redacted = _sanitize_payload(dict(summary))
        if redacted:
            summary["security_redacted"] = True
        summary.setdefault("id", stable_id("chat-summary", summary.get("chat_id", ""), summary.get("summary_type", "")))
        summary.setdefault("project_id", "architectos")
        summary.setdefault("summary_type", "rolling")
        summary["updated_at"] = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO chat_context_summaries(id, chat_id, project_id, summary_type, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (summary["id"], summary["chat_id"], summary["project_id"], summary["summary_type"], summary["updated_at"], json.dumps(summary, sort_keys=True)),
            )
        return summary

    def list_chat_context_summaries(self, chat_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM chat_context_summaries WHERE chat_id = ? ORDER BY summary_type",
                (chat_id,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def get_chat_context_summary(self, chat_id: str, summary_type: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM chat_context_summaries WHERE chat_id = ? AND summary_type = ?",
                (chat_id, summary_type),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def add_keeper_event(self, event: dict[str, Any]) -> dict[str, Any]:
        event, redacted = _sanitize_payload(dict(event))
        if redacted:
            event["security_redacted"] = True
        event.setdefault("id", stable_id("keeper-event", event.get("project_id", "architectos"), event.get("chat_id", ""), event.get("kind", ""), utc_now()))
        event.setdefault("project_id", "architectos")
        event.setdefault("chat_id", "")
        event.setdefault("status", "info")
        event.setdefault("created_at", utc_now())
        event["updated_at"] = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO keeper_events(id, project_id, chat_id, status, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (event["id"], event["project_id"], event["chat_id"], event["status"], event["updated_at"], json.dumps(event, sort_keys=True)),
            )
        return event

    def list_keeper_events(self, project_id: str | None = None, chat_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT payload FROM keeper_events"
        params: list[Any] = []
        clauses = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if chat_id:
            clauses.append("chat_id = ?")
            params.append(chat_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def upsert_provider(self, provider: dict[str, Any]) -> dict[str, Any]:
        provider, redacted = _sanitize_payload(dict(provider))
        if redacted:
            provider["security_redacted"] = True
        provider.setdefault("id", stable_id("provider", provider.get("label", "provider")))
        provider.setdefault("label", provider["id"])
        provider.setdefault("provider_type", "custom")
        provider.setdefault("status", "planned")
        provider.setdefault("enabled", False)
        provider.setdefault("model", "")
        provider.setdefault("notes", "")
        provider.setdefault("command", _default_provider_command(str(provider.get("id", ""))))
        provider.setdefault("timeout_seconds", 120)
        provider.setdefault("approval_required", provider.get("provider_type") == "cli")
        provider.setdefault("workdir_policy", "project-root")
        provider.setdefault("workdir", "")
        provider["updated_at"] = utc_now()
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO providers(id, label, enabled, status, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?)", (provider["id"], provider["label"], int(provider["enabled"]), provider["status"], provider["updated_at"], json.dumps(provider, sort_keys=True)))
        return provider

    def list_providers(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM providers ORDER BY enabled DESC, label").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def upsert_provider_run(self, run: dict[str, Any]) -> dict[str, Any]:
        run, redacted = _sanitize_payload(dict(run))
        if redacted:
            run["security_redacted"] = True
        run.setdefault("id", stable_id("run", run.get("project_id", "architectos"), run.get("provider_id", ""), utc_now()))
        run.setdefault("project_id", "architectos")
        run.setdefault("provider_id", "")
        run.setdefault("status", "running")
        run.setdefault("started_at", utc_now())
        run["updated_at"] = utc_now()
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO provider_runs(id, project_id, provider_id, status, started_at, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?)", (run["id"], run["project_id"], run["provider_id"], run["status"], run["started_at"], run["updated_at"], json.dumps(run, sort_keys=True)))
        return run

    def list_provider_runs(self, project_id: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        sql = "SELECT payload FROM provider_runs"
        params: list[Any] = []
        if project_id:
            sql += " WHERE project_id = ?"
            params.append(project_id)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def set_setting(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        clean_payload, redacted = _sanitize_payload(dict(payload))
        if redacted:
            clean_payload["security_redacted"] = True
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO settings(key, payload, updated_at) VALUES (?, ?, ?)", (key, json.dumps(clean_payload, sort_keys=True), utc_now()))
        return clean_payload

    def get_setting(self, key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def all_settings(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, payload FROM settings ORDER BY key").fetchall()
        return {row["key"]: json.loads(row["payload"]) for row in rows}

    def analytics(self, project_id: str | None = None) -> dict[str, Any]:
        nodes = [node for node in self.list_nodes() if not project_id or node.project_id in {project_id, None}]
        by_scope: dict[str, int] = {}
        by_type: dict[str, int] = {}
        favorites = 0
        for node in nodes:
            by_scope[node.scope] = by_scope.get(node.scope, 0) + 1
            by_type[node.type] = by_type.get(node.type, 0) + 1
            favorites += 1 if node.metadata.get("favorite") else 0
        tasks = self.list_tasks(project_id)
        task_status: dict[str, int] = {}
        for task in tasks:
            task_status[task["status"]] = task_status.get(task["status"], 0) + 1
        return {"nodes": len(nodes), "edges": len(self.list_edges()), "candidates": len(self.list_memory_candidates(project_id, status="candidate", limit=500)), "tasks": len(tasks), "chats": len(self.list_chats(project_id)), "favorites": favorites, "providers": len(self.list_providers()), "by_scope": by_scope, "by_type": by_type, "task_status": task_status, "database": str(self.db_path)}

    def export_bundle(self, project_id: str) -> dict[str, Any]:
        nodes = [node.to_dict() for node in self.list_nodes() if node.project_id in {project_id, None}]
        node_ids = {node["id"] for node in nodes}
        project = cast(Project, self.get_project(project_id) or self.get_project("architectos"))
        bundle = {"format": "architectos.bundle", "version": "0.2", "exported_at": utc_now(), "project": project.to_dict(), "memory_nodes": nodes, "memory_edges": [edge.to_dict() for edge in self.list_edges() if edge.source in node_ids and edge.target in node_ids], "memory_candidates": self.list_memory_candidates(project_id, status=None, limit=500), "tasks": self.list_tasks(project_id), "chats": self.list_chats(project_id), "providers": self.list_providers(), "settings": _strip_sensitive_settings(self.all_settings())}
        clean_bundle, _redacted = _sanitize_payload(bundle)
        return clean_bundle

    def import_bundle(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("format") != "architectos.bundle":
            raise ValueError("unsupported bundle format")
        project_payload = payload.get("project") or {}
        project = self._project_from_payload(project_payload)
        # One transaction: a mid-import failure must not leave a half-restored bundle.
        with self.transaction():
            self.upsert_project(project)
            for node_payload in payload.get("memory_nodes") or []:
                self.upsert_node(MemoryNode.from_dict(node_payload))
            for edge_payload in payload.get("memory_edges") or []:
                edge = MemoryEdge.from_dict(edge_payload)
                self.add_edge(edge.source, edge.target, edge.type, edge.scope, edge.confidence)
            for task in payload.get("tasks") or []:
                self.upsert_task(task)
            for candidate in payload.get("memory_candidates") or []:
                self.upsert_memory_candidate(candidate)
            for chat in payload.get("chats") or []:
                self.upsert_chat(chat)
            for provider in payload.get("providers") or []:
                self.upsert_provider(provider)
            for key, value in dict(payload.get("settings") or {}).items():
                self.set_setting(key, value)
        return {"project_id": project.id, "nodes": len(payload.get("memory_nodes") or []), "tasks": len(payload.get("tasks") or [])}

    def _write_evidence(self, node: MemoryNode) -> str:
        stamp = utc_now().replace(":", "").replace("-", "")
        safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "_", node.label.lower()).strip("_")[:40] or "memory"
        relative = Path("evidence") / f"{stamp}__{safe_label}.md"
        path = self.memory_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Evidence: {node.label}\n\nType: {node.type}\nScope: {node.scope}\nCreated: {node.created_at}\n\n{node.text}\n", encoding="utf-8")
        return relative.as_posix()
