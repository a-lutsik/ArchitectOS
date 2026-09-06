from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import BACKUP_RETENTION
from .constants import SECRET_MASK
from .models import MemoryEdge, MemoryNode, Project, Source, stable_id, utc_now
from .security import SecurityPolicy
from .storage_candidates import StorageCandidatesMixin
from .storage_migrate import SCHEMA_VERSION, StorageMigrateMixin
from .storage_search import StorageSearchMixin
from .storage_sessions import StorageSessionsMixin
from .vecsql import load_sqlite_vec, sqlite_vec_available

__all__ = [
    "SCHEMA_VERSION",
    "SENSITIVE_SETTING_KEYS",
    "SQLiteMemoryRepository",
    "like_prefilter_supported",
    "sanitize_payload",
    "sanitize_text",
    "strip_sensitive_settings",
]

_SECURITY_POLICY = SecurityPolicy()
_LOG = logging.getLogger("architectos.storage")


def sanitize_text(value: str) -> tuple[str, bool]:
    return _SECURITY_POLICY.redact_text(value)


def sanitize_payload(value: Any) -> tuple[Any, bool]:
    return _SECURITY_POLICY.redact_payload(value)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def like_prefilter_supported(query: str) -> bool:
    """True when ``payload LIKE '%query%'`` is a safe superset of a Python substring match.

    Two cases where it is not, and the caller must fall back to scanning:

    * Non-ASCII. Payloads are written with ``json.dumps`` default
      ``ensure_ascii=True``, so "Кириллица" is stored as ``\\u041a...`` and a
      literal LIKE never matches it.
    * Whitespace. Callers match against fields joined by spaces, so a
      multi-word query can straddle two fields in Python while the JSON text
      has punctuation between them.

    ASCII LIKE is case-insensitive in SQLite, which matches the ``.lower()``
    comparison callers do.
    """
    text = (query or "").strip()
    return bool(text) and text.isascii() and not any(char.isspace() for char in text)


# Keys that hold credential material and must never leave the process via
# bundle export (OAuth tokens are persisted in the settings table).
SENSITIVE_SETTING_KEYS = {"access_token", "refresh_token", "client_secret", "code_verifier", "id_token"}


def strip_sensitive_settings(value: Any) -> Any:
    """Recursively remove credential material from a settings payload."""
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name in SENSITIVE_SETTING_KEYS or name.lower().endswith(("_token", "_secret", "_password")):
                continue
            if name == "auth" and isinstance(item, dict):
                # Keep non-secret auth status, drop everything else.
                clean[name] = {
                    "status": item.get("status") or ("authorized" if item.get("access_token") else ""),
                    "expires_at": item.get("expires_at") or 0,
                }
                continue
            if name in ("env", "headers") and isinstance(item, dict):
                # MCP server env vars / HTTP headers are keyed by user-chosen
                # names, so keep the keys but mask every value.
                clean[name] = {str(entry_key): SECRET_MASK for entry_key in item}
                continue
            clean[name] = strip_sensitive_settings(item)
        return clean
    if isinstance(value, list):
        return [strip_sensitive_settings(item) for item in value]
    return value


