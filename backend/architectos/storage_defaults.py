"""Schema migrations and bundled defaults for SQLiteMemoryRepository.

Split out of ``storage`` so seed catalogs (MCP/LSP/provider commands) and
versioned migrations stay next to each other without bloating the repository
class module. ``storage`` re-imports the public names it needs at runtime.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any


def _migration_001_memory_nodes_updated_at_index(repository: Any, conn: sqlite3.Connection) -> None:
    """Index memory_nodes.updated_at for the hot list_nodes ORDER BY path.

    memory_edges gets no matching index: list_edges orders by created_at, so an
    updated_at index would never be used there.
    """
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_nodes_updated_at ON memory_nodes(updated_at)")


def _migration_002_memory_lifecycle_defaults(repository: Any, conn: sqlite3.Connection) -> None:
    """Backfill memory_lifecycle keys added after the initial schema."""
    if repository.get_setting("memory_lifecycle") is None:
        # Fresh database: seed_if_empty writes the full defaults later.
        return
    repository._upgrade_memory_lifecycle_defaults()


def _migration_003_mcp_server_defaults(repository: Any, conn: sqlite3.Connection) -> None:
    """Refresh bundled MCP server entries (remote Granola, Azure DevOps git)."""
    if repository.get_setting("mcp_servers") is None:
        # Fresh database: seed_if_empty writes the full defaults later.
        return
    repository._upgrade_mcp_server_defaults()


def _migration_004_memory_node_source_id(repository: Any, conn: sqlite3.Connection) -> None:
    """Add memory_nodes.source_id (nullable) linking nodes to a sources row."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_nodes)")}
    if "source_id" not in columns:
        conn.execute("ALTER TABLE memory_nodes ADD COLUMN source_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_nodes_source ON memory_nodes(source_id, status)")


# Ordered (version, fn) steps; _migrate applies every step above the database's
# PRAGMA user_version. Keep ids monotonically increasing and every step idempotent.
# ``repository`` is the composed ``SQLiteMemoryRepository`` (via StorageMigrateMixin).
MIGRATIONS: list[tuple[int, Callable[[Any, sqlite3.Connection], None]]] = [
    (1, _migration_001_memory_nodes_updated_at_index),
    (2, _migration_002_memory_lifecycle_defaults),
    (3, _migration_003_mcp_server_defaults),
    (4, _migration_004_memory_node_source_id),
]


def _default_mcp_servers() -> list[dict[str, Any]]:
    return [
        {"id": "filesystem", "label": "Filesystem", "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "."], "enabled": True, "approval_required": True, "status": "configured", "transport": "stdio", "env": {}, "notes": "Read/write files within the active project folder via MCP. Agent tools: fs_read / fs_list / fs_search / fs_write (writes need approval)."},
        {"id": "github", "label": "GitHub", "command": ["npx", "-y", "@modelcontextprotocol/server-github"], "enabled": False, "approval_required": True, "status": "planned", "transport": "stdio", "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": ""}, "notes": "Issues, PRs, and repository access. Requires GITHUB_PERSONAL_ACCESS_TOKEN."},
        _gitlab_mcp_server(),
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


def _gitlab_mcp_server() -> dict[str, Any]:
    return {
        "id": "gitlab",
        "label": "GitLab",
        "command": ["npx", "-y", "@modelcontextprotocol/server-gitlab"],
        "enabled": False,
        "approval_required": True,
        "status": "planned",
        "transport": "stdio",
        "env": {"GITLAB_PERSONAL_ACCESS_TOKEN": "", "GITLAB_API_URL": "https://gitlab.com/api/v4"},
        "notes": "GitLab issues, merge requests, and wiki via MCP. Set GITLAB_PERSONAL_ACCESS_TOKEN.",
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
