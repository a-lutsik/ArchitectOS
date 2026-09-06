"""Diagnose and repair sqlite-vec support for the running Python.

The official python.org macOS build ships sqlite3 without loadable extensions.
This module probes the current interpreter, looks for a capable one (Homebrew /
conda), and after an explicit confirm can install sqlite-vec or Homebrew Python.

Windows portable share packages ship embeddable CPython, which omits pip.
Repair must bootstrap pip (ensurepip / get-pip) before ``-m pip install``, and
must not run a doomed pip on a frozen PyInstaller bundle.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from .paths import is_frozen, resolve_project_root
from .vecsql import reset_sqlite_vec_probe, sqlite_vec_status

_LOG = logging.getLogger("architectos.vec_runtime")

PIP_PACKAGES = ("sqlite-vec>=0.1.6", "numpy>=1.26")
ACTIONS = frozenset({"install_sqlite_vec", "use_capable_python", "install_homebrew_python"})
PREFERRED_PYTHON_NAME = "architectos.python.json"
RUNTIME_VENV_DIRNAME = "python"
LAUNCH_AGENT_LABEL = "com.architectos.server"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
PIP_MISSING_ERROR = (
    "pip is not installed in this Python (Windows embeddable builds omit it). "
    "Fix could not bootstrap pip. Rebuild the share package so sqlite-vec is baked in."
)
PEP668_ERROR = (
    "Homebrew Python is protected (PEP 668) and cannot be pip-patched in place. "
    "ArchitectOS installs sqlite-vec into a local venv under data/python instead."
)

_PROBE_SCRIPT = (
    "import json, sqlite3, sys\n"
    "conn = sqlite3.connect(':memory:')\n"
    "load_ext = hasattr(conn, 'enable_load_extension')\n"
    "installed = False\n"
    "loaded = False\n"
    "reason = 'ok'\n"
    "if not load_ext:\n"
    "    reason = 'load-extension-disabled'\n"
    "else:\n"
    "    try:\n"
    "        import sqlite_vec\n"
    "        installed = True\n"
    "        conn.enable_load_extension(True)\n"
    "        sqlite_vec.load(conn)\n"
    "        conn.enable_load_extension(False)\n"
    "        loaded = True\n"
    "    except ImportError:\n"
    "        reason = 'not-installed'\n"
    "    except Exception:\n"
    "        reason = 'probe-failed'\n"
    "print(json.dumps({'executable': sys.executable, 'version': sys.version.split()[0], "
    "'load_extension': load_ext, 'installed': installed, 'loaded': loaded, 'reason': reason}))\n"
)


def preferred_python_path(root: Path | None = None) -> Path:
    return (root or resolve_project_root()) / "data" / PREFERRED_PYTHON_NAME


def runtime_venv_dir(root: Path | None = None) -> Path:
    return (root or resolve_project_root()) / "data" / RUNTIME_VENV_DIRNAME


def runtime_venv_python(root: Path | None = None) -> Path:
    base = runtime_venv_dir(root)
    if os.name == "nt":
        return base / "Scripts" / "python.exe"
    return base / "bin" / "python"


def _is_venv_python(executable: str | Path) -> bool:
    try:
        path = Path(executable).expanduser()
    except OSError:
        return False
    for parent in (path.parent, path.parent.parent, path.parent.parent.parent):
        if (parent / "pyvenv.cfg").is_file():
            return True
    return False


def _public_python_path(executable: str) -> str:
    """Keep venv and Homebrew bin shims; do not collapse them to Cellar/Frameworks."""
    try:
        path = Path(executable).expanduser()
    except OSError:
        return executable
    if _is_venv_python(path):
        return str(path)
    aliases = (
        "/opt/homebrew/bin/python3.14",
        "/opt/homebrew/bin/python3.13",
        "/opt/homebrew/bin/python3.12",
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3.14",
        "/usr/local/bin/python3.13",
        "/usr/local/bin/python3.12",
        "/usr/local/bin/python3",
    )
    try:
        resolved = path.resolve()
    except OSError:
        return str(path)
    versioned: list[str] = []
    generic: list[str] = []
    for alias in aliases:
        alias_path = Path(alias)
        try:
            if alias_path.is_file() and alias_path.resolve() == resolved:
                (generic if alias_path.name in {"python", "python3"} else versioned).append(str(alias_path))
        except OSError:
            continue
    if versioned:
        return versioned[0]
    if generic:
        return generic[0]
    return str(path if path.exists() else resolved)


def _run(command: list[str], *, timeout: float, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, shell=False, check=False, env=env)


def _is_current_executable(executable: str) -> bool:
    try:
        return os.path.samefile(executable, sys.executable)
    except OSError:
        return os.path.normpath(executable) == os.path.normpath(sys.executable)


def is_embeddable_python(executable: str | None = None) -> bool:
    """True for official embeddable CPython (python*._pth) or the portable runtime layout."""
    try:
        parent = Path(executable or sys.executable).expanduser().resolve().parent
    except OSError:
        return False
    if any(parent.glob("python*._pth")):
        return True
    parts = [part.lower() for part in parent.parts]
    try:
        idx = parts.index("runtime")
    except ValueError:
        return False
    return idx + 1 < len(parts) and parts[idx + 1] == "python"


def _pip_importable() -> bool:
    try:
        import pip  # noqa: F401
    except Exception:
        return False
    return True


def _pip_module_available(python: str) -> bool:
    if _is_current_executable(python):
        return _pip_importable()
    try:
        proc = _run([python, "-c", "import pip"], timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _runtime_writable(executable: str) -> bool:
    try:
        parent = Path(executable).expanduser().resolve().parent
        return bool(os.access(parent, os.W_OK))
    except OSError:
        return False


def probe_current_python() -> dict[str, Any]:
    status = sqlite_vec_status()
    executable = str(status.get("python") or sys.executable)
    frozen = bool(status.get("frozen"))
    return {
        "executable": executable,
        "version": status.get("version") or sys.version.split()[0],
        "load_extension": bool(status.get("load_extension")),
        "installed": bool(status.get("installed")),
        "loaded": bool(status.get("loaded")),
        "reason": str(status.get("reason") or "not-probed"),
        "frozen": frozen,
        "embeddable": is_embeddable_python(executable),
        "pip": _pip_importable(),
        "writable": (not frozen) and _runtime_writable(executable),
        "current": True,
    }


def probe_python(executable: str, *, timeout: float = 8.0) -> dict[str, Any] | None:
    path = Path(executable).expanduser()
    if not path.exists():
        return None
    try:
        proc = _run([str(path), "-c", _PROBE_SCRIPT], timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _LOG.debug("python probe failed for %s: %s", executable, exc)
        return None
    if proc.returncode != 0:
        return None
    try:
        parsed = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    if not isinstance(parsed, dict):
        return None
    parsed["current"] = _is_current_executable(str(path))
    parsed["executable"] = _public_python_path(str(path))
    return parsed


def _candidate_executables() -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []

    def add(raw: str | None) -> None:
        text = str(raw or "").strip()
        if not text:
            return
        path = Path(text).expanduser()
        if not path.is_file():
            return
        display = _public_python_path(str(path))
        try:
            identity = display if _is_venv_python(path) else str(path.resolve())
        except OSError:
            identity = display
        if identity in seen:
            return
        seen.add(identity)
        ordered.append(display)

    add(str(runtime_venv_python()))
    add(sys.executable)
    for name in ("python3", "python3.14", "python3.13", "python3.12", "python"):
        add(shutil.which(name))
    for extra in (
        "/opt/homebrew/bin/python3",
        "/opt/homebrew/bin/python3.14",
        "/usr/local/bin/python3",
        "/usr/local/bin/python3.14",
        str(Path.home() / "miniforge3/bin/python"),
        str(Path.home() / "miniconda3/bin/python"),
        str(Path.home() / "anaconda3/bin/python"),
    ):
        add(extra)
    return ordered


def discover_capable_pythons() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for executable in _candidate_executables():
        probed = probe_current_python() if _is_current_executable(executable) else probe_python(executable)
        if probed and probed.get("load_extension") and not probed.get("current"):
            found.append(probed)
    return found


def _quote(value: str) -> str:
    if any(ch in value for ch in " \t"):
        return f'"{value}"'
    return value


def _command_preview(parts: list[list[str]]) -> str:
    return " && ".join(" ".join(_quote(item) for item in command) for command in parts)


def _brew_bin() -> str | None:
    return shutil.which("brew")


def _homebrew_python_guess() -> str:
    for path in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
        if Path(path).exists():
            return path
    return "/opt/homebrew/bin/python3" if platform.machine() == "arm64" else "/usr/local/bin/python3"


def _empty_repair(
    *,
    action: str | None = None,
    fixable: bool = False,
    needs_check: bool = False,
    summary: str,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "action": action,
        "fixable": fixable,
        "needs_check": needs_check,
        "summary": summary,
        "reason": reason,
        "bootstrap_pip": False,
        "command": [],
        "commands": [],
        "command_preview": "",
    }


def _has_pip(current: dict[str, Any], python: str) -> bool:
    if "pip" in current:
        return bool(current["pip"])
    if current.get("current"):
        return _pip_importable()
    return _pip_module_available(python)


def _is_writable(current: dict[str, Any], python: str) -> bool:
    if current.get("frozen"):
        return False
    if "writable" in current:
        return bool(current["writable"])
    return _runtime_writable(python)


def _pip_install_command(python: str) -> list[str]:
    return [python, "-m", "pip", "install", "--disable-pip-version-check", *PIP_PACKAGES]


def _ensurepip_command(python: str) -> list[str]:
    return [python, "-m", "ensurepip", "--upgrade"]


def _venv_command(base_python: str, root: Path | None = None) -> list[str]:
    return [base_python, "-m", "venv", str(runtime_venv_dir(root))]


def _needs_isolated_venv(python: str) -> bool:
    path = Path(python).expanduser()
    if not python or not path.exists() or _is_venv_python(path):
        return False
    try:
        proc = _run(
            [
                str(path),
                "-c",
                "import pathlib, sysconfig; print((pathlib.Path(sysconfig.get_path('stdlib')) / 'EXTERNALLY-MANAGED').is_file())",
            ],
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and (proc.stdout or "").strip() == "True"


def _ensure_runtime_venv(base_python: str, root: Path | None = None) -> dict[str, Any]:
    command = _venv_command(base_python, root)
    venv_py = runtime_venv_python(root)
    if venv_py.is_file():
        probed = probe_python(str(venv_py))
        if not probed or probed.get("load_extension"):
            step = _step(command, ok=True, stdout=f"reuse {venv_py}", skipped=True)
            step["executable"] = str(venv_py)
            return step
        try:
            shutil.rmtree(runtime_venv_dir(root))
        except OSError as exc:
            step = _step(command, ok=False, error=f"could not replace existing venv: {exc}")
            step["executable"] = ""
            return step
    runtime_venv_dir(root).parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = _run(command, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        step = _step(command, ok=False, error=str(exc))
        step["executable"] = ""
        return step
    ok = proc.returncode == 0 and venv_py.is_file()
    step = _step(
        command,
        ok=ok,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        error="" if ok else ((proc.stderr or proc.stdout or "venv create failed")[-280:]),
    )
    step["executable"] = str(venv_py) if ok else ""
    return step


def _prepare_target_python(base: str, *, isolate: bool | None = None) -> tuple[str, list[dict[str, Any]]]:
    if isolate is None:
        isolate = _needs_isolated_venv(base)
    if not isolate:
        return base, []
    created = _ensure_runtime_venv(base)
    if not created["ok"]:
        return "", [created]
    return str(created.get("executable") or runtime_venv_python()), [created]


def _looks_like_python_executable(path: str) -> bool:
    name = Path(path).name.lower()
    return name.startswith("python")


def macos_launch_agent_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def retarget_macos_launch_agent(python: str, plist_path: Path | None = None) -> dict[str, Any]:
    """Point the login LaunchAgent at a capable interpreter. Leaves frozen binaries alone."""
    if sys.platform != "darwin":
        return {"ok": False, "skipped": True, "reason": "not-macos"}
    path = plist_path or macos_launch_agent_plist()
    if not path.is_file():
        return {"ok": False, "skipped": True, "reason": "no-launch-agent", "path": str(path)}
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        return {"ok": False, "skipped": False, "error": str(exc), "path": str(path)}
    args = [str(item) for item in (data.get("ProgramArguments") or [])]
    if not args or not _looks_like_python_executable(args[0]):
        return {"ok": False, "skipped": True, "reason": "not-python-launch-agent", "path": str(path)}
    target = _public_python_path(python)
    already = False
    try:
        already = os.path.samefile(args[0], target)
    except OSError:
        already = os.path.normpath(args[0]) == os.path.normpath(target)
    uid = os.getuid() if hasattr(os, "getuid") else 0
    restart = f'launchctl kickstart -k "gui/{uid}/{LAUNCH_AGENT_LABEL}"'
    if already:
        return {
            "ok": True,
            "skipped": True,
            "reason": "already-retargeted",
            "path": str(path),
            "executable": target,
            "restart_command": restart,
        }
    previous = args[0]
    args[0] = target
    data["ProgramArguments"] = args
    try:
        with path.open("wb") as handle:
            plistlib.dump(data, handle, fmt=plistlib.FMT_XML, sort_keys=False)
    except OSError as exc:
        return {"ok": False, "skipped": False, "error": str(exc), "path": str(path)}
    return {
        "ok": True,
        "skipped": False,
        "path": str(path),
        "previous": previous,
        "executable": target,
        "restart_command": restart,
    }


def maybe_reexec_preferred_python() -> None:
    """Restart under data/architectos.python.json when the login item still uses python.org."""
    if os.environ.get("ARCHITECTOS_NO_PYTHON_REEXEC") == "1" or is_frozen():
        return
    preferred = _read_preferred_python()
    if not preferred:
        return
    dest = Path(preferred).expanduser()
    if not dest.is_file():
        return
    try:
        if os.path.samefile(dest, sys.executable):
            return
    except OSError:
        return
    _LOG.info("re-exec preferred Python %s", dest)
    os.execv(str(dest), [str(dest), *sys.argv])


def _restart_command(python: str, launch_agent: dict[str, Any] | None = None) -> str:
    if launch_agent and launch_agent.get("ok") and launch_agent.get("restart_command"):
        return str(launch_agent["restart_command"])
    script = resolve_project_root() / "run_architectos.py"
    if script.is_file():
        return f'"{python}" "{script}"'
    return f'"{python}" -m architectos'


def propose_repair(
    current: dict[str, Any] | None = None,
    *,
    candidates: list[dict[str, Any]] | None = None,
    discovered: bool = True,
) -> dict[str, Any]:
    current = current or probe_current_python()
    if current.get("loaded"):
        return _empty_repair(summary="sqlite-vec is already loaded in this Python.")
    if current.get("frozen"):
        return _empty_repair(
            reason="frozen",
            summary=(
                "This packaged app cannot pip-install into itself. sqlite-vec is baked in at build time: "
                'rebuild with Homebrew or conda Python (`pip install -e ".[vectors]"`), then reinstall. '
                "The installer already probes the binary; Ask/Search keep working with the Python fallback until then."
            ),
        )
    python = sys.executable if current.get("current") else str(current.get("executable") or sys.executable)
    if current.get("load_extension"):
        isolate = _needs_isolated_venv(python)
        if isolate:
            target = str(runtime_venv_python())
            commands = [_venv_command(python), _pip_install_command(target)]
            return {
                "action": "use_capable_python",
                "fixable": True,
                "needs_check": False,
                "summary": (
                    f"This Python ({python}) can load SQLite extensions, but pip is blocked (PEP 668). "
                    f"Fix will create a local venv at {runtime_venv_dir()} and install sqlite-vec there."
                ),
                "reason": "pep668-venv",
                "bootstrap_pip": False,
                "isolate_venv": True,
                "base_python": python,
                "command": commands[0],
                "commands": commands,
                "command_preview": _command_preview(commands),
                "target_python": target,
                "restart_command": _restart_command(target),
            }
        has_pip = _has_pip(current, python)
        writable = _is_writable(current, python)
        pip_cmd = _pip_install_command(python)
        if not has_pip and not writable:
            return _empty_repair(
                reason="pip-missing",
                summary=(
                    f"This Python ({python}) has no pip — typical of the Windows embeddable runtime. "
                    "sqlite-vec should be baked into the share package at build time. "
                    "This copy cannot install packages in place."
                ),
            )
        commands = [pip_cmd]
        summary = "This Python can load SQLite extensions. Install sqlite-vec into the current environment."
        if not has_pip:
            commands = [_ensurepip_command(python), pip_cmd]
            summary = (
                f"This Python ({python}) can load SQLite extensions, but pip is missing "
                "(Windows embeddable runtime omits it). Fix will bootstrap pip, then install sqlite-vec."
            )
        return {
            "action": "install_sqlite_vec",
            "fixable": True,
            "needs_check": False,
            "summary": summary,
            "reason": "bootstrap-pip" if not has_pip else "install-sqlite-vec",
            "bootstrap_pip": not has_pip,
            "command": commands[0],
            "commands": commands,
            "command_preview": _command_preview(commands),
            "target_python": python,
            "base_python": python,
            "isolate_venv": False,
        }
    if not discovered:
        return _empty_repair(
            needs_check=True,
            summary=(f"This Python ({current.get('executable')}) cannot load SQLite extensions. Check for a Homebrew or conda interpreter that can, then confirm the fix."),
        )
    if candidates:
        base = str(candidates[0]["executable"])
        isolate = _needs_isolated_venv(base)
        target = str(runtime_venv_python()) if isolate else base
        commands = [_venv_command(base), _pip_install_command(target)] if isolate else [_pip_install_command(target)]
        summary = (
            f"This Python ({current.get('executable')}) cannot load SQLite extensions. "
            f"A capable interpreter was found at {base}. "
        )
        if isolate:
            summary += f"Homebrew blocks system pip, so Fix will create {runtime_venv_dir()} and install sqlite-vec there."
        else:
            summary += "Install sqlite-vec there and restart ArchitectOS with it."
        return {
            "action": "use_capable_python",
            "fixable": True,
            "needs_check": False,
            "summary": summary,
            "reason": "pep668-venv" if isolate else "use-capable-python",
            "command": commands[0],
            "commands": commands,
            "command_preview": _command_preview(commands),
            "target_python": target,
            "base_python": base,
            "isolate_venv": isolate,
            "restart_command": _restart_command(target),
        }
    brew = _brew_bin()
    if brew and sys.platform == "darwin":
        brew_cmd = [brew, "install", "python"]
        python_guess = _homebrew_python_guess()
        target = str(runtime_venv_python())
        commands = [brew_cmd, _venv_command(python_guess), _pip_install_command(target)]
        return {
            "action": "install_homebrew_python",
            "fixable": True,
            "needs_check": False,
            "summary": (
                f"This Python ({current.get('executable')}) is a python.org build: SQLite extensions are compiled out. "
                "Install Homebrew Python, create a local venv, then sqlite-vec, and restart ArchitectOS with that interpreter."
            ),
            "command": brew_cmd,
            "commands": commands,
            "command_preview": _command_preview(commands),
            "target_python": target,
            "base_python": python_guess,
            "isolate_venv": True,
            "restart_command": _restart_command(target),
        }
    return _empty_repair(
        summary=(
            f"This Python ({current.get('executable')}) cannot load SQLite extensions, and no Homebrew/conda Python was found. "
            "Install Homebrew Python (`brew install python`) or conda, then restart ArchitectOS with that interpreter."
        ),
    )


def _read_preferred_python(root: Path | None = None) -> str:
    preferred = preferred_python_path(root)
    if not preferred.exists():
        return ""
    try:
        parsed = json.loads(preferred.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return ""
    return str(parsed.get("executable") or "")


def vector_runtime_status(*, discover: bool = True) -> dict[str, Any]:
    current = probe_current_python()
    needs_other_python = bool(not current.get("loaded") and not current.get("frozen") and not current.get("load_extension"))
    should_discover = bool(discover and needs_other_python)
    candidates = discover_capable_pythons() if should_discover else []
    repair = propose_repair(current, candidates=candidates, discovered=not needs_other_python or should_discover)
    return {
        "ok": bool(current.get("loaded")),
        "current": current,
        "candidates": candidates,
        "brew": bool(_brew_bin()),
        "preferred_python": _read_preferred_python(),
        "repair": repair,
        "numpy": _numpy_available(),
        "backend": "sqlite-vec" if current.get("loaded") else "python",
    }


def _numpy_available() -> bool:
    try:
        import numpy  # noqa: F401
    except Exception:
        return False
    return True


def _save_preferred_python(executable: str, root: Path | None = None) -> None:
    path = preferred_python_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"executable": _public_python_path(executable)}, indent=2) + "\n", encoding="utf-8")


def _step(
    command: list[str],
    *,
    ok: bool,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    error: str = "",
    skipped: bool = False,
) -> dict[str, Any]:
    return {
        "command": command,
        "returncode": returncode,
        "stdout": (stdout or "")[-2000:],
        "stderr": (stderr or "")[-2000:],
        "ok": ok,
        "error": error,
        "skipped": skipped,
    }


def _download_get_pip(dest: Path) -> bool:
    try:
        with urllib.request.urlopen(GET_PIP_URL, timeout=30) as src, dest.open("wb") as out:
            out.write(src.read())
        return dest.is_file() and dest.stat().st_size > 1000
    except OSError as exc:
        _LOG.debug("get-pip download failed: %s", exc)
        return False


def _ensure_pip(python: str) -> dict[str, Any]:
    """Install pip into a writable interpreter when ``python -m pip`` would fail."""
    if _pip_module_available(python):
        return _step([python, "-c", "import pip"], ok=True, stdout="pip already available", skipped=True)

    ensure_cmd = _ensurepip_command(python)
    proc = None
    ensure_err = ""
    try:
        proc = _run(ensure_cmd, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as exc:
        ensure_err = str(exc)
    else:
        if proc.returncode == 0 and _pip_module_available(python):
            return _step(
                ensure_cmd,
                ok=True,
                returncode=proc.returncode,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
            )
        ensure_err = (proc.stderr or proc.stdout or "")[-2000:]

    try:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "get-pip.py"
            if not _download_get_pip(script):
                return _step(
                    ensure_cmd,
                    ok=False,
                    returncode=getattr(proc, "returncode", 1) if proc is not None else 1,
                    stdout=getattr(proc, "stdout", "") or "",
                    stderr=ensure_err,
                    error=PIP_MISSING_ERROR,
                )
            get_cmd = [python, str(script), "--disable-pip-version-check"]
            got = _run(get_cmd, timeout=180)
            ok = got.returncode == 0 and _pip_module_available(python)
            return _step(
                get_cmd,
                ok=ok,
                returncode=got.returncode,
                stdout=got.stdout or "",
                stderr=got.stderr or "",
                error="" if ok else PIP_MISSING_ERROR,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _step(ensure_cmd, ok=False, returncode=1, stderr=str(exc), error=PIP_MISSING_ERROR)


def _pip_install(python: str) -> dict[str, Any]:
    command = _pip_install_command(python)
    if not _pip_module_available(python):
        return _step(command, ok=False, returncode=1, error=PIP_MISSING_ERROR)
    proc = _run(command, timeout=180)
    error = "" if proc.returncode == 0 else _friendly_pip_error(proc.stdout or "", proc.stderr or "")
    return _step(
        command,
        ok=proc.returncode == 0,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        error=error,
    )


def _friendly_pip_error(stdout: str, stderr: str) -> str:
    text = f"{stderr}\n{stdout}"
    if "No module named pip" in text:
        return PIP_MISSING_ERROR
    if "externally-managed-environment" in text or "EXTERNALLY-MANAGED" in text:
        return PEP668_ERROR
    line = next((item.strip() for item in reversed(text.splitlines()) if item.strip()), "")
    return line[:280] or "pip install failed"


def _resolve_homebrew_python() -> str | None:
    for candidate in (_homebrew_python_guess(), "/opt/homebrew/bin/python3", "/usr/local/bin/python3", shutil.which("python3") or ""):
        if candidate and Path(candidate).exists() and not _is_current_executable(candidate):
            probed = probe_python(candidate)
            if probed and probed.get("load_extension"):
                return str(probed.get("executable") or candidate)
    return None


def apply_vector_runtime_repair(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    if payload.get("confirm") is not True:
        raise ValueError("repair requires confirm=true")
    action = str(payload.get("action") or "").strip()
    if action not in ACTIONS:
        raise ValueError(f"unknown repair action: {action or '(empty)'}")
    if is_frozen():
        raise ValueError("cannot repair a packaged build in place")
    report = vector_runtime_status(discover=action != "install_sqlite_vec")
    proposed = report["repair"]
    if proposed.get("action") != action:
        raise ValueError(f"action {action} is not valid for this Python; proposed {proposed.get('action')}")

    steps: list[dict[str, Any]] = []
    base = str(proposed.get("base_python") or proposed.get("target_python") or sys.executable)
    isolate = bool(proposed.get("isolate_venv"))

    def _fail(error: str, *, discover: bool) -> dict[str, Any]:
        return {
            "ok": False,
            "action": action,
            "steps": steps,
            "error": error,
            "status": vector_runtime_status(discover=discover),
        }

    def _finish_switch(python: str) -> dict[str, Any]:
        _save_preferred_python(python)
        agent = retarget_macos_launch_agent(python)
        if agent.get("ok") is False and not agent.get("skipped"):
            steps.append(_step(["launch-agent"], ok=False, error=str(agent.get("error") or "launch agent update failed")))
        return {
            "ok": True,
            "action": action,
            "steps": steps,
            "error": "",
            "restart_required": True,
            "restart_command": _restart_command(python, agent),
            "launch_agent": agent,
            "status": vector_runtime_status(discover=True),
        }

    if action == "install_homebrew_python":
        brew = _brew_bin()
        if not brew:
            raise ValueError("Homebrew is not installed")
        brew_cmd = [brew, "install", "python"]
        proc = _run(brew_cmd, timeout=600, extra_env={"HOMEBREW_NO_AUTO_UPDATE": "1", "NONINTERACTIVE": "1"})
        steps.append(
            {
                "command": brew_cmd,
                "returncode": proc.returncode,
                "stdout": (proc.stdout or "")[-2000:],
                "stderr": (proc.stderr or "")[-2000:],
                "ok": proc.returncode == 0,
            }
        )
        if proc.returncode != 0:
            return _fail((proc.stderr or proc.stdout or "brew install python failed")[-280:], discover=True)
        resolved = _resolve_homebrew_python()
        if not resolved:
            return _fail("Homebrew Python was installed but a capable interpreter was not found on PATH.", discover=True)
        base = resolved
        isolate = True

    target, prepared = _prepare_target_python(base, isolate=isolate)
    steps.extend(prepared)
    if prepared and not prepared[-1]["ok"]:
        return _fail(str(prepared[-1].get("error") or "venv create failed"), discover=action != "install_sqlite_vec")
    if not target:
        return _fail("could not prepare a writable Python for sqlite-vec", discover=action != "install_sqlite_vec")

    boot = _ensure_pip(target)
    if not boot.get("skipped"):
        steps.append(boot)
    if not boot["ok"]:
        return _fail(str(boot.get("error") or PIP_MISSING_ERROR), discover=action != "install_sqlite_vec")

    pip = _pip_install(target)
    steps.append(pip)
    if not pip["ok"]:
        return _fail(str(pip.get("error") or _friendly_pip_error(pip.get("stdout") or "", pip.get("stderr") or "")), discover=action != "install_sqlite_vec")

    if action == "install_sqlite_vec" and _is_current_executable(target):
        reset_sqlite_vec_probe()
        status = vector_runtime_status(discover=False)
        return {"ok": bool(status["ok"]), "action": action, "steps": steps, "restart_required": False, "status": status}

    return _finish_switch(target)


def format_vector_runtime_report(report: dict[str, Any] | None = None) -> str:
    """Human-readable probe for installers and ``architectos vector-runtime``."""
    report = report or vector_runtime_status(discover=False)
    current = report.get("current") or {}
    backend = "sqlite-vec" if report.get("ok") else "python"
    lines = [
        f"Memory search backend: {backend}",
        "  sqlite-vec ranks memories by meaning inside SQLite, so Ask/Search stay fast as the graph grows.",
    ]
    if report.get("ok"):
        lines.append("  sqlite-vec is loaded. No action needed.")
        return "\n".join(lines) + "\n"
    lines.append("  Not loaded — ArchitectOS still works via the in-memory Python index (slower with more memories).")
    python = current.get("executable") or sys.executable
    if current.get("frozen"):
        lines.append("  This packaged binary cannot be pip-patched. sqlite-vec is enabled at build time.")
        if not current.get("load_extension"):
            lines.append("  The builder Python could not load SQLite extensions (typical of python.org on macOS).")
        elif not current.get("installed"):
            lines.append('  sqlite-vec was not bundled. Rebuild with: pip install -e ".[vectors,packaging]"')
        lines.append("  Rebuild with Homebrew/conda Python, then reinstall this package.")
    else:
        lines.append(f"  Python: {python}")
        if not current.get("load_extension"):
            lines.append("  This Python cannot load SQLite extensions. Homebrew and conda builds can.")
        elif not current.get("installed"):
            if current.get("pip") is False or current.get("embeddable"):
                lines.append("  sqlite-vec is not installed and this Python may have no pip (Windows embeddable).")
                lines.append("  Rebuild the share package so sqlite-vec is vendored, or use Fix to bootstrap pip.")
            else:
                lines.append('  sqlite-vec is not installed. Confirm Fix in Settings → Memory Embeddings, or: pip install -e ".[vectors]"')
        lines.append("  Settings → Memory Embeddings can check and, after confirmation, apply a fix.")
    return "\n".join(lines) + "\n"


def format_builder_probe(report: dict[str, Any] | None = None) -> str:
    report = report or vector_runtime_status(discover=False)
    current = report.get("current") or {}
    loaded = "yes" if report.get("ok") else "no"
    load_ext = "yes" if current.get("load_extension") else "no"
    installed = "yes" if current.get("installed") else "no"
    lines = [
        "sqlite-vec build probe",
        f"  Python: {current.get('executable') or sys.executable}",
        f"  load_extension: {load_ext}  package: {installed}  loaded: {loaded}",
    ]
    if report.get("ok"):
        lines.append("  Result: sqlite-vec will be collected into the frozen binary.")
    else:
        lines.append("  Result: packaged search will use the Python fallback (app still works).")
        if not current.get("load_extension"):
            lines.append("  To bake sqlite-vec in, rebuild with Homebrew or conda Python:")
            lines.append('    brew install python && /opt/homebrew/bin/python3 -m pip install -e ".[vectors,packaging]"')
        else:
            lines.append('  Install the extra before freezing: pip install -e ".[vectors]"')
        if os.environ.get("ARCHITECTOS_REQUIRE_SQLITE_VEC") == "1":
            lines.append("  ARCHITECTOS_REQUIRE_SQLITE_VEC=1 — this build will fail.")
    return "\n".join(lines) + "\n"


def emit_builder_probe() -> int:
    """Print the freeze-time probe. Exit 1 only when ARCHITECTOS_REQUIRE_SQLITE_VEC=1 and sqlite-vec is not loaded."""
    report = vector_runtime_status(discover=False)
    sys.stderr.write(format_builder_probe(report))
    sys.stderr.flush()
    if os.environ.get("ARCHITECTOS_REQUIRE_SQLITE_VEC") == "1" and not report.get("ok"):
        return 1
    return 0


def cli_vector_runtime(argv: list[str] | None = None) -> int:
    args = list(argv or [])
    json_mode = "--json" in args
    discover = "--discover" in args
    report = vector_runtime_status(discover=discover)
    if json_mode:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        sys.stdout.write(format_vector_runtime_report(report))
    return 0
