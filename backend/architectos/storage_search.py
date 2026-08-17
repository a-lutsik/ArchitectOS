"""FTS5 and embedding search methods for SQLiteMemoryRepository.

Split out of ``storage`` so lexical/vector indexing stays next to each other
without bloating the core repository CRUD module. ``SQLiteMemoryRepository``
inherits ``StorageSearchMixin`` so existing call sites keep working.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from .models import MemoryNode, utc_now

_LOG = logging.getLogger("architectos.storage")


class StorageSearchMixin:
    def search_memory_fts(
        self,
        query: str,
        project_id: str | None = None,
        scope: str | None = None,
        limit: int = 64,
    ) -> list[str]:
        """Return node ids ranked by FTS5 BM25 (best first). Empty if no usable match."""
        and_match = self._fts_match_query(query, joiner="AND")
        or_match = self._fts_match_query(query, joiner="OR")
        if not and_match and not or_match:
            return []
        ids = self._run_memory_fts(and_match, project_id=project_id, scope=scope, limit=limit) if and_match else []
        if not ids and or_match and or_match != and_match:
            ids = self._run_memory_fts(or_match, project_id=project_id, scope=scope, limit=limit)
        return ids

    def _run_memory_fts(
        self,
        match: str,
        *,
        project_id: str | None,
        scope: str | None,
        limit: int,
    ) -> list[str]:
        limit = max(1, min(int(limit or 64), 200))
        clauses = ["memory_nodes_fts MATCH ?", "status = 'active'"]
        params: list[Any] = [match]
        if scope:
            clauses.append("scope = ?")
            params.append(scope)
            if scope in {"project", "interface"} and project_id:
                clauses.append("project_id = ?")
                params.append(project_id)
        elif project_id:
            clauses.append("(scope IN ('shared', 'global') OR project_id = ? OR project_id IS NULL OR project_id = '')")
            params.append(project_id)
        params.append(limit)
        sql = (
            "SELECT node_id FROM memory_nodes_fts "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY bm25(memory_nodes_fts) LIMIT ?"
        )
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            _LOG.warning("memory FTS query failed, returning no hits: %s", exc)
            return []
        return [str(row["node_id"]) for row in rows if row["node_id"]]

    def rebuild_memory_fts(self) -> dict[str, int]:
        with self._connect() as conn:
            conn.execute("DELETE FROM memory_nodes_fts")
            rows = conn.execute("SELECT payload FROM memory_nodes").fetchall()
            count = 0
            for row in rows:
                node = MemoryNode.from_dict(json.loads(row["payload"]))
                if self._sync_memory_fts_row(conn, node):
                    count += 1
        return {"indexed": count}

    def _ensure_memory_fts(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_nodes_fts USING fts5(
                    node_id UNINDEXED,
                    label,
                    text,
                    type UNINDEXED,
                    scope UNINDEXED,
                    project_id UNINDEXED,
                    status UNINDEXED,
                    tokenize='porter unicode61'
                )
                """
            )
            fts_count = int(conn.execute("SELECT count(*) FROM memory_nodes_fts").fetchone()[0])
            node_count = int(conn.execute("SELECT count(*) FROM memory_nodes WHERE status = 'active'").fetchone()[0])
        if node_count and fts_count == 0:
            self.rebuild_memory_fts()

    @staticmethod
    def _sync_memory_fts_row(conn: sqlite3.Connection, node: MemoryNode) -> bool:
        conn.execute("DELETE FROM memory_nodes_fts WHERE node_id = ?", (node.id,))
        if node.status != "active":
            return False
        conn.execute(
            """
            INSERT INTO memory_nodes_fts(node_id, label, text, type, scope, project_id, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node.id,
                node.label or "",
                node.text or "",
                node.type or "",
                node.scope or "",
                node.project_id or "",
                node.status or "",
            ),
        )
        return True

    @staticmethod
    def _fts_match_query(query: str, joiner: str = "AND") -> str:
        # Must accept non-ASCII (e.g. Cyrillic); FTS5 uses unicode61 tokenizer.
        # Drop stopwords so the OR fallback does not flood with ADO noise from "что".
        from .embeddings import QUERY_STOPWORDS, content_tokens, expand_query_terms

        terms = expand_query_terms(content_tokens(query))
        if not terms:
            return ""
        parts: list[str] = []
        for term in terms[:16]:
            safe = term.replace('"', "")
            if not safe or safe in QUERY_STOPWORDS or len(safe) < 2:
                continue
            parts.append(f'"{safe}"*')
        if not parts:
            return ""
        sep = " OR " if str(joiner).upper() == "OR" else " AND "
        return sep.join(parts)

    def upsert_memory_embedding(
        self,
        *,
        node_id: str,
        scope: str,
        project_id: str | None,
        status: str,
        provider_id: str,
        model: str,
        dimensions: int,
        vector: list[float],
    ) -> None:
        from .embeddings import pack_vector

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO memory_embeddings(
                    node_id, scope, project_id, status, provider, model, dimensions, vector, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    scope or "project",
                    project_id,
                    status or "active",
                    provider_id,
                    model,
                    int(dimensions),
                    pack_vector(vector),
                    utc_now(),
                ),
            )

    def delete_memory_embedding(self, node_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM memory_embeddings WHERE node_id = ?", (node_id,))

    def list_nodes_missing_embeddings(self, limit: int = 500) -> list[MemoryNode]:
        limit = max(1, min(int(limit or 500), 5000))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT n.payload
                FROM memory_nodes n
                LEFT JOIN memory_embeddings e ON e.node_id = n.id
                WHERE n.status = 'active' AND e.node_id IS NULL
                ORDER BY n.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [MemoryNode.from_dict(json.loads(row["payload"])) for row in rows]

    def memory_embedding_coverage(self, project_id: str | None = None) -> dict[str, Any]:
        """Count active memory nodes vs rows present in memory_embeddings."""
        with self._connect() as conn:
            if project_id:
                active = int(
                    conn.execute(
                        """
                        SELECT count(*) FROM memory_nodes
                        WHERE status = 'active'
                          AND (project_id = ? OR project_id IS NULL OR project_id = '')
                        """,
                        (project_id,),
                    ).fetchone()[0]
                    or 0
                )
                indexed = int(
                    conn.execute(
                        """
                        SELECT count(*)
                        FROM memory_nodes n
                        JOIN memory_embeddings e ON e.node_id = n.id
                        WHERE n.status = 'active'
                          AND (n.project_id = ? OR n.project_id IS NULL OR n.project_id = '')
                        """,
                        (project_id,),
                    ).fetchone()[0]
                    or 0
                )
            else:
                active = int(conn.execute("SELECT count(*) FROM memory_nodes WHERE status = 'active'").fetchone()[0] or 0)
                indexed = int(
                    conn.execute(
                        """
                        SELECT count(*)
                        FROM memory_nodes n
                        JOIN memory_embeddings e ON e.node_id = n.id
                        WHERE n.status = 'active'
                        """
                    ).fetchone()[0]
                    or 0
                )
            by_rows = conn.execute(
                """
                SELECT provider, model, dimensions, count(*) AS c
                FROM memory_embeddings
                GROUP BY provider, model, dimensions
                ORDER BY c DESC
                """
            ).fetchall()
        missing = max(0, active - indexed)
        pct = round((100.0 * indexed / active), 1) if active else 0.0
        return {
            "active_nodes": active,
            "indexed": indexed,
            "missing": missing,
            "coverage_pct": pct,
            "by_provider": [
                {
                    "provider": str(row["provider"] or ""),
                    "model": str(row["model"] or ""),
                    "dimensions": int(row["dimensions"] or 0),
                    "count": int(row["c"] or 0),
                }
                for row in by_rows
            ],
        }

    def list_memory_embedding_rows(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT node_id, scope, project_id, status, dimensions, vector
                FROM memory_embeddings
                WHERE status = 'active'
                """
            ).fetchall()
        return [
            {
                "node_id": str(row["node_id"]),
                "scope": str(row["scope"] or ""),
                "project_id": str(row["project_id"] or ""),
                "status": str(row["status"] or ""),
                "dimensions": int(row["dimensions"] or 0),
                "vector": row["vector"] or b"",
            }
            for row in rows
        ]

    def search_memory_vectors(
        self,
        query_vector: list[float],
        *,
        project_id: str | None = None,
        scope: str | None = None,
        limit: int = 64,
        min_score: float = 0.22,
    ) -> list[str]:
        return [node_id for node_id, _ in self.search_memory_vectors_scored(
            query_vector,
            project_id=project_id,
            scope=scope,
            limit=limit,
            min_score=min_score,
        )]

    def search_memory_vectors_scored(
        self,
        query_vector: list[float],
        *,
        project_id: str | None = None,
        scope: str | None = None,
        limit: int = 64,
        min_score: float = 0.22,
    ) -> list[tuple[str, float]]:
        from .embeddings import cosine, unpack_vector

        if not query_vector:
            return []
        dims = len(query_vector)
        limit = max(1, min(int(limit or 64), 200))
        clauses = ["dimensions = ?", "status = 'active'"]
        params: list[Any] = [dims]
        if scope:
            clauses.append("scope = ?")
            params.append(scope)
            if scope in {"project", "interface"} and project_id:
                clauses.append("project_id = ?")
                params.append(project_id)
        elif project_id:
            clauses.append("(scope IN ('shared', 'global') OR project_id = ? OR project_id IS NULL OR project_id = '')")
            params.append(project_id)
        sql = f"SELECT node_id, vector FROM memory_embeddings WHERE {' AND '.join(clauses)}"
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            _LOG.warning("memory vector query failed, returning no hits: %s", exc)
            return []
        ranked: list[tuple[str, float]] = []
        for row in rows:
            vector = unpack_vector(row["vector"])
            if len(vector) != dims:
                continue
            score = cosine(query_vector, vector)
            if score >= min_score:
                ranked.append((str(row["node_id"]), score))
        ranked.sort(key=lambda item: item[1], reverse=True)
        return ranked[:limit]

    def rebuild_memory_embeddings(self) -> dict[str, int]:
        with self._connect() as conn:
            conn.execute("DELETE FROM memory_embeddings")
        return {"cleared": 1}
