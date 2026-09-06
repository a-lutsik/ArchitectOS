"""Source registry: kinds, slugs, legacy aliases, and default local bindings."""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

SOURCE_KINDS = frozenset({
    "docs", "code", "adr", "issues", "pull_requests", "meetings", "wiki",
    "git_history", "chat", "inbox",
})

SOURCE_DRIVERS = frozenset({"local_files", "local_git", "chat", "mcp", "http", "ftp"})

# Legacy ingest ids from pre-registry AutoScan (one release of aliases).
INGESTION_SOURCE_ALIASES: dict[str, str] = {
    "issue": "issues",
    "issues": "issues",
    "tracker": "issues",
    "trackers": "issues",
    "pr": "pull_requests",
    "prs": "pull_requests",
    "pull_request": "pull_requests",
    "pull_requests": "pull_requests",
    "meeting": "meetings",
    "meetings": "meetings",
    "commit": "git_history",
    "commits": "git_history",
    "git": "git_history",
    "granola": "meetings",
    "granola_meeting": "meetings",
    "granola_meetings": "meetings",
    "azure-boards": "issues",
    "azure_boards": "issues",
    "azureboards": "issues",
    "boards": "issues",
    "ado-boards": "issues",
    "ado_boards": "issues",
    "work-items": "issues",
    "work_items": "issues",
    "workitems": "issues",
    "azure-devops": "issues",
    "azure_devops": "issues",
    "azure-wiki": "wiki",
    "azure_wiki": "wiki",
    "azurewiki": "wiki",
    "ado-wiki": "wiki",
    "ado_wiki": "wiki",
    "wiki": "wiki",
    "azure-git": "pull_requests",
    "azure_git": "pull_requests",
    "azure-repos": "pull_requests",
    "azure_repos": "pull_requests",
    "ado-git": "pull_requests",
    "ado_git": "pull_requests",
    "ado-repos": "pull_requests",
    "ado_repos": "pull_requests",
    "azure-prs": "pull_requests",
    "azure_prs": "pull_requests",
    "github": "issues",
    "gitlab": "issues",
    "inbox": "inbox",
    "folder": "inbox",
    "drop_folder": "inbox",
    "drop-folder": "inbox",
    "watch_folder": "inbox",
    "watch-folder": "inbox",
}

# Map legacy ingest source id -> registry binding key suffix.
LEGACY_SOURCE_BINDING_KEYS: dict[str, str] = {
    "docs": "local:docs",
    "code": "local:code",
    "adr": "local:adr",
    "issues": "local:issues",
    "prs": "local:pull_requests",
    "pull_requests": "local:pull_requests",
    "meetings": "local:meetings",
    "chat": "local:chat",
    "git": "local:git_history",
    "git_history": "local:git_history",
    "inbox": "local:inbox",
    "granola": "mcp:granola:meetings",
    "azure-boards": "mcp:azure-devops:issues",
    "azure-wiki": "mcp:azure-devops:wiki",
    "azure-git": "mcp:azure-devops-git:pull_requests",
    "github": "mcp:github:issues",
    "gitlab": "mcp:gitlab:issues",
}

KIND_LABELS: dict[str, str] = {
    "docs": "docs",
    "code": "code",
    "adr": "ADRs",
    "issues": "issues",
    "pull_requests": "PRs",
    "meetings": "meetings",
    "wiki": "wiki",
    "git_history": "git",
    "chat": "Ask",
    "inbox": "inbox",
}

# Human titles for Settings / AutoScan (not graph origin slugs).
DEFAULT_SOURCE_TITLES: dict[tuple[str, str], str] = {
    ("chat", "chat"): "Ask",
    ("local_git", "git_history"): "Local Git",
    ("azure-boards", "issues"): "Azure Boards",
    ("azure-git", "pull_requests"): "Azure PRs",
    ("azure-wiki", "wiki"): "Azure Wiki",
    ("github", "issues"): "GitHub Issues",
    ("github", "pull_requests"): "GitHub PRs",
    ("gitlab", "issues"): "GitLab Issues",
    ("gitlab", "pull_requests"): "GitLab PRs",
    ("gitlab", "wiki"): "GitLab Wiki",
    ("granola", "meetings"): "Granola",
}

# Short blurbs shown under the name in Settings → Memory sources.
DEFAULT_SOURCE_DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("chat", "chat"): "Captures durable facts from Ask conversations into the review queue.",
    ("local_git", "git_history"): "Indexes commits from this project's local git history on disk.",
    ("azure-boards", "issues"): "Work items from Azure DevOps (Boards MCP).",
    ("azure-git", "pull_requests"): "Pull requests from Azure Repos (Git MCP).",
    ("azure-wiki", "wiki"): "Wiki pages from Azure DevOps (Wiki MCP).",
    ("github", "issues"): "Issues from linked GitHub repositories (GitHub MCP).",
    ("github", "pull_requests"): "Pull requests from linked GitHub repositories (GitHub MCP).",
    ("gitlab", "issues"): "Issues from linked GitLab projects (GitLab MCP).",
    ("gitlab", "pull_requests"): "Merge requests from linked GitLab projects (GitLab MCP).",
    ("gitlab", "wiki"): "Wiki pages from linked GitLab projects (GitLab MCP).",
    ("granola", "meetings"): "Meeting notes synced from Granola via MCP.",
}

