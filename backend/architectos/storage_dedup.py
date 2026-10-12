"""Dedup index for the memory gate (``memory_dedup``), kept beside ``memory_nodes``.

Two tables, synced in the same connection as every ``upsert_node`` (like the FTS
table), so the gate reads candidates instead of scanning every node per write:

- ``memory_dedup_index`` — one row per node the gate may compare against
  (active, not structural, not superseded): its features as JSON and a hash of
  its label "subject". Duplicate/update/conflict all require the same subject,
  so they are found exactly by an indexed lookup on that hash.
- ``memory_dedup_stems`` — inverted index stem → node. The possible-duplicate
  flag needs a lexical neighbour under any label; counting shared stems in SQL
  finds every node above the flag threshold (see ``MemoryDedupGate``).

Volatile code/doc snapshots (``Artifact``) get a subject row but no stems: their
texts are large and the flag is meant for facts.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from .models import MemoryNode

_LOG = logging.getLogger(__name__)

# Node types whose stems are not indexed (subject lookups still work for them).
UNSTEMMED_TYPES = frozenset({"Artifact"})
# SQLite caps bound parameters; longer texts are matched on a stable sample.
MAX_QUERY_STEMS = 400


def _project_key(project_id: str | None) -> str:
    return str(project_id or "")


def dedup_index_eligible(node: MemoryNode) -> bool:
    if node.status != "active":
        return False
    meta = node.metadata or {}
    return not (meta.get("structural") or meta.get("hub") or meta.get("scope_root") or meta.get("invalid_at"))


class StorageDedupMixin:
    def _ensure_dedup_index(self) -> None:
        # Imported lazily: memory_dedup imports storage, which composes this mixin.
        from .memory_dedup import DEDUP_INDEX_VERSION

        with self._connect() as conn:  # type: ignore[attr-defined]
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_dedup_index (
                    node_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    project_key TEXT NOT NULL,
                    node_type TEXT NOT NULL,
                    label TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    features TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_dedup_subject
                    ON memory_dedup_index(scope, project_key, subject);
                CREATE TABLE IF NOT EXISTS memory_dedup_stems (
                    stem TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    project_key TEXT NOT NULL,
                    PRIMARY KEY (stem, node_id)
                ) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS idx_memory_dedup_stems_lookup
                    ON memory_dedup_stems(scope, project_key, stem);
                CREATE INDEX IF NOT EXISTS idx_memory_dedup_stems_node
                    ON memory_dedup_stems(node_id);
                """
            )
            indexed = int(conn.execute("SELECT count(*) FROM memory_dedup_index").fetchone()[0])
            stale = int(
                conn.execute(
                    "SELECT count(*) FROM memory_dedup_index WHERE version < ?", (DEDUP_INDEX_VERSION,)
                ).fetchone()[0]
            )
            active = int(conn.execute("SELECT count(*) FROM memory_nodes WHERE status = 'active'").fetchone()[0])
        # Backfill once (new table, or rows from an older feature format).
        if stale or (active and not indexed):
            result = self.rebuild_dedup_index()
            _LOG.info("dedup index rebuilt: %s node(s)", result["indexed"])

    def rebuild_dedup_index(self) -> dict[str, int]:
        with self._connect() as conn:  # type: ignore[attr-defined]
            conn.execute("DELETE FROM memory_dedup_stems")
            conn.execute("DELETE FROM memory_dedup_index")
            rows = conn.execute("SELECT payload FROM memory_nodes WHERE status = 'active'").fetchall()
            count = 0
            for row in rows:
                node = MemoryNode.from_dict(json.loads(row["payload"]))
                if self._sync_dedup_index_row(conn, node, fresh=True):
                    count += 1
        return {"indexed": count}

    @staticmethod
    def _delete_dedup_rows(conn: sqlite3.Connection, node_ids: list[str]) -> None:
        for start in range(0, len(node_ids), 500):
            chunk = node_ids[start:start + 500]
            placeholders = ", ".join("?" for _ in chunk)
            conn.execute(f"DELETE FROM memory_dedup_stems WHERE node_id IN ({placeholders})", chunk)
            conn.execute(f"DELETE FROM memory_dedup_index WHERE node_id IN ({placeholders})", chunk)

    @staticmethod
    def _sync_dedup_index_row(conn: sqlite3.Connection, node: MemoryNode, *, fresh: bool = False) -> bool:
        """Make the index rows of ``node`` match it. Returns whether it is indexed.

        Re-tokenizing is skipped when label/text, type, scope and project are
        unchanged — most upserts only touch lifecycle metadata.
        """
        from .memory_dedup import DEDUP_INDEX_VERSION, fact_features, features_to_json, source_hash, subject_key

        if not dedup_index_eligible(node):
            if not fresh:
                StorageDedupMixin._delete_dedup_rows(conn, [node.id])
            return False
        label = str(node.label or "")
        digest = source_hash(label, str(node.text or ""))
        scope = str(node.scope or "")
        project_key = _project_key(node.project_id)
        node_type = str(node.type or "")
        if not fresh:
            row = conn.execute(
                "SELECT source_hash, version, scope, project_key, node_type FROM memory_dedup_index WHERE node_id = ?",
                (node.id,),
            ).fetchone()
            if row and tuple(row) == (digest, DEDUP_INDEX_VERSION, scope, project_key, node_type):
                return True
            StorageDedupMixin._delete_dedup_rows(conn, [node.id])
        features = fact_features(label, str(node.text or ""))
        conn.execute(
            """
            INSERT INTO memory_dedup_index(node_id, scope, project_key, node_type, label, subject, source_hash, version, features)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node.id, scope, project_key, node_type, label, subject_key(features), digest,
                DEDUP_INDEX_VERSION, features_to_json(features),
            ),
        )
        if node_type not in UNSTEMMED_TYPES and features.stems:
            conn.executemany(
                "INSERT OR IGNORE INTO memory_dedup_stems(stem, node_id, scope, project_key) VALUES (?, ?, ?, ?)",
                [(stem, node.id, scope, project_key) for stem in features.stems],
            )
        return True

    def dedup_subject_rows(self, scope: str, project_id: str | None, subject: str) -> list[dict[str, Any]]:
        """Indexed nodes in this scope/project filed under the same label subject."""
        with self._connect() as conn:  # type: ignore[attr-defined]
            rows = conn.execute(
                """
                SELECT node_id, node_type, label, features FROM memory_dedup_index
                WHERE scope = ? AND project_key = ? AND subject = ?
                """,
                (scope, _project_key(project_id), subject),
            ).fetchall()
        return [dict(row) for row in rows]

    def dedup_stem_neighbours(
        self,
        scope: str,
        project_id: str | None,
        stems: list[str],
        min_shared: int,
        limit: int = 300,
    ) -> list[dict[str, Any]]:
        """Indexed nodes sharing at least ``min_shared`` of ``stems``, most shared first."""
        sample = sorted(stems)[:MAX_QUERY_STEMS]
        if not sample:
            return []
        placeholders = ", ".join("?" for _ in sample)
        with self._connect() as conn:  # type: ignore[attr-defined]
            rows = conn.execute(
                f"""
                SELECT i.node_id, i.node_type, i.label, i.features, hits.shared
                FROM (
                    SELECT node_id, count(*) AS shared FROM memory_dedup_stems
                    WHERE scope = ? AND project_key = ? AND stem IN ({placeholders})
                    GROUP BY node_id HAVING count(*) >= ?
                    ORDER BY shared DESC LIMIT ?
                ) AS hits
                JOIN memory_dedup_index AS i ON i.node_id = hits.node_id
                """,
                (scope, _project_key(project_id), *sample, max(1, int(min_shared)), max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]
