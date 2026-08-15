"""Project file write/delete operations.

Extracted from ``service.py`` so the file-mutation surface (used by the HTTP
``/api/project/file/save|delete`` endpoints) is small and auditable in one
place. All functions confine paths to the project root they are given.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def safe_project_path(root: Path, relative_path: str) -> Path:
    raw = str(relative_path or "").strip()
    if not raw:
        raise ValueError("file path is required")
    # Resolve the root too: a symlinked root (e.g. a project under /tmp on
    # macOS) must match its resolved candidates instead of rejecting them all.
    resolved_root = root.resolve()
    candidate = (resolved_root / raw).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        raise ValueError("file path is outside project root") from None
    return candidate


def save_project_file(root: Path, project_id: str, relative_path: str, text: str) -> dict[str, Any]:
    """Save or create a project file."""
    if not relative_path:
        raise ValueError("path is required")
    resolved_root = root.resolve()
    path = safe_project_path(root, relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {
        "success": True,
        "project_id": project_id,
        "path": str(path.relative_to(resolved_root)),
        "size": path.stat().st_size,
    }


def delete_project_file(root: Path, project_id: str, relative_path: str) -> dict[str, Any]:
    """Delete a project file."""
    if not relative_path:
        raise ValueError("path is required")
    resolved_root = root.resolve()
    path = safe_project_path(root, relative_path)
    if not path.exists():
        raise ValueError("file does not exist")
    path.unlink()
    return {
        "success": True,
        "project_id": project_id,
        "path": str(path.relative_to(resolved_root)),
    }
