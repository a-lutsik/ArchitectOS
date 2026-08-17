"""Background memory rescan scheduling and status."""
from __future__ import annotations

import logging
import threading
from typing import Any

from .constants import ALL_LOCAL_INGESTION_SOURCES, SYSTEM_PROJECT_ID
from .models import Project, utc_now
from .teams_graph import teams_graph_configured

_LOG = logging.getLogger("architectos.service")


class IngestionRescanMixin:
    def memory_rescan_status(self) -> dict[str, Any]:
        with self._memory_rescan_lock:
            return dict(self._memory_rescan_state)

    def _default_rescan_sources(self, include_mcp: bool = True) -> list[str]:
        sources = list(ALL_LOCAL_INGESTION_SOURCES)
        if not include_mcp:
            return sources
        granola = self.mcp_manager.get_server("granola")
        if granola and granola.enabled:
            sources.append("granola")
        ado = self.mcp_manager.get_server("azure-devops")
        if ado and ado.enabled:
            sources.append("azure-boards")
            sources.append("azure-wiki")
        ado_git = self.mcp_manager.get_server("azure-devops-git")
        if (ado and ado.enabled) or (ado_git and ado_git.enabled):
            sources.append("azure-git")
        if teams_graph_configured():
            sources.append("teams-meetings")
        return sources

    def _memory_rescan_settings(self) -> dict[str, Any]:
        life = dict(self.repository.get_setting("memory_lifecycle") or {})
        return {
            "auto_rescan_on_startup": bool(life.get("auto_rescan_on_startup", True)),
            "auto_rescan_limit": max(1, int(life.get("auto_rescan_limit") or 24)),
            "auto_rescan_all_projects": bool(life.get("auto_rescan_all_projects", True)),
        }

    def rescan_memory_sources(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        settings = self._memory_rescan_settings()
        include_mcp = payload.get("include_mcp")
        if include_mcp is None:
            include_mcp = True
        sources = payload.get("sources")
        if not sources or sources == "all" or sources == ["all"]:
            sources = self._default_rescan_sources(include_mcp=bool(include_mcp))
        else:
            sources = self._normalize_ingestion_sources(sources)
        limit = max(1, int(payload.get("limit") or settings["auto_rescan_limit"]))
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
        teams_count = 0
        for project in projects:
            try:
                self._log_ingest(
                    f"Rescan project {project.name or project.id} · sources={', '.join(sources)}",
                    current=f"project:{project.id}",
                )
                ingest = self.ingest_memory({
                    "project_id": project.id,
                    "sources": sources,
                    "limit": limit,
                    "root_path": project.root_path,
                    "all_items": payload.get("all_items"),
                    "ingest_mode": payload.get("ingest_mode") or payload.get("mode"),
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
            teams_count += int(ingest.get("teams_count") or 0)
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
                "teams_count": ingest.get("teams_count") or 0,
            })
        return {
            "ok": True,
            "sources": sources,
            "limit": limit,
            "projects": results,
            "count": total,
            "duplicates": duplicates,
            "memory_written": memory_written,
            "boards_count": boards_count,
            "azure_git_count": azure_git_count,
            "wiki_count": wiki_count,
            "teams_count": teams_count,
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
            if not root:
                continue
            projects.append(project)
        return projects

    def start_background_maintenance(self) -> None:
        """Spawn embedding warmup/backfill threads. Called by the HTTP entry
        points, not __init__, so tests and short-lived service instances never
        race daemon threads against tempdir cleanup."""
        if self.memory_embeddings.enabled() and bool((self.repository.get_setting("memory_retrieval") or {}).get("reindex_on_startup", True)):
            # Never block HTTP startup on embedding backfill — local Ollama/bge-m3 can
            # take minutes across thousands of nodes.
            threading.Thread(target=self._backfill_embeddings_safe, name="embed-backfill", daemon=True).start()
        if self.memory_embeddings.enabled():
            threading.Thread(target=self._warmup_embeddings_safe, name="embed-warmup", daemon=True).start()
        if self.memory_embeddings.enabled() and not self._embedding_worker_started:
            self._embedding_worker_started = True
            threading.Thread(target=self._embedding_index_worker, name="embed-index", daemon=True).start()
        threading.Thread(target=self._memory_decay_loop, name="memory-decay", daemon=True).start()
        threading.Thread(target=self._chat_session_idle_loop, name="chat-session-idle", daemon=True).start()

    def schedule_startup_memory_rescan(self) -> dict[str, Any]:
        settings = self._memory_rescan_settings()
        if not settings["auto_rescan_on_startup"]:
            return {"scheduled": False, "reason": "disabled"}
        return self.schedule_memory_rescan({
            "trigger": "startup",
            "all_projects": settings["auto_rescan_all_projects"],
            "limit": settings["auto_rescan_limit"],
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

    # Ingest-time hygiene: an exact restatement of an existing fact updates it in
    # place instead of spawning a duplicate; a close-but-not-identical fact is
    # flagged (never silently merged) so a value change like "cap 5k -> 9k" stays a
    # distinct fact the consolidation/supersede path can reason about.
