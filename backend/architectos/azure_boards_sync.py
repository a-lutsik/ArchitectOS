"""Azure Boards work-item import and memory upserts.

Payload parsing helpers live in ``azure_boards_parse``; this module keeps
ingest/upsert/chunk sync and inherits ``AzureBoardsParseMixin``.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any

from .azure_boards_parse import AzureBoardsParseMixin
from .chunking import (
    work_item_comment_chunks,
    work_item_description_chunks,
    work_item_main_parts,
)
from .constants import (
    ADO_BOARD_MEMORY_TYPES,
    ADO_BOARD_WORK_ITEM_TYPES,
    ADO_FETCH_WORKERS,
    ADO_ITEM_TIMEOUT,
)
from .mcp import MCPError
from .models import stable_id, utc_now
from .tool_gateway import call_ado_tool

_LOG = logging.getLogger("architectos.service")


class AzureBoardsSyncMixin(AzureBoardsParseMixin):
    """Azure Boards work-item ingest, chunking, and memory upserts."""

    def _import_azure_boards_to_memory(self, project_id: str, limit: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        items = self._ingest_azure_boards_candidates(project_id, limit, payload)
        imported: list[dict[str, Any]] = []
        updated = 0
        nodes: list[Any] = []
        chunk_nodes = 0
        for item in items:
            node, was_update = self._upsert_azure_boards_memory_node(project_id, item)
            if was_update:
                updated += 1
            chunk_nodes += self._sync_azure_boards_work_item_chunks(node, item, project_id)
            nodes.append((node, item))
            imported.append(node.to_dict())
            self._set_ingest_partial(payload, {
                "imported": list(imported),
                "updated": updated,
                "count": len(imported),
                "chunk_nodes": chunk_nodes,
            })

        # Build work item lookup map once to optimize DB queries
        by_work_item: dict[str, Any] = {}
        for existing in self.repository.list_nodes():
            if existing.status != "active":
                continue
            if existing.project_id not in {project_id, None}:
                continue
            meta = dict(existing.metadata or {})
            if str(meta.get("chunk_role") or "").strip():
                continue
            work_item_id = str(meta.get("work_item_id") or "").strip()
            if work_item_id:
                by_work_item[work_item_id] = existing

        for node, item in nodes:
            self._link_azure_boards_relations(node, item, by_work_item)
        result = {
            "imported": imported,
            "updated": updated,
            "count": len(imported),
            "chunk_nodes": chunk_nodes,
        }
        self._set_ingest_partial(payload, result)
        return result

    def _upsert_azure_boards_memory_node(self, project_id: str, item: dict[str, Any]) -> tuple[Any, bool]:
        metadata = self._azure_boards_memory_metadata(item)
        work_item_id = str(metadata.get("work_item_id") or "").strip()
        existing = self._find_memory_node_by_work_item_id(project_id, work_item_id) if work_item_id else None
        if existing:
            existing.type = str(item.get("type") or existing.type)
            existing.label = str(item.get("label") or existing.label)
            existing.text = str(item.get("text") or existing.text)
            existing.confidence = float(item.get("confidence") or existing.confidence or 0.8)
            merged = dict(existing.metadata or {})
            merged.update(metadata)
            existing.metadata = merged
            existing.updated_at = utc_now()
            node = self.repository.upsert_node(existing)
            node = self.memory_lifecycle.initialize_node(node, "azure_boards_refresh")
            self.graph_auto_linker.link_node(node, project_id)
            return node, True
        node = self.repository.add_node(
            str(item.get("type") or "Artifact"),
            str(item.get("label") or f"ADO work item {work_item_id}"),
            str(item.get("scope") or "project"),
            str(item.get("text") or ""),
            self._memory_project_id_for_scope(str(item.get("scope") or "project"), project_id),
            None,
            float(item.get("confidence") or 0.8),
            metadata,
        )
        node = self.memory_lifecycle.initialize_node(node, "azure_boards")
        self.graph_auto_linker.link_node(node, project_id)
        return node, False

    def _azure_boards_memory_metadata(self, item: dict[str, Any]) -> dict[str, Any]:
        meta = dict(item.get("metadata") or {})
        payload = {
            "source": "azure_boards",
            "source_type": "azure-boards",
            "source_ref": item.get("source_ref") or meta.get("work_item_id"),
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
            "chunk_role",
            "description_full",
            "comment_chunk_count",
            "description_chunk_count",
        ):
            if meta.get(key) not in (None, "", [], {}):
                payload[key] = meta[key]
        return payload

    def _find_memory_node_by_work_item_id(self, project_id: str, work_item_id: str):
        for node in self.repository.list_nodes():
            if node.status != "active":
                continue
            if node.project_id not in {project_id, None}:
                continue
            meta = dict(node.metadata or {})
            if str(meta.get("chunk_role") or "") in {"comment", "description"}:
                continue
            if str(meta.get("work_item_id") or "").strip() == work_item_id:
                return node
        return None

    def _ingest_azure_boards_candidates(self, project_id: str, limit: int, payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        payload = payload or {}
        if "_ingest_source_deadline" not in payload:
            payload = self._with_ingest_timeouts("azure-boards", payload)
        server = self.mcp_manager.get_server("azure-devops")
        if not server or not server.enabled:
            raise ValueError("Enable Azure DevOps MCP server before ingesting Boards work items.")
        ado_project = self._azure_boards_project(payload)
        item_timeout = self._ingest_item_timeout(payload, ADO_ITEM_TIMEOUT)
        self._log_ingest("Collecting work item IDs from Azure Boards...", source="azure-boards", current="azure-boards")
        ids = self._azure_boards_collect_ids(ado_project, limit, payload)
        if self._ingest_deadline_expired(payload, "azure-boards"):
            return []
        if not ids:
            self._log_ingest("No work items found in Azure Boards.", source="azure-boards", current="azure-boards")
            return []

        items_to_fetch = ids[:limit]
        total_items = len(items_to_fetch)
        # stdio MCP cannot usefully multiplex; keep workers at 1 unless explicitly raised.
        workers = min(ADO_FETCH_WORKERS, total_items) or 1
        self._log_ingest(
            f"Found {len(ids)} work item(s). Fetching details for up to {limit} items "
            f"({workers} at a time, item_timeout={item_timeout:g}s)...",
            source="azure-boards",
            current="azure-boards",
        )

        results: list[dict[str, Any] | None] = [None] * total_items
        mcp_lock = threading.Lock()

        def fetch_one(idx: int, work_item_id: int) -> str:
            # Serialize MCP calls: parallel requests into one stdio process stall each other.
            with mcp_lock:
                if self._ingest_deadline_expired(payload, "azure-boards"):
                    return f"timeout:{work_item_id}"
                self._log_ingest(
                    f"[{idx + 1}/{total_items}] Fetching work item #{work_item_id}...",
                    source="azure-boards",
                    current="azure-boards",
                )
                try:
                    candidate = self._azure_boards_work_item_candidate(
                        project_id,
                        ado_project,
                        work_item_id,
                        item_timeout=item_timeout,
                    )
                    results[idx] = candidate
                    if candidate:
                        rels = len(list((candidate.get("metadata") or {}).get("relations") or []))
                        # Publish mid-flight so a source timeout can salvage downloads.
                        self._set_ingest_partial(payload, [c for c in results if c is not None])
                        return f"ok:{work_item_id}:{rels}"
                    return f"skip:{work_item_id}"
                except Exception as exc:  # noqa: BLE001 - one bad item must not stop the run
                    self._log_ingest(
                        f"Failed to fetch work item #{work_item_id}: {exc}",
                        level="warn",
                        source="azure-boards",
                        current="azure-boards",
                    )
                    return f"error:{work_item_id}"

        completed = 0
        # Avoid ``with ThreadPoolExecutor``: on timeout/abandon its ``__exit__`` would
        # ``shutdown(wait=True)`` and block on wedged MCP workers past the source budget.
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {
                executor.submit(fetch_one, idx, work_item_id): work_item_id
                for idx, work_item_id in enumerate(items_to_fetch)
            }
            pending = set(futures)
            while pending:
                remaining = self._ingest_deadline_remaining(payload)
                if remaining is not None and remaining <= 0:
                    self._log_ingest(
                        f"Azure Boards source timeout — cancelling {len(pending)} remaining item(s).",
                        level="warn",
                        source="azure-boards",
                        current="azure-boards",
                    )
                    for future in pending:
                        future.cancel()
                    self._abort_ingest_source("azure-boards")
                    self._set_ingest_partial(payload, [c for c in results if c is not None])
                    break
                wait_for = item_timeout + 5.0
                if remaining is not None:
                    wait_for = max(0.1, min(wait_for, remaining))
                try:
                    for future in as_completed(pending, timeout=wait_for):
                        pending.discard(future)
                        completed += 1
                        work_item_id: int | None = futures[future]
                        status = "done"
                        try:
                            status = future.result(timeout=0) or "done"
                        except Exception:
                            status = "error"
                        if status.startswith("ok:"):
                            parts = status.split(":")
                            rels = parts[2] if len(parts) > 2 else "?"
                            self._log_ingest(
                                f"[{completed}/{total_items}] Fetched work item #{work_item_id} ({rels} relations).",
                                source="azure-boards",
                                current="azure-boards",
                            )
                        elif status.startswith("skip:"):
                            self._log_ingest(
                                f"[{completed}/{total_items}] Skipped work item #{work_item_id}.",
                                source="azure-boards",
                                current="azure-boards",
                            )
                        elif status.startswith("timeout:"):
                            self._log_ingest(
                                f"[{completed}/{total_items}] Timed out work item #{work_item_id}.",
                                level="warn",
                                source="azure-boards",
                                current="azure-boards",
                            )
                        else:
                            self._log_ingest(
                                f"[{completed}/{total_items}] Failed work item #{work_item_id}.",
                                level="warn",
                                source="azure-boards",
                                current="azure-boards",
                            )
                        break
                except FuturesTimeoutError:
                    # No future finished within the wait window — likely a wedged MCP item.
                    self._log_ingest(
                        f"Azure Boards item wait exceeded {wait_for:g}s — restarting MCP and continuing.",
                        level="warn",
                        source="azure-boards",
                        current="azure-boards",
                    )
                    self._abort_ingest_source("azure-boards")
                    # Drop one stuck future if possible so the loop can progress.
                    stuck = next(iter(pending), None)
                    if stuck is not None:
                        pending.discard(stuck)
                        stuck.cancel()
                        completed += 1
                        work_item_id = futures.get(stuck)
                        if work_item_id:
                            self._log_ingest(
                                f"[{completed}/{total_items}] Abandoned work item #{work_item_id} after timeout.",
                                level="warn",
                                source="azure-boards",
                                current="azure-boards",
                            )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        candidates = [c for c in results if c is not None]
        self._set_ingest_partial(payload, candidates)
        if candidates and self._ingest_deadline_remaining(payload) is not None:
            remaining = self._ingest_deadline_remaining(payload)
            if remaining is not None and remaining <= 0:
                self._log_ingest(
                    f"Azure Boards returning {len(candidates)} candidate(s) collected before source timeout.",
                    level="warn",
                    source="azure-boards",
                    current="azure-boards",
                )
        return candidates

    def _azure_boards_collect_ids(self, ado_project: str, limit: int, payload: dict[str, Any]) -> list[int]:
        requested = payload.get("work_item_ids") or payload.get("ids")
        if isinstance(requested, list) and requested:
            ids: list[int] = []
            for item in requested:
                try:
                    value = int(item)
                except (TypeError, ValueError):
                    continue
                if value > 0 and value not in ids:
                    ids.append(value)
            return ids[: max(limit * 2, limit)]

        collected: list[int] = []
        seen: set[int] = set()
        item_timeout = self._ingest_item_timeout(payload, ADO_ITEM_TIMEOUT)

        def add_ids(values: list[int]) -> None:
            for value in values:
                if value > 0 and value not in seen:
                    seen.add(value)
                    collected.append(value)

        # Boards ingest covers the whole project by default; "assigned to me" is an explicit opt-in.
        mine_only = bool(payload.get("mine_only") or payload.get("assigned_to_me"))
        raw_all_items = payload.get("all_items")
        all_items = (True if raw_all_items is None else bool(raw_all_items)) and not mine_only
        types = self._normalize_azure_boards_work_item_types(payload.get("work_item_types"))
        if not types:
            self._log_ingest("No Azure Boards work item types selected.", level="warn", source="azure-boards", current="azure-boards")
            return []
        if self._ingest_deadline_expired(payload, "azure-boards"):
            return []
        if all_items:
            add_ids(self._azure_boards_ids_from_wiql(ado_project, max(limit * 2, 50), types, timeout=item_timeout))
        else:
            add_ids(self._azure_boards_ids_from_my_work(ado_project, max(limit * 2, 20), timeout=item_timeout))

        if len(collected) < limit and not self._ingest_deadline_expired(payload, "azure-boards"):
            search_text = str(payload.get("search_text") or payload.get("query") or "a OR e OR i OR o OR u").strip() or "a OR e"
            add_ids(self._azure_boards_ids_from_search(ado_project, search_text, types, max(limit * 2, 20), timeout=item_timeout))
        return collected

    def _normalize_azure_boards_work_item_types(self, raw: Any) -> list[str]:
        if raw in (None, "", "all", "*"):
            return list(ADO_BOARD_WORK_ITEM_TYPES)
        values = raw if isinstance(raw, list) else [raw]
        known = {item.lower(): item for item in ADO_BOARD_WORK_ITEM_TYPES}
        selected: list[str] = []
        for value in values:
            key = str(value or "").strip().lower()
            if not key:
                continue
            canonical = known.get(key)
            if canonical and canonical not in selected:
                selected.append(canonical)
        return selected

    def _azure_boards_ids_from_wiql(
        self,
        ado_project: str,
        top: int,
        work_item_types: list[str],
        *,
        timeout: float | None = None,
    ) -> list[int]:
        self._log_ingest("Running WIQL query to fetch latest project work items...", source="azure-boards", current="azure-boards")
        escaped_types = ", ".join(f"'{item.replace("'", "''")}'" for item in work_item_types)
        type_clause = f" AND [System.WorkItemType] IN ({escaped_types})" if escaped_types else ""
        try:
            result = call_ado_tool(
                self.mcp_manager,
                "wiql",
                {
                    "wiql": f"SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = '{ado_project}'{type_clause} ORDER BY [System.ChangedDate] DESC",
                    "project": ado_project,
                    "top": max(1, int(top)),
                },
                timeout=timeout or ADO_ITEM_TIMEOUT,
            )
        except MCPError as exc:
            self._log_ingest(f"WIQL query failed: {exc}", level="warn", source="azure-boards", current="azure-boards")
            return []
        ids = self._azure_boards_ids_from_payload(result.get("result") or result)[:top]
        self._log_ingest(f"WIQL query returned {len(ids)} item ID(s).", source="azure-boards", current="azure-boards")
        return ids

    def _azure_boards_ids_from_my_work(self, ado_project: str, top: int, *, timeout: float | None = None) -> list[int]:
        self._log_ingest("Fetching work items assigned to me...", source="azure-boards", current="azure-boards")
        try:
            result = call_ado_tool(
                self.mcp_manager,
                "my_work",
                {"project": ado_project, "type": "assignedtome", "top": top, "includeCompleted": True},
                timeout=timeout or ADO_ITEM_TIMEOUT,
            )
        except MCPError as exc:
            self._log_ingest(f"my work items query failed: {exc}", level="warn", source="azure-boards", current="azure-boards")
            return []
        ids = self._azure_boards_ids_from_payload(result.get("result") or result)
        self._log_ingest(f"Found {len(ids)} item(s) assigned to me.", source="azure-boards", current="azure-boards")
        return ids

    def _azure_boards_ids_from_search(
        self,
        ado_project: str,
        search_text: str,
        work_item_types: list[str],
        top: int,
        *,
        timeout: float | None = None,
    ) -> list[int]:
        self._log_ingest(f"Searching work items matching '{search_text}'...", source="azure-boards", current="azure-boards")
        ids: list[int] = []
        call_timeout = timeout or ADO_ITEM_TIMEOUT
        for work_item_type in work_item_types:
            self._log_ingest(f"Searching type {work_item_type}...", source="azure-boards", current="azure-boards")
            try:
                result = call_ado_tool(
                    self.mcp_manager,
                    "search",
                    {
                        "searchText": search_text,
                        "project": ado_project,
                        "workItemType": [work_item_type],
                        "top": min(top, 25),
                        "skip": 0,
                    },
                    timeout=call_timeout,
                )
            except MCPError:
                continue
            ids.extend(self._azure_boards_ids_from_payload(result.get("result") or result))
            if len(ids) >= top:
                break
        deduped: list[int] = []
        seen: set[int] = set()
        for value in ids:
            if value not in seen:
                seen.add(value)
                deduped.append(value)
        self._log_ingest(f"Search returned {len(deduped[:top])} item ID(s).", source="azure-boards", current="azure-boards")
        return deduped[:top]

    def _azure_boards_work_item_candidate(
        self,
        project_id: str,
        ado_project: str,
        work_item_id: int,
        *,
        item_timeout: float | None = None,
    ) -> dict[str, Any] | None:
        # Prefer relations over expand=all: "all" pulls revisions/attachments and can
        # hang the Azure DevOps MCP stdio process on hub items.
        timeout = float(item_timeout if item_timeout is not None else ADO_ITEM_TIMEOUT)
        timeout = max(5.0, min(timeout, 600.0))
        try:
            detailed = call_ado_tool(
                self.mcp_manager,
                "get_item",
                {"id": work_item_id, "project": ado_project, "expand": "relations"},
                timeout=timeout,
            )
        except MCPError as exc:
            self._log_ingest(
                f"Skipped work item #{work_item_id} (details unavailable: {exc}).",
                level="warn",
                source="azure-boards",
                current="azure-boards",
            )
            # Timed-out/hung MCP calls leave the stdio process wedged; restart so the
            # next item is not blocked behind the dead request.
            if "timed out" in str(exc).lower():
                try:
                    self.mcp_manager.close_session("azure-devops")
                    self._log_ingest(
                        "Restarted Azure DevOps MCP session after timeout.",
                        level="warn",
                        source="azure-boards",
                        current="azure-boards",
                    )
                except Exception:
                    pass
            return None
        work_item = self._azure_boards_first_work_item(detailed.get("result") or detailed)
        if not work_item:
            return None
        comments: list[dict[str, Any]] = []
        fields = dict(work_item.get("fields") or {})
        comment_count = fields.get("System.CommentCount")
        if comment_count is None or int(comment_count) > 0:
            try:
                comments_result = call_ado_tool(
                    self.mcp_manager,
                    "list_comments",
                    {"project": ado_project, "workItemId": work_item_id, "top": 20},
                    timeout=min(timeout, 15.0),
                )
                comments = self._azure_boards_comments_from_payload(comments_result.get("result") or comments_result)
            except MCPError as exc:
                comments = []
                if "timed out" in str(exc).lower():
                    try:
                        self.mcp_manager.close_session("azure-devops")
                    except Exception:
                        pass
        return self._build_azure_boards_candidate(project_id, ado_project, work_item, comments)

    def _build_azure_boards_candidate(
        self,
        project_id: str,
        ado_project: str,
        work_item: dict[str, Any],
        comments: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        fields = dict(work_item.get("fields") or {})
        work_item_id = int(work_item.get("id") or fields.get("System.Id") or 0)
        if work_item_id <= 0:
            return None
        wi_type = str(fields.get("System.WorkItemType") or "Work Item").strip()
        title = str(fields.get("System.Title") or f"Work Item {work_item_id}").strip()
        state = str(fields.get("System.State") or "").strip()
        assigned = self._azure_boards_identity(fields.get("System.AssignedTo"))
        iteration = str(fields.get("System.IterationPath") or "").strip()
        area = str(fields.get("System.AreaPath") or "").strip()
        tags = str(fields.get("System.Tags") or "").strip()
        description = self._azure_boards_html_to_text(
            fields.get("System.Description")
            or fields.get("Microsoft.VSTS.TCM.ReproSteps")
            or fields.get("System.History")
            or ""
        )
        acceptance = self._azure_boards_html_to_text(fields.get("Microsoft.VSTS.Common.AcceptanceCriteria") or "")
        parent_id = fields.get("System.Parent")
        relations = self._azure_boards_relations(work_item)
        url = str(work_item.get("url") or fields.get("System.Href") or f"https://dev.azure.com/{ado_project}/_workitems/edit/{work_item_id}")
        desc_chunks = work_item_description_chunks(description)
        comment_chunks = work_item_comment_chunks(comments)
        text = work_item_main_parts(
            wi_type=wi_type,
            work_item_id=work_item_id,
            title=title,
            state=state,
            assigned=assigned,
            iteration=iteration,
            area=area,
            tags=tags,
            parent_id=parent_id,
            description=description,
            acceptance=acceptance,
            relations=relations,
            comment_count=len(comment_chunks),
            description_chunk_count=len(desc_chunks),
        )
        memory_type = ADO_BOARD_MEMORY_TYPES.get(wi_type.lower(), "Requirement" if "req" in wi_type.lower() else "Artifact")
        return {
            "id": stable_id("candidate", project_id, "azure-boards", str(work_item_id), title[:80]),
            "project_id": project_id,
            "source_type": "azure-boards",
            "source_ref": str(work_item_id),
            "label": f"ADO {wi_type} #{work_item_id}: {title}"[:180],
            "type": memory_type,
            "scope": "project",
            "text": text[:4500],
            "confidence": 0.8,
            "metadata": {
                "template": "azure_boards_work_item",
                "source": "azure_devops_mcp",
                "chunk_role": "main",
                "work_item_id": str(work_item_id),
                "work_item_type": wi_type,
                "work_item_state": state,
                "work_item_url": url,
                "assigned_to": assigned,
                "iteration_path": iteration,
                "area_path": area,
                "tags": tags,
                "parent_id": str(parent_id or ""),
                "ado_project": ado_project,
                "relations": relations,
                "comments": comments[:12],
                "description_full": description[:12000],
                "comment_chunk_count": len(comment_chunks),
                "description_chunk_count": len(desc_chunks),
            },
        }

    def _sync_azure_boards_work_item_chunks(self, parent_node, item: dict[str, Any], project_id: str) -> int:
        """Create/update linked comment + overflow-description nodes for a work item."""
        metadata = dict(item.get("metadata") or {})
        work_item_id = str(metadata.get("work_item_id") or "").strip()
        if not work_item_id:
            return 0
        comments = list(metadata.get("comments") or [])
        description = str(metadata.get("description_full") or "")
        created = 0
        keep_ids: set[str] = set()

        for chunk in work_item_comment_chunks(comments):
            node = self._upsert_azure_boards_chunk_node(
                project_id=project_id,
                parent_node=parent_node,
                work_item_id=work_item_id,
                chunk_role="comment",
                chunk_key=f"comment:{chunk['comment_id']}:{chunk['part_index']}",
                label=str(chunk["label"]),
                text=str(chunk["text"]),
                extra={
                    "comment_id": chunk["comment_id"],
                    "comment_author": chunk["author"],
                    "comment_created_date": chunk["created_date"],
                    "part_index": chunk["part_index"],
                    "part_count": chunk["part_count"],
                },
                edge_type="HAS_COMMENT",
            )
            keep_ids.add(node.id)
            created += 1

        for index, piece in enumerate(work_item_description_chunks(description)):
            node = self._upsert_azure_boards_chunk_node(
                project_id=project_id,
                parent_node=parent_node,
                work_item_id=work_item_id,
                chunk_role="description",
                chunk_key=f"description:{index}",
                label=f"ADO #{work_item_id} description part {index + 1}"[:180],
                text=piece,
                extra={"part_index": index},
                edge_type="HAS_CHUNK",
            )
            keep_ids.add(node.id)
            created += 1

        # Soft-archive stale chunk nodes from prior ingest of this work item.
        for existing in self.repository.list_nodes():
            if existing.status != "active":
                continue
            if existing.project_id not in {project_id, None}:
                continue
            meta = dict(existing.metadata or {})
            if str(meta.get("parent_work_item_id") or "") != work_item_id:
                continue
            if str(meta.get("chunk_role") or "") not in {"comment", "description"}:
                continue
            if existing.id in keep_ids:
                continue
            existing.status = "archived"
            existing.metadata["archived_reason"] = "stale_work_item_chunk"
            self.repository.upsert_node(existing)
        return created

    def _upsert_azure_boards_chunk_node(
        self,
        *,
        project_id: str,
        parent_node,
        work_item_id: str,
        chunk_role: str,
        chunk_key: str,
        label: str,
        text: str,
        extra: dict[str, Any],
        edge_type: str,
    ):
        existing = self._find_memory_node_by_chunk_key(project_id, work_item_id, chunk_key)
        metadata = {
            "source": "azure_boards",
            "source_type": "azure-boards-chunk",
            "chunk_role": chunk_role,
            "chunk_key": chunk_key,
            "work_item_id": work_item_id,
            "parent_work_item_id": work_item_id,
            "parent_node_id": parent_node.id,
            **extra,
        }
        if existing:
            existing.label = label
            existing.text = text
            existing.type = "Doc"
            existing.status = "active"
            merged = dict(existing.metadata or {})
            merged.update(metadata)
            existing.metadata = merged
            node = self.repository.upsert_node(existing)
        else:
            node = self.repository.add_node(
                "Doc",
                label,
                "project",
                text,
                self._memory_project_id_for_scope("project", project_id),
                None,
                0.72,
                metadata,
            )
            node = self.memory_lifecycle.initialize_node(node, "azure_boards_chunk")
        try:
            self.repository.add_edge(parent_node.id, node.id, edge_type, "project", 0.9)
        except Exception:  # noqa: BLE001
            pass
        return node

    def _find_memory_node_by_chunk_key(self, project_id: str, work_item_id: str, chunk_key: str):
        for node in self.repository.list_nodes():
            if node.project_id not in {project_id, None}:
                continue
            meta = dict(node.metadata or {})
            if str(meta.get("parent_work_item_id") or meta.get("work_item_id") or "") != work_item_id:
                continue
            if str(meta.get("chunk_key") or "") == chunk_key:
                return node
        return None

