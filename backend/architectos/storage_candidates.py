"""Memory candidate queue persistence for SQLiteMemoryRepository.

Split out of ``storage`` so the review-queue CRUD stays together without
bloating core node/edge repository methods. ``SQLiteMemoryRepository``
inherits ``StorageCandidatesMixin``.
"""
from __future__ import annotations

import json
from typing import Any

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
        candidate.setdefault("id", stable_id("candidate", candidate["project_id"], candidate["source_type"], candidate["source_ref"], candidate["label"]))
        existing = self.get_memory_candidate(candidate["id"])
        if existing and existing.get("status") in {"promoted", "rejected"} and candidate.get("status") == "candidate":
            for key in ("status", "promoted_node_id", "promoted_at", "rejected_at", "reject_reason"):
                if key in existing:
                    candidate[key] = existing[key]
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO memory_candidates(id, project_id, source_type, status, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (candidate["id"], candidate["project_id"], candidate["source_type"], candidate["status"], candidate["updated_at"], json.dumps(candidate, sort_keys=True)),
            )
        return candidate

    def get_memory_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM memory_candidates WHERE id = ?", (candidate_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def delete_memory_candidate(self, candidate_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM memory_candidates WHERE id = ?", (candidate_id,))
        return cursor.rowcount > 0

    def list_memory_candidates(self, project_id: str | None = None, status: str | None = "candidate", limit: int | None = 50) -> list[dict[str, Any]]:
        sql = "SELECT payload FROM memory_candidates"
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
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

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
        return {
            "pending": int(counts.get("candidate") or 0),
            "accepted": int(counts.get("promoted") or 0),
            "rejected": int(counts.get("rejected") or 0),
            "duplicate": duplicates,
            "total": int(counts.get("candidate") or 0) + int(counts.get("promoted") or 0) + int(counts.get("rejected") or 0),
        }

    def update_memory_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        candidate = dict(candidate)
        candidate["updated_at"] = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO memory_candidates(id, project_id, source_type, status, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (candidate["id"], candidate["project_id"], candidate["source_type"], candidate["status"], candidate["updated_at"], json.dumps(candidate, sort_keys=True)),
            )
        return candidate
