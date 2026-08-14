"""Memory ingestion + rescan/maintenance scheduling for :class:`ArchitectOSService`.

Extracted from ``service.py``. Depends only on shared constants and other leaf
modules (never on ``service`` itself), so importing it introduces no cycle. All
per-source ingest helpers (``self._ingest_*``) and repositories stay on the
service and are reached through ``self`` via the MRO.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any

from .constants import (
    ALL_LOCAL_INGESTION_SOURCES,
    DEFAULT_INGEST_TIMEOUTS,
    SYSTEM_PROJECT_ID,
)
from .mcp import MCPError
from .models import Project, utc_now
from .teams_graph import TeamsGraphError, teams_graph_configured

_LOG = logging.getLogger("architectos.service")


class IngestionServiceMixin:
    """Ingest scheduling, per-source timeout budgets, and rescan maintenance."""

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
