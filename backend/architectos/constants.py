"""Shared module-level constants for ArchitectOS.

Extracted from ``service.py`` so service mixins (ingestion, fs-tools, ...) can
import them without importing the service module itself (which would be a
circular dependency). Pure data + one env-int helper; no service logic.
"""

from __future__ import annotations

import os
import re

TEXT_EXTENSIONS = {".md", ".txt", ".rst", ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".kt", ".kts", ".go", ".rs", ".c", ".h", ".cpp", ".cc", ".hpp", ".cs", ".rb", ".php", ".swift", ".scala", ".sql", ".css", ".scss", ".sass", ".less", ".html", ".htm", ".vue", ".svelte", ".xml", ".gradle", ".groovy", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".toml", ".ini", ".properties", ".json", ".yml", ".yaml", ".tf", ".proto", ".dart"}
DOC_EXTENSIONS = {".md", ".txt", ".rst", ".adoc", ".org"}
CODE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".kt", ".kts", ".go", ".rs",
    ".c", ".h", ".cpp", ".cc", ".hpp", ".cs", ".rb", ".php", ".swift", ".scala", ".sql",
    ".css", ".scss", ".sass", ".less", ".html", ".htm", ".vue", ".svelte", ".xml",
    ".gradle", ".groovy", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".toml", ".ini", ".properties", ".json", ".yml", ".yaml", ".tf", ".proto", ".dart",
}
DEFAULT_EXCLUDES = {
    ".git", "__pycache__", "node_modules", ".pytest_cache", "data", "memory",
    "target", "dist", "build", "out", ".idea", ".vscode", "vendor", ".gradle",
    "coverage", ".next", ".turbo", "bin", "obj",
    # Tooling scratch/artifact dirs that should never become memory nodes.
    ".playwright-mcp", ".playwright", "playwright-report", "test-results",
    ".cache", ".venv", "venv", ".mypy_cache", ".ruff_cache", ".terraform",
    ".gradle-cache", ".egg-info",
}
# Files that are pure build/runtime artifacts: text-like enough to slip past the
# binary sniff, but noise as memory (logs, lockfiles, sourcemaps, temp files).
NOISE_FILE_SUFFIXES = frozenset({
    ".log", ".lock", ".tmp", ".temp", ".pid", ".map", ".bak", ".swp", ".swo",
    ".ipynb_checkpoints",
})
NOISE_FILE_NAMES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "cargo.lock", "composer.lock", "gemfile.lock", "podfile.lock",
    ".ds_store", "thumbs.db", "npm-debug.log", "yarn-error.log",
})
TEXT_PREVIEW_BYTES = 200_000
TEXT_SAMPLE_BYTES = 8_192
FS_READ_MAX_LINES = 400
FS_READ_MAX_CHARS = 14_000
SOURCE_FILE_SUFFIXES = frozenset({
    ".java", ".kt", ".kts", ".scala", ".groovy", ".cs", ".ts", ".tsx", ".js", ".jsx", ".mjs",
    ".py", ".go", ".rs", ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".hpp", ".sql", ".xml",
    ".json", ".yaml", ".yml", ".properties", ".md", ".txt", ".html", ".css", ".scss", ".sh",
})
CODE_FILE_MAX_BYTES = 320_000
GENERIC_FILE_MAX_BYTES = 160_000
SYSTEM_PROJECT_ID = "architectos"
TERMINAL_SHELLS = {
    "powershell": ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"],
    "cmd": ["cmd.exe", "/d", "/c"],
    "bash": ["bash", "-lc"],
    "sh": ["sh", "-lc"],
}
TERMINAL_DANGEROUS_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\b(remove-item|rm|del|erase|rmdir|rd)\b",
        r"\b(format|diskpart|shutdown|restart-computer|stop-computer|taskkill|stop-process)\b",
        r"\bgit\s+(reset\s+--hard|clean)\b",
        r"\bmkfs(?:\.\w+)?\b",
        # Exfiltration / remote code execution / privilege changes.
        r"\b(curl|wget|fetch)\b.*\|\s*(sh|bash|zsh|powershell|cmd)\b",
        r"\b(curl|wget)\b.*\s-o\s",
        r"\bpython(?:3)?\s+-c\b",
        r"\bnode\s+-e\b",
        r"\bperl\s+-e\b",
        r"\bruby\s+-e\b",
        r"\bchmod\b",
        r"\bchown\b",
        r"\bdd\b",
        r">\s*/(etc|dev|sys|proc)/",
        r"\bsudo\b",
        r"\bpowershell\b.*\b(-enc|-encodedcommand|iex|invoke-expression)\b",
        r"`[^`]+`|\$\([^)]+\)",  # command substitution
    ]
]
# Client must echo this exact string to override a blocked command (UI checkbox alone is not enough).
TERMINAL_DESTRUCTIVE_CONFIRM = "I_UNDERSTAND_DESTRUCTIVE"
from .source_registry import INGESTION_SOURCE_ALIASES  # noqa: E402

