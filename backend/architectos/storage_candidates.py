"""Memory candidate queue persistence for SQLiteMemoryRepository.

Split out of ``storage`` so the review-queue CRUD stays together without
bloating core node/edge repository methods. ``SQLiteMemoryRepository``
inherits ``StorageCandidatesMixin``.
"""
from __future__ import annotations

import json
from typing import Any

from .candidate_identity import candidate_origin_key
from .models import stable_id, utc_now


def _sanitize_text(value: str) -> tuple[str, bool]:
    # Late import: ``storage`` composes this mixin and defines ``sanitize_text``.
    from .storage import sanitize_text

    return sanitize_text(value)


class StorageCandidatesMixin:
    def upsert_memory_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        candidate = dict(candidate)
        clean_label, label_redacted = _sanitize_text(str(candidate.get("label") or "Memory candidate"))
        clean_text, text_redacted = _sanitize_text(str(candidate.get("text") or ""))
        clean_source, source_redacted = _sanitize_text(str(candidate.get("source_ref") or ""))
        if not clean_text or len(clean_text) < 4:
            raise ValueError("candidate text must contain at least 4 non-secret characters")
        candidate["label"] = clean_label[:180]
        candidate["text"] = clean_text[:4000]
        candidate["source_ref"] = clean_source[:500]
        candidate.setdefault("project_id", "architectos")
        candidate.setdefault("source_type", "manual")
        candidate.setdefault("type", "Lesson")
        candidate.setdefault("scope", "project")
        candidate.setdefault("status", "candidate")
        candidate.setdefault("confidence", 0.62)
        candidate.setdefault("created_at", utc_now())
        candidate["updated_at"] = utc_now()
        if label_redacted or text_redacted or source_redacted:
            metadata = dict(candidate.get("metadata") or {})
            metadata["redacted"] = True
            candidate["metadata"] = metadata
        origin_key = candidate_origin_key(candidate)
        if origin_key:
            metadata = dict(candidate.get("metadata") or {})
            metadata["origin_key"] = origin_key
            candidate["metadata"] = metadata
        candidate.setdefault("id", stable_id("candidate", candidate["project_id"], candidate["source_type"], candidate["source_ref"], candidate["label"]))
        existing = self.get_memory_candidate(candidate["id"])
        if not existing and origin_key:
            existing = self.find_memory_candidate_by_origin(str(candidate["project_id"]), origin_key)
            if existing and existing.get("id"):
                candidate["id"] = existing["id"]
        incoming_meta = dict(candidate.get("metadata") or {})
        if existing and existing.get("status") in {"promoted", "rejected"} and candidate.get("status") == "candidate":
            # Sticky origins normally stay decided. Revisions (significant content
            # change) must open a fresh review card instead of returning the old one.
            existing_meta = dict(existing.get("metadata") or {})
            is_revision = bool(incoming_meta.get("revision"))
            new_hash = str(incoming_meta.get("content_hash") or "").strip()
            old_hash = str(existing_meta.get("content_hash") or "").strip()
            if is_revision and (not old_hash or not new_hash or new_hash != old_hash):
                candidate["id"] = stable_id(
                    "candidate-rev",
                    str(candidate.get("project_id") or ""),
                    origin_key or candidate["id"],
                    new_hash or utc_now(),
                )
                existing = None
            else:
                return existing
        existing_meta = dict((existing or {}).get("metadata") or {})
        if (
            existing
            and existing.get("status") in {"candidate", "duplicate"}
            and str(candidate.get("status") or "candidate") == "candidate"
        ):
            existing_src = str(existing.get("source_type") or "").strip().lower()
            incoming_src = str(candidate.get("source_type") or "").strip().lower()
            # Ask vs MCP: one fact, one card. Do not recast the channel.
            if existing_src in {"chat", "mcp"} and incoming_src in {"chat", "mcp"} and existing_src != incoming_src:
                return existing
        if (
            existing
            and existing.get("status") in {"candidate", "duplicate"}
            and str(candidate.get("status") or "candidate") == "candidate"
            and incoming_meta.get("duplicate")
            and str(incoming_meta.get("duplicate_kind") or "") == "candidate"
            and not existing_meta.get("duplicate")
        ):
            # Same queue id as an unmarked original: keep the original so
            # "Reject duplicates" cannot wipe the only copy that should become memory.
            return existing
        source_col = str(candidate.get("source_type") or "manual").strip() or "manual"
        candidate["source_type"] = source_col
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO memory_candidates(id, project_id, source_type, status, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (candidate["id"], candidate["project_id"], source_col, candidate["status"], candidate["updated_at"], json.dumps(candidate, sort_keys=True)),
            )
        return candidate

    def find_memory_candidate_by_origin(self, project_id: str, origin_key: str) -> dict[str, Any] | None:
        key = str(origin_key or "").strip()
        if not key:
            return None
        pending: dict[str, Any] | None = None
        for item in self.list_memory_candidates(project_id, status=None, limit=None):
            if candidate_origin_key(item) != key:
                continue
            if item.get("status") in {"promoted", "rejected"}:
                return item
            if pending is None:
                pending = item
        return pending

    def get_memory_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM memory_candidates WHERE id = ?", (candidate_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def delete_memory_candidate(self, candidate_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM memory_candidates WHERE id = ?", (candidate_id,))
        return cursor.rowcount > 0

    def _memory_candidate_filter_clauses(
        self,
        project_id: str | None = None,
        status: str | None = "candidate",
        *,
        source_type: str | None = None,
        duplicate: bool | None = None,
        duplicate_kind: str | None = None,
        revision: bool | None = None,
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if source_type:
            # Prefer payload.source_type — that is what the review UI displays.
            clauses.append(
                "COALESCE("
                "NULLIF(TRIM(COALESCE(json_extract(payload, '$.source_type'), '')), ''), "
                "NULLIF(TRIM(source_type), ''), "
                "'manual') = ?"
            )
            params.append(str(source_type))
        if duplicate is True:
            clauses.append(
                "json_extract(payload, '$.metadata.duplicate') IS NOT NULL "
                "AND json_extract(payload, '$.metadata.duplicate') != 0 "
                "AND json_extract(payload, '$.metadata.duplicate') != 'false'"
            )
        elif duplicate is False:
            clauses.append(
                "(json_extract(payload, '$.metadata.duplicate') IS NULL "
                "OR json_extract(payload, '$.metadata.duplicate') = 0 "
                "OR json_extract(payload, '$.metadata.duplicate') = 'false')"
            )
        kind = str(duplicate_kind or "").strip().lower()
        if kind in {"memory", "candidate"}:
            clauses.append("json_extract(payload, '$.metadata.duplicate_kind') = ?")
            params.append(kind)
        if revision is True:
            clauses.append(
                "json_extract(payload, '$.metadata.revision') IS NOT NULL "
                "AND json_extract(payload, '$.metadata.revision') != 0 "
                "AND json_extract(payload, '$.metadata.revision') != 'false'"
            )
        elif revision is False:
            clauses.append(
                "(json_extract(payload, '$.metadata.revision') IS NULL "
                "OR json_extract(payload, '$.metadata.revision') = 0 "
                "OR json_extract(payload, '$.metadata.revision') = 'false')"
            )
        return clauses, params

    def list_memory_candidates(
        self,
        project_id: str | None = None,
        status: str | None = "candidate",
        limit: int | None = 50,
        *,
        offset: int = 0,
        source_type: str | None = None,
        duplicate: bool | None = None,
        duplicate_kind: str | None = None,
        revision: bool | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT payload FROM memory_candidates"
        clauses, params = self._memory_candidate_filter_clauses(
            project_id,
            status,
            source_type=source_type,
            duplicate=duplicate,
            duplicate_kind=duplicate_kind,
            revision=revision,
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
            if offset and int(offset) > 0:
                sql += " OFFSET ?"
                params.append(int(offset))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def count_memory_candidates_filtered(
        self,
        project_id: str | None = None,
        status: str | None = "candidate",
        *,
        source_type: str | None = None,
        duplicate: bool | None = None,
        duplicate_kind: str | None = None,
        revision: bool | None = None,
    ) -> int:
        sql = "SELECT COUNT(*) AS n FROM memory_candidates"
        clauses, params = self._memory_candidate_filter_clauses(
            project_id,
            status,
            source_type=source_type,
            duplicate=duplicate,
            duplicate_kind=duplicate_kind,
            revision=revision,
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._connect() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
        return int(row["n"] or 0) if row else 0

    def list_memory_candidate_sources(
        self,
        project_id: str | None = None,
        status: str | None = None,
        *,
        duplicate: bool | None = None,
    ) -> list[dict[str, Any]]:
        # Prefer payload.source_type so the dropdown matches the chips on cards.
        src_expr = (
            "COALESCE("
            "NULLIF(TRIM(COALESCE(json_extract(payload, '$.source_type'), '')), ''), "
            "NULLIF(TRIM(source_type), ''), "
            "'manual')"
        )
        sql = f"SELECT {src_expr} AS src, COUNT(*) AS n FROM memory_candidates"
        clauses, params = self._memory_candidate_filter_clauses(
            project_id, status, duplicate=duplicate
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " GROUP BY src ORDER BY n DESC, src ASC"
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            {"id": str(row["src"] or "manual"), "count": int(row["n"] or 0)}
            for row in rows
            if str(row["src"] or "").strip()
        ]

    def iter_memory_candidate_ids(self, project_id: str | None = None, status: str | None = "candidate") -> list[str]:
        sql = "SELECT id FROM memory_candidates"
        params: list[Any] = []
        clauses = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [str(row["id"]) for row in rows]

    def count_memory_candidates_by_status(self, project_id: str | None = None) -> dict[str, int]:
        sql = "SELECT status, COUNT(*) AS n FROM memory_candidates"
        params: list[Any] = []
        if project_id:
            sql += " WHERE project_id = ?"
            params.append(project_id)
        sql += " GROUP BY status"
        counts = {"candidate": 0, "promoted": 0, "rejected": 0}
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            for row in rows:
                key = str(row["status"] or "")
                counts[key] = int(row["n"] or 0)
            dup_sql = (
                "SELECT COUNT(*) AS n FROM memory_candidates "
                "WHERE status = 'candidate' "
                "AND json_extract(payload, '$.metadata.duplicate') IS NOT NULL "
                "AND json_extract(payload, '$.metadata.duplicate') != 0 "
                "AND json_extract(payload, '$.metadata.duplicate') != 'false'"
            )
            dup_params: list[Any] = []
            if project_id:
                dup_sql = (
                    "SELECT COUNT(*) AS n FROM memory_candidates "
                    "WHERE project_id = ? AND status = 'candidate' "
                    "AND json_extract(payload, '$.metadata.duplicate') IS NOT NULL "
                    "AND json_extract(payload, '$.metadata.duplicate') != 0 "
                    "AND json_extract(payload, '$.metadata.duplicate') != 'false'"
                )
                dup_params = [project_id]
            try:
                duplicates = int(conn.execute(dup_sql, tuple(dup_params)).fetchone()["n"] or 0)
            except Exception:
                duplicates = 0
                for item in self.list_memory_candidates(project_id, "candidate", min(500, max(counts.get("candidate", 0), 1))):
                    if (item.get("metadata") or {}).get("duplicate"):
                        duplicates += 1
            rev_sql = (
                "SELECT COUNT(*) AS n FROM memory_candidates "
                "WHERE status = 'candidate' "
                "AND json_extract(payload, '$.metadata.revision') IS NOT NULL "
                "AND json_extract(payload, '$.metadata.revision') != 0 "
                "AND json_extract(payload, '$.metadata.revision') != 'false'"
            )
            rev_params: list[Any] = []
            if project_id:
                rev_sql = (
                    "SELECT COUNT(*) AS n FROM memory_candidates "
                    "WHERE project_id = ? AND status = 'candidate' "
                    "AND json_extract(payload, '$.metadata.revision') IS NOT NULL "
                    "AND json_extract(payload, '$.metadata.revision') != 0 "
                    "AND json_extract(payload, '$.metadata.revision') != 'false'"
                )
                rev_params = [project_id]
            try:
                revisions = int(conn.execute(rev_sql, tuple(rev_params)).fetchone()["n"] or 0)
            except Exception:
                revisions = 0
                for item in self.list_memory_candidates(project_id, "candidate", min(500, max(counts.get("candidate", 0), 1))):
                    if (item.get("metadata") or {}).get("revision"):
                        revisions += 1
        return {
            "pending": int(counts.get("candidate") or 0),
            "accepted": int(counts.get("promoted") or 0),
            "rejected": int(counts.get("rejected") or 0),
            "duplicate": duplicates,
            "revision": revisions,
            "total": int(counts.get("candidate") or 0) + int(counts.get("promoted") or 0) + int(counts.get("rejected") or 0),
        }

    def update_memory_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        candidate = dict(candidate)
        candidate["updated_at"] = utc_now()
        source_col = str(candidate.get("source_type") or "manual").strip() or "manual"
        candidate["source_type"] = source_col
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO memory_candidates(id, project_id, source_type, status, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (candidate["id"], candidate["project_id"], source_col, candidate["status"], candidate["updated_at"], json.dumps(candidate, sort_keys=True)),
            )
        return candidate
