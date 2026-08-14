"""PyInstaller entry for the ArchitectOS desktop server sidecar.

Defaults to ``--no-browser`` so the Tauri webview owns the UI.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_backend_path() -> None:
    if getattr(sys, "frozen", False):
        return
    root = Path(__file__).resolve().parents[1]
    backend = root / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))


def main() -> int:
    _ensure_backend_path()
    from architectos.launcher import main as launcher_main

    argv = list(sys.argv[1:])
    if "--no-browser" not in argv and "--app-window" not in argv:
        argv.insert(0, "--no-browser")
    return launcher_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
