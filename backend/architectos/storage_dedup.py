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

Rows written by an older feature version (``version`` below
``DEDUP_INDEX_VERSION``) stay until the background pass
(``refresh_dedup_index``) reaches them; until then readers compute their
features from the node on the fly.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from typing import Any

from .models import MemoryNode

_LOG = logging.getLogger(__name__)

# Node types whose stems are not indexed (subject lookups still work for them).
UNSTEMMED_TYPES = frozenset({"Artifact"})
# SQLite caps bound parameters; longer texts are matched on a stable sample.
MAX_QUERY_STEMS = 400
# Outdated rows one read recomputes in memory; the rest only count for the near flag.
MAX_STALE_RECOMPUTE = 50
# Rows the background pass reads per query.
REFRESH_READ_ROWS = 50
# A background batch that held the write lock longer than this is logged as a warning.
REFRESH_SLOW_BATCH_SECONDS = 0.5


def _project_key(project_id: str | None) -> str:
    return str(project_id or "")


def dedup_index_eligible(node: MemoryNode) -> bool:
    if node.status != "active":
        return False
    meta = node.metadata or {}
    return not (meta.get("structural") or meta.get("hub") or meta.get("scope_root") or meta.get("invalid_at"))


class StorageDedupMixin:
    def _ensure_dedup_index(self) -> None:
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
                CREATE TABLE IF NOT EXISTS dedup_rejected_pairs (
                    left_id TEXT NOT NULL,
                    right_id TEXT NOT NULL,
                    at TEXT NOT NULL,
                    PRIMARY KEY (left_id, right_id)
                ) WITHOUT ROWID;
                """
            )
            indexed = int(conn.execute("SELECT count(*) FROM memory_dedup_index").fetchone()[0])
            active = int(conn.execute("SELECT count(*) FROM memory_nodes WHERE status = 'active'").fetchone()[0])
        # Backfill an empty index once. Rows of an older feature version are left to
        # refresh_dedup_index: start-up never takes the write lock for them.
        if active and not indexed:
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

    def refresh_dedup_index(self, budget_seconds: float = 0.15, *, after_rowid: int = 0) -> dict[str, int]:
        """One background batch over rows of an older feature version, in rowid order.

        The batch is one transaction that stops taking rows once ``budget_seconds``
        have passed, so a foreground writer waits for one batch, not the whole
        pass; progress lives in the row versions, a rolled-back batch is simply
        redone. The caller hands the returned ``cursor`` to the next batch of the
        same pass as ``after_rowid``, so no batch walks rows an earlier one has
        finished, and the first outdated row is found by a read before the
        transaction: an idle round takes no write lock. ``remaining`` is 1 when
        the batch did work (call again) and 0 when nothing is outdated after the
        cursor. Atom rows (text starts with a chat/MCP fact header) are recomputed
        from the node read inside the transaction; the features of other texts do
        not depend on the version, so their rows only get the new version.
        ``database is locked`` is reported as ``locked`` for the caller to retry
        on its next round.
        """
        from .memory_dedup import DEDUP_INDEX_VERSION, atom_statement

        with self._connect() as conn:  # type: ignore[attr-defined]
            first = conn.execute(
                "SELECT rowid FROM memory_dedup_index WHERE version < ? AND rowid > ? ORDER BY rowid LIMIT 1",
                (DEDUP_INDEX_VERSION, after_rowid),
            ).fetchone()
        if first is None:
            return {"recomputed": 0, "stamped": 0, "locked": 0, "remaining": 0, "cursor": after_rowid}
        cursor = int(first[0]) - 1
        recomputed = stamped = 0
        try:
            with self.transaction() as conn:  # type: ignore[attr-defined]
                started = time.monotonic()
                outdated = self._walk_rowids(
                    conn,
                    """
                    SELECT i.rowid AS row_id, i.node_id, n.payload FROM memory_dedup_index AS i
                    LEFT JOIN memory_nodes AS n ON n.id = i.node_id
                    WHERE i.version < ? AND i.rowid > ? ORDER BY i.rowid LIMIT ?
                    """,
                    (DEDUP_INDEX_VERSION,),
                    cursor,
                )
                for row in outdated:
                    cursor = int(row["row_id"])
                    if row["payload"] is None:
                        # The node is gone: nothing to compare against any more.
                        self._delete_dedup_rows(conn, [str(row["node_id"])])
                    else:
                        node = MemoryNode.from_dict(json.loads(row["payload"]))
                        if atom_statement(str(node.text or "")):
                            self._sync_dedup_index_row(conn, node)
                            recomputed += 1
                        else:
                            conn.execute(
                                "UPDATE memory_dedup_index SET version = ? WHERE node_id = ?",
                                (DEDUP_INDEX_VERSION, node.id),
                            )
                            stamped += 1
                    if time.monotonic() - started >= budget_seconds:
                        break
                held = time.monotonic() - started
        except sqlite3.OperationalError as exc:
            self._skip_locked_batch("refresh", exc)
            return {"recomputed": 0, "stamped": 0, "locked": 1, "remaining": 1, "cursor": after_rowid}
        self._log_dedup_batch("refresh", held, recomputed=recomputed, stamped=stamped)
        return {"recomputed": recomputed, "stamped": stamped, "locked": 0, "remaining": 1, "cursor": cursor}

    def index_missing_dedup_rows(self, budget_seconds: float = 0.15, *, after_rowid: int = 0) -> dict[str, int]:
        """One background batch adding index rows for active nodes without one, in node rowid order.

        A lost (or never written) row leaves its node out of every gate decision.
        Candidates are read and checked for eligibility before the transaction,
        so nodes that are never indexed (structural, hubs) cost no write lock;
        the transaction re-reads each node before writing its row. The cursor,
        ``remaining`` and ``locked`` work as in ``refresh_dedup_index``.
        """
        candidates: list[tuple[int, str]] = []
        with self._connect() as conn:  # type: ignore[attr-defined]
            nodes = self._walk_rowids(
                conn,
                """
                SELECT n.rowid AS row_id, n.id, n.payload FROM memory_nodes AS n
                WHERE n.status = 'active'
                  AND NOT EXISTS (SELECT 1 FROM memory_dedup_index AS i WHERE i.node_id = n.id)
                  AND n.rowid > ? ORDER BY n.rowid LIMIT ?
                """,
                (),
                after_rowid,
            )
            for row in nodes:
                if dedup_index_eligible(MemoryNode.from_dict(json.loads(row["payload"]))):
                    candidates.append((int(row["row_id"]), str(row["id"])))
                    if len(candidates) == REFRESH_READ_ROWS:
                        break
        if not candidates:
            return {"added": 0, "locked": 0, "remaining": 0, "cursor": after_rowid}
        cursor = after_rowid
        added = 0
        try:
            with self.transaction() as conn:  # type: ignore[attr-defined]
                started = time.monotonic()
                for row_id, node_id in candidates:
                    cursor = row_id
                    row = conn.execute("SELECT payload FROM memory_nodes WHERE id = ?", (node_id,)).fetchone()
                    if row is not None and self._sync_dedup_index_row(conn, MemoryNode.from_dict(json.loads(row["payload"]))):
                        added += 1
                    if time.monotonic() - started >= budget_seconds:
                        break
                held = time.monotonic() - started
        except sqlite3.OperationalError as exc:
            self._skip_locked_batch("backfill", exc)
            return {"added": 0, "locked": 1, "remaining": 1, "cursor": after_rowid}
        self._log_dedup_batch("backfill", held, added=added)
        return {"added": added, "locked": 0, "remaining": 1, "cursor": cursor}

    def dedup_index_status(self) -> dict[str, int]:
        """Indexed rows and rows of an older feature version still left (one full read, no lock)."""
        from .memory_dedup import DEDUP_INDEX_VERSION

        with self._connect() as conn:  # type: ignore[attr-defined]
            indexed, remaining = conn.execute(
                "SELECT count(*), coalesce(sum(version < ?), 0) FROM memory_dedup_index", (DEDUP_INDEX_VERSION,)
            ).fetchone()
        return {"indexed": int(indexed), "remaining": int(remaining)}

    @staticmethod
    def _walk_rowids(
        conn: sqlite3.Connection, sql: str, params: tuple[Any, ...], after_rowid: int
    ) -> Iterator[sqlite3.Row]:
        """Rows of ``sql`` (``row_id`` column, ending in ``rowid > ? ORDER BY rowid LIMIT ?``) read in chunks."""
        while True:
            rows = conn.execute(sql, (*params, after_rowid, REFRESH_READ_ROWS)).fetchall()
            yield from rows
            if len(rows) < REFRESH_READ_ROWS:
                return
            after_rowid = int(rows[-1]["row_id"])

    @staticmethod
    def _skip_locked_batch(name: str, exc: sqlite3.OperationalError) -> None:
        """Let a background batch give way to a held write lock; any other error is raised."""
        if "database is locked" not in str(exc):
            raise exc
        _LOG.warning("dedup index %s batch skipped, database is locked: %s", name, exc)

    @staticmethod
    def _log_dedup_batch(name: str, held: float, **counts: int) -> None:
        level = logging.WARNING if held > REFRESH_SLOW_BATCH_SECONDS else logging.DEBUG
        _LOG.log(level, "dedup index %s batch held the write lock %.3fs · %s", name, held, counts)

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

    @staticmethod
    def _read_current_features(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        """Index rows as dicts; ``current`` tells whether ``features`` match this release.

        Only an atom row of an older version is outdated: the features of other
        texts do not depend on the version (the background pass only stamps
        them), and the stored features show which rows are atoms. An outdated
        row gets features computed from its node here, in memory and without a
        write, for the first ``MAX_STALE_RECOMPUTE`` of them; the rest keep their
        stored features with ``current`` False and only count for the near flag.
        """
        from .memory_dedup import (
            DEDUP_INDEX_VERSION,
            fact_features,
            features_from_json,
            features_to_json,
            is_atom_features,
        )

        result = [dict(row) for row in rows]
        outdated: dict[str, dict[str, Any]] = {}
        for row in result:
            row["current"] = (
                int(row.pop("version")) >= DEDUP_INDEX_VERSION
                or not is_atom_features(features_from_json(str(row["features"])))
            )
            if not row["current"] and len(outdated) < MAX_STALE_RECOMPUTE:
                outdated[str(row["node_id"])] = row
        if not outdated:
            return result
        placeholders = ", ".join("?" for _ in outdated)
        for node_row in conn.execute(
            f"SELECT payload FROM memory_nodes WHERE id IN ({placeholders})", list(outdated)
        ).fetchall():
            node = MemoryNode.from_dict(json.loads(node_row["payload"]))
            row = outdated[node.id]
            row["features"] = features_to_json(fact_features(str(node.label or ""), str(node.text or "")))
            row["current"] = True
        return result

    def dedup_subject_rows(self, scope: str, project_id: str | None, subject: str) -> list[dict[str, Any]]:
        """Indexed nodes in this scope/project filed under the same label subject.

        Every row carries current features: outdated rows past the recompute
        limit are left out (decisions are taken on these rows).
        """
        with self._connect() as conn:  # type: ignore[attr-defined]
            rows = conn.execute(
                """
                SELECT node_id, node_type, label, features, version FROM memory_dedup_index
                WHERE scope = ? AND project_key = ? AND subject = ?
                ORDER BY node_id
                """,
                (scope, _project_key(project_id), subject),
            ).fetchall()
            return [row for row in self._read_current_features(conn, rows) if row["current"]]

    def dedup_stem_neighbours(
        self,
        scope: str,
        project_id: str | None,
        stems: list[str],
        min_shared: int,
        limit: int = 300,
    ) -> list[dict[str, Any]]:
        """Indexed nodes sharing at least ``min_shared`` of ``stems``, most shared first.

        ``current`` is False on outdated rows past the recompute limit.
        """
        sample = sorted(stems)[:MAX_QUERY_STEMS]
        if not sample:
            return []
        placeholders = ", ".join("?" for _ in sample)
        with self._connect() as conn:  # type: ignore[attr-defined]
            rows = conn.execute(
                f"""
                SELECT i.node_id, i.node_type, i.label, i.features, i.version, hits.shared
                FROM (
                    SELECT node_id, count(*) AS shared FROM memory_dedup_stems
                    WHERE scope = ? AND project_key = ? AND stem IN ({placeholders})
                    GROUP BY node_id HAVING count(*) >= ?
                    ORDER BY shared DESC LIMIT ?
                ) AS hits
                JOIN memory_dedup_index AS i ON i.node_id = hits.node_id
                ORDER BY hits.shared DESC, i.node_id
                """,
                (scope, _project_key(project_id), *sample, max(1, int(min_shared)), max(1, int(limit))),
            ).fetchall()
            return self._read_current_features(conn, rows)

    def dedup_rows_by_ids(self, node_ids: list[str]) -> list[dict[str, Any]]:
        """Index rows for these nodes; ids that are not indexed (ineligible) are absent.

        Like ``dedup_subject_rows``, outdated rows past the recompute limit are left out.
        """
        ids = [str(node_id) for node_id in dict.fromkeys(node_ids) if node_id][:MAX_QUERY_STEMS]
        if not ids:
            return []
        placeholders = ", ".join("?" for _ in ids)
        with self._connect() as conn:  # type: ignore[attr-defined]
            rows = conn.execute(
                f"SELECT node_id, node_type, label, scope, project_key, features, version FROM memory_dedup_index "
                f"WHERE node_id IN ({placeholders}) ORDER BY node_id",
                ids,
            ).fetchall()
            return [row for row in self._read_current_features(conn, rows) if row["current"]]

    def dedup_subject_groups(self, project_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        """Subjects shared by 2+ indexed nodes (compaction candidates), largest first.

        ``project_id`` narrows to that project's nodes; ``None`` covers every
        project plus shared/global scopes. Artifacts are left to revisions.
        """
        sql = (
            "SELECT scope, project_key, subject, count(*) AS size FROM memory_dedup_index "
            "WHERE node_type NOT IN ({unstemmed})"
        ).format(unstemmed=", ".join("?" for _ in UNSTEMMED_TYPES))
        params: list[Any] = sorted(UNSTEMMED_TYPES)
        if project_id is not None:
            sql += " AND project_key = ?"
            params.append(_project_key(project_id))
        sql += " GROUP BY scope, project_key, subject HAVING count(*) >= 2 ORDER BY size DESC, subject LIMIT ?"
        params.append(max(1, int(limit)))
        with self._connect() as conn:  # type: ignore[attr-defined]
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    @staticmethod
    def _pair_key(left: str, right: str) -> tuple[str, str]:
        return (left, right) if left <= right else (right, left)

    def add_dedup_rejected_pair(self, left_id: str, right_id: str) -> None:
        """Remember that a person said these two nodes are different facts."""
        from .models import utc_now

        a, b = self._pair_key(str(left_id), str(right_id))
        with self._connect() as conn:  # type: ignore[attr-defined]
            conn.execute(
                "INSERT OR REPLACE INTO dedup_rejected_pairs(left_id, right_id, at) VALUES (?, ?, ?)",
                (a, b, utc_now()),
            )

    def dedup_rejected_pairs_for(self, node_ids: list[str]) -> set[tuple[str, str]]:
        ids = [str(node_id) for node_id in dict.fromkeys(node_ids) if node_id][:MAX_QUERY_STEMS]
        if not ids:
            return set()
        placeholders = ", ".join("?" for _ in ids)
        with self._connect() as conn:  # type: ignore[attr-defined]
            rows = conn.execute(
                f"SELECT left_id, right_id FROM dedup_rejected_pairs "
                f"WHERE left_id IN ({placeholders}) OR right_id IN ({placeholders})",
                [*ids, *ids],
            ).fetchall()
        return {(str(row[0]), str(row[1])) for row in rows}

    def list_recent_dedup_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Newest gate/compaction decisions (``dedup`` memory events) across all nodes."""
        with self._connect() as conn:  # type: ignore[attr-defined]
            rows = conn.execute(
                "SELECT id, node_id, event_type, actor, timestamp, details FROM memory_node_events "
                "WHERE event_type = 'dedup' ORDER BY timestamp DESC, rowid DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [self._memory_event_from_row(row) for row in rows]  # type: ignore[attr-defined]