ALL_LOCAL_INGESTION_SOURCES = ["docs", "code", "chat", "git_history", "adr", "issues", "pull_requests", "meetings", "inbox"]
ALL_INGESTION_SOURCES = [*ALL_LOCAL_INGESTION_SOURCES, "granola", "azure-boards", "azure-wiki", "azure-git", "github", "gitlab"]
ADO_BOARD_WORK_ITEM_TYPES = ["Requirement", "Feature", "User Story", "Task", "Bug", "Epic", "Product Backlog Item"]


def _ado_env_int(name: str, default: int, minimum: int = 1, maximum: int = 100000) -> int:
    try:
        value = int(str(os.environ.get(name) or "").strip() or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


# Hub work items (Epics/Features) can carry hundreds of relations. Fetching and
# linking every one blocks the import, so we bound how many we keep/link per item.
ADO_MAX_RELATIONS = _ado_env_int("ADO_MAX_RELATIONS", 120, minimum=1, maximum=5000)
# Azure DevOps MCP is a single stdio process. Parallel fetches pile up behind one
# hung hub item and look like a full freeze — keep default sequential.
ADO_FETCH_WORKERS = _ado_env_int("ADO_FETCH_WORKERS", 1, minimum=1, maximum=8)
# Per-work-item MCP timeout. Hub items can stall the Node MCP server; skip and
# restart the session so the rest of the import keeps moving.
ADO_ITEM_TIMEOUT = _ado_env_int("ADO_ITEM_TIMEOUT", 25, minimum=5, maximum=600)
# Whole Azure Boards source budget (collect IDs + fetch details).
ADO_SOURCE_TIMEOUT = _ado_env_int("ADO_SOURCE_TIMEOUT", 90, minimum=15, maximum=7200)

# Per-source ingest budgets. Payload can override via timeouts / item_timeout / source_timeout.
DEFAULT_INGEST_TIMEOUTS: dict[str, dict[str, int]] = {
    "files": {"item": 10, "source": 180},
    "chat": {"item": 10, "source": 120},
    "git": {"item": 10, "source": 60},
    "inbox": {"item": 10, "source": 120},
    "granola": {"item": 30, "source": 240},
    "azure-boards": {"item": ADO_ITEM_TIMEOUT, "source": ADO_SOURCE_TIMEOUT},
    "azure-git": {"item": 30, "source": 180},
    "azure-wiki": {"item": 20, "source": 300},
    "github": {"item": 25, "source": 180},
    "gitlab": {"item": 25, "source": 180},
}
ADO_BOARD_MEMORY_TYPES = {
    "requirement": "Requirement",
    "feature": "Feature",
    "epic": "Feature",
    "user story": "Requirement",
    "product backlog item": "Requirement",
    "task": "Artifact",
    "bug": "Constraint",
}

# Placeholder that replaces secret values (MCP env vars, HTTP headers) in API
# responses and bundle exports. UIs must treat it as "keep the stored value"
# when a server configuration round-trips through an editor.
SECRET_MASK = "********"
