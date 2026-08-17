"""Memory candidate review queue for the ingestion service.

Split out of ``ingestion_service`` so list/promote/reject/batch stays together
without bloating ingest scheduling and rescan. ``IngestionServiceMixin``
inherits ``IngestionCandidatesMixin``.
"""
from __future__ import annotations

from typing import Any

from .models import utc_now


class IngestionCandidatesMixin:
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
