#!/usr/bin/env python3
"""Orchestrate ArchitectOS installer builds (desktop / MCP / IDE).

Wraps the platform scripts under scripts/. Does not run in default PR CI —
invoke manually when packaging releases.

Examples:
  python scripts/build_all_installers.py --target all
  python scripts/build_all_installers.py --target desktop
  python scripts/build_all_installers.py --target mcp
  python scripts/build_all_installers.py --target ide
  python scripts/build_all_installers.py --target share
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def is_windows() -> bool:
    return os.name == "nt"


def script_for(name: str) -> Path:
    if name == "build_share_package":
        py = SCRIPTS / "build_share_package.py"
        if py.is_file():
            return py
        raise FileNotFoundError(f"Missing {py}")
    if is_windows():
        ps1 = SCRIPTS / f"{name}.ps1"
        if ps1.is_file():
            return ps1
    sh = SCRIPTS / f"{name}.sh"
    if sh.is_file():
        return sh
    ps1 = SCRIPTS / f"{name}.ps1"
    if ps1.is_file():
        return ps1
    raise FileNotFoundError(f"Missing build script for {name} under {SCRIPTS}")


def run_script(path: Path, extra_args: list[str] | None = None) -> int:
    print(f"==> Running {path.relative_to(ROOT)}", flush=True)
    extra = list(extra_args or [])
    if path.suffix.lower() == ".py":
        cmd = [sys.executable, str(path), *extra]
    elif path.suffix.lower() == ".ps1":
        cmd = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(path),
            *extra,
        ]
    else:
        cmd = ["bash", str(path), *extra]
    completed = subprocess.run(cmd, cwd=ROOT)
    return int(completed.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build ArchitectOS installers into dist/.")
    parser.add_argument(
        "--target",
        choices=("desktop", "mcp", "ide", "share", "all"),
        default="all",
        help="Which installer pipeline to run (default: all = desktop+mcp+ide).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print expected output layout and exit.",
    )
    args = parser.parse_args(argv)

    if args.list:
        print(
            """dist/
  desktop/ArchitectOS_<ver>_macos.dmg
  desktop/ArchitectOS_<ver>_windows.msi
  mcp/architectos-mcp_<ver>_macos.zip
  mcp/architectos-mcp_<ver>_windows.zip
  ide/ArchitectOS-Memory-<ver>.zip
  share/ArchitectOS_Full_<ver>_<platform>.zip   # server + OS autostart install"""
        )
        return 0

    if args.target == "all":
        targets = ["desktop", "mcp", "ide"]
    else:
        targets = [args.target]
    mapping = {
        "desktop": ("build_desktop", []),
        "mcp": ("build_mcp", []),
        "ide": ("build_ide_plugin", []),
        "share": ("build_share_package", ["--with-mcp", "--with-ide"]),
    }

    failures: list[str] = []
    for target in targets:
        name, extra = mapping[target]
        try:
            path = script_for(name)
        except FileNotFoundError as err:
            print(f"error: {err}", file=sys.stderr)
            failures.append(target)
            continue
        code = run_script(path, extra)
        if code != 0:
            print(f"error: {target} build exited with {code}", file=sys.stderr)
            failures.append(target)

    if failures:
        print(f"Failed: {', '.join(failures)}", file=sys.stderr)
        print("See docs/INSTALLERS.md for prerequisites and smoke checks.", file=sys.stderr)
        return 1

    print("All requested installer builds finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