# Older auto-generated names we may overwrite when applying new defaults.
LEGACY_AUTO_SOURCE_NAMES: frozenset[str] = frozenset({
    "ask", "ask Ask", "Ask",
    "git", "git git", "Local Git",
    "azure", "azure PRs", "azure wiki", "Azure Boards", "Azure PRs", "Azure Wiki",
    "github", "github PRs", "GitHub Issues", "GitHub PRs",
    "gitlab", "gitlab PRs", "gitlab wiki", "GitLab Issues", "GitLab PRs", "GitLab Wiki",
    "granola", "Granola",
})

PRESET_ADAPTER_SLUGS: dict[str, str] = {
    "github": "github",
    "gitlab": "gitlab",
    "granola": "granola",
    "azure-devops": "azure",
    "azure-devops-git": "azure",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _clean_slug(raw: str) -> str:
    text = str(raw or "").strip().lower()
    for prefix in ("mcp-", "mcp_", "mcp "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = _SLUG_RE.sub("-", text).strip("-")
    if text in {"", "mcp", "http", "https", "ftp", "sftp", "source"}:
        return ""
    return text


def host_label_from_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(str(url or "").strip())
    except ValueError:
        return ""
    host = (parsed.hostname or parsed.netloc or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    return host.split(".")[0]


def origin_slug_from_parts(
    *,
    driver: str = "",
    adapter: str = "",
    mcp_server_id: str = "",
    mcp_label: str = "",
    http_url: str = "",
    ftp_url: str = "",
    preset: str = "",
) -> str:
    if preset:
        slug = _clean_slug(PRESET_ADAPTER_SLUGS.get(preset, preset))
        if slug:
            return slug
    if driver == "mcp":
        for candidate in (mcp_server_id, mcp_label, adapter):
            slug = _clean_slug(candidate)
            if slug:
                return slug
    if driver == "http":
        slug = host_label_from_url(http_url)
        if slug:
            return slug
    if driver == "ftp":
        slug = host_label_from_url(ftp_url)
        if slug:
            return slug
    if driver == "local_files":
        return "repo"
    if driver == "local_git":
        return "git"
    if driver == "chat":
        return "ask"
    if driver == "inbox":
        return "inbox"
    return ""


def origin_slug(source: dict[str, Any]) -> str:
    config = dict(source.get("config") or {})
    return origin_slug_from_parts(
        driver=str(config.get("driver") or ""),
        adapter=str(config.get("adapter") or ""),
        mcp_server_id=str(config.get("mcp_server_id") or ""),
        mcp_label=str(config.get("mcp_label") or ""),
        http_url=str((config.get("http") or {}).get("url") or ""),
        ftp_url=str((config.get("ftp") or {}).get("url") or ""),
        preset=str(config.get("adapter") or config.get("mcp_server_id") or ""),
    )


def default_source_title(
    *,
    driver: str = "",
    adapter: str = "",
    kind: str = "",
    mcp_server_id: str = "",
) -> str:
    """Default Settings/AutoScan label for a binding."""
    kind = str(kind or "").strip()
    driver = str(driver or "").strip()
    adapter = str(adapter or "").strip()
    server = str(mcp_server_id or "").strip()
    for key in ((adapter, kind), (server, kind), (driver, kind)):
        title = DEFAULT_SOURCE_TITLES.get(key)
        if title:
            return title
    if driver == "local_files":
        return KIND_LABELS.get(kind, kind or "Local files")
    if driver == "chat":
        return "Ask"
    if driver == "local_git":
        return "Local Git"
    return ""


def default_source_title_for(source: dict[str, Any]) -> str:
    config = dict(source.get("config") or {})
    return default_source_title(
        driver=str(config.get("driver") or ""),
        adapter=str(config.get("adapter") or ""),
        kind=str(source.get("kind") or ""),
        mcp_server_id=str(config.get("mcp_server_id") or ""),
    )


def default_source_description(
    *,
    driver: str = "",
    adapter: str = "",
    kind: str = "",
    mcp_server_id: str = "",
) -> str:
    """Human blurb for Settings / AutoScan tooltips."""
    kind = str(kind or "").strip()
    driver = str(driver or "").strip()
    adapter = str(adapter or "").strip()
    server = str(mcp_server_id or "").strip()
    for key in ((adapter, kind), (server, kind), (driver, kind)):
        text = DEFAULT_SOURCE_DESCRIPTIONS.get(key)
        if text:
            return text
    if driver == "chat":
        return DEFAULT_SOURCE_DESCRIPTIONS[("chat", "chat")]
    if driver == "local_git":
        return DEFAULT_SOURCE_DESCRIPTIONS[("local_git", "git_history")]
    return ""


def default_source_description_for(source: dict[str, Any]) -> str:
    config = dict(source.get("config") or {})
    return default_source_description(
        driver=str(config.get("driver") or ""),
        adapter=str(config.get("adapter") or ""),
        kind=str(source.get("kind") or ""),
        mcp_server_id=str(config.get("mcp_server_id") or ""),
    )


def unique_source_name(base: str, existing: set[str], *, kind: str = "") -> str:
    """Pick a unique display name; keep human titles intact (spaces/case)."""
    name = str(base or "").strip() or "source"
    taken = {str(item or "").strip() for item in existing}
    taken_fold = {item.casefold() for item in taken if item}
    if name not in taken and name.casefold() not in taken_fold:
        return name
    # Prefer kind suffix only for machine-ish slugs, not for "GitHub Issues".
    slug = _clean_slug(name)
    if kind and (" " not in name) and slug == name.casefold().replace(" ", "-"):
        label = KIND_LABELS.get(kind, kind)
        with_kind = f"{name} {label}"
        if _clean_slug(label) != slug and with_kind not in taken and with_kind.casefold() not in taken_fold:
            return with_kind
    index = 2
    while True:
        candidate = f"{name} ({index})"
        if candidate not in taken and candidate.casefold() not in taken_fold:
            return candidate
        index += 1


def default_local_bindings(project_id: str) -> list[dict[str, Any]]:
    """Seed bindings for a new project (docs+adr on; rest off)."""
    from .models import stable_id, utc_now

    now = utc_now()
    specs = [
        ("docs", "local_files", True),
        ("adr", "local_files", True),
        ("code", "local_files", False),
        ("issues", "local_files", False),
        ("pull_requests", "local_files", False),
        ("meetings", "local_files", False),
        ("git_history", "local_git", False),
        ("chat", "chat", False),
        ("inbox", "local_files", False),
    ]
    bindings: list[dict[str, Any]] = []
    names: set[str] = set()
    for kind, driver, enabled in specs:
        base = default_source_title(driver=driver, kind=kind) or KIND_LABELS.get(kind, kind)
        name = unique_source_name(base, names, kind=kind)
        names.add(name)
        binding_key = f"local:{kind}"
        bindings.append({
            "id": stable_id("source", project_id, binding_key),
            "project_id": project_id,
            "name": name,
            "kind": kind,
            "config": {
                "driver": driver,
                "binding_key": binding_key,
                "origin_label": origin_slug_from_parts(driver=driver) or name,
                "name_customized": False,
                "enabled": enabled,
                "ingest_mode": "candidates",
                "schedule": {"enabled": enabled, "interval_minutes": 60, "on_startup": enabled},
                "timeouts": {"item": 25, "source": 180},
                "max_items": 0,
            },
            "created_at": now,
            "updated_at": now,
        })
    return bindings


def normalize_kind(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return INGESTION_SOURCE_ALIASES.get(raw, raw)


def effective_ingest_cap(limit: int, *, default: int = 10_000) -> int:
    """Map unlimited ingest (0) to a high internal cap for legacy adapters."""
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        parsed = 0
    return default if parsed <= 0 else parsed


def binding_config(source: dict[str, Any]) -> dict[str, Any]:
    return dict(source.get("config") or {})


def source_enabled(source: dict[str, Any]) -> bool:
    return bool(binding_config(source).get("enabled"))


def source_driver(source: dict[str, Any]) -> str:
    return str(binding_config(source).get("driver") or "")


def source_binding_key(source: dict[str, Any]) -> str:
    cfg = binding_config(source)
    if cfg.get("binding_key"):
        return str(cfg["binding_key"])
    kind = str(source.get("kind") or "")
    driver = str(cfg.get("driver") or "")
    if driver == "local_files" or driver == "local_git" or driver == "chat":
        return f"local:{kind}"
    if driver == "mcp":
        server = str(cfg.get("mcp_server_id") or cfg.get("adapter") or "mcp")
        return f"mcp:{server}:{kind}"
    if driver == "http":
        return f"http:{host_label_from_url(str((cfg.get('http') or {}).get('url') or ''))}:{kind}"
    if driver == "ftp":
        return f"ftp:{host_label_from_url(str((cfg.get('ftp') or {}).get('url') or ''))}:{kind}"
    return f"custom:{source.get('id') or kind}"
