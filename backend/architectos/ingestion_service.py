"""Memory ingestion engine, dedup hygiene, candidate review queue, and rescan scheduling.

Extracted from ``service.py``. ``MemoryIngestionEngine`` lives in ``memory_ingestion`` (re-exported here);
``IngestionServiceMixin`` carries ingest scheduling, per-source timeout budgets,
rescan maintenance, the manual ``add_memory`` dedup path, and the memory
candidate review queue (list/promote/reject/batch). Depends only on shared
constants and other leaf modules (never on ``service`` itself), so importing it
introduces no cycle. Repositories and cross-domain helpers stay on the service
and are reached through ``self`` via the MRO.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any

from .constants import (
    ALL_LOCAL_INGESTION_SOURCES,
    DEFAULT_INGEST_TIMEOUTS,
    SYSTEM_PROJECT_ID,
)
from .mcp import MCPError
from .memory_ingestion import (
    DUPLICATE_STOPWORDS,
    MemoryIngestionEngine,
    _token_set,
    _token_similarity,
)
from .models import Project, utc_now
from .teams_graph import TeamsGraphError, teams_graph_configured

# Re-exported for ArchitectOSService / graph_autolinker / older imports.
__all__ = [
    "DUPLICATE_STOPWORDS",
    "IngestionServiceMixin",
    "MemoryIngestionEngine",
    "_token_set",
    "_token_similarity",
]

_LOG = logging.getLogger("architectos.service")


class IngestionServiceMixin:
    """Ingest scheduling, per-source timeout budgets, and rescan maintenance."""

    # Owned by ArchitectOSService.__init__; declared here so mypy can see the
    # shapes through the mixin (the host class assigns the real values).
    _memory_ingest_state: dict[str, Any]
    _memory_rescan_state: dict[str, Any]
    _embedding_worker_started: bool

    def memory_ingest_status(self) -> dict[str, Any]:
        with self._memory_ingest_lock:
            state = dict(self._memory_ingest_state)
            state["logs"] = list(state.get("logs") or [])
        try:
            state["inbox_dir"] = str(self._inbox_dir())
        except OSError:
            state["inbox_dir"] = ""
        return state

    def _reset_ingest_progress(self, *, project_id: str, sources: list[str], keep_running: bool = True) -> None:
        with self._memory_ingest_lock:
            self._memory_ingest_state = {
                "running": keep_running,
                "project_id": project_id,
                "sources": list(sources),
                "current": "starting",
                "started_at": utc_now(),
                "finished_at": "",
                "error": "",
                "result": None,
                "logs": [],
            }

    def _log_ingest(self, message: str, *, level: str = "info", source: str = "", current: str | None = None) -> None:
        entry = {
            "at": utc_now(),
            "level": level,
            "source": source,
            "message": str(message),
        }
        with self._memory_ingest_lock:
            logs = list(self._memory_ingest_state.get("logs") or [])
            logs.append(entry)
            self._memory_ingest_state["logs"] = logs[-200:]
            if current is not None:
                self._memory_ingest_state["current"] = current
            elif source:
                self._memory_ingest_state["current"] = source

    def schedule_memory_ingest(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(payload or {})
        with self._memory_ingest_lock:
            if self._memory_ingest_state.get("running"):
                state = dict(self._memory_ingest_state)
                state["logs"] = list(state.get("logs") or [])
                return {"scheduled": False, "reason": "already_running", **state}
            sources = self._normalize_ingestion_sources(
                payload.get("sources") or ["docs", "code", "chat", "git", "adr", "issues", "prs", "meetings", "inbox"]
            )
            project_id = str(payload.get("project_id") or "architectos")
            self._memory_ingest_state = {
                "running": True,
                "project_id": project_id,
                "sources": sources,
                "current": "queued",
                "started_at": utc_now(),
                "finished_at": "",
                "error": "",
                "result": None,
                "logs": [{
                    "at": utc_now(),
                    "level": "info",
                    "source": "",
                    "message": f"Queued ingest for {project_id}: {', '.join(sources)}",
                }],
            }

        def worker() -> None:
            try:
                result = self.ingest_memory(payload)
                with self._memory_ingest_lock:
                    self._memory_ingest_state.update({
                        "running": False,
                        "finished_at": utc_now(),
                        "error": "",
                        "result": result,
                        "current": "done",
                    })
            except Exception as exc:  # noqa: BLE001 - keep UI responsive on ingest failure
                self._log_ingest(str(exc), level="error", current="error")
                with self._memory_ingest_lock:
                    self._memory_ingest_state.update({
                        "running": False,
                        "finished_at": utc_now(),
                        "error": str(exc),
                        "result": None,
                        "current": "error",
                    })

        threading.Thread(target=worker, name="architectos-memory-ingest", daemon=True).start()
        return {"scheduled": True, **self.memory_ingest_status()}

    def ingest_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        sources = self._normalize_ingestion_sources(payload.get("sources") or ["docs", "code", "chat", "git", "adr", "issues", "prs", "meetings", "inbox"])
        limit = max(1, int(payload.get("limit") or 20))
        ingest_mode = self._normalize_ingest_mode(payload.get("ingest_mode") or payload.get("mode"))
        direct_to_memory = ingest_mode == "memory"
        mcp_direct_to_memory = ingest_mode in {"memory", "mixed"}
        project = self.repository.get_project(project_id)
        with self._memory_ingest_lock:
            progress_owned = not bool(self._memory_ingest_state.get("running"))
        if progress_owned:
            self._reset_ingest_progress(project_id=project_id, sources=sources, keep_running=True)
        else:
            with self._memory_ingest_lock:
                self._memory_ingest_state["project_id"] = project_id
                self._memory_ingest_state["sources"] = list(sources)
                self._memory_ingest_state["current"] = "starting"
        self._log_ingest(
            f"Starting ingest · project={project_id} · limit={limit} · mode={ingest_mode} · sources={', '.join(sources)}",
            current="starting",
        )
        candidates: list[dict[str, Any]] = []
        warnings: list[str] = []
        file_sources = {"docs", "code", "adr", "issues", "prs", "meetings"}
        needs_project_root = any(source in sources for source in file_sources | {"git"})
        root: Path | None = None
        try:
            if needs_project_root:
                root_raw = str(payload.get("root_path") or (project.root_path if project and project.root_path else self.project_root))
                if project_id == SYSTEM_PROJECT_ID and not str(payload.get("root_path") or (project.root_path if project else "")).strip() and self._is_architectos_source_root():
                    raise ValueError("Choose or create a project folder before ingesting memory.")
                root = Path(root_raw).resolve()
                if not root.exists() or not root.is_dir():
                    raise ValueError(f"project path does not exist: {root}")
                self._log_ingest(f"Using project root {root}", source="files", current="files")
            if any(source in sources for source in file_sources):
                assert root is not None
                wanted = [item for item in sources if item in file_sources]
                self._log_ingest(f"Scanning project files for {', '.join(wanted)}…", source="files", current="files")
                file_candidates = self._ingest_file_candidates(project_id, root, sources, limit * 2)
                candidates.extend(file_candidates)
                by_file = Counter(str(item.get("source_type") or "file") for item in file_candidates)
                detail = ", ".join(f"{key}={value}" for key, value in sorted(by_file.items())) or "none"
                self._log_ingest(f"File scan done · {len(file_candidates)} candidate(s) ({detail})", source="files")
            if "chat" in sources:
                self._log_ingest("Reading chat history…", source="chat", current="chat")
                chat_candidates = self._ingest_chat_candidates(project_id, limit)
                candidates.extend(chat_candidates)
                self._log_ingest(f"Chat done · {len(chat_candidates)} candidate(s)", source="chat")
            if "git" in sources:
                assert root is not None
                self._log_ingest("Scanning git history…", source="git", current="git")
                git_candidates = self._ingest_git_candidates(project_id, root, limit)
                candidates.extend(git_candidates)
                self._log_ingest(f"Git done · {len(git_candidates)} candidate(s)", source="git")
            if "inbox" in sources:
                self._log_ingest("Scanning inbox folder…", source="inbox", current="inbox")
                inbox_candidates = self._ingest_inbox_candidates(project_id, limit)
                candidates.extend(inbox_candidates)
                self._log_ingest(f"Inbox done · {len(inbox_candidates)} candidate(s)", source="inbox")
            if "granola" in sources:
                self._log_ingest("Calling Granola MCP…", source="granola", current="granola")
                try:
                    granola_result = self._run_source_with_timeout(
                        "granola",
                        payload,
                        warnings,
                        lambda timed: self._ingest_granola_candidates(project_id, limit, timed),
                    )
                    if granola_result is not None:
                        granola_candidates = list(granola_result or [])
                        candidates.extend(granola_candidates)
                        self._log_ingest(f"Granola done · {len(granola_candidates)} candidate(s)", source="granola")
                except MCPError as exc:
                    warnings.append(f"Granola MCP skipped: {exc}")
                    self._log_ingest(f"Granola skipped: {exc}", level="warn", source="granola")
            boards_imported: list[dict[str, Any]] = []
            boards_updated = 0
            if "azure-boards" in sources:
                self._log_ingest("Importing Azure Boards work items…", source="azure-boards", current="azure-boards")
                try:
                    if mcp_direct_to_memory:
                        boards = self._run_source_with_timeout(
                            "azure-boards",
                            payload,
                            warnings,
                            lambda timed: self._import_azure_boards_to_memory(project_id, limit, timed),
                        )
                        if boards is not None:
                            boards_imported = list(boards.get("imported") or [])
                            boards_updated = int(boards.get("updated") or 0)
                            message = f"Azure Boards done · {len(boards_imported)} written ({boards_updated} updated)"
                            self._log_ingest(message, source="azure-boards")
                    else:
                        boards_candidates = self._run_source_with_timeout(
                            "azure-boards",
                            payload,
                            warnings,
                            lambda timed: self._ingest_azure_boards_candidates(project_id, limit, timed),
                        )
                        if boards_candidates is not None:
                            candidates.extend(list(boards_candidates or []))
                            message = f"Azure Boards done · {len(boards_candidates)} candidate(s)"
                            self._log_ingest(message, source="azure-boards")
                except MCPError as exc:
                    warnings.append(f"Azure Boards MCP skipped: {exc}")
                    self._log_ingest(f"Azure Boards skipped: {exc}", level="warn", source="azure-boards")
                except ValueError as exc:
                    warnings.append(f"Azure Boards skipped: {exc}")
                    self._log_ingest(f"Azure Boards skipped: {exc}", level="warn", source="azure-boards")
            azure_git_imported: list[dict[str, Any]] = []
            azure_git_updated = 0
            if "azure-git" in sources:
                self._log_ingest("Importing Azure Git repos and pull requests…", source="azure-git", current="azure-git")
                try:
                    if mcp_direct_to_memory:
                        azure_git = self._run_source_with_timeout(
                            "azure-git",
                            payload,
                            warnings,
                            lambda timed: self._import_azure_git_to_memory(project_id, limit, timed),
                        )
                        if azure_git is not None:
                            azure_git_imported = list(azure_git.get("imported") or [])
                            azure_git_updated = int(azure_git.get("updated") or 0)
                            message = f"Azure Git done · {len(azure_git_imported)} written ({azure_git_updated} updated)"
                            self._log_ingest(message, source="azure-git")
                    else:
                        def _git_candidates(timed: dict[str, Any]) -> list[dict[str, Any]]:
                            ado_project = self._azure_boards_project(timed)
                            server_id = str(timed.get("server_id") or "").strip() or self._azure_git_mcp_server_id()
                            return self._ingest_azure_git_candidates(project_id, ado_project, server_id, limit, timed)

                        azure_git_candidates = self._run_source_with_timeout("azure-git", payload, warnings, _git_candidates)
                        if azure_git_candidates is not None:
                            candidates.extend(list(azure_git_candidates or []))
                            message = f"Azure Git done · {len(azure_git_candidates)} candidate(s)"
                            self._log_ingest(message, source="azure-git")
                except MCPError as exc:
                    warnings.append(f"Azure Git MCP skipped: {exc}")
                    self._log_ingest(f"Azure Git skipped: {exc}", level="warn", source="azure-git")
                except ValueError as exc:
                    warnings.append(f"Azure Git skipped: {exc}")
                    self._log_ingest(f"Azure Git skipped: {exc}", level="warn", source="azure-git")
            wiki_imported: list[dict[str, Any]] = []
            wiki_updated = 0
            if "azure-wiki" in sources:
                self._log_ingest("Importing Azure Wiki pages…", source="azure-wiki", current="azure-wiki")
                try:
                    if mcp_direct_to_memory:
                        wiki = self._run_source_with_timeout(
                            "azure-wiki",
                            payload,
                            warnings,
                            lambda timed: self._import_azure_wiki_to_memory(project_id, limit, timed),
                        )
                        if wiki is not None:
                            wiki_imported = list(wiki.get("imported") or [])
                            wiki_updated = int(wiki.get("updated") or 0)
                            message = f"Azure Wiki done · {len(wiki_imported)} written ({wiki_updated} updated)"
                            self._log_ingest(message, source="azure-wiki")
                    else:
                        wiki_candidates = self._run_source_with_timeout(
                            "azure-wiki",
                            payload,
                            warnings,
                            lambda timed: self._ingest_azure_wiki_candidates(project_id, limit, timed),
                        )
                        if wiki_candidates is not None:
                            candidates.extend(list(wiki_candidates or []))
                            message = f"Azure Wiki done · {len(wiki_candidates)} candidate(s)"
                            self._log_ingest(message, source="azure-wiki")
                except MCPError as exc:
                    warnings.append(f"Azure Wiki MCP skipped: {exc}")
                    self._log_ingest(f"Azure Wiki skipped: {exc}", level="warn", source="azure-wiki")
                except ValueError as exc:
                    warnings.append(f"Azure Wiki skipped: {exc}")
                    self._log_ingest(f"Azure Wiki skipped: {exc}", level="warn", source="azure-wiki")
            teams_imported: list[dict[str, Any]] = []
            teams_updated = 0
            if "teams-meetings" in sources:
                self._log_ingest("Importing Teams meetings (Graph transcripts / AI Insights)…", source="teams-meetings", current="teams-meetings")
                try:
                    if mcp_direct_to_memory:
                        teams = self._run_source_with_timeout(
                            "teams-meetings",
                            payload,
                            warnings,
                            lambda timed: self._import_teams_meetings_to_memory(project_id, limit, timed),
                        )
                        if teams is not None:
                            teams_imported = list(teams.get("imported") or [])
                            teams_updated = int(teams.get("updated") or 0)
                            message = f"Teams done · {len(teams_imported)} written ({teams_updated} updated)"
                            self._log_ingest(message, source="teams-meetings")
                    else:
                        teams_candidates = self._run_source_with_timeout(
                            "teams-meetings",
                            payload,
                            warnings,
                            lambda timed: self._ingest_teams_meeting_candidates(project_id, limit, timed),
                        )
                        if teams_candidates is not None:
                            candidates.extend(list(teams_candidates or []))
                            message = f"Teams done · {len(teams_candidates)} candidate(s)"
                            self._log_ingest(message, source="teams-meetings")
                except TeamsGraphError as exc:
                    warnings.append(f"Teams Graph skipped: {exc}")
                    self._log_ingest(f"Teams skipped: {exc}", level="warn", source="teams-meetings")
                except ValueError as exc:
                    warnings.append(f"Teams Graph skipped: {exc}")
                    self._log_ingest(f"Teams skipped: {exc}", level="warn", source="teams-meetings")
            self._log_ingest(f"Preparing {'memory writes' if direct_to_memory else 'review queue'} from {len(candidates)} raw candidate(s)…", current="prepare")
            prepared = self.ingestion_engine.prepare_candidates(project_id, candidates, limit)
            if direct_to_memory:
                written_nodes = self._write_candidates_directly_to_memory(project_id, prepared)
                saved: list[dict[str, Any]] = []
            else:
                written_nodes = []
                saved = [self.repository.upsert_memory_candidate(candidate) for candidate in prepared]
            duplicates = sum(1 for candidate in saved if dict(candidate.get("metadata") or {}).get("duplicate"))
            by_source = Counter(str(candidate.get("source_type") or "manual") for candidate in saved)
            if boards_imported:
                by_source["azure-boards"] = by_source.get("azure-boards", 0) + len(boards_imported)
            if azure_git_imported:
                by_source["azure-git"] = by_source.get("azure-git", 0) + len(azure_git_imported)
            if wiki_imported:
                by_source["azure-wiki"] = by_source.get("azure-wiki", 0) + len(wiki_imported)
            if teams_imported:
                by_source["teams-meetings"] = by_source.get("teams-meetings", 0) + len(teams_imported)
            result = {
                "project_id": project_id,
                "sources": sources,
                "ingest_mode": ingest_mode,
                "candidates": saved,
                "count": len(saved),
                "duplicates": duplicates,
                "by_source": dict(sorted(by_source.items())),
                "pending": len(self.repository.list_memory_candidates(project_id, "candidate", 500)),
                "warnings": warnings,
                "boards_imported": boards_imported,
                "boards_count": len(boards_imported),
                "boards_updated": boards_updated,
                "azure_git_imported": azure_git_imported,
                "azure_git_count": len(azure_git_imported),
                "azure_git_updated": azure_git_updated,
                "wiki_imported": wiki_imported,
                "wiki_count": len(wiki_imported),
                "wiki_updated": wiki_updated,
                "teams_imported": teams_imported,
                "teams_count": len(teams_imported),
                "teams_updated": teams_updated,
                "direct_imported": [node.to_dict() for node in written_nodes],
                "direct_count": len(written_nodes),
                "memory_written": len(written_nodes) + len(boards_imported) + len(azure_git_imported) + len(wiki_imported) + len(teams_imported),
            }
            self._log_ingest(
                f"Ingest finished · {result['count']} candidate(s), {duplicates} duplicate hint(s), {result['memory_written']} memory writes",
                current="done",
            )
            with self._memory_ingest_lock:
                self._memory_ingest_state["result"] = result
                self._memory_ingest_state["current"] = "done"
                if progress_owned:
                    self._memory_ingest_state["running"] = False
                    self._memory_ingest_state["finished_at"] = utc_now()
                    self._memory_ingest_state["error"] = ""
            return result
        except Exception as exc:
            self._log_ingest(str(exc), level="error", current="error")
            with self._memory_ingest_lock:
                if progress_owned:
                    self._memory_ingest_state["running"] = False
                    self._memory_ingest_state["finished_at"] = utc_now()
                    self._memory_ingest_state["error"] = str(exc)
            raise

    def _normalize_ingest_mode(self, value: Any) -> str:
        raw = str(value or "").strip().lower().replace("-", "_")
        if raw in {"memory", "direct", "direct_memory", "direct_to_memory", "add_to_memory"}:
            return "memory"
        if raw in {"candidate", "candidates", "review", "review_queue"}:
            return "candidates"
        if raw in {"", "mixed", "legacy", "auto"}:
            return "mixed"
        raise ValueError("ingest_mode must be candidates, memory, or mixed")

    def _resolve_ingest_timeouts(self, source: str, payload: dict[str, Any] | None = None) -> tuple[float, float]:
        """Return (item_timeout_seconds, source_timeout_seconds) for an ingest source."""
        payload = payload or {}
        defaults = DEFAULT_INGEST_TIMEOUTS.get(source) or {"item": 30, "source": 300}
        item_default = float(defaults.get("item") or 30)
        source_default = float(defaults.get("source") or 300)

        per_source = {}
        raw_map = payload.get("timeouts")
        if isinstance(raw_map, dict):
            entry = raw_map.get(source) or raw_map.get(source.replace("-", "_"))
            if isinstance(entry, dict):
                per_source = entry
            elif isinstance(entry, (int, float, str)):
                per_source = {"source": entry}

        source_key = source.replace("-", "_")
        item_raw = (
            per_source.get("item")
            or per_source.get("item_timeout")
            or payload.get(f"{source_key}_item_timeout")
            or payload.get("item_timeout")
            or item_default
        )
        source_raw = (
            per_source.get("source")
            or per_source.get("source_timeout")
            or payload.get(f"{source_key}_source_timeout")
            or payload.get("source_timeout")
            or source_default
        )
        try:
            item_timeout = float(item_raw)
        except (TypeError, ValueError):
            item_timeout = item_default
        try:
            source_timeout = float(source_raw)
        except (TypeError, ValueError):
            source_timeout = source_default
        item_timeout = max(5.0, min(item_timeout, 600.0))
        source_timeout = max(item_timeout, min(source_timeout, 7200.0))
        return item_timeout, source_timeout

    def _with_ingest_timeouts(self, source: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        enriched = dict(payload or {})
        item_timeout, source_timeout = self._resolve_ingest_timeouts(source, enriched)
        enriched["_ingest_source"] = source
        enriched["_ingest_item_timeout"] = item_timeout
        enriched["_ingest_source_timeout"] = source_timeout
        enriched["_ingest_source_deadline"] = time.monotonic() + source_timeout
        return enriched

    @staticmethod
    def _ingest_item_timeout(payload: dict[str, Any] | None, default: float = 30.0) -> float:
        payload = payload or {}
        try:
            value = float(payload.get("_ingest_item_timeout") or default)
        except (TypeError, ValueError):
            value = default
        return max(5.0, min(value, 600.0))

    @staticmethod
    def _ingest_deadline_remaining(payload: dict[str, Any] | None) -> float | None:
        payload = payload or {}
        deadline = payload.get("_ingest_source_deadline")
        if deadline is None:
            return None
        try:
            return max(0.0, float(deadline) - time.monotonic())
        except (TypeError, ValueError):
            return None

    def _ingest_deadline_expired(self, payload: dict[str, Any] | None, source: str = "") -> bool:
        remaining = self._ingest_deadline_remaining(payload)
        if remaining is None:
            return False
        if remaining > 0:
            return False
        label = source or str((payload or {}).get("_ingest_source") or "source")
        self._log_ingest(
            f"{label} source timeout reached — skipping remaining work.",
            level="warn",
            source=label,
            current=label,
        )
        return True

    def _abort_ingest_source(self, source: str) -> None:
        """Best-effort: kill wedged MCP sessions so a hung item cannot block later sources."""
        session_ids = {
            "azure-boards": ("azure-devops",),
            "azure-git": ("azure-devops-git", "azure-devops"),
            "azure-wiki": ("azure-devops",),
            "granola": ("granola",),
        }.get(source, ())
        for server_id in session_ids:
            try:
                self.mcp_manager.close_session(server_id)
            except Exception:
                pass

    def _set_ingest_partial(self, payload: dict[str, Any] | None, result: Any) -> None:
        """Publish a salvageable mid-flight result for source-timeout recovery."""
        box = (payload or {}).get("_ingest_partial")
        if isinstance(box, dict):
            box["result"] = result

    def _run_source_with_timeout(
        self,
        source: str,
        payload: dict[str, Any] | None,
        warnings: list[str],
        fn: Callable[[dict[str, Any]], Any],
    ) -> Any:
        """Run one ingest source under a hard source-level timeout.

        Important: never use ``with ThreadPoolExecutor(...)`` here. Its ``__exit__``
        calls ``shutdown(wait=True)``, which would block past the timeout while a
        wedged MCP worker keeps running.

        Sources should call ``_set_ingest_partial`` as items complete so a timeout
        can still return already-fetched candidates instead of discarding them.
        """
        timed = self._with_ingest_timeouts(source, payload)
        item_timeout = float(timed["_ingest_item_timeout"])
        source_timeout = float(timed["_ingest_source_timeout"])
        partial_box: dict[str, Any] = {"result": None}
        timed["_ingest_partial"] = partial_box
        self._log_ingest(
            f"Timeouts · item={item_timeout:g}s · source={source_timeout:g}s",
            source=source,
            current=source,
        )
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(fn, timed)
            try:
                return future.result(timeout=source_timeout)
            except FuturesTimeoutError:
                message = f"{source} source timeout after {source_timeout:g}s"
                warnings.append(message)
                self._log_ingest(message, level="warn", source=source, current=source)
                self._abort_ingest_source(source)
                # Brief grace: inner path may be returning partial candidates right now.
                try:
                    result = future.result(timeout=2.0)
                    if result is not None:
                        self._log_ingest(
                            f"{source} kept partial result after timeout.",
                            level="warn",
                            source=source,
                            current=source,
                        )
                        return result
                except FuturesTimeoutError:
                    pass
                except Exception:
                    pass
                salvaged = partial_box.get("result")
                if salvaged is not None:
                    count = len(salvaged) if isinstance(salvaged, list) else (
                        len(salvaged.get("imported") or []) if isinstance(salvaged, dict) else 1
                    )
                    self._log_ingest(
                        f"{source} salvaged {count} partial item(s) after timeout.",
                        level="warn",
                        source=source,
                        current=source,
                    )
                    return salvaged
                future.cancel()
                return None
        finally:
            # Detach immediately — do not wait for hung MCP/stdio workers.
            executor.shutdown(wait=False, cancel_futures=True)

    def _write_candidates_directly_to_memory(self, project_id: str, candidates: list[dict[str, Any]]) -> list[Any]:
        written = []
        for candidate in candidates:
            scope = str(candidate.get("scope") or "project")
            metadata = self._promoted_candidate_metadata(candidate)
            metadata["source"] = "autoscan_direct"
            metadata["ingest_mode"] = "memory"
            metadata["candidate_source_id"] = candidate.get("id")
            node = self.repository.add_node(
                str(candidate.get("type") or "Lesson"),
                str(candidate.get("label") or "AutoScan memory"),
                scope,
                str(candidate.get("text") or ""),
                self._memory_project_id_for_scope(scope, project_id),
                None,
                float(candidate.get("confidence") or 0.72),
                metadata,
            )
            node = self.memory_lifecycle.initialize_node(node, "autoscan_direct")
            self.graph_auto_linker.link_node(node, project_id)
            if str(candidate.get("source_type") or "") == "azure-boards":
                self._link_azure_boards_relations(node, candidate)
            written.append(node)
        return written

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
    NEAR_DUPLICATE_SIMILARITY = 0.72

    def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        label = str(payload.get("label") or "").strip()
        text = str(payload.get("text") or "").strip()
        scope = str(payload.get("scope") or "project")
        if not label:
            raise ValueError("memory label is required")
        if not text:
            raise ValueError("memory text is required")
        node_type = str(payload.get("type") or "Lesson")
        project_id = self._memory_project_id_for_scope(scope, payload.get("project_id") or "architectos")

        dedup_enabled = payload.get("dedup", True) is not False
        exact, near_id, near_score = (
            self._ingest_dedup_scan(label, text, node_type, scope, project_id)
            if dedup_enabled
            else (None, "", 0.0)
        )
        if exact is not None:
            return self._register_duplicate_add(exact, payload)

        node = self.repository.add_node(
            node_type,
            label,
            scope,
            text,
            project_id,
            payload.get("interface_id"),
            float(payload.get("confidence") or 0.8),
            {"source": str(payload.get("source") or "ui"), "source_type": str(payload.get("source_type") or payload.get("source") or "manual")},
        )
        node = self.memory_lifecycle.initialize_node(node, "ui")
        if near_id and near_score >= self.NEAR_DUPLICATE_SIMILARITY:
            node.metadata["possible_duplicate_of"] = near_id
            node.metadata["possible_duplicate_score"] = round(near_score, 3)
            node = self.repository.upsert_node(node)
        self.graph_auto_linker.link_node(node, payload.get("project_id") or "architectos")
        return node.to_dict()

    def _ingest_dedup_scan(
        self, label: str, text: str, node_type: str, scope: str, project_id: str | None
    ) -> tuple[Any | None, str, float]:
        """Scan existing facts for an exact restatement or a near-duplicate.

        Returns ``(exact_node, best_near_id, best_near_score)``. "Exact" means the
        same fingerprint (top content tokens) within the same type/scope/project —
        deterministic and dependency-free. Near-duplicate is the best token
        similarity among same-type facts, surfaced for flagging only.
        """
        engine = self.ingestion_engine
        incoming_tokens = engine._tokens(f"{label} {text}")
        incoming_key = (self._normalize_fact_text(label), self._normalize_fact_text(text))
        best_near_id, best_near_score = "", 0.0
        for node in self.repository.list_nodes():
            if node.status != "active":
                continue
            meta = dict(node.metadata or {})
            if meta.get("structural") or meta.get("hub") or meta.get("scope_root"):
                continue
            if node.scope != scope or node.project_id != project_id:
                continue
            if node.type != node_type:
                continue
            # Exact = identical normalized label + text. Deliberately NOT the token
            # fingerprint: the fingerprint tokenizer drops digits, so facts that
            # differ only by a number ("cap 5000" vs "cap 9000", "node 0" vs
            # "node 1") share a fingerprint and must never be auto-merged.
            if (self._normalize_fact_text(node.label), self._normalize_fact_text(node.text)) == incoming_key:
                return node, node.id, 1.0
            node_tokens = set(meta.get("tokens") or []) or engine._tokens(f"{node.label} {node.text}")
            score = engine._similarity(incoming_tokens, node_tokens)
            if score > best_near_score:
                best_near_id, best_near_score = node.id, score
        return None, best_near_id, best_near_score

    @staticmethod
    def _normalize_fact_text(value: str) -> str:
        return " ".join(str(value or "").split()).strip().lower()

    def _register_duplicate_add(self, existing: Any, payload: dict[str, Any]) -> dict[str, Any]:
        """Fold a re-added identical fact into the node it duplicates.

        Non-destructive update: bump confidence toward the incoming value, record
        a dedup hit, refresh lifecycle access, and return the surviving node marked
        ``dedup_merged`` so callers can tell no new node was created.
        """
        meta = dict(existing.metadata or {})
        meta["dedup_hits"] = int(meta.get("dedup_hits") or 0) + 1
        meta["last_dedup_at"] = utc_now()
        existing.metadata = meta
        try:
            existing.confidence = max(float(existing.confidence or 0.0), float(payload.get("confidence") or 0.0))
        except (TypeError, ValueError):
            pass
        existing = self.repository.upsert_node(existing)
        try:
            self.memory_lifecycle.refresh_nodes([existing.id], "dedup")
        except Exception as exc:  # lifecycle bump is best-effort
            _LOG.warning("dedup refresh skipped: %s", exc)
        result = existing.to_dict()
        result["dedup_merged"] = True
        return result

    def _memory_project_id_for_scope(self, scope: str, project_id: str | None) -> str | None:
        normalized = str(scope or "project")
        if normalized in {"shared", "global"}:
            return None
        return project_id or "architectos"

    def list_memory_candidates(self, project_id: str | None = None, status: str | None = "candidate", limit: int = 50) -> dict[str, Any]:
        normalized = str(status or "").strip().lower()
        if normalized in {"", "all", "*"}:
            status = None
        elif normalized in {"pending"}:
            status = "candidate"
        counts = self.repository.count_memory_candidates_by_status(project_id)
        return {
            "candidates": self.repository.list_memory_candidates(project_id, status, limit),
            "counts": counts,
            "pending": counts.get("pending", 0),
            "accepted": counts.get("accepted", 0),
            "rejected": counts.get("rejected", 0),
        }

    def promote_memory_candidate(
        self,
        candidate_id: str,
        payload: dict[str, Any] | None = None,
        *,
        by_work_item: dict[str, Any] | None = None,
        hub_cache: dict[tuple[str, str | None, str], Any] | None = None,
        existing_edge_ids: set[str] | None = None,
        nodes_snapshot: list[Any] | None = None,
        project_root_cache: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        candidate = self.repository.get_memory_candidate(candidate_id)
        if not candidate:
            raise ValueError("memory candidate not found")
        payload = payload or {}
        if candidate.get("status") == "promoted" and candidate.get("promoted_node_id"):
            node = self.repository.get_node(str(candidate["promoted_node_id"]))
            return {"candidate": candidate, "memory": node.to_dict() if node else None}
        node_type = str(payload.get("type") or candidate.get("type") or "Lesson")
        created_at = utc_now()
        metadata = self.memory_lifecycle.seed_metadata(
            node_type=node_type,
            created_at=created_at,
            metadata=self._promoted_candidate_metadata(candidate),
            source="promoted_candidate",
            label=str(payload.get("label") or candidate.get("label") or ""),
        )
        node = self.repository.add_node(
            node_type,
            str(payload.get("label") or candidate.get("label") or "Memory candidate"),
            str(payload.get("scope") or candidate.get("scope") or "project"),
            str(payload.get("text") or candidate.get("text") or ""),
            self._memory_project_id_for_scope(
                str(payload.get("scope") or candidate.get("scope") or "project"),
                str(candidate.get("project_id") or "architectos"),
            ),
            payload.get("interface_id"),
            float(payload.get("confidence") or candidate.get("confidence") or 0.72),
            metadata,
        )
        # Lifecycle already seeded — initialize_node is a no-op write skip.
        node = self.memory_lifecycle.initialize_node(node, "promoted_candidate")
        if nodes_snapshot is not None:
            nodes_snapshot.append(node)
        self.graph_auto_linker.link_node(
            node,
            str(candidate.get("project_id") or "architectos"),
            hub_cache=hub_cache,
            existing_edge_ids=existing_edge_ids,
            nodes_snapshot=nodes_snapshot,
            project_root_cache=project_root_cache,
        )
        self._link_azure_boards_relations(node, candidate, by_work_item=by_work_item)
        self._link_chat_session_relations(node, candidate)
        candidate["status"] = "promoted"
        candidate["promoted_node_id"] = node.id
        candidate["promoted_at"] = utc_now()
        candidate = self.repository.update_memory_candidate(candidate)
        return {"candidate": candidate, "memory": node.to_dict()}

    def _promoted_candidate_metadata(self, candidate: dict[str, Any]) -> dict[str, Any]:
        meta = dict(candidate.get("metadata") or {})
        payload = {
            "source": "memory_candidate",
            "candidate_id": candidate["id"],
            "source_type": candidate.get("source_type"),
            "source_ref": candidate.get("source_ref"),
        }
        for key in (
            "work_item_id",
            "work_item_type",
            "work_item_state",
            "work_item_url",
            "assigned_to",
            "iteration_path",
            "area_path",
            "tags",
            "relations",
            "comments",
            "parent_id",
            "ado_project",
            "template",
            "auto_accepted",
            "chat_id",
            "session_revision",
            "session_start_message_index",
            "session_end_message_index",
        ):
            if meta.get(key) not in (None, "", [], {}):
                payload[key] = meta[key]
        return payload

    def _link_chat_session_relations(self, node, candidate: dict[str, Any]) -> None:
        """Link promoted summaries from the same chat in revision order."""
        metadata = dict(candidate.get("metadata") or {})
        if str(metadata.get("template") or "") != "chat_session_summary":
            return
        chat_id = str(metadata.get("chat_id") or candidate.get("source_ref") or "").strip()
        revision = int(metadata.get("session_revision") or 0)
        if not chat_id or revision <= 1:
            return
        previous: tuple[int, str] | None = None
        for item in self.repository.list_memory_candidates(str(candidate.get("project_id") or "architectos"), status=None, limit=500):
            item_meta = dict(item.get("metadata") or {})
            if str(item_meta.get("template") or "") != "chat_session_summary":
                continue
            if str(item_meta.get("chat_id") or item.get("source_ref") or "") != chat_id:
                continue
            item_revision = int(item_meta.get("session_revision") or 0)
            node_id = str(item.get("promoted_node_id") or "")
            if not node_id or item_revision >= revision:
                continue
            if previous is None or item_revision > previous[0]:
                previous = (item_revision, node_id)
        if previous:
            self.repository.add_edge(previous[1], node.id, "NEXT_SESSION", str(candidate.get("scope") or "project"), 0.95)

    def _link_azure_boards_relations(self, node, candidate: dict[str, Any], by_work_item: dict[str, Any] | None = None) -> None:
        metadata = dict(candidate.get("metadata") or {})
        relations = list(metadata.get("relations") or [])
        if not relations:
            return
        project_id = str(candidate.get("project_id") or "architectos")
        if by_work_item is None:
            by_work_item = {}
            for existing in self.repository.list_nodes():
                if existing.status != "active":
                    continue
                if existing.project_id not in {project_id, None}:
                    continue
                work_item_id = str(dict(existing.metadata or {}).get("work_item_id") or "").strip()
                if work_item_id:
                    by_work_item[work_item_id] = existing
        for relation in relations:
            if not isinstance(relation, dict):
                continue
            related_id = str(relation.get("work_item_id") or "").strip()
            target = by_work_item.get(related_id)
            if not target or target.id == node.id:
                continue
            edge_type = str(relation.get("link_type") or "related").replace(" ", "_")[:48] or "related"
            try:
                self.repository.add_edge(node.id, target.id, edge_type, "project", 0.86)
            except Exception:  # noqa: BLE001 - linking is best-effort
                continue

    def reject_memory_candidate(self, candidate_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        candidate = self.repository.get_memory_candidate(candidate_id)
        if not candidate:
            raise ValueError("memory candidate not found")
        candidate["status"] = "rejected"
        candidate["rejected_at"] = utc_now()
        candidate["reject_reason"] = str((payload or {}).get("reason") or "")
        return {"candidate": self.repository.update_memory_candidate(candidate)}

    def batch_update_memory_candidates(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        project_id = str(payload.get("project_id") or "architectos")
        action = str(payload.get("action") or "").strip().lower()
        if action in {"accept", "approve", "promote"}:
            action = "promote"
        elif action in {"reject", "decline"}:
            action = "reject"
        else:
            raise ValueError("action must be promote/accept or reject")

        status_filter = self._normalize_candidate_batch_status(payload.get("status") or payload.get("filter"))
        duplicate_only = bool(payload.get("duplicate_only") or payload.get("duplicates_only") or status_filter == "duplicate")
        exclude_duplicates = bool(payload.get("exclude_duplicates") or False)
        if status_filter == "duplicate":
            status_filter = "candidate"
            duplicate_only = True
        process_all = bool(payload.get("all") or payload.get("process_all") or payload.get("entire_queue"))
        # Default page-sized batches stay modest; Accept/Reject all processes the full matching set.
        if process_all:
            limit = None
        else:
            limit = max(1, min(int(payload.get("limit") or 500), 5000))
        reason = str(payload.get("reason") or ("Batch rejected in Memory UI" if action == "reject" else ""))

        listed_status = None if status_filter in {None, "all"} else status_filter
        candidates = self.repository.list_memory_candidates(project_id, listed_status, limit)
        selected: list[dict[str, Any]] = []
        for candidate in candidates:
            meta = dict(candidate.get("metadata") or {})
            is_duplicate = bool(meta.get("duplicate"))
            if duplicate_only and not is_duplicate:
                continue
            if exclude_duplicates and is_duplicate:
                continue
            selected.append(candidate)

        promoted = 0
        rejected = 0
        skipped = 0
        errors: list[str] = []
        results: list[dict[str, Any]] = []

        # Accept-all used to re-scan all memory nodes/edges per candidate (O(n²)).
        # Share indexes for the batch; embedding indexing is queued asynchronously
        # by the node-upsert listener, so no listener pausing is needed.
        by_work_item: dict[str, Any] | None = None
        hub_cache: dict[tuple[str, str | None, str], Any] | None = None
        existing_edge_ids: set[str] | None = None
        nodes_snapshot: list[Any] | None = None
        project_root_cache: dict[str, Any] | None = None
        if action == "promote" and selected:
            nodes_snapshot = list(self.repository.list_nodes())
            by_work_item = {}
            for existing in nodes_snapshot:
                if existing.status != "active":
                    continue
                if existing.project_id not in {project_id, None}:
                    continue
                work_item_id = str(dict(existing.metadata or {}).get("work_item_id") or "").strip()
                if work_item_id:
                    by_work_item[work_item_id] = existing
            hub_cache = {}
            project_root_cache = {}
            existing_edge_ids = {edge.id for edge in self.repository.list_edges()}

        for candidate in selected:
            candidate_id = str(candidate.get("id") or "")
            status = str(candidate.get("status") or "")
            try:
                if action == "promote":
                    if status == "promoted" and candidate.get("promoted_node_id"):
                        skipped += 1
                        continue
                    result = self.promote_memory_candidate(
                        candidate_id,
                        {},
                        by_work_item=by_work_item,
                        hub_cache=hub_cache,
                        existing_edge_ids=existing_edge_ids,
                        nodes_snapshot=nodes_snapshot,
                        project_root_cache=project_root_cache,
                    )
                    memory = result.get("memory") or {}
                    memory_id = memory.get("id")
                    if memory_id and by_work_item is not None:
                        work_item_id = str((memory.get("metadata") or {}).get("work_item_id") or "").strip()
                        if work_item_id and nodes_snapshot is not None:
                            # Prefer the node already appended to the snapshot.
                            node = next((item for item in reversed(nodes_snapshot) if item.id == memory_id), None)
                            if node is None:
                                node = self.repository.get_node(str(memory_id))
                            if node:
                                by_work_item[work_item_id] = node
                    promoted += 1
                    results.append({"id": candidate_id, "status": "promoted", "memory_id": memory_id})
                else:
                    if status == "rejected":
                        skipped += 1
                        continue
                    if status == "promoted":
                        # Do not delete durable memory on batch reject; only skip already promoted.
                        skipped += 1
                        continue
                    self.reject_memory_candidate(candidate_id, {"reason": reason})
                    rejected += 1
                    results.append({"id": candidate_id, "status": "rejected"})
            except Exception as exc:  # noqa: BLE001 - continue batch on single failures
                errors.append(f"{candidate_id}: {exc}")

        counts = self.repository.count_memory_candidates_by_status(project_id)
        return {
            "project_id": project_id,
            "action": action,
            "status": listed_status or "all",
            "duplicate_only": duplicate_only,
            "exclude_duplicates": exclude_duplicates,
            "process_all": process_all,
            "matched": len(selected),
            "promoted": promoted,
            "rejected": rejected,
            "skipped": skipped,
            "errors": errors[:50],
            "error_count": len(errors),
            "results": results[:100],
            "pending": counts.get("pending", 0),
            "accepted": counts.get("accepted", 0),
            "counts": counts,
        }

    def _normalize_candidate_batch_status(self, value: Any) -> str | None:
        raw = str(value or "").strip().lower()
        if not raw or raw in {"pending", "candidate", "all_pending"}:
            return "candidate"
        if raw in {"rejected", "reject"}:
            return "rejected"
        if raw in {"promoted", "promote", "accepted"}:
            return "promoted"
        if raw in {"duplicate", "duplicates"}:
            return "duplicate"
        if raw in {"all", "*"}:
            return "all"
        raise ValueError("status/filter must be pending, duplicate, rejected, promoted, or all")