from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def pick_folder(initial_path: str) -> dict[str, Any]:
    path = Path(initial_path).expanduser()
    if not path.exists() or not path.is_dir():
        path = Path.home()
    return _native_folder_picker(str(path))


def _native_folder_picker(initial_path: str) -> dict[str, Any]:
    if sys.platform == "darwin":
        return _folder_picker_macos(initial_path)
    if os.name == "nt":
        return _folder_picker_windows(initial_path)
    return _folder_picker_linux(initial_path)


def _folder_picker_macos(initial_path: str) -> dict[str, Any]:
    escaped = initial_path.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Finder" to activate\n'
        f'set defaultLocation to POSIX file "{escaped}"\n'
        "try\n"
        '  set chosenFolder to choose folder with prompt "Select project folder" default location defaultLocation\n'
        "  return POSIX path of chosenFolder\n"
        "on error number -128\n"
        '  return ""\n'
        "end try"
    )
    return _run_folder_picker_command(["/usr/bin/osascript", "-e", script])


def _folder_picker_windows(initial_path: str) -> dict[str, Any]:
    literal = initial_path.replace("'", "''")
    command = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
        f"$dialog.SelectedPath = '{literal}'; "
        "$dialog.Description = 'Select project folder'; "
        "$result = $dialog.ShowDialog(); "
        "if ($result -eq [System.Windows.Forms.DialogResult]::OK) { $dialog.SelectedPath } else { '' }"
    )
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not powershell:
        return {"supported": False, "cancelled": True, "path": "", "message": "PowerShell is required for the folder picker on Windows."}
    return _run_folder_picker_command([powershell, "-NoProfile", "-STA", "-Command", command])


def _folder_picker_linux(initial_path: str) -> dict[str, Any]:
    candidates: list[tuple[str, list[str]]] = []
    if shutil.which("zenity"):
        candidates.append(("zenity", ["--file-selection", "--directory", f"--filename={initial_path.rstrip('/')}/"]))
    if shutil.which("kdialog"):
        candidates.append(("kdialog", ["--getexistingdirectory", initial_path]))
    for executable, args in candidates:
        try:
            proc = subprocess.run([executable, *args], capture_output=True, text=True, timeout=300, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"supported": False, "cancelled": True, "path": "", "message": str(exc)}
        if proc.returncode == 0:
            selected = proc.stdout.strip()
            return {"supported": True, "cancelled": not bool(selected), "path": selected}
        if proc.returncode == 1:
            return {"supported": True, "cancelled": True, "path": ""}
    return {
        "supported": False,
        "cancelled": True,
        "path": "",
        "message": "Install zenity or kdialog to pick folders from the browser, or type the path manually.",
    }


def _run_folder_picker_command(command: list[str]) -> dict[str, Any]:
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"supported": False, "cancelled": True, "path": "", "message": str(exc)}
    stderr = (proc.stderr or "").strip()
    stdout = (proc.stdout or "").strip()
    if proc.returncode != 0:
        if "-128" in stderr or "canceled" in stderr.lower() or "cancelled" in stderr.lower():
            return {"supported": True, "cancelled": True, "path": ""}
        message = stderr or stdout or "Folder picker failed."
        return {"supported": False, "cancelled": True, "path": "", "message": message}
    return {"supported": True, "cancelled": not bool(stdout), "path": stdout}


def pick_folder_subprocess(initial_path: str) -> dict[str, Any]:
    # Run this file by path so the worker does not depend on PYTHONPATH /
    # installed package layout (ArchitectOS adds backend/ only in the parent process).
    script = Path(__file__).resolve()
    command = [sys.executable, str(script), initial_path]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"supported": False, "cancelled": True, "path": "", "message": str(exc)}
    if proc.returncode != 0 and not proc.stdout.strip():
        message = (proc.stderr or proc.stdout or "Folder picker process failed.").strip()
        return {"supported": False, "cancelled": True, "path": "", "message": message}
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        message = (proc.stderr or proc.stdout or "Folder picker returned invalid data.").strip()
        return {"supported": False, "cancelled": True, "path": "", "message": message}
    return payload


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    initial_path = args[0] if args else str(Path.home())
    print(json.dumps(pick_folder(initial_path), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