class SQLiteMemoryRepository(StorageSearchMixin, StorageCandidatesMixin, StorageSessionsMixin, StorageMigrateMixin):
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.data_dir = project_root / "data"
        self.memory_dir = project_root / "memory"
        self.evidence_dir = self.memory_dir / "evidence"
        self.db_path = self.data_dir / "architectos.db"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self._node_upsert_listeners: list[Any] = []
        # Thread-local slot for the connection owned by transaction(); must exist
        # before _migrate() runs its first _connect().
        self._local = threading.local()
        self._migrate()
        try:
            # The database holds memory and settings data; restrict it to the owner.
            os.chmod(self.db_path, 0o600)
        except OSError as exc:
            # Windows may ignore or reject POSIX modes; keep that harmless.
            _LOG.debug("could not set 0o600 on database file: %s", exc)

    def register_node_upsert_listener(self, listener) -> None:
        if listener not in self._node_upsert_listeners:
            self._node_upsert_listeners.append(listener)

    @staticmethod
    def _prepare_connection(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error as exc:
            _LOG.debug("SQLite pragma setup failed: %s", exc)
        if sqlite_vec_available():
            load_sqlite_vec(conn)

    def vector_backend(self) -> str:
        return "sqlite-vec" if sqlite_vec_available() else "python"

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Inside transaction() the current thread already owns a connection; join it
        # without committing or closing (the outer transaction owns the lifecycle).
        active = getattr(self._local, "transaction_connection", None)
        if active is not None:
            yield active
            return
        # timeout + WAL: embedding backfill writers must not freeze Search palette reads.
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        self._prepare_connection(conn)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run the wrapped repository calls as one atomic unit (BEGIN IMMEDIATE).

        On clean exit the transaction commits; on exception it rolls back and the
        exception re-raises. The connection is pinned to the creating thread via
        ``self._local`` (sqlite3 connections must stay on their creating thread);
        nested ``transaction()`` calls in the same thread reuse the active one.
        """
        active = getattr(self._local, "transaction_connection", None)
        if active is not None:
            yield active
            return
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        self._prepare_connection(conn)
        self._local.transaction_connection = conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            self._local.transaction_connection = None
            conn.close()

    def health_check(self) -> dict[str, Any]:
        try:
            with self._connect() as conn:
                quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
                tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
            status = "ok" if quick_check == "ok" and tables >= 8 else "degraded"
            return {"status": status, "path": str(self.db_path), "quick_check": quick_check, "tables": tables}
        except sqlite3.Error as exc:
            return {"status": "error", "path": str(self.db_path), "error": str(exc)}

    def create_backup(self, label: str = "", retention: int = BACKUP_RETENTION) -> dict[str, Any]:
        stamp = utc_now().replace(":", "").replace("-", "")
        safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "_", label.strip()).strip("_")[:32]
        suffix = f"__{safe_label}" if safe_label else ""
        backup_dir = self.project_root / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"architectos-{stamp}{suffix}.db"
        source = sqlite3.connect(self.db_path)
        target = sqlite3.connect(backup_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        manifest = {
            "path": str(backup_path),
            "size": backup_path.stat().st_size,
            "created_at": utc_now(),
            "source": str(self.db_path),
            "label": label.strip(),
        }
        (backup_path.with_suffix(".json")).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        self._prune_backups(retention)
        return manifest

    def list_backups(self, limit: int = 20) -> list[dict[str, Any]]:
        backup_dir = self.project_root / "backups"
        if not backup_dir.exists():
            return []
        items = []
        for path in sorted(backup_dir.glob("architectos-*.db"), key=lambda item: item.stat().st_mtime, reverse=True):
            manifest_path = path.with_suffix(".json")
            if manifest_path.exists():
                try:
                    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    payload = {}
            else:
                payload = {}
            payload.update({"path": str(path), "size": path.stat().st_size})
            items.append(payload)
            if len(items) >= limit:
                break
        return items

    def _prune_backups(self, retention: int) -> None:
        if retention < 1:
            return
        backup_dir = self.project_root / "backups"
        backups = sorted(backup_dir.glob("architectos-*.db"), key=lambda item: item.stat().st_mtime, reverse=True)
        for stale in backups[retention:]:
            stale.unlink(missing_ok=True)
            stale.with_suffix(".json").unlink(missing_ok=True)

    def list_projects(self) -> list[Project]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM projects ORDER BY name").fetchall()
        return [self._project_from_payload(json.loads(row["payload"])) for row in rows]

    def _project_from_payload(self, data: dict[str, Any]) -> Project:
        payload = dict(data)
        payload.setdefault("config", {})
        return Project(**payload)

    def get_project(self, project_id: str) -> Project | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM projects WHERE id = ?", (project_id,)).fetchone()
        return self._project_from_payload(json.loads(row["payload"])) if row else None

    def upsert_project(self, project: Project) -> Project:
        project.updated_at = utc_now()
        payload = project.to_dict()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO projects(id, name, root_path, description, created_at, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (project.id, project.name, project.root_path, project.description, project.created_at, project.updated_at, json.dumps(payload, sort_keys=True)),
            )
        return project

    def add_project(self, name: str, root_path: str = "", description: str = "") -> Project:
        project = Project(id=stable_id("project", name, root_path), name=name.strip(), root_path=root_path.strip(), description=description.strip())
        return self.upsert_project(project)

    # -- Sources (external data origins) ------------------------------------
    def upsert_source(self, source: Source) -> Source:
        source.updated_at = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sources(id, project_id, name, kind, created_at, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source.id, source.project_id, source.name, source.kind, source.created_at, source.updated_at, json.dumps(source.to_dict(), sort_keys=True)),
            )
        return source

    def get_source(self, source_id: str) -> Source | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM sources WHERE id = ?", (source_id,)).fetchone()
        return Source.from_dict(json.loads(row["payload"])) if row else None

    def list_sources(self, project_id: str | None = None) -> list[Source]:
        with self._connect() as conn:
            if project_id:
                rows = conn.execute("SELECT payload FROM sources WHERE project_id = ? ORDER BY name", (project_id,)).fetchall()
            else:
                rows = conn.execute("SELECT payload FROM sources ORDER BY project_id, name").fetchall()
        return [Source.from_dict(json.loads(row["payload"])) for row in rows]

    # -- Memory node events (history) ----------------------------------------
    def record_memory_event(
        self,
        node_id: str,
        event_type: str,
        actor: str | None = None,
        details: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        event = {
            "id": f"event_{uuid4().hex[:16]}",
            "node_id": node_id,
            "event_type": event_type,
            "actor": actor or "",
            "timestamp": timestamp or utc_now(),
            "details": dict(details or {}),
        }
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO memory_node_events(id, node_id, event_type, actor, timestamp, details) VALUES (?, ?, ?, ?, ?, ?)",
                (event["id"], event["node_id"], event["event_type"], event["actor"], event["timestamp"], json.dumps(event["details"], sort_keys=True)),
            )
        return event

    def list_memory_events(self, node_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, node_id, event_type, actor, timestamp, details FROM memory_node_events WHERE node_id = ? ORDER BY timestamp, rowid LIMIT ?",
                (node_id, max(1, int(limit or 200))),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            try:
                details = json.loads(row["details"] or "{}")
            except ValueError:
                details = {}
            events.append({
                "id": row["id"],
                "node_id": row["node_id"],
                "event_type": row["event_type"],
                "actor": row["actor"] or "",
                "timestamp": row["timestamp"],
                "details": details if isinstance(details, dict) else {},
            })
        return events

    def add_node(
        self,
        node_type: str,
        label: str,
        scope: str,
        text: str,
        project_id: str | None = None,
        interface_id: str | None = None,
        confidence: float = 0.8,
        metadata: dict[str, Any] | None = None,
        source_id: str | None = None,
    ) -> MemoryNode:
        clean_text, redacted = sanitize_text(text)
        clean_label, label_redacted = sanitize_text(label)
        if not clean_text or len(clean_text) < 4:
            raise ValueError("memory text must contain at least 4 non-secret characters")
        now = utc_now()
        node = MemoryNode(
            id=stable_id("node", node_type, scope, project_id or "", interface_id or "", clean_label),
            type=node_type if node_type else "Concept",
            label=clean_label[:180],
            scope=scope if scope else "project",
            text=clean_text,
            project_id=project_id,
            interface_id=interface_id,
            source_id=source_id,
            confidence=max(0.0, min(1.0, confidence)),
            metadata=dict(metadata or {}),
            created_at=now,
            updated_at=now,
        )
        if source_id:
            node.metadata.setdefault("source_id", source_id)
        if redacted or label_redacted:
            node.metadata["redacted"] = True
        node.evidence = [self._write_evidence(node)]
        return self.upsert_node(node)

    def upsert_node(self, node: MemoryNode, *, notify: bool = True) -> MemoryNode:
        node.updated_at = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO memory_nodes(id, type, label, scope, project_id, interface_id, source_id, status, confidence, created_at, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (node.id, node.type, node.label, node.scope, node.project_id, node.interface_id, node.source_id, node.status, node.confidence, node.created_at, node.updated_at, json.dumps(node.to_dict(), sort_keys=True)),
            )
            self._sync_memory_fts_row(conn, node)
        if notify:
            for listener in self._node_upsert_listeners:
                try:
                    listener(node)
                except Exception:
                    _LOG.exception("node upsert listener failed for node %s", node.id)
        return node

    def get_node(self, node_id: str) -> MemoryNode | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM memory_nodes WHERE id = ?", (node_id,)).fetchone()
        return MemoryNode.from_dict(json.loads(row["payload"])) if row else None

    def toggle_memory_favorite(self, node_id: str) -> MemoryNode:
        node = self.get_node(node_id)
        if not node:
            raise ValueError("memory not found")
        node.metadata["favorite"] = not bool(node.metadata.get("favorite", False))
        return self.upsert_node(node)

    def add_edge(self, source: str, target: str, edge_type: str, scope: str, confidence: float = 0.8) -> MemoryEdge:
        now = utc_now()
        edge = MemoryEdge(id=stable_id("edge", edge_type, source, target, scope), source=source, target=target, type=edge_type or "RELATED_TO", scope=scope or "project", confidence=max(0.0, min(1.0, confidence)), created_at=now, updated_at=now)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO memory_edges(id, source, target, type, scope, status, confidence, created_at, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (edge.id, edge.source, edge.target, edge.type, edge.scope, edge.status, edge.confidence, edge.created_at, edge.updated_at, json.dumps(edge.to_dict(), sort_keys=True)),
            )
        return edge

    def upsert_edge(self, edge: MemoryEdge) -> MemoryEdge:
        edge.updated_at = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO memory_edges(id, source, target, type, scope, status, confidence, created_at, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (edge.id, edge.source, edge.target, edge.type, edge.scope, edge.status, edge.confidence, edge.created_at, edge.updated_at, json.dumps(edge.to_dict(), sort_keys=True)),
            )
        return edge

    def delete_edge(self, edge_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM memory_edges WHERE id = ?", (edge_id,))
            return cur.rowcount > 0

    def list_nodes(
        self,
        limit: int | None = None,
        project_id: str | None = None,
        *,
        status: str | None = None,
        include_shared: bool = False,
        node_type: str | None = None,
        node_types: list[str] | tuple[str, ...] | None = None,
        scope: str | None = None,
        text_like: str | None = None,
    ) -> list[MemoryNode]:
        """Load memory nodes with optional project / status filters.

        When ``project_id`` is set and ``include_shared`` is True, also returns
        nodes with a null/empty project_id and nodes scoped as shared/global —
        the set the graph view needs without a full-table scan.

        ``text_like`` narrows to rows whose JSON payload contains the substring.
        It is a *superset* of an in-Python field match, so callers must still
        apply their exact predicate; see :func:`like_prefilter_supported` for
        when it may be used at all.
        """
        sql = "SELECT payload FROM memory_nodes"
        params: list[Any] = []
        clauses: list[str] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        wanted_types = [
            str(item).strip()
            for item in (list(node_types) if node_types is not None else ([node_type] if node_type else []))
            if str(item or "").strip()
        ]
        if wanted_types:
            clauses.append(f"type IN ({','.join('?' for _ in wanted_types)})")
            params.extend(wanted_types)
        if scope:
            clauses.append("scope = ?")
            params.append(scope)
        if text_like:
            clauses.append("payload LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(text_like)}%")
        if project_id and include_shared:
            clauses.append("(project_id = ? OR project_id IS NULL OR project_id = '' OR scope IN ('shared', 'global'))")
            params.append(project_id)
        elif project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        # Timestamps have second resolution, so ties are common; rowid keeps the
        # order deterministic (insertion order) regardless of the query plan.
        sql += " ORDER BY updated_at DESC, created_at DESC, rowid ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [MemoryNode.from_dict(json.loads(row["payload"])) for row in rows]

    def _project_clause(self, project_id: str | None, include_shared: bool) -> tuple[str, list[Any]]:
        if project_id and include_shared:
            return (
                " AND (project_id = ? OR project_id IS NULL OR project_id = '' OR scope IN ('shared', 'global'))",
                [project_id],
            )
        if project_id:
            return (" AND project_id = ?", [project_id])
        return ("", [])

    def nodes_revision(self, project_id: str | None = None, *, include_shared: bool = False) -> str:
        """Cheap change token for the active node set, for caching derived views.

        Count plus newest timestamp, so it misses an in-place edit that keeps the
        count and reuses the same one-second timestamp. Callers must therefore
        tolerate a briefly stale value; it exists to avoid rescanning the store on
        every request, not as a correctness guarantee.
        """
        clause, params = self._project_clause(project_id, include_shared)
        sql = "SELECT count(*), max(updated_at) FROM memory_nodes WHERE status = 'active'" + clause
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return f"{row[0] or 0}:{row[1] or ''}"

    def list_nodes_missing_lifecycle(
        self, project_id: str | None = None, *, include_shared: bool = False
    ) -> list[MemoryNode]:
        """Nodes with no lifecycle tier seeded yet, in any status.

        The annotate pass is a no-op for nodes that already carry a tier, so
        selecting them in SQL lets a warmed-up store skip decoding every payload.
        """
        clause, params = self._project_clause(project_id, include_shared)
        sql = (
            "SELECT payload FROM memory_nodes "
            "WHERE coalesce(json_extract(payload, '$.metadata.memory_tier'), '') = ''" + clause
        )
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [MemoryNode.from_dict(json.loads(row["payload"])) for row in rows]

    def list_nodes_by_ids(self, node_ids: list[str]) -> list[MemoryNode]:
        ids = [str(item) for item in node_ids if str(item).strip()]
        if not ids:
            return []
        by_id: dict[str, MemoryNode] = {}
        with self._connect() as conn:
            for chunk_start in range(0, len(ids), 200):
                chunk = ids[chunk_start : chunk_start + 200]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT payload FROM memory_nodes WHERE id IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in rows:
                    node = MemoryNode.from_dict(json.loads(row["payload"]))
                    by_id[node.id] = node
        return [by_id[node_id] for node_id in ids if node_id in by_id]

    def add_retrieval_feedback(self, payload: dict[str, Any]) -> dict[str, Any]:
        rating = int(payload.get("rating") or 0)
        if rating not in {-1, 1}:
            raise ValueError("rating must be 1 (helpful) or -1 (not helpful)")
        hit_ids = [str(item).strip() for item in (payload.get("hit_ids") or []) if str(item).strip()]
        record = {
            "id": str(payload.get("id") or stable_id("feedback", payload.get("project_id") or "architectos", utc_now(), str(rating), ",".join(hit_ids[:6]))),
            "project_id": str(payload.get("project_id") or "architectos"),
            "chat_id": str(payload.get("chat_id") or ""),
            "run_id": str(payload.get("run_id") or ""),
            "message_index": payload.get("message_index"),
            "query": str(payload.get("query") or "")[:500],
            "rating": rating,
            "hit_ids": hit_ids,
            "note": str(payload.get("note") or "")[:500],
            "created_at": utc_now(),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO retrieval_feedback(id, project_id, chat_id, run_id, query, rating, created_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["id"],
                    record["project_id"],
                    record["chat_id"],
                    record["run_id"],
                    record["query"],
                    record["rating"],
                    record["created_at"],
                    json.dumps(record, sort_keys=True),
                ),
            )
        return record

    def list_retrieval_feedback(self, project_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 50), 200))
        with self._connect() as conn:
            if project_id:
                rows = conn.execute(
                    "SELECT payload FROM retrieval_feedback WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
                    (project_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload FROM retrieval_feedback ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def feedback_scores_for_nodes(self, project_id: str | None = None) -> dict[str, float]:
        """Aggregate helpfulness per memory node id from recent feedback."""
        scores: dict[str, float] = {}
        for item in self.list_retrieval_feedback(project_id, limit=200):
            rating = int(item.get("rating") or 0)
            weight = 1.0 if rating > 0 else -0.6 if rating < 0 else 0.0
            for node_id in item.get("hit_ids") or []:
                key = str(node_id).strip()
                if key:
                    scores[key] = scores.get(key, 0.0) + weight
        return scores

    def list_edges(self, limit: int | None = None) -> list[MemoryEdge]:
        sql = "SELECT payload FROM memory_edges ORDER BY created_at, rowid ASC"
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [MemoryEdge.from_dict(json.loads(row["payload"])) for row in rows]

    def list_edge_ids(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id FROM memory_edges").fetchall()
        return {str(row["id"]) for row in rows}

    def list_edges_touching(self, node_ids: list[str] | set[str]) -> list[MemoryEdge]:
        """Edges with at least one endpoint in ``node_ids`` (graph neighborhood fetch)."""
        ids = [str(node_id) for node_id in node_ids if str(node_id or "")]
        if not ids:
            return []
        results: list[MemoryEdge] = []
        # Keep IN clauses well under the SQLite variable limit (999).
        for offset in range(0, len(ids), 400):
            chunk = ids[offset:offset + 400]
            placeholders = ",".join("?" for _ in chunk)
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT payload FROM memory_edges WHERE source IN ({placeholders}) OR target IN ({placeholders})",
                    (*chunk, *chunk),
                ).fetchall()
            results.extend(MemoryEdge.from_dict(json.loads(row["payload"])) for row in rows)
        return results

    def delete_node_edges(self, node_id: str) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM memory_edges WHERE source = ? OR target = ?", (node_id, node_id))
            return cur.rowcount

