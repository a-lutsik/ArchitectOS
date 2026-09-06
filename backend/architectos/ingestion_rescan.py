"""Background memory rescan scheduling and per-source interval ticker."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from .constants import SYSTEM_PROJECT_ID
from .models import Project, utc_now

_LOG = logging.getLogger("architectos.service")


class IngestionRescanMixin:
    def memory_rescan_status(self) -> dict[str, Any]:
        with self._memory_rescan_lock:
            return dict(self._memory_rescan_state)

    def _source_scheduler_settings(self) -> dict[str, Any]:
        life = dict(self.repository.get_setting("memory_lifecycle") or {})
        return {
            "auto_rescan_on_startup": bool(life.get("auto_rescan_on_startup", True)),
            "auto_rescan_all_projects": bool(life.get("auto_rescan_all_projects", True)),
            "scheduler_enabled": life.get("source_scheduler_enabled", True) is not False,
            "scheduler_tick_minutes": max(1, int(life.get("source_scheduler_tick_minutes") or 5)),
            "default_interval_minutes": max(5, int(life.get("source_default_interval_minutes") or 60)),
        }

    def _memory_rescan_settings(self) -> dict[str, Any]:
        settings = self._source_scheduler_settings()
        return {
            "auto_rescan_on_startup": settings["auto_rescan_on_startup"],
            "auto_rescan_limit": 0,
            "auto_rescan_all_projects": settings["auto_rescan_all_projects"],
        }

    def _default_rescan_bindings(self, project_id: str) -> list[dict[str, Any]]:
        self.ensure_project_sources(project_id)
        return [
            item for item in self.list_project_sources(project_id)
            if dict(item.get("config") or {}).get("enabled")
        ]

    def _bindings_due_for_scan(self, project_id: str) -> list[dict[str, Any]]:
        now = time.time()
        due: list[dict[str, Any]] = []
        for binding in self.list_project_sources(project_id):
            cfg = dict(binding.get("config") or {})
            if not cfg.get("enabled"):
                continue
            schedule = dict(cfg.get("schedule") or {})
            if not schedule.get("enabled", cfg.get("enabled")):
                continue
            interval = max(5, int(schedule.get("interval_minutes") or self._source_scheduler_settings()["default_interval_minutes"])) * 60
            last_raw = str(cfg.get("last_run_at") or "")
            if not last_raw:
                due.append(binding)
                continue
            try:
                from datetime import datetime

                last_dt = datetime.fromisoformat(last_raw.replace("Z", "+00:00"))
                elapsed = now - last_dt.timestamp()
            except ValueError:
                elapsed = interval + 1
            if elapsed >= interval:
                due.append(binding)
        return due

    def rescan_memory_sources(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        settings = self._memory_rescan_settings()
        all_projects = bool(payload.get("all_projects", settings["auto_rescan_all_projects"]))
        project_id = str(payload.get("project_id") or "").strip()
        projects = self._projects_for_memory_rescan(project_id or None, all_projects=all_projects)
        results: list[dict[str, Any]] = []
        warnings: list[str] = []
        total = 0
        duplicates = 0
        memory_written = 0
        boards_count = 0
        azure_git_count = 0
        wiki_count = 0
        for project in projects:
            bindings = self._default_rescan_bindings(project.id)
            if payload.get("sources") and payload.get("sources") not in ("all", ["all"]):
                bindings = self._sources_for_ingest_request(project.id, payload)
            elif payload.get("due_only"):
                bindings = self._bindings_due_for_scan(project.id)
            source_ids = [str(item.get("id") or "") for item in bindings]
            try:
                self._log_ingest(
                    f"Rescan project {project.name or project.id} · sources={len(bindings)}",
                    current=f"project:{project.id}",
                )
                ingest = self.ingest_memory({
                    "project_id": project.id,
                    "sources": source_ids or "all",
                    "root_path": project.root_path,
                    "all_items": payload.get("all_items"),
                    "ingest_mode": payload.get("ingest_mode") or payload.get("mode"),
                    "limit": 0,
                })
            except ValueError as exc:
                warnings.append(f"{project.name or project.id}: {exc}")
                continue
            warnings.extend(str(item) for item in (ingest.get("warnings") or []))
            total += int(ingest.get("count") or 0)
            duplicates += int(ingest.get("duplicates") or 0)
            memory_written += int(ingest.get("memory_written") or 0)
            boards_count += int(ingest.get("boards_count") or 0)
            azure_git_count += int(ingest.get("azure_git_count") or 0)
            wiki_count += int(ingest.get("wiki_count") or 0)
            results.append({
                "project_id": project.id,
                "name": project.name,
                "count": ingest.get("count") or 0,
                "duplicates": ingest.get("duplicates") or 0,
                "by_source": ingest.get("by_source") or {},
                "pending": ingest.get("pending") or 0,
                "memory_written": ingest.get("memory_written") or 0,
                "boards_count": ingest.get("boards_count") or 0,
                "azure_git_count": ingest.get("azure_git_count") or 0,
                "wiki_count": ingest.get("wiki_count") or 0,
            })
        return {
            "ok": True,
            "sources": [str(item.get("name") or "") for item in (bindings if projects else [])],
            "limit": 0,
            "projects": results,
            "count": total,
            "duplicates": duplicates,
            "memory_written": memory_written,
            "boards_count": boards_count,
            "azure_git_count": azure_git_count,
            "wiki_count": wiki_count,
            "teams_count": 0,
            "warnings": warnings,
            "pending": sum(int(item.get("pending") or 0) for item in results),
        }

    def _projects_for_memory_rescan(self, project_id: str | None, *, all_projects: bool) -> list[Project]:
        if not all_projects:
            target_id = project_id
            if not target_id:
                workspace = dict(self.repository.get_setting("workspace") or {})
                target_id = str(workspace.get("current_project_id") or "")
            project = self.repository.get_project(target_id) if target_id else None
            if not project:
                raise ValueError(f"project not found: {target_id or '(none)'}")
            return [project]
        projects: list[Project] = []
        for project in self.repository.list_projects():
            root = str(project.root_path or "").strip()
            if project.id == SYSTEM_PROJECT_ID and not root:
                continue
            projects.append(project)
        return projects

    def start_background_maintenance(self) -> None:
        """Spawn embedding warmup/backfill threads. Called by the HTTP entry
        points, not __init__, so tests and short-lived service instances never
        race daemon threads against tempdir cleanup."""
        spawned: list[str] = []
        if self.memory_embeddings.enabled() and bool((self.repository.get_setting("memory_retrieval") or {}).get("reindex_on_startup", True)):
            spawned.append("embed-backfill")
            threading.Thread(target=self._backfill_embeddings_safe, name="embed-backfill", daemon=True).start()
        if self.memory_embeddings.enabled():
            spawned.append("embed-warmup")
            threading.Thread(target=self._warmup_embeddings_safe, name="embed-warmup", daemon=True).start()
        if self.memory_embeddings.enabled() and not self._embedding_worker_started:
            self._embedding_worker_started = True
            spawned.append("embed-index")
            threading.Thread(target=self._embedding_index_worker, name="embed-index", daemon=True).start()
        spawned.extend(["memory-decay", "chat-session-idle", "source-scheduler"])
        threading.Thread(target=self._memory_decay_loop, name="memory-decay", daemon=True).start()
        threading.Thread(target=self._chat_session_idle_loop, name="chat-session-idle", daemon=True).start()
        threading.Thread(target=self._source_scheduler_loop, name="source-scheduler", daemon=True).start()
        _LOG.info(
            "start_background_maintenance pid=%s: spawned %s",
            os.getpid(),
            ", ".join(spawned),
        )

    def schedule_startup_memory_rescan(self) -> dict[str, Any]:
        settings = self._memory_rescan_settings()
        if not settings["auto_rescan_on_startup"]:
            return {"scheduled": False, "reason": "disabled"}
        return self.schedule_memory_rescan({
            "trigger": "startup",
            "all_projects": settings["auto_rescan_all_projects"],
            "limit": 0,
            "include_mcp": True,
        })

    def schedule_memory_rescan(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(payload or {})
        trigger = str(payload.get("trigger") or "manual")
        with self._memory_rescan_lock:
            if self._memory_rescan_state.get("running"):
                return {"scheduled": False, "reason": "already_running", **dict(self._memory_rescan_state)}
            self._memory_rescan_state = {
                "running": True,
                "trigger": trigger,
                "started_at": utc_now(),
                "finished_at": "",
                "error": "",
                "result": None,
            }

        def worker() -> None:
            try:
                result = self.rescan_memory_sources(payload)
                with self._memory_rescan_lock:
                    self._memory_rescan_state.update({
                        "running": False,
                        "finished_at": utc_now(),
                        "error": "",
                        "result": result,
                    })
                _LOG.info(
                    "memory rescan (%s) finished: %s candidate(s) across %s project(s)",
                    trigger,
                    result.get("count"),
                    len(result.get("projects") or []),
                )
            except Exception as exc:  # noqa: BLE001 - background worker must not crash the app
                _LOG.exception("memory rescan (%s) failed", trigger)
                with self._memory_rescan_lock:
                    self._memory_rescan_state.update({
                        "running": False,
                        "finished_at": utc_now(),
                        "error": str(exc),
                        "result": None,
                    })

        threading.Thread(target=worker, name=f"architectos-memory-rescan-{trigger}", daemon=True).start()
        return {"scheduled": True, "trigger": trigger, **self.memory_rescan_status()}

    def _source_scheduler_loop(self) -> None:
        while True:
            settings = self._source_scheduler_settings()
            tick = max(60, settings["scheduler_tick_minutes"] * 60)
            time.sleep(tick)
            if not settings["scheduler_enabled"]:
                continue
            with self._memory_ingest_lock:
                if self._memory_ingest_state.get("running"):
                    continue
            try:
                workspace = dict(self.repository.get_setting("workspace") or {})
                project_id = str(workspace.get("current_project_id") or "")
                if not project_id:
                    continue
                due = self._bindings_due_for_scan(project_id)
                if not due:
                    continue
                self.schedule_memory_ingest({
                    "project_id": project_id,
                    "sources": [str(item.get("id") or "") for item in due],
                    "limit": 0,
                    "trigger": "scheduler",
                })
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("source scheduler tick skipped: %s", exc)
