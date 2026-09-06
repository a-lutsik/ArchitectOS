"""Local git history candidate ingest and project diffs.

Split out of ``project_scan_service``. Composed into ``ProjectScanServiceMixin``.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from .models import stable_id

_LOG = logging.getLogger("architectos.service")


class ProjectGitIngestMixin:
    """Git log clustering into memory candidates and project_git_diff."""

    def _ingest_git_candidates(self, project_id: str, root: Path, limit: int = 0, *, max_items: int | None = None) -> list[dict[str, Any]]:
        cap = max_items if max_items is not None else limit
        fetch = 80 if cap <= 0 else min(max(cap * 4, 20), 80)
        try:
            proc = subprocess.run(["git", "-C", str(root), "log", "--pretty=format:%h%x09%s", "-n", str(fetch)], text=True, capture_output=True, timeout=5, shell=False)
        except OSError:
            return []
        except subprocess.TimeoutExpired:
            return []
        output = (proc.stdout or "").strip()
        if proc.returncode != 0 or not output:
            return []
        commits = []
        for line in output.splitlines():
            if "\t" in line:
                commit_hash, subject = line.split("\t", 1)
            else:
                parts = line.split(" ", 1)
                commit_hash, subject = parts[0], parts[1] if len(parts) > 1 else line
            commits.append((commit_hash.strip(), subject.strip()))
        candidates = [{
            "id": stable_id("candidate", project_id, "git", root.as_posix()),
            "project_id": project_id,
            "source_type": "git",
            "source_ref": str(root),
            "label": "Git: recent commit history",
            "type": "Decision",
            "scope": "project",
            "text": "Recent git history worth reviewing for durable memory:\n\n" + "\n".join(f"{item[0]} {item[1]}" for item in commits)[:2500],
            "confidence": 0.55,
            "metadata": {"root": str(root), "commits": len(commits), "template": "git_history"},
        }]
        candidates.extend(self._build_git_cluster_candidates(project_id, root, commits, cap if cap > 0 else len(commits)))
        return candidates if cap <= 0 else candidates[:cap]

    def _build_git_cluster_candidates(self, project_id: str, root: Path, commits: list[tuple[str, str]], limit: int) -> list[dict[str, Any]]:
        clusters: dict[str, list[tuple[str, str]]] = {}
        for commit_hash, subject in commits:
            cluster = self._commit_cluster(subject)
            clusters.setdefault(cluster, []).append((commit_hash, subject))
        candidates: list[dict[str, Any]] = []
        for cluster, items in sorted(clusters.items(), key=lambda item: (-len(item[1]), item[0])):
            if len(items) < 2 and len(clusters) > 1:
                continue
            lines = [f"- {commit_hash} {subject}" for commit_hash, subject in items[:12]]
            candidates.append({
                "id": stable_id("candidate", project_id, "git_cluster", root.as_posix(), cluster),
                "project_id": project_id,
                "source_type": "git_cluster",
                "source_ref": f"{root}#{cluster}",
                "label": f"Git cluster: {cluster}",
                "type": "Decision",
                "scope": "project",
                "text": f"Semantic commit cluster '{cluster}' suggests durable project memory.\n\n" + "\n".join(lines),
                "confidence": min(0.78, 0.54 + 0.04 * len(items)),
                "metadata": {"root": str(root), "cluster": cluster, "commits": len(items), "template": "commit_cluster"},
            })
            if len(candidates) >= limit:
                break
        return candidates

    def _commit_cluster(self, subject: str) -> str:
        lower = subject.lower()
        prefix_match = re.match(r"^([a-z]+)(\([^)]+\))?!?:", lower)
        if prefix_match:
            prefix = prefix_match.group(1)
            if prefix in {"feat", "feature"}:
                return "feature"
            if prefix in {"fix", "bugfix", "hotfix"}:
                return "fix"
            if prefix in {"docs", "doc"}:
                return "docs"
            if prefix in {"test", "tests"}:
                return "tests"
            if prefix in {"refactor", "perf", "chore", "build", "ci"}:
                return prefix
        keyword_clusters = [
            ("security", ("security", "secret", "token", "auth", "approval")),
            ("memory", ("memory", "candidate", "ingest", "context")),
            ("provider", ("provider", "openai", "ollama", "claude", "codex", "router")),
            ("graph", ("graph", "node", "edge", "canvas")),
            ("workflow", ("workflow", "task", "review")),
            ("release", ("release", "version", "backup", "readiness", "package")),
        ]
        for cluster, keywords in keyword_clusters:
            if any(keyword in lower for keyword in keywords):
                return cluster
        return "general"

    def project_git_diff(self, project_id: str | None = None, relative_path: str | None = None) -> dict[str, Any]:
        project_id = project_id or "architectos"
        root = self._project_root(project_id)
        command = ["git", "-C", str(root), "diff", "--"]
        checked_path = ""
        if relative_path:
            path = self._safe_project_path(root, relative_path)
            checked_path = str(path.relative_to(root))
            command.append(checked_path)
        try:
            proc = subprocess.run(command, text=True, capture_output=True, timeout=8, shell=False)
        except OSError as exc:
            return {"project_id": project_id, "root": str(root), "path": checked_path, "status": "unavailable", "diff": "", "message": f"git diff failed: {exc}"}
        except subprocess.TimeoutExpired:
            return {"project_id": project_id, "root": str(root), "path": checked_path, "status": "timeout", "diff": "", "message": "git diff timed out."}
        diff, diff_redacted = self.security_policy.redact_text((proc.stdout or "")[:80_000])
        stderr, stderr_redacted = self.security_policy.redact_text((proc.stderr or "").strip()[:600])
        return {"project_id": project_id, "root": str(root), "path": checked_path, "status": "ok" if proc.returncode == 0 else "unavailable", "diff": diff, "message": stderr, "redacted": diff_redacted or stderr_redacted}
