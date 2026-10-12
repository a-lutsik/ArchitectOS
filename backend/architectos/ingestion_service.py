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

import json
import logging
import re
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from .chat_memory import _FACT_TYPE_RANK, _prefer_fact_type
from .constants import (
    SYSTEM_PROJECT_ID,
)
from .ingestion_candidates import IngestionCandidatesMixin
from .ingestion_rescan import IngestionRescanMixin
from .ingestion_timeouts import IngestionTimeoutsMixin
from .mcp import MCPError
from .memory_dedup import DedupDecision, MemoryDedupGate, content_salt, dedup_settings, incoming_features
from .memory_ingestion import (
    DUPLICATE_STOPWORDS,
    MemoryIngestionEngine,
    _token_set,
    _token_similarity,
)
from .models import utc_now
from .source_service import SourceRegistryMixin
from .storage import sanitize_text

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

    NEAR_DUPLICATE_SIMILARITY = MemoryDedupGate.NEAR_DUPLICATE_SIMILARITY

    def add_memory(self, payload: dict[str, Any], *, _force_new: bool = False) -> dict[str, Any]:
        """Store one fact through the dedup gate (see ``memory_dedup``).

        ``_force_new`` skips the gate but still avoids overwriting a node that
        holds the same label (used by undo).
        """
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

        dedup_enabled = payload.get("dedup", True) is not False and not _force_new
        decision = (
            self._dedup_decide(label, text, node_type, scope, project_id)
            if dedup_enabled
            else DedupDecision()
        )
        if decision.action in {"duplicate", "enrich"}:
            existing = self.repository.get_node(decision.target_id)
            if existing is not None and existing.status == "active":
                return self._fold_into_existing(existing, payload, decision)
            decision = DedupDecision()

        interface_id = payload.get("interface_id")
        # Node ids derive from type+scope+label, so a second, different fact
        # under an existing label used to replace the first one in place.
        id_salt = (
            self._label_collision_salt(node_type, label, scope, project_id, interface_id, text)
            if dedup_enabled or _force_new
            else ""
        )
        node = self.repository.add_node(
            node_type,
            label,
            scope,
            text,
            project_id,
            interface_id,
            float(payload.get("confidence") or 0.8),
            {"source": str(payload.get("source") or "ui"), "source_type": str(payload.get("source_type") or payload.get("source") or "manual")},
            source_id=source_id,
            id_salt=id_salt,
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
        node, event_id = self._apply_dedup_relation(node, decision, payload)
        self.graph_auto_linker.link_node(node, payload.get("project_id") or "architectos")
        result = node.to_dict()
        if dedup_enabled:
            result["dedup"] = decision.to_dict()
            if event_id:
                result["dedup"]["event_id"] = event_id
        return result

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
        dedup gate, lifecycle init, auto-link, embedding queue); the gate index
        is written with each node, so later items see earlier ones. Items are
        isolated: a failing item is reported in ``results`` and never aborts the
        batch. Unlike ``add_memory_from_agent``
        no multi-fact splitting happens — one item is one node. Top-level
        project_id/scope/source_id/type/confidence act as defaults that
        individual items may override.
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
        stats = {"created": 0, "deduplicated": 0, "enriched": 0, "superseded": 0, "conflicts": 0, "errors": 0}
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
            action = str((node.get("dedup") or {}).get("action") or "")
            if action == "update":
                stats["superseded"] += 1
            elif action == "conflict":
                stats["conflicts"] += 1
            elif action == "enrich":
                stats["enriched"] += 1
            if status == "created" and node.get("id"):
                ids.append(node["id"])
            entry = {"index": index, "status": status, "id": node.get("id"), "label": node.get("label")}
            if action:
                entry["dedup_action"] = action
            results.append(entry)
        stats["received"] = len(raw_items)
        stats["processed"] = len(items)
        return {"ids": ids, "results": results, "stats": stats}

    def preview_memory_dedup(self, payload: dict[str, Any]) -> dict[str, Any]:
        """What the dedup gate would do with this fact, without writing anything."""
        label = str(payload.get("label") or "").strip()
        text = str(payload.get("text") or "").strip()
        if not label and not text:
            raise ValueError("label or text is required")
        scope = str(payload.get("scope") or "project")
        node_type = str(payload.get("type") or "Lesson")
        project_id = self._memory_project_id_for_scope(scope, payload.get("project_id") or "architectos")
        decision = self._dedup_decide(
            label, text, node_type, scope, project_id, allow_llm=payload.get("use_llm", True) is not False,
        )
        return {"label": label, "type": node_type, "scope": scope, "project_id": project_id, "dedup": decision.to_dict()}

    def memory_dedup_settings(self) -> dict[str, Any]:
        return dedup_settings(self.repository.get_setting("memory_dedup"))

    def _dedup_decide(
        self,
        label: str,
        text: str,
        node_type: str,
        scope: str,
        project_id: str | None,
        *,
        allow_llm: bool = True,
    ) -> DedupDecision:
        """Full gate decision: deterministic rules, then the optional gray zone.

        1. ``MemoryDedupGate.classify`` (index, stdlib, no network).
        2. Still ``new``: neighbours from embeddings (real provider only) and the
           lexical near match go through ``gray_candidates``; with
           ``llm_judge`` on, the best ones are judged duplicate / enrich / new.
           Otherwise a close semantic neighbour only sets the
           possible-duplicate flag.
        3. ``mode: suggest`` turns any non-new decision into a new node that
           records what would have happened.
        """
        settings = self.memory_dedup_settings()
        if settings["mode"] == "off":
            return DedupDecision()
        decision = self.memory_dedup.classify(label, text, node_type, scope, project_id)
        if decision.action == "new":
            decision = self._dedup_gray_zone(decision, label, text, node_type, scope, project_id, settings, allow_llm)
        if settings["mode"] == "suggest" and decision.action != "new":
            return DedupDecision(
                near_id=decision.target_id,
                near_score=max(decision.score, decision.near_score),
                details={**decision.details, "suggested_action": decision.action, "suggested_reason": decision.reason},
            )
        return decision

    def _dedup_gray_zone(
        self,
        decision: DedupDecision,
        label: str,
        text: str,
        node_type: str,
        scope: str,
        project_id: str | None,
        settings: dict[str, Any],
        allow_llm: bool,
    ) -> DedupDecision:
        semantic = self._semantic_dedup_scores(label, text, scope, project_id, settings)
        candidate_ids = set(semantic)
        if decision.near_id:
            candidate_ids.add(decision.near_id)
        if not candidate_ids:
            return decision
        rows = [
            row for row in self.repository.dedup_rows_by_ids(sorted(candidate_ids))
            if row["scope"] == scope and row["project_key"] == (project_id or "")
        ]
        incoming = incoming_features(label, text)
        if allow_llm and settings["llm_judge"] and settings["llm_max_candidates"]:
            gray = self.memory_dedup.gray_candidates(
                incoming, rows, node_type, semantic_scores=semantic, settings=settings,
            )
            for candidate in gray[: settings["llm_max_candidates"]]:
                verdict = self._judge_dedup_pair(label, text, node_type, candidate, project_id, settings)
                action = verdict.get("action")
                if action in {"duplicate", "enrich"} and float(verdict.get("confidence") or 0.0) >= settings["llm_min_confidence"]:
                    return DedupDecision(
                        action,
                        target_id=candidate["node_id"],
                        target_label=candidate["label"],
                        score=float(verdict.get("confidence") or 0.0),
                        reason=f"llm judge: {verdict.get('reason') or action}",
                        near_id=candidate["node_id"],
                        near_score=candidate["rank"],
                        details={
                            "judge": "llm",
                            "provider": verdict.get("provider", ""),
                            "lexical": round(candidate["lexical"], 3),
                            "cosine": round(candidate["cosine"], 3),
                        },
                    )
        if decision.near_score < self.NEAR_DUPLICATE_SIMILARITY:
            typed = [
                row for row in rows
                if self.memory_dedup.type_matches(str(row["node_type"]), node_type) and str(row["node_id"]) in semantic
            ]
            best = max(typed, key=lambda row: (semantic[str(row["node_id"])], str(row["node_id"])), default=None)
            if best is not None and semantic[str(best["node_id"])] >= settings["semantic_flag_score"]:
                decision.near_id = str(best["node_id"])
                decision.near_score = semantic[decision.near_id]
                decision.details = {**decision.details, "near_kind": "semantic"}
        return decision

    def _semantic_dedup_scores(
        self, label: str, text: str, scope: str, project_id: str | None, settings: dict[str, Any]
    ) -> dict[str, float]:
        """Cosine of the closest stored nodes, or {} without a real embedding provider.

        The offline hash embedder is lexical, not semantic, so it is skipped.
        Bounded by ``memory_retrieval.vector_query_timeout_ms`` like Search.
        """
        if settings["semantic"] == "off":
            return {}
        engine = self.memory_embeddings
        if not engine.enabled() or str(getattr(engine.provider, "provider_id", "") or "") in {"", "hash"}:
            return {}
        try:
            timeout_s = max(0.05, min(int(engine.settings().get("vector_query_timeout_ms") or 2500), 5000) / 1000.0)
        except (TypeError, ValueError):
            timeout_s = 2.5
        scores, _elapsed, _timed_out = self._vector_search_memory_timed(
            sanitize_text(f"{label}\n{text}")[0], project_id=project_id, scope=scope, limit=8, timeout_s=timeout_s,
        )
        return scores

    _DEDUP_JUDGE_ACTIONS = frozenset({"duplicate", "enrich", "new"})

    def _judge_dedup_pair(
        self,
        label: str,
        text: str,
        node_type: str,
        candidate: dict[str, Any],
        project_id: str | None,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        """Ask the routed LLM whether an incoming fact repeats or extends a stored one."""
        from .adapters import ProviderRequest

        stored = self.repository.get_node(str(candidate["node_id"]))
        if stored is None:
            return {"action": "new", "error": "stored node missing"}
        prompt = (
            "You deduplicate a project memory. Compare an INCOMING fact with a STORED fact and answer:\n"
            '- "duplicate": both state the same fact and INCOMING adds nothing that STORED lacks.\n'
            '- "enrich": the same fact, INCOMING keeps everything STORED says and adds details.\n'
            '- "new": different facts or subjects, any changed value, name, negation or scope, or if unsure.\n'
            "Answer with a single JSON object only:\n"
            '{"action": "duplicate"|"enrich"|"new", "reason": "<short>", "confidence": 0.0-1.0}\n\n'
            f"STORED [{stored.type}] {stored.label}:\n{(stored.text or '')[:1200]}\n\n"
            f"INCOMING [{node_type}] {sanitize_text(label)[0]}:\n{sanitize_text(text)[0][:1200]}"
        )
        request = ProviderRequest(message=prompt, context="", project_id=project_id or "architectos", role="review")
        try:
            result = self.provider_router.route(
                self.repository.list_providers(), request, settings["llm_provider_id"] or None,
            )
        except Exception as exc:  # noqa: BLE001 - the judge is optional, never fail the write
            _LOG.warning("dedup judge failed: %s", exc)
            return {"action": "new", "error": str(exc)}
        verdict = self._parse_dedup_verdict(str(result.get("text") or ""))
        verdict["provider"] = str((result.get("selected_provider") or {}).get("id") or "")
        return verdict

    @classmethod
    def _parse_dedup_verdict(cls, text: str) -> dict[str, Any]:
        match = re.search(r"\{.*\}", text or "", re.DOTALL)
        if not match:
            return {"action": "new", "error": "no JSON in LLM response"}
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return {"action": "new", "error": "invalid JSON in LLM response"}
        if not isinstance(data, dict):
            return {"action": "new", "error": "unexpected LLM response shape"}
        action = str(data.get("action") or "new").strip().lower()
        try:
            confidence = max(0.0, min(float(data.get("confidence") or 0.0), 1.0))
        except (TypeError, ValueError):
            confidence = 0.0
        return {
            "action": action if action in cls._DEDUP_JUDGE_ACTIONS else "new",
            "reason": str(data.get("reason") or "")[:300],
            "confidence": confidence,
        }

    def rebuild_memory_dedup_index(self) -> dict[str, Any]:
        """Recompute the dedup gate index from ``memory_nodes`` (repair / manual reindex)."""
        return self.repository.rebuild_dedup_index()

    def _label_collision_salt(
        self,
        node_type: str,
        label: str,
        scope: str,
        project_id: str | None,
        interface_id: Any,
        text: str,
    ) -> str:
        """Salt the new node's id when a live node already holds its label-derived id.

        Tombstoned (archived/merged/deleted) nodes keep the former overwrite
        behaviour: re-adding under their label revives the id.
        """
        existing_id = self.repository.memory_node_id(node_type, label, scope, project_id, interface_id)
        existing = self.repository.get_node(existing_id)
        if existing is None or existing.status != "active":
            return ""
        return content_salt(text)

    def _apply_dedup_relation(
        self, node: Any, decision: DedupDecision, payload: dict[str, Any]
    ) -> tuple[Any, str]:
        """Link a freshly created node to the fact it updates or contradicts."""
        target_id = decision.target_id
        if decision.action in {"update", "conflict"} and target_id and target_id != node.id:
            edge_type = "SUPERSEDES" if decision.action == "update" else "CONTRADICTS"
            edge_id = ""
            try:
                # SUPERSEDES stamps invalid_at on the old fact (non-destructive).
                edge = self.create_graph_edge({
                    "source": node.id,
                    "target": target_id,
                    "type": edge_type,
                    "scope": node.scope,
                    "confidence": 0.9 if decision.action == "update" else 0.8,
                })
                edge_id = str((edge.get("edge") or {}).get("id") or "")
            except ValueError as exc:
                _LOG.warning("dedup %s edge skipped: %s", edge_type, exc)
            meta = dict(node.metadata or {})
            meta["dedup_action"] = decision.action
            if decision.action == "update":
                meta["supersedes"] = target_id
            else:
                meta["conflicts_with"] = target_id
                meta["needs_review"] = "conflict"
            node.metadata = meta
            node = self.repository.upsert_node(node)
            event_id = self._record_dedup_event(
                node.id,
                payload,
                {**decision.to_dict(), "edge_id": edge_id},
            )
            return node, event_id
        suggested = str(decision.details.get("suggested_action") or "")
        if decision.near_id and decision.near_id != node.id and (
            suggested or decision.near_score >= self.NEAR_DUPLICATE_SIMILARITY
        ):
            node.metadata["possible_duplicate_of"] = decision.near_id
            node.metadata["possible_duplicate_score"] = round(decision.near_score, 3)
            if decision.details.get("near_kind"):
                node.metadata["possible_duplicate_kind"] = decision.details["near_kind"]
            if suggested:
                # mode=suggest: the gate's verdict, for review instead of applied.
                node.metadata["dedup_suggestion"] = {
                    "action": suggested,
                    "target_id": decision.near_id,
                    "reason": str(decision.details.get("suggested_reason") or ""),
                }
            node = self.repository.upsert_node(node)
        return node, ""

    def _record_dedup_event(self, node_id: str, payload: dict[str, Any], details: dict[str, Any]) -> str:
        try:
            event = self.repository.record_memory_event(
                node_id, "dedup", actor=str(payload.get("source") or "ui"), details=details,
            )
        except Exception as exc:  # history is best-effort, never blocks ingestion
            _LOG.warning("dedup event skipped: %s", exc)
            return ""
        return str(event.get("id") or "")

    @staticmethod
    def _dedup_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
        """Incoming fact as undo needs it. Redacted like a stored node: no secrets in the DB."""
        snapshot = {
            key: payload.get(key)
            for key in ("type", "scope", "project_id", "interface_id", "source", "source_type", "source_id", "confidence")
            if payload.get(key) not in (None, "")
        }
        snapshot["label"] = sanitize_text(str(payload.get("label") or ""))[0]
        snapshot["text"] = sanitize_text(str(payload.get("text") or ""))[0]
        return snapshot

    def _fold_into_existing(
        self, existing: Any, payload: dict[str, Any], decision: DedupDecision | None = None
    ) -> dict[str, Any]:
        """Fold a re-added (``duplicate``) or fuller (``enrich``) fact into a stored node.

        Bump confidence toward the incoming value, record a dedup hit and its
        source, keep the stronger fact type, and for ``enrich`` replace the
        stored text with the fuller incoming one (the old text goes into the
        event). Logs a ``dedup`` event that ``undo_memory_dedup`` can reverse
        and returns the surviving node marked ``dedup_merged`` so callers can
        tell no new node was created.
        """
        decision = decision or DedupDecision(
            "duplicate", target_id=existing.id, target_label=str(existing.label or ""), score=1.0,
            reason="exact restatement",
        )
        meta = dict(existing.metadata or {})
        meta["dedup_hits"] = int(meta.get("dedup_hits") or 0) + 1
        meta["last_dedup_at"] = utc_now()
        sources = list(meta.get("dedup_sources") or [])
        sources.append({
            "source": str(payload.get("source") or "ui"),
            "source_type": str(payload.get("source_type") or payload.get("source") or "manual"),
            "at": meta["last_dedup_at"],
        })
        meta["dedup_sources"] = sources[-20:]
        # Cross-type restatement keeps the stronger fact type (Constraint >
        # Requirement > Decision > Lesson); refresh_nodes below then re-evaluates
        # the lifecycle tier for the new type.
        incoming_type = str(payload.get("type") or "")
        previous_type = str(existing.type or "")
        if incoming_type in _FACT_TYPE_RANK and previous_type in _FACT_TYPE_RANK:
            stronger = _prefer_fact_type(previous_type, incoming_type)
            if stronger != previous_type:
                existing.type = stronger
                meta["dedup_type_from"] = previous_type
        previous_text = str(existing.text or "")
        if decision.action == "enrich":
            existing.text = sanitize_text(str(payload.get("text") or ""))[0] or previous_text
            meta["dedup_enrichments"] = int(meta.get("dedup_enrichments") or 0) + 1
            meta["last_enriched_at"] = meta["last_dedup_at"]
        existing.metadata = meta
        try:
            existing.confidence = max(float(existing.confidence or 0.0), float(payload.get("confidence") or 0.0))
        except (TypeError, ValueError):
            pass
        existing = self.repository.upsert_node(existing)
        details = {**decision.to_dict(), "incoming": self._dedup_snapshot(payload)}
        if existing.type != previous_type:
            details.update({"type_from": previous_type, "type_to": existing.type})
        if existing.text != previous_text:
            details["previous_text"] = previous_text
        event_id = self._record_dedup_event(existing.id, payload, details)
        try:
            self.memory_lifecycle.refresh_nodes([existing.id], "dedup")
        except Exception as exc:  # lifecycle bump is best-effort
            _LOG.warning("dedup refresh skipped: %s", exc)
        refreshed = self.repository.get_node(existing.id) or existing
        result = refreshed.to_dict()
        result["dedup_merged"] = True
        result["dedup"] = decision.to_dict()
        if event_id:
            result["dedup"]["event_id"] = event_id
        return result

    def undo_memory_dedup(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Reverse one gate decision recorded as a ``dedup`` memory event.

        - duplicate / enrich: the swallowed fact is stored as its own node; the
          dedup hit, any type upgrade and (enrich) the replaced text of the
          surviving node are rolled back;
        - update / conflict: the SUPERSEDES / CONTRADICTS edge is removed and the
          old fact is valid again; the newer node stays.
        """
        event_id = str(payload.get("event_id") or "").strip()
        event = self.repository.get_memory_event(event_id) if event_id else None
        if not event or event.get("event_type") != "dedup":
            raise ValueError("dedup event not found")
        details = dict(event.get("details") or {})
        action = str(details.get("action") or "")
        node = self.repository.get_node(str(event.get("node_id") or ""))
        if node is None:
            raise ValueError("memory not found")
        meta = dict(node.metadata or {})
        undone = list(meta.get("dedup_undone") or [])
        if event_id in undone:
            raise ValueError("dedup event already undone")
        restored: dict[str, Any] | None = None
        if action in {"duplicate", "enrich"}:
            if "previous_text" in details:
                incoming_text = str(dict(details.get("incoming") or {}).get("text") or "")
                if node.text != incoming_text:
                    raise ValueError("memory text changed since the merge; undo it manually")
                node.text = str(details["previous_text"])
                meta["dedup_enrichments"] = max(0, int(meta.get("dedup_enrichments") or 0) - 1)
            meta["dedup_hits"] = max(0, int(meta.get("dedup_hits") or 0) - 1)
            type_from = str(details.get("type_from") or "")
            if type_from and node.type == details.get("type_to"):
                node.type = type_from
                meta.pop("dedup_type_from", None)
        elif action in {"update", "conflict"}:
            edge_id = str(details.get("edge_id") or "")
            if edge_id:
                self.repository.delete_edge(edge_id)
            target = self.repository.get_node(str(details.get("target_id") or ""))
            if action == "update" and target is not None:
                target_meta = dict(target.metadata or {})
                if target_meta.get("superseded_by") == node.id:
                    target_meta.pop("invalid_at", None)
                    target_meta.pop("superseded_by", None)
                    target.metadata = target_meta
                    self.repository.upsert_node(target)
            for key in ("dedup_action", "supersedes", "conflicts_with", "needs_review"):
                meta.pop(key, None)
        else:
            raise ValueError(f"dedup action cannot be undone: {action or 'unknown'}")
        meta["dedup_undone"] = [*undone, event_id]
        node.metadata = meta
        node = self.repository.upsert_node(node)
        if action in {"duplicate", "enrich"}:
            incoming = dict(details.get("incoming") or {})
            restored = self.add_memory(incoming, _force_new=True)
            restored_node = self.repository.get_node(str(restored.get("id") or ""))
            if restored_node is not None:
                restored_node.metadata["dedup_restored_from"] = event_id
                restored = self.repository.upsert_node(restored_node).to_dict()
        try:
            self.repository.record_memory_event(
                node.id,
                "dedup_undo",
                actor=str(payload.get("actor") or "ui"),
                details={"event_id": event_id, "action": action, "restored_id": (restored or {}).get("id", "")},
            )
        except Exception as exc:  # history is best-effort
            _LOG.warning("dedup undo event skipped: %s", exc)
        return {"event_id": event_id, "action": action, "memory": node.to_dict(), "restored": restored}

    def _memory_project_id_for_scope(self, scope: str, project_id: str | None) -> str | None:
        normalized = str(scope or "project")
        if normalized in {"shared", "global"}:
            return None
        return project_id or "architectos"

