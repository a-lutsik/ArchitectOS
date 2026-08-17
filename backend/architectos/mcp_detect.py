"""Node binary preference and Azure DevOps remote detection for MCP."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

ADO_ORG_PLACEHOLDERS = {"$ADO_ORG", "${ADO_ORG}", "<organization>", "<org>", "ORGANIZATION", "organization"}
ADO_MCP_PACKAGE = "@azure-devops/mcp"
ADO_MCP_MIN_NODE = (20, 0, 0)
_REMOTE_URL_ORG_RE = re.compile(r"https?://dev\.azure\.com/([^/\s]+)", re.I)
_SSH_ORG_RE = re.compile(r"(?:git@)?ssh\.dev\.azure\.com:v3/([^/\s]+)/([^/\s]+)/([^/\s]+)", re.I)
_VSS_ORG_RE = re.compile(r"https?://([^.]+)\.visualstudio\.com/", re.I)

def _parse_node_version(raw: str) -> tuple[int, int, int] | None:
    text = str(raw or "").strip()
    if text.lower().startswith("v"):
        text = text[1:]
    parts = text.split(".")
    if len(parts) < 2:
        return None
    try:
        major = int(parts[0])
        minor = int(parts[1])
        patch = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    except ValueError:
        return None
    return major, minor, patch


def _node_version(node_bin: str) -> tuple[int, int, int] | None:
    try:
        completed = subprocess.run(
            [node_bin, "-v"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _parse_node_version((completed.stdout or completed.stderr or "").strip())


def _candidate_node_bins() -> list[str]:
    """Prefer modern Node installs (nvm/Homebrew) over stale /usr/local/bin Node 18."""
    seen: set[str] = set()
    ordered: list[str] = []

    def add(path: str | Path | None) -> None:
        if not path:
            return
        text = str(path)
        if not text or text in seen:
            return
        seen.add(text)
        ordered.append(text)

    which_node = shutil.which("node")
    add(which_node)
    home = Path.home()
    nvm_dir = Path(os.environ.get("NVM_DIR") or (home / ".nvm"))
    versions_dir = nvm_dir / "versions" / "node"
    if versions_dir.is_dir():
        version_dirs = sorted(
            [item for item in versions_dir.iterdir() if item.is_dir()],
            key=lambda item: _parse_node_version(item.name) or (0, 0, 0),
            reverse=True,
        )
        for item in version_dirs:
            add(item / "bin" / "node")
    for brew_node in (
        Path("/opt/homebrew/opt/node/bin/node"),
        Path("/usr/local/opt/node/bin/node"),
        Path("/opt/homebrew/bin/node"),
    ):
        add(brew_node)
    add("/usr/local/bin/node")
    add("/usr/bin/node")
    return [path for path in ordered if Path(path).exists()]


def prefer_modern_node_env(env: dict[str, str] | None = None, *, minimum: tuple[int, int, int] = ADO_MCP_MIN_NODE) -> dict[str, str]:
    """Return env with PATH prepended by a Node >= minimum bin dir when available."""
    merged = dict(env or os.environ)
    selected: str | None = None
    for node_bin in _candidate_node_bins():
        version = _node_version(node_bin)
        if version and version >= minimum:
            selected = node_bin
            break
    if not selected:
        return merged
    bin_dir = str(Path(selected).resolve().parent)
    current = str(merged.get("PATH") or "")
    parts = [item for item in current.split(os.pathsep) if item]
    if bin_dir in parts:
        parts = [bin_dir, *[item for item in parts if item != bin_dir]]
    else:
        parts.insert(0, bin_dir)
    merged["PATH"] = os.pathsep.join(parts)
    return merged


def detect_azure_devops_from_git(root: Path) -> dict[str, str]:
    """Best-effort org/project/repo extraction from `git remote -v` under root."""
    for url in _git_remote_urls(root):
        detected = parse_azure_devops_remote_url(url)
        if detected.get("org"):
            return detected
    return {}


def parse_azure_devops_remote_url(url: str) -> dict[str, str]:
    match = _SSH_ORG_RE.search(url)
    if match:
        return {"org": match.group(1), "project": match.group(2), "repo": match.group(3)}
    match = _REMOTE_URL_ORG_RE.search(url)
    if match:
        org = match.group(1)
        parts = url.rstrip("/").split("/")
        project = ""
        repo = ""
        if "_git" in parts:
            idx = parts.index("_git")
            project = parts[idx - 1] if idx >= 1 else ""
            repo = parts[idx + 1] if idx + 1 < len(parts) else ""
        return {"org": org, "project": project, "repo": repo}
    match = _VSS_ORG_RE.search(url)
    if match:
        return {"org": match.group(1), "project": "", "repo": ""}
    return {}

def _git_remote_urls(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    git = shutil.which("git")
    if not git:
        return []
    try:
        completed = subprocess.run(
            [git, "remote", "-v"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    urls: list[str] = []
    for line in (completed.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            urls.append(parts[1].strip())
    return urls
