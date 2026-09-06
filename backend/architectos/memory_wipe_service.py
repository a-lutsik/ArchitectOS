"""Hard-delete memory by source or project (settings cleanup actions)."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("architectos.memory_wipe")


class MemoryWipeMixin:
    """Physical wipe of memory nodes, candidates, and related index rows."""

    def clear_source_memory(
        self,
        source_id: str,
        *,
        confirm: bool = False,
        delete_binding: bool = False,
    ) -> dict[str, Any]:
        """Wipe ingested memory for one source binding.

        Without ``confirm``, returns preview counts only. With ``confirm``,
        physically deletes matching nodes/candidates and related rows.
        When ``delete_binding`` is True, also removes the ``sources`` row
        (removable sources only — callers must enforce that).
        """
        source_id = str(source_id or "").strip()
        if not source_id:
            raise ValueError("source_id is required")
        source = self.repository.get_source(source_id)
        if not source:
            raise ValueError(f"source not found: {source_id}")
        project_id = str(source.project_id or "")
        node_ids, evidence_paths = self._collect_source_node_ids(source_id, project_id)
        candidate_ids = self._collect_source_candidate_ids(source_id, project_id)
        preview = self._preview_wipe_counts(node_ids, candidate_ids, evidence_paths)
        preview.update({
            "source_id": source_id,
            "project_id": project_id,
            "delete_binding": bool(delete_binding),
            "confirmed": False,
        })
        if not confirm:
            return preview

        result = self._hard_delete_memory(
            node_ids=node_ids,
            candidate_ids=candidate_ids,
            evidence_paths=evidence_paths,
            project_id=project_id,
        )
        binding_deleted = False
        if delete_binding:
            with self.repository._connect() as conn:
                conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
            binding_deleted = True
        try:
            pruned = self.graph_auto_linker.prune_empty_source_hubs(project_id or None)
        except Exception:
            _LOG.exception("prune_empty_source_hubs failed after source clear")
            pruned = {}
        return {
            **result,
            "source_id": source_id,
            "project_id": project_id,
            "delete_binding": binding_deleted,
            "confirmed": True,
            "pruned_hubs": pruned,
        }

    def wipe_project_memory(self, project_id: str, *, confirm: bool = False) -> dict[str, Any]:
        """Wipe all durable memory for one project (bindings/chats/tasks kept)."""
        project_id = str(project_id or "").strip()
        if not project_id:
            raise ValueError("project_id is required")
        project = self.repository.get_project(project_id)
        if not project:
            raise ValueError(f"project not found: {project_id}")

        node_ids, evidence_paths = self._collect_project_node_ids(project_id)
        candidate_ids = self._collect_project_candidate_ids(project_id)
        preview = self._preview_wipe_counts(node_ids, candidate_ids, evidence_paths)
        preview.update({
            "project_id": project_id,
            "project_name": str(getattr(project, "name", None) or project_id),
            "confirmed": False,
        })
        if not confirm:
            return preview

        result = self._hard_delete_memory(
            node_ids=node_ids,
            candidate_ids=candidate_ids,
            evidence_paths=evidence_paths,
            project_id=project_id,
        )
        try:
            self._ensure_project_memory_root(project)
        except Exception:
            _LOG.exception("ensure project memory root failed after project wipe")
        try:
            pruned = self.graph_auto_linker.prune_empty_source_hubs(project_id)
        except Exception:
            _LOG.exception("prune_empty_source_hubs failed after project wipe")
            pruned = {}
        return {
            **result,
            "project_id": project_id,
            "project_name": str(getattr(project, "name", None) or project_id),
            "confirmed": True,
            "pruned_hubs": pruned,
        }

    def _collect_source_node_ids(self, source_id: str, project_id: str) -> tuple[list[str], list[str]]:
        """Nodes for a source: column/metadata source_id, plus hubs with matching source_key."""
        node_ids: list[str] = []
        evidence_paths: list[str] = []
        params: list[Any] = [source_id, source_id, source_id]
        project_clause = ""
        if project_id:
            project_clause = " AND (project_id = ? OR project_id IS NULL OR project_id = '')"
            params.append(project_id)
        with self.repository._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, payload FROM memory_nodes
                WHERE (
                    source_id = ?
                    OR coalesce(json_extract(payload, '$.metadata.source_id'), '') = ?
                    OR (
                      coalesce(json_extract(payload, '$.metadata.hub'), 0) IN (1, 'true', '1')
                      AND coalesce(json_extract(payload, '$.metadata.source_key'), '') = ?
                    )
                ){project_clause}
                """,
                params,
            ).fetchall()
        for row in rows:
            node_id = str(row["id"] or "").strip()
            if not node_id:
                continue
            node_ids.append(node_id)
            evidence_paths.extend(self._evidence_paths_from_payload(row["payload"]))
        return list(dict.fromkeys(node_ids)), list(dict.fromkeys(evidence_paths))

    def _collect_project_node_ids(self, project_id: str) -> tuple[list[str], list[str]]:
        node_ids: list[str] = []
        evidence_paths: list[str] = []
        with self.repository._connect() as conn:
            rows = conn.execute(
                "SELECT id, payload FROM memory_nodes WHERE project_id = ? AND type != 'Project'",
                (project_id,),
            ).fetchall()
        for row in rows:
            node_id = str(row["id"] or "").strip()
            if not node_id:
                continue
            node_ids.append(node_id)
            evidence_paths.extend(self._evidence_paths_from_payload(row["payload"]))
        return list(dict.fromkeys(node_ids)), list(dict.fromkeys(evidence_paths))

    def _collect_source_candidate_ids(self, source_id: str, project_id: str) -> list[str]:
        ids: list[str] = []
        for item in self.repository.list_memory_candidates(project_id or None, status=None, limit=None):
            meta = dict(item.get("metadata") or {})
            cand_source = str(
                meta.get("source_id")
                or item.get("source_id")
                or ""
            ).strip()
            if cand_source == source_id:
                cid = str(item.get("id") or "").strip()
                if cid:
                    ids.append(cid)
        return ids

    def _collect_project_candidate_ids(self, project_id: str) -> list[str]:
        return [
            str(item.get("id") or "").strip()
            for item in self.repository.list_memory_candidates(project_id, status=None, limit=None)
            if str(item.get("id") or "").strip()
        ]

    def _preview_wipe_counts(
        self,
        node_ids: list[str],
        candidate_ids: list[str],
        evidence_paths: list[str],
    ) -> dict[str, Any]:
        edge_count = 0
        embedding_count = 0
        if node_ids:
            edge_count = len(self.repository.list_edges_touching(node_ids))
            with self.repository._connect() as conn:
                embedding_count = self._count_in_chunks(
                    conn, "SELECT count(*) FROM memory_embeddings WHERE node_id IN ({ph})", node_ids
                )
        return {
            "nodes": len(node_ids),
            "edges": edge_count,
            "candidates": len(candidate_ids),
            "embeddings": embedding_count,
            "evidence_files": len(evidence_paths),
        }

    def _hard_delete_memory(
        self,
        *,
        node_ids: list[str],
        candidate_ids: list[str],
        evidence_paths: list[str],
        project_id: str | None = None,
    ) -> dict[str, Any]:
        edges_deleted = 0
        nodes_deleted = 0
        embeddings_deleted = 0
        events_deleted = 0
        fts_deleted = 0
        candidates_deleted = 0

        with self.repository.transaction() as conn:
            if node_ids:
                edges_deleted = self._delete_in_chunks(
                    conn,
                    "DELETE FROM memory_edges WHERE source IN ({ph}) OR target IN ({ph})",
                    node_ids,
                    double=True,
                )
                try:
                    fts_deleted = self._delete_in_chunks(
                        conn, "DELETE FROM memory_nodes_fts WHERE node_id IN ({ph})", node_ids
                    )
                except sqlite3.Error as exc:
                    _LOG.debug("wipe skipped memory_nodes_fts: %s", exc)
                try:
                    embeddings_deleted = self._delete_in_chunks(
                        conn, "DELETE FROM memory_embeddings WHERE node_id IN ({ph})", node_ids
                    )
                except sqlite3.Error as exc:
                    _LOG.debug("wipe skipped memory_embeddings: %s", exc)
                try:
                    events_deleted = self._delete_in_chunks(
                        conn, "DELETE FROM memory_node_events WHERE node_id IN ({ph})", node_ids
                    )
                except sqlite3.Error as exc:
                    _LOG.debug("wipe skipped memory_node_events: %s", exc)
                nodes_deleted = self._delete_in_chunks(
                    conn, "DELETE FROM memory_nodes WHERE id IN ({ph})", node_ids
                )
            if candidate_ids:
                candidates_deleted = self._delete_in_chunks(
                    conn, "DELETE FROM memory_candidates WHERE id IN ({ph})", candidate_ids
                )

        evidence_removed = self._delete_evidence_files(evidence_paths)
        return {
            "nodes": nodes_deleted,
            "edges": edges_deleted,
            "candidates": candidates_deleted,
            "embeddings": embeddings_deleted,
            "events": events_deleted,
            "fts": fts_deleted,
            "evidence_files": evidence_removed,
            "project_id": project_id or "",
        }

    def _delete_evidence_files(self, relative_paths: list[str]) -> int:
        evidence_root = Path(self.repository.evidence_dir).resolve()
        removed = 0
        for rel in relative_paths:
            text = str(rel or "").strip()
            if not text:
                continue
            # Paths are stored as "evidence/....md" relative to memory_dir.
            candidate = (Path(self.repository.memory_dir) / text).resolve()
            try:
                candidate.relative_to(evidence_root)
            except ValueError:
                _LOG.warning("refusing to delete evidence outside evidence_dir: %s", text)
                continue
            if candidate.is_file():
                try:
                    candidate.unlink()
                    removed += 1
                except OSError as exc:
                    _LOG.debug("evidence delete failed for %s: %s", candidate, exc)
        return removed

    @staticmethod
    def _evidence_paths_from_payload(payload: Any) -> list[str]:
        if payload is None:
            return []
        if isinstance(payload, str):
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                return []
        elif isinstance(payload, dict):
            data = payload
        else:
            return []
        return [str(item).strip() for item in (data.get("evidence") or []) if str(item).strip()]

    @staticmethod
    def _chunked(ids: list[str], size: int = 400):
        for offset in range(0, len(ids), size):
            yield ids[offset : offset + size]

    def _delete_in_chunks(
        self,
        conn: sqlite3.Connection,
        sql_template: str,
        ids: list[str],
        *,
        double: bool = False,
    ) -> int:
        total = 0
        for chunk in self._chunked(ids):
            placeholders = ",".join("?" for _ in chunk)
            sql = sql_template.format(ph=placeholders)
            params: list[Any] = [*chunk, *chunk] if double else list(chunk)
            cur = conn.execute(sql, params)
            total += int(cur.rowcount or 0)
        return total

    def _count_in_chunks(self, conn: sqlite3.Connection, sql_template: str, ids: list[str]) -> int:
        total = 0
        for chunk in self._chunked(ids):
            placeholders = ",".join("?" for _ in chunk)
            sql = sql_template.format(ph=placeholders)
            row = conn.execute(sql, chunk).fetchone()
            total += int((row[0] if row else 0) or 0)
        return total
