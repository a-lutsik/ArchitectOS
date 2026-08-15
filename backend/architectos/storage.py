from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import json
import logging
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, cast

from .config import BACKUP_RETENTION
from .constants import SECRET_MASK
from .models import MemoryEdge, MemoryNode, Project, stable_id, utc_now
from .security import SecurityPolicy

_SECURITY_POLICY = SecurityPolicy()
_LOG = logging.getLogger("architectos.storage")


def sanitize_text(value: str) -> tuple[str, bool]:
    return _SECURITY_POLICY.redact_text(value)


def sanitize_payload(value: Any) -> tuple[Any, bool]:
    return _SECURITY_POLICY.redact_payload(value)


# Keys that hold credential material and must never leave the process via
# bundle export (OAuth tokens are persisted in the settings table).
SENSITIVE_SETTING_KEYS = {"access_token", "refresh_token", "client_secret", "code_verifier", "id_token"}

# Current database schema version. MIGRATIONS (bottom of this module) holds one
# (version, fn) entry per step; _migrate applies every step above the database's
# PRAGMA user_version (0 for databases predating versioning) and stamps the rest.
SCHEMA_VERSION = 3


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


class SQLiteMemoryRepository:
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
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error as exc:
            _LOG.debug("SQLite pragma setup failed: %s", exc)
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
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error as exc:
            _LOG.debug("SQLite pragma setup failed: %s", exc)
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

    def _migrate(self) -> None:
        with self._connect() as conn:
            current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            # Baseline: idempotent creates, safe for both fresh and existing DBs.
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    root_path TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_nodes (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    label TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    project_id TEXT,
                    interface_id TEXT,
                    status TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_edges (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    target TEXT NOT NULL,
                    type TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_candidates (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    favorite INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS providers (
                    id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_runs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_context_summaries (
                    id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    summary_type TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS keeper_events (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_nodes_scope ON memory_nodes(scope, project_id, status);
                CREATE INDEX IF NOT EXISTS idx_memory_nodes_type ON memory_nodes(type, status);
                CREATE INDEX IF NOT EXISTS idx_memory_edges_source ON memory_edges(source, status);
                CREATE INDEX IF NOT EXISTS idx_memory_edges_target ON memory_edges(target, status);
                CREATE INDEX IF NOT EXISTS idx_memory_candidates_project_status ON memory_candidates(project_id, status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project_id, status);
                CREATE INDEX IF NOT EXISTS idx_chat_project ON chat_sessions(project_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_provider_runs_project ON provider_runs(project_id, updated_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_context_summary_type ON chat_context_summaries(chat_id, summary_type);
                CREATE INDEX IF NOT EXISTS idx_keeper_events_project ON keeper_events(project_id, chat_id, updated_at);
                CREATE TABLE IF NOT EXISTS memory_embeddings (
                    node_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    project_id TEXT,
                    status TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_embeddings_scope_project
                    ON memory_embeddings(scope, project_id, status, dimensions);
                CREATE TABLE IF NOT EXISTS retrieval_feedback (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    chat_id TEXT,
                    run_id TEXT,
                    query TEXT NOT NULL DEFAULT '',
                    rating INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_retrieval_feedback_project
                    ON retrieval_feedback(project_id, created_at);
                """
            )
            for version, migration in MIGRATIONS:
                if version > current_version:
                    migration(self, conn)
            if current_version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._ensure_memory_fts()

    def seed_if_empty(self) -> None:
        projects = self.list_projects()
        system_project = self.get_project("architectos")
        if not projects or not system_project:
            self.upsert_project(Project(
                id="architectos",
                name="System Workspace",
                root_path="",
                description="Internal ArchitectOS workspace. Create or select a project folder before indexing memory.",
            ))
        elif system_project.name == "ArchitectOS" or Path(system_project.root_path or "") == self.project_root:
            system_project.name = "System Workspace"
            system_project.root_path = ""
            system_project.description = "Internal ArchitectOS workspace. Create or select a project folder before indexing memory."
            self.upsert_project(system_project)

        self._delete_empty_non_system_projects()
        self._delete_seed_memory()
        self._repair_missing_project_roots()

        if not self.list_tasks("architectos"):
            for title, status, priority, detail in [
                ("Security layer", "done", "high", "Add explicit approvals, prompt/result redaction, and no secret persistence checks."),
                ("E2E tests and installer docs", "done", "medium", "Add E2E tests, fake adapter tests, installer/start script checks, and configuration docs."),
                ("Production hardening", "done", "high", "Readiness checks, backups, security headers, release metadata, and production docs."),
            ]:
                self.upsert_task({"id": stable_id("task", "architectos", title), "project_id": "architectos", "title": title, "status": status, "priority": priority, "detail": detail, "linked_memory_ids": [], "created_at": utc_now()})

        if not self.list_providers():
            for provider_id, label, kind, model in [
                ("codex-cli", "Codex CLI", "cli", "gpt-5-codex"),
                ("claude-code", "Claude Code", "cli", ""),
                ("gemini-cli", "Antigravity CLI", "cli", ""),
                ("ollama", "Ollama", "local", ""),
                ("openai", "OpenAI", "api", ""),
                ("azure-openai", "Azure OpenAI", "api", ""),
                ("anthropic", "Anthropic", "api", ""),
                ("openrouter", "OpenRouter", "api", ""),
            ]:
                self.upsert_provider({"id": provider_id, "label": label, "provider_type": kind, "status": "planned", "enabled": False, "model": model, "notes": "", "command": _default_provider_command(provider_id), "timeout_seconds": 120, "approval_required": kind == "cli", "workdir_policy": "project-root", "workdir": ""})

        if not self.get_setting("ui"):
            self.set_setting("ui", {"theme": "system", "density": "comfortable", "memory_enabled": True, "language": "en", "onboarding_complete": False})
        else:
            self._ensure_onboarding_flag()
        if not self.get_setting("security"):
            self.set_setting("security", {"redact_prompts": True, "redact_results": True, "redact_audit": True, "persist_raw_provider_payloads": False})
        if not self.get_setting("memory_lifecycle"):
            self.set_setting("memory_lifecycle", {
                "enabled": True,
                "refresh_on_access": True,
                "short_term_ttl_days": 14,
                "archive_after_days": 30,
                "delete_after_days": 0,
                "promote_after_hits": 5,
                "auto_rescan_on_startup": True,
                "auto_rescan_limit": 24,
                "auto_rescan_all_projects": True,
                "chat_memory_mode": "strict",
                "chat_candidate_ttl_days": 7,
                "chat_session_idle_minutes": 30,
                "chat_store_facts_only": True,
                "long_term_types": ["Decision", "Constraint", "Requirement"],
                "architecture_keywords": ["architecture", "adr", "decision", "constraint", "security", "provider", "routing"],
            })
        if not self.get_setting("memory_retrieval"):
            from .embeddings import default_memory_retrieval_settings

            self.set_setting("memory_retrieval", default_memory_retrieval_settings())
        if not self.get_setting("router"):
            self.set_setting("router", {"strategy": "balanced", "weights": {"quality": 0.4, "cost": 0.3, "speed": 0.2, "availability": 0.1}, "role_aware": True})
        if not self.get_setting("agents"):
            self.set_setting("agents", {
                "enabled": True,
                "synthesize": True,
                "roles": [
                    {"id": "code", "name": "Code", "provider_id": "auto", "instruction": "Act as the implementation engineer. Propose concrete code changes, edge cases, and tests."},
                    {"id": "architecture", "name": "Architecture", "provider_id": "auto", "instruction": "Act as the software architect. Evaluate design, trade-offs, boundaries, and long-term impact."},
                    {"id": "docs", "name": "Docs", "provider_id": "auto", "instruction": "Act as the technical writer. Produce clear documentation, summaries, and onboarding notes."},
                    {"id": "review", "name": "Review", "provider_id": "auto", "instruction": "Act as the reviewer. Find correctness risks, security issues, missing tests, and follow-up tasks."},
                ],
            })
        if not self.get_setting("mcp_servers"):
            self.set_setting("mcp_servers", {"servers": _default_mcp_servers()})
        if not self.get_setting("code_intel"):
            self.set_setting("code_intel", {"servers": _default_code_intel_servers()})
        if not self.get_setting("workspace"):
            self.set_setting("workspace", {"current_project_id": "architectos"})
        self._ensure_workspace_project_setting()

    def _upgrade_memory_lifecycle_defaults(self) -> None:
        life = dict(self.get_setting("memory_lifecycle") or {})
        changed = False
        defaults = {
            "auto_rescan_on_startup": True,
            "auto_rescan_limit": 24,
            "auto_rescan_all_projects": True,
            "chat_memory_mode": "strict",
            "chat_candidate_ttl_days": 7,
            "chat_session_idle_minutes": 30,
            "chat_store_facts_only": True,
        }
        for key, value in defaults.items():
            if key not in life:
                life[key] = value
                changed = True
        if changed:
            self.set_setting("memory_lifecycle", life)

    def _delete_seed_memory(self) -> None:
        seed_ids = [node.id for node in self.list_nodes() if dict(node.metadata or {}).get("seed")]
        if not seed_ids:
            return
        placeholders = ", ".join("?" for _ in seed_ids)
        with self._connect() as conn:
            conn.execute(f"DELETE FROM memory_edges WHERE source IN ({placeholders}) OR target IN ({placeholders})", [*seed_ids, *seed_ids])
            conn.execute(f"DELETE FROM memory_nodes WHERE id IN ({placeholders})", seed_ids)
            try:
                conn.execute(f"DELETE FROM memory_nodes_fts WHERE node_id IN ({placeholders})", seed_ids)
            except sqlite3.Error as exc:
                _LOG.debug("seed cleanup skipped for memory_nodes_fts: %s", exc)
            try:
                conn.execute(f"DELETE FROM memory_embeddings WHERE node_id IN ({placeholders})", seed_ids)
            except sqlite3.Error as exc:
                _LOG.debug("seed cleanup skipped for memory_embeddings: %s", exc)

    def _delete_empty_non_system_projects(self) -> None:
        stale_ids = [project.id for project in self.list_projects() if project.id != "architectos" and not project.root_path]
        if not stale_ids:
            return
        placeholders = ", ".join("?" for _ in stale_ids)
        with self._connect() as conn:
            conn.execute(f"DELETE FROM projects WHERE id IN ({placeholders})", stale_ids)

    def _ensure_workspace_project_setting(self) -> None:
        projects = self.list_projects()
        project_ids = {project.id for project in projects}
        workspace = dict(self.get_setting("workspace") or {})
        current_project_id = str(workspace.get("current_project_id") or "")
        if current_project_id in project_ids:
            return
        preferred = next((project.id for project in projects if project.id != "architectos" and project.root_path), "architectos")
        workspace["current_project_id"] = preferred
        self.set_setting("workspace", workspace)

    def _repair_missing_project_roots(self) -> None:
        workspace = dict(self.get_setting("workspace") or {})
        current_project_id = str(workspace.get("current_project_id") or "")
        workspace_changed = False
        for project in self.list_projects():
            if project.id == "architectos":
                continue
            root = str(project.root_path or "").strip()
            if not root:
                continue
            try:
                valid = Path(root).expanduser().is_dir()
            except OSError:
                valid = False
            if valid:
                continue
            project.root_path = ""
            self.upsert_project(project)
            if current_project_id == project.id:
                workspace["current_project_id"] = "architectos"
                workspace_changed = True
        if workspace_changed:
            self.set_setting("workspace", workspace)

    def _ensure_onboarding_flag(self) -> None:
        ui = dict(self.get_setting("ui") or {})
        if "onboarding_complete" in ui:
            return
        projects = self.list_projects()
        has_real_project = any(project.id != "architectos" and project.root_path for project in projects)
        ui["onboarding_complete"] = has_real_project
        self.set_setting("ui", ui)

    def _upgrade_mcp_server_defaults(self) -> None:
        payload = self.get_setting("mcp_servers") or {}
        servers = list(payload.get("servers") or [])
        changed = False
        org, project = self._discover_azure_devops_defaults()
        ado_ready = self._azure_devops_credentials_present()
        for server in servers:
            if server.get("id") == "granola" and server.get("command") == ["npx", "-y", "mcp-granola"]:
                server.update(_granola_remote_mcp_server())
                changed = True
            if server.get("id") == "azure-devops" and self._azure_devops_needs_upgrade(server):
                preserved = {
                    "enabled": bool(server.get("enabled")),
                    "status": str(server.get("status") or "planned"),
                }
                server.clear()
                server.update(_azure_devops_mcp_server(org or "$ADO_ORG", project))
                server.update(preserved)
                changed = True
            if server.get("id") == "azure-devops-git" and self._azure_devops_needs_upgrade(server):
                preserved = {
                    "enabled": bool(server.get("enabled")),
                    "status": str(server.get("status") or "planned"),
                }
                server.clear()
                server.update(_azure_devops_git_mcp_server(org or "$ADO_ORG", project))
                server.update(preserved)
                changed = True
            if ado_ready and server.get("id") in {"azure-devops", "azure-devops-git"} and not server.get("enabled"):
                server["enabled"] = True
                if str(server.get("status") or "") in {"", "planned", "disabled"}:
                    server["status"] = "configured"
                changed = True
            if ado_ready and server.get("id") in {"azure-devops", "azure-devops-git"}:
                env = dict(server.get("env") or {})
                if project and not str(env.get("ado_mcp_project") or "").strip():
                    env["ado_mcp_project"] = project
                    server["env"] = env
                    changed = True
            if server.get("id") == "filesystem":
                command = [str(part) for part in (server.get("command") or [])]
                package = "@modelcontextprotocol/server-filesystem"
                if package not in " ".join(command):
                    server["command"] = ["npx", "-y", package, "."]
                    changed = True
                elif command and command[-1] != "." and not Path(command[-1]).is_absolute():
                    server["command"] = [*command[:-1], "."]
                    changed = True
                elif not command:
                    server["command"] = ["npx", "-y", package, "."]
                    changed = True
                if not server.get("enabled"):
                    server["enabled"] = True
                    if str(server.get("status") or "") in {"", "planned", "disabled"}:
                        server["status"] = "configured"
                    changed = True
                notes = str(server.get("notes") or "")
                if "Agent tools:" not in notes:
                    server["notes"] = (
                        "Read/write files within the active project folder via MCP. "
                        "Agent tools: fs_read / fs_list / fs_search / fs_write (writes need approval)."
                    )
                    changed = True
        if not any(str(item.get("id")) == "azure-devops-git" for item in servers):
            insert_at = next((index + 1 for index, item in enumerate(servers) if item.get("id") == "azure-devops"), len(servers))
            git_server = _azure_devops_git_mcp_server(org or "$ADO_ORG", project)
            if ado_ready:
                git_server["enabled"] = True
                git_server["status"] = "configured"
            servers.insert(insert_at, git_server)
            changed = True
        if changed:
            self.set_setting("mcp_servers", {"servers": servers})

    @staticmethod
    def _azure_devops_credentials_present() -> bool:
        org = str(os.environ.get("ADO_ORG") or os.environ.get("AZURE_DEVOPS_ORG") or "").strip()
        token = str(os.environ.get("ADO_MCP_AUTH_TOKEN") or os.environ.get("PERSONAL_ACCESS_TOKEN") or "").strip()
        return bool(org and token)

    def _discover_azure_devops_defaults(self) -> tuple[str, str]:
        from .mcp import detect_azure_devops_from_git

        candidates: list[Path] = [self.project_root]
        for project in self.list_projects():
            root = str(project.root_path or "").strip()
            if root:
                candidates.append(Path(root).expanduser())
        for candidate_dir in candidates:
            detected = detect_azure_devops_from_git(candidate_dir)
            org = str(detected.get("org") or "").strip()
            if org:
                return org, str(detected.get("project") or "").strip()
        return "", ""

    @staticmethod
    def _azure_devops_needs_upgrade(server: dict[str, Any]) -> bool:
        transport = str(server.get("transport") or "").strip().lower()
        command = [str(part) for part in (server.get("command") or [])]
        joined = " ".join(command)
        if transport in {"remote http", "remote-http", "http", "remote", "streamable-http"} and not server.get("url"):
            return True
        if "@azure-devops/mcp" not in joined:
            return True
        if "--authentication" not in joined and "-a" not in joined:
            return True
        package_idx = next((i for i, part in enumerate(command) if "@azure-devops/mcp" in part), -1)
        if package_idx < 0:
            return True
        next_part = command[package_idx + 1] if package_idx + 1 < len(command) else ""
        if not next_part or next_part.startswith("-"):
            return True
        return False

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
            confidence=max(0.0, min(1.0, confidence)),
            metadata=dict(metadata or {}),
            created_at=now,
            updated_at=now,
        )
        if redacted or label_redacted:
            node.metadata["redacted"] = True
        node.evidence = [self._write_evidence(node)]
        return self.upsert_node(node)

    def upsert_node(self, node: MemoryNode, *, notify: bool = True) -> MemoryNode:
        node.updated_at = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO memory_nodes(id, type, label, scope, project_id, interface_id, status, confidence, created_at, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (node.id, node.type, node.label, node.scope, node.project_id, node.interface_id, node.status, node.confidence, node.created_at, node.updated_at, json.dumps(node.to_dict(), sort_keys=True)),
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

    def list_nodes(self, limit: int | None = None, project_id: str | None = None) -> list[MemoryNode]:
        sql = "SELECT payload FROM memory_nodes"
        params: list[Any] = []
        if project_id:
            sql += " WHERE project_id = ?"
            params.append(project_id)
        # Timestamps have second resolution, so ties are common; rowid keeps the
        # order deterministic (insertion order) regardless of the query plan.
        sql += " ORDER BY updated_at DESC, created_at DESC, rowid ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
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


    def upsert_memory_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        candidate = dict(candidate)
        clean_label, label_redacted = sanitize_text(str(candidate.get("label") or "Memory candidate"))
        clean_text, text_redacted = sanitize_text(str(candidate.get("text") or ""))
        clean_source, source_redacted = sanitize_text(str(candidate.get("source_ref") or ""))
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

    def upsert_task(self, task: dict[str, Any]) -> dict[str, Any]:
        task, redacted = sanitize_payload(dict(task))
        if redacted:
            task["security_redacted"] = True
        task.setdefault("id", stable_id("task", task.get("project_id", "architectos"), task.get("title", ""), utc_now()))
        task.setdefault("project_id", "architectos")
        task.setdefault("status", "todo")
        task.setdefault("priority", "medium")
        task.setdefault("detail", "")
        task.setdefault("linked_memory_ids", [])
        task.setdefault("created_at", utc_now())
        task["updated_at"] = utc_now()
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO tasks(id, project_id, status, priority, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?)", (task["id"], task["project_id"], task["status"], task["priority"], task["updated_at"], json.dumps(task, sort_keys=True)))
        return task

    def list_tasks(self, project_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT payload FROM tasks"
        params: tuple[str, ...] = ()
        if project_id:
            sql += " WHERE project_id = ?"
            params = (project_id,)
        sql += " ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def upsert_chat(self, chat: dict[str, Any]) -> dict[str, Any]:
        chat, redacted = sanitize_payload(dict(chat))
        if redacted:
            chat["security_redacted"] = True
        chat.setdefault("id", stable_id("chat", chat.get("project_id", "architectos"), chat.get("title", ""), utc_now()))
        chat.setdefault("project_id", "architectos")
        chat.setdefault("title", "New dialog")
        chat.setdefault("messages", [])
        chat.setdefault("favorite", False)
        chat.setdefault("created_at", utc_now())
        chat["updated_at"] = utc_now()
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO chat_sessions(id, project_id, title, favorite, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?)", (chat["id"], chat["project_id"], chat["title"], int(chat["favorite"]), chat["updated_at"], json.dumps(chat, sort_keys=True)))
        return chat

    def list_chats(self, project_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT payload FROM chat_sessions"
        params: tuple[str, ...] = ()
        if project_id:
            sql += " WHERE project_id = ?"
            params = (project_id,)
        sql += " ORDER BY favorite DESC, updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def get_chat(self, chat_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM chat_sessions WHERE id = ?", (chat_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def upsert_chat_context_summary(self, summary: dict[str, Any]) -> dict[str, Any]:
        summary, redacted = sanitize_payload(dict(summary))
        if redacted:
            summary["security_redacted"] = True
        summary.setdefault("id", stable_id("chat-summary", summary.get("chat_id", ""), summary.get("summary_type", "")))
        summary.setdefault("project_id", "architectos")
        summary.setdefault("summary_type", "rolling")
        summary["updated_at"] = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO chat_context_summaries(id, chat_id, project_id, summary_type, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (summary["id"], summary["chat_id"], summary["project_id"], summary["summary_type"], summary["updated_at"], json.dumps(summary, sort_keys=True)),
            )
        return summary

    def list_chat_context_summaries(self, chat_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM chat_context_summaries WHERE chat_id = ? ORDER BY summary_type",
                (chat_id,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def get_chat_context_summary(self, chat_id: str, summary_type: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM chat_context_summaries WHERE chat_id = ? AND summary_type = ?",
                (chat_id, summary_type),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def add_keeper_event(self, event: dict[str, Any]) -> dict[str, Any]:
        event, redacted = sanitize_payload(dict(event))
        if redacted:
            event["security_redacted"] = True
        event.setdefault("id", stable_id("keeper-event", event.get("project_id", "architectos"), event.get("chat_id", ""), event.get("kind", ""), utc_now()))
        event.setdefault("project_id", "architectos")
        event.setdefault("chat_id", "")
        event.setdefault("status", "info")
        event.setdefault("created_at", utc_now())
        event["updated_at"] = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO keeper_events(id, project_id, chat_id, status, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (event["id"], event["project_id"], event["chat_id"], event["status"], event["updated_at"], json.dumps(event, sort_keys=True)),
            )
        return event

    def list_keeper_events(self, project_id: str | None = None, chat_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT payload FROM keeper_events"
        params: list[Any] = []
        clauses = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if chat_id:
            clauses.append("chat_id = ?")
            params.append(chat_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def upsert_provider(self, provider: dict[str, Any]) -> dict[str, Any]:
        provider, redacted = sanitize_payload(dict(provider))
        if redacted:
            provider["security_redacted"] = True
        provider.setdefault("id", stable_id("provider", provider.get("label", "provider")))
        provider.setdefault("label", provider["id"])
        provider.setdefault("provider_type", "custom")
        provider.setdefault("status", "planned")
        provider.setdefault("enabled", False)
        provider.setdefault("model", "")
        provider.setdefault("notes", "")
        provider.setdefault("command", _default_provider_command(str(provider.get("id", ""))))
        provider.setdefault("timeout_seconds", 120)
        provider.setdefault("approval_required", provider.get("provider_type") == "cli")
        provider.setdefault("workdir_policy", "project-root")
        provider.setdefault("workdir", "")
        provider["updated_at"] = utc_now()
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO providers(id, label, enabled, status, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?)", (provider["id"], provider["label"], int(provider["enabled"]), provider["status"], provider["updated_at"], json.dumps(provider, sort_keys=True)))
        return provider

    def list_providers(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM providers ORDER BY enabled DESC, label").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def upsert_provider_run(self, run: dict[str, Any]) -> dict[str, Any]:
        run, redacted = sanitize_payload(dict(run))
        if redacted:
            run["security_redacted"] = True
        run.setdefault("id", stable_id("run", run.get("project_id", "architectos"), run.get("provider_id", ""), utc_now()))
        run.setdefault("project_id", "architectos")
        run.setdefault("provider_id", "")
        run.setdefault("status", "running")
        run.setdefault("started_at", utc_now())
        run["updated_at"] = utc_now()
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO provider_runs(id, project_id, provider_id, status, started_at, updated_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?)", (run["id"], run["project_id"], run["provider_id"], run["status"], run["started_at"], run["updated_at"], json.dumps(run, sort_keys=True)))
        return run

    def list_provider_runs(self, project_id: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        sql = "SELECT payload FROM provider_runs"
        params: list[Any] = []
        if project_id:
            sql += " WHERE project_id = ?"
            params.append(project_id)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def set_setting(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        clean_payload, redacted = sanitize_payload(dict(payload))
        if redacted:
            clean_payload["security_redacted"] = True
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO settings(key, payload, updated_at) VALUES (?, ?, ?)", (key, json.dumps(clean_payload, sort_keys=True), utc_now()))
        return clean_payload

    def get_setting(self, key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def all_settings(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, payload FROM settings ORDER BY key").fetchall()
        return {row["key"]: json.loads(row["payload"]) for row in rows}

    def analytics(self, project_id: str | None = None) -> dict[str, Any]:
        nodes = [node for node in self.list_nodes() if not project_id or node.project_id in {project_id, None}]
        by_scope: dict[str, int] = {}
        by_type: dict[str, int] = {}
        favorites = 0
        for node in nodes:
            by_scope[node.scope] = by_scope.get(node.scope, 0) + 1
            by_type[node.type] = by_type.get(node.type, 0) + 1
            favorites += 1 if node.metadata.get("favorite") else 0
        tasks = self.list_tasks(project_id)
        task_status: dict[str, int] = {}
        for task in tasks:
            task_status[task["status"]] = task_status.get(task["status"], 0) + 1
        return {"nodes": len(nodes), "edges": len(self.list_edges()), "candidates": len(self.list_memory_candidates(project_id, status="candidate", limit=500)), "tasks": len(tasks), "chats": len(self.list_chats(project_id)), "favorites": favorites, "providers": len(self.list_providers()), "by_scope": by_scope, "by_type": by_type, "task_status": task_status, "database": str(self.db_path)}

    def export_bundle(self, project_id: str) -> dict[str, Any]:
        nodes = [node.to_dict() for node in self.list_nodes() if node.project_id in {project_id, None}]
        node_ids = {node["id"] for node in nodes}
        project = cast(Project, self.get_project(project_id) or self.get_project("architectos"))
        bundle = {"format": "architectos.bundle", "version": "0.2", "exported_at": utc_now(), "project": project.to_dict(), "memory_nodes": nodes, "memory_edges": [edge.to_dict() for edge in self.list_edges() if edge.source in node_ids and edge.target in node_ids], "memory_candidates": self.list_memory_candidates(project_id, status=None, limit=500), "tasks": self.list_tasks(project_id), "chats": self.list_chats(project_id), "providers": self.list_providers(), "settings": strip_sensitive_settings(self.all_settings())}
        clean_bundle, _redacted = sanitize_payload(bundle)
        return clean_bundle

    def import_bundle(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("format") != "architectos.bundle":
            raise ValueError("unsupported bundle format")
        project_payload = payload.get("project") or {}
        project = self._project_from_payload(project_payload)
        # One transaction: a mid-import failure must not leave a half-restored bundle.
        with self.transaction():
            self.upsert_project(project)
            for node_payload in payload.get("memory_nodes") or []:
                self.upsert_node(MemoryNode.from_dict(node_payload))
            for edge_payload in payload.get("memory_edges") or []:
                edge = MemoryEdge.from_dict(edge_payload)
                self.add_edge(edge.source, edge.target, edge.type, edge.scope, edge.confidence)
            for task in payload.get("tasks") or []:
                self.upsert_task(task)
            for candidate in payload.get("memory_candidates") or []:
                self.upsert_memory_candidate(candidate)
            for chat in payload.get("chats") or []:
                self.upsert_chat(chat)
            for provider in payload.get("providers") or []:
                self.upsert_provider(provider)
            for key, value in dict(payload.get("settings") or {}).items():
                self.set_setting(key, value)
        return {"project_id": project.id, "nodes": len(payload.get("memory_nodes") or []), "tasks": len(payload.get("tasks") or [])}

    def _write_evidence(self, node: MemoryNode) -> str:
        stamp = utc_now().replace(":", "").replace("-", "")
        safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "_", node.label.lower()).strip("_")[:40] or "memory"
        relative = Path("evidence") / f"{stamp}__{safe_label}.md"
        path = self.memory_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Evidence: {node.label}\n\nType: {node.type}\nScope: {node.scope}\nCreated: {node.created_at}\n\n{node.text}\n", encoding="utf-8")
        return relative.as_posix()


def _migration_001_memory_nodes_updated_at_index(repository: SQLiteMemoryRepository, conn: sqlite3.Connection) -> None:
    """Index memory_nodes.updated_at for the hot list_nodes ORDER BY path.

    memory_edges gets no matching index: list_edges orders by created_at, so an
    updated_at index would never be used there.
    """
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_nodes_updated_at ON memory_nodes(updated_at)")


def _migration_002_memory_lifecycle_defaults(repository: SQLiteMemoryRepository, conn: sqlite3.Connection) -> None:
    """Backfill memory_lifecycle keys added after the initial schema."""
    if repository.get_setting("memory_lifecycle") is None:
        # Fresh database: seed_if_empty writes the full defaults later.
        return
    repository._upgrade_memory_lifecycle_defaults()


def _migration_003_mcp_server_defaults(repository: SQLiteMemoryRepository, conn: sqlite3.Connection) -> None:
    """Refresh bundled MCP server entries (remote Granola, Azure DevOps git)."""
    if repository.get_setting("mcp_servers") is None:
        # Fresh database: seed_if_empty writes the full defaults later.
        return
    repository._upgrade_mcp_server_defaults()


# Ordered (version, fn) steps; _migrate applies every step above the database's
# PRAGMA user_version. Keep ids monotonically increasing and every step idempotent.
MIGRATIONS: list[tuple[int, Callable[[SQLiteMemoryRepository, sqlite3.Connection], None]]] = [
    (1, _migration_001_memory_nodes_updated_at_index),
    (2, _migration_002_memory_lifecycle_defaults),
    (3, _migration_003_mcp_server_defaults),
]


def _default_mcp_servers() -> list[dict[str, Any]]:
    return [
        {"id": "filesystem", "label": "Filesystem", "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "."], "enabled": True, "approval_required": True, "status": "configured", "transport": "stdio", "env": {}, "notes": "Read/write files within the active project folder via MCP. Agent tools: fs_read / fs_list / fs_search / fs_write (writes need approval)."},
        {"id": "github", "label": "GitHub", "command": ["npx", "-y", "@modelcontextprotocol/server-github"], "enabled": False, "approval_required": True, "status": "planned", "transport": "stdio", "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": ""}, "notes": "Issues, PRs, and repository access. Requires GITHUB_PERSONAL_ACCESS_TOKEN."},
        _azure_devops_mcp_server(),
        _azure_devops_git_mcp_server(),
        {"id": "jira", "label": "Jira", "command": ["npx", "-y", "mcp-jira"], "enabled": False, "approval_required": True, "status": "planned", "transport": "stdio", "env": {}, "notes": "Jira issues and boards."},
        {"id": "slack", "label": "Slack", "command": ["npx", "-y", "@modelcontextprotocol/server-slack"], "enabled": False, "approval_required": True, "status": "planned", "transport": "stdio", "env": {}, "notes": "Slack channels and messages."},
        {"id": "confluence", "label": "Confluence", "command": ["npx", "-y", "mcp-confluence"], "enabled": False, "approval_required": True, "status": "planned", "transport": "stdio", "env": {}, "notes": "Confluence pages and spaces."},
        _granola_remote_mcp_server(),
    ]


def _azure_devops_mcp_server(org: str = "$ADO_ORG", project: str = "") -> dict[str, Any]:
    env: dict[str, str] = {}
    if project:
        env["ado_mcp_project"] = project
    return {
        "id": "azure-devops",
        "label": "Azure DevOps",
        "command": ["npx", "-y", "@azure-devops/mcp", org, "--authentication", "envvar"],
        "enabled": False,
        "approval_required": True,
        "status": "planned",
        "transport": "stdio",
        "url": "",
        "headers": {},
        "env": env,
        "notes": "Work items, wiki, repos, and pipelines via @azure-devops/mcp. Set ADO_ORG and ADO_MCP_AUTH_TOKEN (PAT) in .env. AutoScan: Azure Boards / Wiki / Git.",
    }


def _azure_devops_git_mcp_server(org: str = "$ADO_ORG", project: str = "") -> dict[str, Any]:
    env: dict[str, str] = {}
    if project:
        env["ado_mcp_project"] = project
    return {
        "id": "azure-devops-git",
        "label": "Azure DevOps Git",
        "command": ["npx", "-y", "@azure-devops/mcp", org, "--authentication", "envvar", "-d", "core", "repositories"],
        "enabled": False,
        "approval_required": True,
        "status": "planned",
        "transport": "stdio",
        "url": "",
        "headers": {},
        "env": env,
        "notes": "Repos and pull requests only (domains: core, repositories). Set ADO_ORG and ADO_MCP_AUTH_TOKEN in .env. AutoScan: Azure Git.",
    }


def _granola_remote_mcp_server() -> dict[str, Any]:
    return {
        "id": "granola",
        "label": "Granola",
        "command": [],
        "url": "https://mcp.granola.ai/mcp",
        "enabled": False,
        "approval_required": True,
        "status": "planned",
        "transport": "http",
        "headers": {},
        "env": {},
        "notes": "Official remote Granola MCP. Enable after completing Granola authorization.",
    }


def _default_code_intel_servers() -> list[dict[str, Any]]:
    return [
        {"id": "python", "label": "Python", "language_id": "python", "extensions": [".py"], "command": ["pyright-langserver", "--stdio"], "enabled": True, "status": "planned", "notes": "Requires pyright (npm).", "install_command": "npm install -g pyright"},
        {"id": "typescript", "label": "TypeScript", "language_id": "typescript", "extensions": [".ts", ".tsx", ".js", ".jsx"], "command": ["typescript-language-server", "--stdio"], "enabled": True, "status": "planned", "notes": "Requires typescript-language-server (npm).", "install_command": "npm install -g typescript-language-server typescript"},
        {"id": "go", "label": "Go", "language_id": "go", "extensions": [".go"], "command": ["gopls"], "enabled": True, "status": "planned", "notes": "Requires gopls on PATH.", "install_command": "go install golang.org/x/tools/gopls@latest"},
        {"id": "rust", "label": "Rust", "language_id": "rust", "extensions": [".rs"], "command": ["rust-analyzer"], "enabled": True, "status": "planned", "notes": "Requires rust-analyzer via rustup.", "install_command": "rustup component add rust-analyzer"},
        {"id": "java", "label": "Java", "language_id": "java", "extensions": [".java"], "command": ["jdtls"], "enabled": True, "status": "planned", "notes": "Requires Eclipse JDT language server (jdtls).", "install_command": ""},
    ]


def _default_provider_command(provider_id: str) -> list[str]:
    if provider_id == "codex-cli":
        return ["codex", "exec", "--skip-git-repo-check", "-"]
    if provider_id == "claude-code":
        return ["claude", "--print"]
    if provider_id == "gemini-cli":
        return ["agy", "-p"]
    return []
