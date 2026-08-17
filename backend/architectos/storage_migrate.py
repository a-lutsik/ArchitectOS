"""Schema migration, seed catalogs, and upgrade helpers for SQLiteMemoryRepository."""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from .mcp import detect_azure_devops_from_git
from .models import Project, stable_id, utc_now

SCHEMA_VERSION = 3

from .storage_defaults import (
    MIGRATIONS,
    _azure_devops_git_mcp_server,
    _azure_devops_mcp_server,
    _default_code_intel_servers,
    _default_mcp_servers,
    _default_provider_command,
    _granola_remote_mcp_server,
)

_LOG = logging.getLogger("architectos.storage")


class StorageMigrateMixin:
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
