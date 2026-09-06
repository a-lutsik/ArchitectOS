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
from .source_service import SourceRegistryMixin

# Re-exported for ArchitectOSService / graph_autolinker / older imports.
__all__ = [
    "DUPLICATE_STOPWORDS",
    "IngestionServiceMixin",
    "MemoryIngestionEngine",
    "_token_set",
    "_token_similarity",
]

_LOG = logging.getLogger("architectos.service")


class IngestionServiceMixin(IngestionTimeoutsMixin, IngestionRescanMixin, IngestionCandidatesMixin, SourceRegistryMixin):
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
            sources = payload.get("sources")
            if not sources:
                pid = str(payload.get("project_id") or "architectos")
                self.ensure_project_sources(pid)
                enabled = [item["id"] for item in self.list_project_sources(pid, enabled_only=True)]
                sources = enabled or [item["id"] for item in self.list_project_sources(pid)]
            else:
                sources = self._normalize_ingestion_sources(sources) if isinstance(sources, list) and sources and not str(sources[0]).startswith("source_") else list(sources)
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
        self.ensure_project_sources(project_id)
        bindings = self._sources_for_ingest_request(project_id, payload)
        prepare_limit = max(0, int(payload.get("limit") or 0))
        ingest_mode = self._normalize_ingest_mode(payload.get("ingest_mode") or payload.get("mode"))
        direct_to_memory = ingest_mode == "memory"
        source_labels = [str(item.get("name") or item.get("kind") or "") for item in bindings]
        with self._memory_ingest_lock:
            progress_owned = not bool(self._memory_ingest_state.get("running"))
        if progress_owned:
            self._reset_ingest_progress(project_id=project_id, sources=source_labels, keep_running=True)
        else:
            with self._memory_ingest_lock:
                self._memory_ingest_state["project_id"] = project_id
                self._memory_ingest_state["sources"] = source_labels
                self._memory_ingest_state["current"] = "starting"
        self._log_ingest(
            f"Starting ingest · project={project_id} · mode={ingest_mode} · sources={', '.join(source_labels) or 'none'}",
            current="starting",
        )
        candidates: list[dict[str, Any]] = []
        warnings: list[str] = []
        boards_imported: list[dict[str, Any]] = []
        boards_updated = 0
        azure_git_imported: list[dict[str, Any]] = []
        azure_git_updated = 0
        wiki_imported: list[dict[str, Any]] = []
        wiki_updated = 0
        try:
            for binding in bindings:
                name = str(binding.get("name") or binding.get("kind") or "source")
                self._log_ingest(f"Scanning {name}…", source=name, current=name)
                result = self.ingest_source_binding(binding, project_id, payload, warnings)
                candidates.extend(list(result.get("candidates") or []))
                imported = list(result.get("imported") or [])
                adapter = str(dict(binding.get("config") or {}).get("adapter") or "")
                if adapter == "azure-boards":
                    boards_imported.extend(imported)
                    boards_updated += int(result.get("updated") or 0)
                elif adapter == "azure-git":
                    azure_git_imported.extend(imported)
                    azure_git_updated += int(result.get("updated") or 0)
                elif adapter == "azure-wiki":
                    wiki_imported.extend(imported)
                    wiki_updated += int(result.get("updated") or 0)
                count = len(result.get("candidates") or []) + len(imported)
                self._log_ingest(f"{name} done · {count} item(s)", source=name)
            self._log_ingest(
                f"Preparing {'memory writes' if direct_to_memory else 'review queue'} from {len(candidates)} raw candidate(s)…",
                current="prepare",
            )
            prepared = self.ingestion_engine.prepare_candidates(project_id, candidates, prepare_limit)
            skipped_reviewed = int(getattr(self.ingestion_engine, "skipped_reviewed", 0) or 0)
            if skipped_reviewed:
                self._log_ingest(f"Skipped {skipped_reviewed} already-reviewed source(s)", current="prepare")
            if direct_to_memory:
                written_nodes = self._write_candidates_directly_to_memory(project_id, prepared)
                saved: list[dict[str, Any]] = []
            else:
                written_nodes = []
                saved = [self.repository.upsert_memory_candidate(candidate) for candidate in prepared]
                if saved:
                    self.ingestion_engine.refresh_candidate_duplicate_flags(project_id)
                    saved = [
                        self.repository.get_memory_candidate(str(item.get("id") or "")) or item
                        for item in saved
                    ]
                    auto_nodes = self._auto_promote_code_revisions(project_id, saved)
                    if auto_nodes:
                        written_nodes.extend(auto_nodes)
                        saved = [
                            self.repository.get_memory_candidate(str(item.get("id") or "")) or item
                            for item in saved
                            if str(
                                (self.repository.get_memory_candidate(str(item.get("id") or "")) or item).get("status")
                                or ""
                            )
                            == "candidate"
                        ]
            duplicates = sum(1 for candidate in saved if dict(candidate.get("metadata") or {}).get("duplicate"))
            by_source = Counter(str(dict(candidate.get("metadata") or {}).get("source_name") or candidate.get("source_type") or "manual") for candidate in saved)
            if boards_imported:
                by_source["azure"] = by_source.get("azure", 0) + len(boards_imported)
            if azure_git_imported:
                by_source["azure"] = by_source.get("azure", 0) + len(azure_git_imported)
            if wiki_imported:
                by_source["azure"] = by_source.get("azure", 0) + len(wiki_imported)
            result = {
                "project_id": project_id,
                "sources": source_labels,
                "source_ids": [str(item.get("id") or "") for item in bindings],
                "ingest_mode": ingest_mode,
                "candidates": saved,
                "count": len(saved),
                "duplicates": duplicates,
                "by_source": dict(sorted(by_source.items())),
                "pending": len(self.repository.list_memory_candidates(project_id, "candidate", 500)),
                "warnings": warnings,
                "skipped_reviewed": skipped_reviewed,
                "boards_imported": boards_imported,
                "boards_count": len(boards_imported),
                "boards_updated": boards_updated,
                "azure_git_imported": azure_git_imported,
                "azure_git_count": len(azure_git_imported),
                "azure_git_updated": azure_git_updated,
                "wiki_imported": wiki_imported,
                "wiki_count": len(wiki_imported),
                "wiki_updated": wiki_updated,
                "teams_imported": [],
                "teams_count": 0,
                "teams_updated": 0,
                "direct_imported": [node.to_dict() for node in written_nodes],
                "direct_count": len(written_nodes),
                "memory_written": len(written_nodes) + len(boards_imported) + len(azure_git_imported) + len(wiki_imported),
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

    def _auto_promote_code_revisions(self, project_id: str, candidates: list[dict[str, Any]]) -> list[Any]:
        """Auto-promote mechanical code/docs artifact revisions so Review is not flooded."""
        from .memory_ingestion import REVISIONABLE_SOURCE_TYPES

        written: list[Any] = []
        for candidate in candidates:
            meta = dict(candidate.get("metadata") or {})
            if not meta.get("revision"):
                continue
            source_type = str(candidate.get("source_type") or meta.get("source_type") or "").strip().lower()
            node_type = str(candidate.get("type") or "")
            # Only mechanical file artifacts — human facts stay in Review.
            if source_type not in REVISIONABLE_SOURCE_TYPES:
                continue
            if node_type not in {"Artifact", "Doc"} and source_type not in {"code", "docs"}:
                continue
            if str(meta.get("template") or "") in {
                "chat_session_atom", "chat_fact_keeper", "chat_turn_keeper", "mcp_turn_atom",
            }:
                continue
            cid = str(candidate.get("id") or "")
            if not cid:
                continue
            try:
                meta["auto_accepted"] = True
                candidate["metadata"] = meta
                self.repository.update_memory_candidate(candidate)
                result = self.promote_memory_candidate(cid, refresh_duplicates=False)
                memory = result.get("memory")
                if memory and memory.get("id"):
                    node = self.repository.get_node(str(memory["id"]))
                    if node:
                        written.append(node)
            except Exception as exc:  # noqa: BLE001 - never fail the whole ingest on one file
                _LOG = __import__("logging").getLogger("architectos.service")
                _LOG.warning("auto-promote code revision failed for %s: %s", cid, exc)
        if written:
            self.ingestion_engine.refresh_candidate_duplicate_flags(project_id)
        return written

    def _write_candidates_directly_to_memory(self, project_id: str, candidates: list[dict[str, Any]]) -> list[Any]:
        written = []
        for candidate in candidates:
            scope = str(candidate.get("scope") or "project")
            metadata = self._promoted_candidate_metadata(candidate)
            metadata["source"] = "autoscan_direct"
            metadata["ingest_mode"] = "memory"
            metadata["candidate_source_id"] = candidate.get("id")
            source_id = str(dict(candidate.get("metadata") or {}).get("source_id") or candidate.get("source_id") or "").strip() or None
            cand_meta = dict(candidate.get("metadata") or {})
            is_revision = bool(cand_meta.get("revision"))
            content_hash = str(cand_meta.get("content_hash") or "").strip()
            interface_id = f"rev:{content_hash}" if is_revision and content_hash else None
            node = self.repository.add_node(
                str(candidate.get("type") or "Lesson"),
                str(candidate.get("label") or "AutoScan memory"),
                scope,
                str(candidate.get("text") or ""),
                self._memory_project_id_for_scope(scope, project_id),
                interface_id,
                float(candidate.get("confidence") or 0.72),
                metadata,
                source_id=source_id,
            )
            node = self.memory_lifecycle.initialize_node(node, "autoscan_direct")
            self.graph_auto_linker.link_node(node, project_id)
            if str(candidate.get("source_type") or "") == "azure-boards":
                self._link_azure_boards_relations(node, candidate)
            revision_of = str(cand_meta.get("revision_of") or "").strip()
            if is_revision and revision_of and revision_of != node.id:
                self._apply_revision_supersede(node, revision_of, candidate)
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
        source_id = str(payload.get("source_id") or "").strip() or None
        if source_id and not self.repository.get_source(source_id):
            raise ValueError(f"source not found: {source_id}")

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
            source_id=source_id,
        )
        node = self.memory_lifecycle.initialize_node(node, "ui")
        try:
            self.repository.record_memory_event(
                node.id,
                "created",
                actor=str(payload.get("source") or "ui"),
                details={"type": node.type, "scope": node.scope, "project_id": node.project_id, "source_id": source_id or ""},
            )
        except Exception as exc:  # history is best-effort, never blocks ingestion
            _LOG.warning("memory created event skipped: %s", exc)
        if near_id and near_score >= self.NEAR_DUPLICATE_SIMILARITY:
            node.metadata["possible_duplicate_of"] = near_id
            node.metadata["possible_duplicate_score"] = round(near_score, 3)
            node = self.repository.upsert_node(node)
        self.graph_auto_linker.link_node(node, payload.get("project_id") or "architectos")
        return node.to_dict()

    def add_memory_from_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist an explicit MCP memory_add, splitting multi-fact blobs into nodes."""
        from .chat_memory import facts_from_memory_blob

        facts = facts_from_memory_blob(str(payload.get("text") or ""))
        if len(facts) < 2:
            return self.add_memory(payload)
        created: list[dict[str, Any]] = []
        for index, fact in enumerate(facts):
            item = dict(payload)
            item["text"] = fact["text"]
            item["type"] = fact["type"]
            item["label"] = str(payload.get("label") or "").strip() if index == 0 else fact["text"][:72]
            created.append(self.add_memory(item))
        primary = dict(created[0])
        primary["also_created"] = [node.get("id") for node in created[1:] if node.get("id")]
        return primary

    BULK_ADD_MAX_ITEMS = 200

    def add_memory_bulk(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Store a batch of memory nodes in one call (News→Event-style pipelines).

        Each item goes through the regular ``add_memory`` path (redaction,
        exact/near dedup, lifecycle init, auto-link, embedding queue). Items are
        isolated: a failing item is reported in ``results`` and never aborts the
        batch. Unlike ``add_memory_from_agent`` no multi-fact splitting happens —
        one item is one node. Top-level project_id/scope/source_id/type/confidence
        act as defaults that individual items may override.
        """
        raw_items: Any = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("items must be a non-empty list")
        items = raw_items[: self.BULK_ADD_MAX_ITEMS]
        defaults = {
            key: payload.get(key)
            for key in ("project_id", "scope", "source_id", "type", "confidence", "source", "source_type")
            if payload.get(key) is not None
        }
        results: list[dict[str, Any]] = []
        stats = {"created": 0, "deduplicated": 0, "errors": 0}
        ids: list[str] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                results.append({"index": index, "status": "error", "error": "item must be an object"})
                stats["errors"] += 1
                continue
            try:
                node = self.add_memory({**defaults, **item})
            except Exception as exc:  # noqa: BLE001 - per-item isolation is the contract
                results.append({"index": index, "status": "error", "error": str(exc)})
                stats["errors"] += 1
                continue
            status = "deduplicated" if node.get("dedup_merged") else "created"
            stats[status] += 1
            if status == "created" and node.get("id"):
                ids.append(node["id"])
            results.append({"index": index, "status": status, "id": node.get("id"), "label": node.get("label")})
        stats["received"] = len(raw_items)
        stats["processed"] = len(items)
        return {"ids": ids, "results": results, "stats": stats}

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

