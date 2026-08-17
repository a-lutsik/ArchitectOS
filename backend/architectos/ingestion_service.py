"""Memory ingest scheduling, rescan, and manual add/dedup.

Extracted from ``service.py``. ``MemoryIngestionEngine`` lives in ``memory_ingestion``
(re-exported here); candidate review (list/promote/reject/batch) lives in
``ingestion_candidates`` via ``IngestionCandidatesMixin``. This mixin keeps
ingest scheduling, per-source timeout budgets, rescan maintenance, and the
manual ``add_memory`` dedup path. Depends only on shared constants and other
leaf modules (never on ``service`` itself). Repositories and cross-domain
helpers stay on the service and are reached through ``self`` via the MRO.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from .constants import (
    SYSTEM_PROJECT_ID,
)
from .ingestion_candidates import IngestionCandidatesMixin
from .ingestion_rescan import IngestionRescanMixin
from .ingestion_timeouts import IngestionTimeoutsMixin
from .mcp import MCPError
from .memory_ingestion import (
    DUPLICATE_STOPWORDS,
    MemoryIngestionEngine,
    _token_set,
    _token_similarity,
)
from .models import utc_now
from .teams_graph import TeamsGraphError

# Re-exported for ArchitectOSService / graph_autolinker / older imports.
__all__ = [
    "DUPLICATE_STOPWORDS",
    "IngestionServiceMixin",
    "MemoryIngestionEngine",
    "_token_set",
    "_token_similarity",
]

_LOG = logging.getLogger("architectos.service")


class IngestionServiceMixin(IngestionTimeoutsMixin, IngestionRescanMixin, IngestionCandidatesMixin):
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

