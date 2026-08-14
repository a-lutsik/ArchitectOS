"""Resolve ArchitectOS project and frontend roots for source and packaged runs.

Shared by the HTTP server, launcher, and MCP server so Full Desktop and MCP
installers agree on ``ARCHITECTOS_ROOT`` / ``data/architectos.db``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS")


def default_architectos_root() -> Path:
    """Default data root for packaged installs (shared by desktop + MCP)."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ArchitectOS"
    return Path.home() / "ArchitectOS"


def resolve_project_root() -> Path:
    """Directory that owns ``data/`` (DB, runtime.json, inbox).

    Order:
    1. ``ARCHITECTOS_ROOT`` if set
    2. Packaged default (``~/ArchitectOS`` or ``%LOCALAPPDATA%\\ArchitectOS``)
    3. Repository root when running from a checkout
    4. Current working directory
    """
    override = os.environ.get("ARCHITECTOS_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if is_frozen():
        return default_architectos_root().expanduser().resolve()
    # backend/architectos/paths.py → repo root
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "run_architectos.py").exists() or (candidate / "frontend").is_dir() or (candidate / "data").is_dir():
        return candidate
    return Path.cwd().resolve()


def resolve_frontend_root() -> Path:
    """Static UI directory (may live inside a PyInstaller bundle, not under data root)."""
    override = os.environ.get("ARCHITECTOS_FRONTEND", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if is_frozen():
        meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        for candidate in (meipass / "frontend", Path(sys.executable).resolve().parent / "frontend"):
            if (candidate / "index.html").is_file():
                return candidate
    return resolve_project_root() / "frontend"
