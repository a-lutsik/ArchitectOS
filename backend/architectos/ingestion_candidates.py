"""Memory candidate review queue for the ingestion service.

Split out of ``ingestion_service`` so list/promote/reject/batch stays together
without bloating ingest scheduling and rescan. ``IngestionServiceMixin``
inherits ``IngestionCandidatesMixin``.
"""
from __future__ import annotations

from typing import Any

from .candidate_identity import candidate_origin_key
from .models import utc_now


class IngestionCandidatesMixin:
    def list_memory_candidates(
        self,
        project_id: str | None = None,
        status: str | None = "candidate",
        limit: int = 50,
        *,
        offset: int = 0,
        source_type: str | None = None,
    ) -> dict[str, Any]:
        normalized = str(status or "").strip().lower()
        duplicate_filter: bool | None = None
        revision_filter: bool | None = None
        if normalized in {"", "all", "*"}:
            status = None
        elif normalized in {"pending"}:
            status = "candidate"
        elif normalized in {"duplicate", "duplicates"}:
            status = "candidate"
            duplicate_filter = True
        elif normalized in {"revision", "revisions", "updated", "update"}:
            status = "candidate"
            revision_filter = True
        source = str(source_type or "").strip() or None
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or 50), 500))
        counts = self.repository.count_memory_candidates_by_status(project_id)
        filtered_total = self.repository.count_memory_candidates_filtered(
            project_id,
            status,
            source_type=source,
            duplicate=duplicate_filter,
            revision=revision_filter,
        )
        candidates = self.repository.list_memory_candidates(
            project_id,
            status,
            limit,
            offset=offset,
            source_type=source,
            duplicate=duplicate_filter,
            revision=revision_filter,
        )
        # Source options follow the current Show filter (not the selected Source),
        # so the dropdown lists every source that can appear under Needs review / etc.
        sources = self.repository.list_memory_candidate_sources(
            project_id, status, duplicate=duplicate_filter
        )
        if not sources and candidates:
            # Defensive fallback if the column/json aggregate returned nothing.
            tallies: dict[str, int] = {}
            for item in candidates:
                key = str(item.get("source_type") or (item.get("metadata") or {}).get("source_type") or "manual").strip() or "manual"
                tallies[key] = tallies.get(key, 0) + 1
            sources = [{"id": key, "count": n} for key, n in sorted(tallies.items(), key=lambda pair: (-pair[1], pair[0]))]
        return {
            "candidates": candidates,
            "counts": counts,
            "pending": counts.get("pending", 0),
            "accepted": counts.get("accepted", 0),
            "rejected": counts.get("rejected", 0),
            "filtered_total": filtered_total,
            "offset": offset,
            "limit": limit,
            "sources": sources,
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
        refresh_duplicates: bool = True,
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
        cand_meta = dict(candidate.get("metadata") or {})
        is_revision = bool(cand_meta.get("revision"))
        revision_of = str(cand_meta.get("revision_of") or "").strip()
        content_hash = str(cand_meta.get("content_hash") or "").strip()
        # Revisions must not collide with the predecessor's stable label-based id.
        interface_id = payload.get("interface_id")
        if interface_id is None and is_revision and content_hash:
            interface_id = f"rev:{content_hash}"
        elif interface_id is None and is_revision:
            interface_id = f"rev:{created_at}"
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
            interface_id,
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
        if is_revision and revision_of and revision_of != node.id:
            self._apply_revision_supersede(node, revision_of, candidate)
        candidate["status"] = "promoted"
        candidate["promoted_node_id"] = node.id
        candidate["promoted_at"] = utc_now()
        candidate = self.repository.update_memory_candidate(candidate)
        if refresh_duplicates:
            self._refresh_candidate_duplicate_flags(candidate)
        return {"candidate": candidate, "memory": node.to_dict()}

    def _apply_revision_supersede(self, new_node: Any, old_id: str, candidate: dict[str, Any]) -> None:
        """Link new revision → old via SUPERSEDES and stamp invalid_at on the predecessor."""
        old = self.repository.get_node(old_id)
        if not old:
            return
        try:
            self.create_graph_edge({
                "source": new_node.id,
                "target": old_id,
                "type": "SUPERSEDES",
                "scope": str(candidate.get("scope") or new_node.scope or "project"),
                "confidence": 0.9,
            })
        except Exception:
            # Fallback: stamp invalid_at even if edge creation fails (e.g. missing mixin).
            apply = getattr(self, "_apply_supersede", None)
            if callable(apply):
                apply(new_node, old)
            else:
                from .models import utc_now as _utc_now
                meta = dict(old.metadata or {})
                meta.setdefault("invalid_at", str(getattr(new_node, "created_at", "") or _utc_now()))
                meta["superseded_by"] = new_node.id
                old.metadata = meta
                self.repository.upsert_node(old)
        self._mark_related_facts_source_stale(old, new_node)

    def _mark_related_facts_source_stale(self, old_node: Any, new_node: Any) -> None:
        """Downrank Lesson/Decision nodes that reference the revised code path (not auto-SUPERSEDES)."""
        path = str(dict(old_node.metadata or {}).get("source_ref") or dict(old_node.metadata or {}).get("path") or "").strip()
        if not path:
            return
        path_lower = path.lower()
        rel = path_lower
        # Prefer relative path segment if absolute was stored.
        if "/" in rel:
            # Match on the relative source_ref style used by AutoScan.
            pass
        from .models import utc_now as _utc_now
        when = str(getattr(new_node, "created_at", "") or _utc_now())
        for node in self.repository.list_nodes():
            if node.status != "active" or node.id in {old_node.id, new_node.id}:
                continue
            if str(node.type or "") not in {"Lesson", "Decision", "Requirement"}:
                continue
            meta = dict(node.metadata or {})
            if meta.get("invalid_at"):
                continue
            blob = " ".join(
                [
                    str(node.label or ""),
                    str(node.text or ""),
                    str(meta.get("fact_subject") or ""),
                    str(meta.get("path") or ""),
                    str(meta.get("source_ref") or ""),
                ]
            ).lower()
            if path_lower not in blob and not any(
                part and part in blob for part in path_lower.replace("\\", "/").split("/")[-2:]
            ):
                continue
            meta["source_stale_at"] = when
            meta["source_stale_from"] = new_node.id
            node.metadata = meta
            self.repository.upsert_node(node)
    def _promoted_candidate_metadata(self, candidate: dict[str, Any]) -> dict[str, Any]:
        meta = dict(candidate.get("metadata") or {})
        payload = {
            "source": "memory_candidate",
            "candidate_id": candidate["id"],
            "source_type": candidate.get("source_type"),
            "source_ref": candidate.get("source_ref"),
        }
        origin_key = candidate_origin_key(candidate) or meta.get("origin_key")
        if origin_key:
            payload["origin_key"] = origin_key
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
            "meeting_id",
            "teams_meeting_id",
            "pull_request_id",
            "repository_id",
            "cluster",
            "root",
            "path",
            "content_hash",
            "structure_fingerprint",
            "import_normalized_hash",
            "symbol_body_hashes",
            "extension",
            "revision",
            "revision_of",
            "revision_of_label",
            "change_kind",
            "change_summary",
            "fact_key",
            "fact_subject",
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

    def reject_memory_candidate(
        self,
        candidate_id: str,
        payload: dict[str, Any] | None = None,
        *,
        refresh_duplicates: bool = True,
    ) -> dict[str, Any]:
        candidate = self.repository.get_memory_candidate(candidate_id)
        if not candidate:
            raise ValueError("memory candidate not found")
        candidate["status"] = "rejected"
        candidate["rejected_at"] = utc_now()
        candidate["reject_reason"] = str((payload or {}).get("reason") or "")
        updated = self.repository.update_memory_candidate(candidate)
        if refresh_duplicates:
            self._refresh_candidate_duplicate_flags(updated)
        return {"candidate": updated}

    def _refresh_candidate_duplicate_flags(self, candidate: dict[str, Any] | None = None, project_id: str | None = None) -> int:
        engine = getattr(self, "ingestion_engine", None)
        if engine is None:
            return 0
        pid = str(project_id or (candidate or {}).get("project_id") or "architectos")
        return int(engine.refresh_candidate_duplicate_flags(pid) or 0)

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
        duplicate_kind = str(payload.get("duplicate_kind") or "").strip().lower() or None
        if duplicate_kind not in {None, "memory", "candidate"}:
            raise ValueError("duplicate_kind must be memory, candidate, or omitted")
        if duplicate_kind:
            duplicate_only = True
        if status_filter == "duplicate":
            status_filter = "candidate"
            duplicate_only = True
        source_type = str(payload.get("source_type") or payload.get("source") or "").strip() or None
        process_all = bool(payload.get("all") or payload.get("process_all") or payload.get("entire_queue"))
        # Default page-sized batches stay modest; Accept/Reject all processes the full matching set.
        if process_all:
            limit = None
        else:
            limit = max(1, min(int(payload.get("limit") or 500), 5000))
        reason = str(payload.get("reason") or ("Batch rejected in Memory UI" if action == "reject" else ""))

        listed_status = None if status_filter in {None, "all"} else status_filter
        list_duplicate = True if duplicate_only else None
        candidates = self.repository.list_memory_candidates(
            project_id,
            listed_status,
            limit,
            source_type=source_type,
            duplicate=list_duplicate,
            duplicate_kind=duplicate_kind,
        )
        selected: list[dict[str, Any]] = []
        for candidate in candidates:
            meta = dict(candidate.get("metadata") or {})
            is_duplicate = bool(meta.get("duplicate"))
            if duplicate_only and not is_duplicate:
                continue
            if exclude_duplicates and is_duplicate:
                continue
            if duplicate_kind and str(meta.get("duplicate_kind") or "") != duplicate_kind:
                continue
            selected.append(candidate)

        # Reject-duplicates must leave one survivor per candidate-candidate cluster.
        if action == "reject" and duplicate_only and selected and duplicate_kind != "memory":
            selected = self._select_duplicate_rejects_keep_one(project_id, selected, source_type=source_type)

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
                        refresh_duplicates=False,
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
                    self.reject_memory_candidate(candidate_id, {"reason": reason}, refresh_duplicates=False)
                    rejected += 1
                    results.append({"id": candidate_id, "status": "rejected"})
            except Exception as exc:  # noqa: BLE001 - continue batch on single failures
                errors.append(f"{candidate_id}: {exc}")

        if promoted or rejected:
            self._refresh_candidate_duplicate_flags(project_id=project_id)

        counts = self.repository.count_memory_candidates_by_status(project_id)
        return {
            "project_id": project_id,
            "action": action,
            "status": listed_status or "all",
            "source_type": source_type,
            "duplicate_only": duplicate_only,
            "exclude_duplicates": exclude_duplicates,
            "duplicate_kind": duplicate_kind,
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

    def _select_duplicate_rejects_keep_one(
        self,
        project_id: str,
        selected: list[dict[str, Any]],
        *,
        source_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Keep one pending survivor per candidate-candidate cluster; reject the rest.

        Memory-kind duplicates (already in durable memory) are all rejected.
        """
        pending = self.repository.list_memory_candidates(project_id, "candidate", None)
        by_id = {str(item.get("id") or ""): item for item in pending if item.get("id")}
        selected_ids = {str(item.get("id") or "") for item in selected if item.get("id")}

        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        # Seed clusters from all pending so keepers outside the source filter still protect the group.
        fingerprint_groups: dict[str, list[str]] = {}
        for item in pending:
            cid = str(item.get("id") or "")
            if not cid:
                continue
            parent.setdefault(cid, cid)
            meta = dict(item.get("metadata") or {})
            dup_of = str(meta.get("duplicate_of") or "").strip()
            if dup_of and dup_of in by_id:
                union(cid, dup_of)
            fp = str(meta.get("fingerprint") or "").strip()
            if fp:
                fingerprint_groups.setdefault(fp, []).append(cid)

        for ids in fingerprint_groups.values():
            if len(ids) < 2:
                continue
            root = ids[0]
            for other in ids[1:]:
                union(root, other)

        clusters: dict[str, list[dict[str, Any]]] = {}
        for item in pending:
            cid = str(item.get("id") or "")
            if not cid:
                continue
            clusters.setdefault(find(cid), []).append(item)

        keepers: set[str] = set()
        for members in clusters.values():
            if len(members) <= 1:
                # Orphan duplicate badges / memory lookalikes: reject as selected.
                continue
            kinds = {
                str(dict(m.get("metadata") or {}).get("duplicate_kind") or "")
                for m in members
                if bool(dict(m.get("metadata") or {}).get("duplicate"))
            }
            # Memory duplicates already exist in durable memory — reject all selected.
            if "memory" in kinds:
                continue
            keeper = self._pick_duplicate_cluster_keeper(members)
            if keeper and keeper.get("id"):
                keepers.add(str(keeper["id"]))

        result: list[dict[str, Any]] = []
        for item in selected:
            cid = str(item.get("id") or "")
            if not cid or cid not in selected_ids:
                continue
            if cid in keepers:
                continue
            if source_type and str(item.get("source_type") or "") != source_type:
                continue
            result.append(item)
        return result

    def _pick_duplicate_cluster_keeper(self, members: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not members:
            return None

        def sort_key(item: dict[str, Any]) -> tuple:
            meta = dict(item.get("metadata") or {})
            is_dup = 1 if bool(meta.get("duplicate")) else 0
            created = str(item.get("created_at") or "")
            cid = str(item.get("id") or "")
            return (is_dup, created, cid)

        return sorted(members, key=sort_key)[0]

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
