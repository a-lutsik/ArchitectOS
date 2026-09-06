from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import DEFAULT_HOST, DEFAULT_PORT
from .models import utc_now
from .paths import resolve_project_root
from .server import ArchitectOSHandler

DEFAULT_PORT_ATTEMPTS = 20
PROJECT_ROOT = resolve_project_root()
RUNTIME_STATE_PATH = PROJECT_ROOT / "data" / "architectos.runtime.json"
APP_BROWSER_NAMES = (
    "msedge",
    "microsoft-edge",
    "chrome",
    "google-chrome",
    "chromium",
    "chromium-browser",
)
WINDOWS_APP_BROWSER_PATHS = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
)
MACOS_APP_BROWSER_PATHS = (
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
)


class ArchitectOSHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def build_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def is_port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def find_available_port(host: str, preferred_port: int, attempts: int = DEFAULT_PORT_ATTEMPTS) -> int:
    if attempts < 1:
        raise ValueError("port attempts must be at least 1")
    for candidate in range(preferred_port, preferred_port + attempts):
        if is_port_available(host, candidate):
            return candidate
    raise RuntimeError(f"no available port found from {preferred_port} to {preferred_port + attempts - 1}")


def build_app_window_command(executable: str, url: str) -> list[str]:
    return [executable, f"--app={url}"]


def find_app_browser() -> str | None:
    candidates: list[str] = list(APP_BROWSER_NAMES)
    if os.name == "nt":
        candidates.extend(str(path) for path in WINDOWS_APP_BROWSER_PATHS)
    elif sys.platform == "darwin":
        # macOS browsers are .app bundles, not PATH entries.
        candidates.extend(str(path) for path in MACOS_APP_BROWSER_PATHS)

    seen: set[str] = set()
    for candidate in candidates:
        executable = candidate if Path(candidate).is_file() else shutil.which(candidate)
        if not executable:
            continue
        executable = str(Path(executable)) if Path(executable).is_file() else executable
        if executable in seen:
            continue
        seen.add(executable)
        return executable
    return None


def open_default_browser(url: str) -> dict[str, Any]:
    opened = webbrowser.open(url)
    return {"mode": "browser", "status": "ok" if opened else "unknown"}


def open_app_window(url: str) -> dict[str, Any]:
    executable = find_app_browser()
    if not executable:
        fallback = open_default_browser(url)
        fallback.update(
            {
                "status": "fallback",
                "message": "Edge/Chrome app mode was not found; opened the default browser.",
            }
        )
        return fallback

    command = build_app_window_command(executable, url)
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"mode": "app-window", "status": "ok", "executable": executable}


def runtime_state_path(project_root: Path = PROJECT_ROOT) -> Path:
    return project_root / "data" / "architectos.runtime.json"


def write_runtime_state(project_root: Path, state: dict[str, Any]) -> dict[str, Any]:
    payload = dict(state)
    payload.setdefault("app", "ArchitectOS")
    payload.setdefault("started_at", utc_now())
    payload["updated_at"] = utc_now()
    path = runtime_state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return payload


def read_runtime_state(project_root: Path = PROJECT_ROOT) -> dict[str, Any] | None:
    path = runtime_state_path(project_root)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def clear_runtime_state(project_root: Path = PROJECT_ROOT) -> None:
    path = runtime_state_path(project_root)
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:  # Windows may not support signal 0; treat as unknown → not alive
        return False
    return True


def _is_architectos_listener(host: str, port: int, timeout: float = 1.5) -> bool:
    """Best-effort probe: does the listener on host:port answer like ArchitectOS?"""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=timeout) as response:
            body = response.read(64 * 1024)
    except Exception:  # noqa: BLE001 - a probe failure only means "not ours"
        return False
    lowered = body.lower()
    return b'name="architectos-token"' in lowered or b"architectos" in lowered


def running_instance_url(host: str, port: int) -> str | None:
    """URL of a live ArchitectOS instance already bound to host:port, else None.

    Single-instance guard: when an ArchitectOS server already answers on the
    requested port we return its URL so callers reuse it instead of starting a
    second full process (which would duplicate background maintenance/ingest
    against the same database).
    """
    if is_port_available(host, port):
        return None  # nothing is listening on the requested port
    state = read_runtime_state(PROJECT_ROOT) or {}
    pid = state.get("pid")
    state_port = int(state.get("port") or 0)
    if isinstance(pid, int) and _pid_is_alive(pid) and state_port == port:
        url = str(state.get("url") or "").strip()
        return url or build_url(host, port)
    if _is_architectos_listener(host, port):
        return build_url(host, port)
    return None


def create_server(host: str, port: int) -> ArchitectOSHTTPServer:
    return ArchitectOSHTTPServer((host, port), ArchitectOSHandler)


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    strict_port: bool = False,
    port_attempts: int = DEFAULT_PORT_ATTEMPTS,
    app_window: bool = False,
) -> int:
    existing_url = running_instance_url(host, port)
    if existing_url is not None:
        print(
            f"[launcher] pid={os.getpid()} reusing already-running ArchitectOS instance "
            f"at {existing_url}; NOT starting a second server (single-instance)."
        )
        if open_browser:
            launch = open_app_window(existing_url) if app_window else open_default_browser(existing_url)
            if launch["mode"] == "app-window":
                print("Opened ArchitectOS in an app window.")
            elif launch["status"] == "fallback":
                print(launch["message"])
        return 0

    if strict_port:
        if not is_port_available(host, port):
            raise RuntimeError(f"port {port} is already in use on {host}")
        selected_port = port
        print(f"[launcher] pid={os.getpid()} strict: binding to port {selected_port}")
    else:
        selected_port = find_available_port(host, port, port_attempts)
        if selected_port != port:
            print(
                f"[launcher] WARNING pid={os.getpid()}: preferred port {port} is occupied "
                f"by a non-ArchitectOS process; starting on port {selected_port} instead. "
                f"Stop that process to reclaim port {port}."
            )
        else:
            print(f"[launcher] pid={os.getpid()}: port {port} free; binding to it")

    server = create_server(host, selected_port)
    from .server import ArchitectOSHandler

    ArchitectOSHandler.get_service().start_background_maintenance()
    ArchitectOSHandler.get_service().schedule_startup_memory_rescan()
    url = build_url(host, selected_port)
    launch_mode = "none"
    if open_browser:
        launch_mode = "app-window" if app_window else "browser"
    write_runtime_state(
        PROJECT_ROOT,
        {
            "host": host,
            "port": selected_port,
            "preferred_port": port,
            "url": url,
            "pid": os.getpid(),
            "state_file": str(RUNTIME_STATE_PATH),
            "launch_mode": launch_mode,
            "auth_token": ArchitectOSHandler.get_service().auth_token,
        },
    )

    print(f"[launcher] pid={os.getpid()} ArchitectOS running at {url}")
    if selected_port != port:
        print(f"[launcher] Preferred port {port} was busy; using {selected_port}.")
    print(f"Runtime state: {RUNTIME_STATE_PATH}")
    print("Press Ctrl+C to stop.")

    if open_browser:
        launch_url = f"{url}/?v={os.getpid()}" if app_window else url
        launch = open_app_window(launch_url) if app_window else open_default_browser(launch_url)
        if launch["mode"] == "app-window":
            print("Opened ArchitectOS in an app window.")
        elif launch["status"] == "fallback":
            print(launch["message"])

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        clear_runtime_state(PROJECT_ROOT)
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the ArchitectOS local desktop shell.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host to bind. Defaults to 127.0.0.1.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Preferred local port.")
    parser.add_argument("--strict-port", action="store_true", help="Fail instead of selecting the next free port.")
    parser.add_argument("--no-browser", action="store_true", help="Start the server without opening a browser.")
    parser.add_argument("--app-window", action="store_true", help="Open Edge/Chrome app mode instead of a browser tab.")
    parser.add_argument("--port-attempts", type=int, default=DEFAULT_PORT_ATTEMPTS, help="Number of ports to try when strict mode is off.")
    return parser


def main(argv: list[str] | None = None) -> int:
    from .ssl_util import configure_default_ssl

    configure_default_ssl()
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "hook":
        from .agent_hooks import main as hook_main

        return hook_main(raw[1:])
    if raw and raw[0] in {"vector-runtime", "sqlite-vec"}:
        from .vec_runtime import cli_vector_runtime

        return cli_vector_runtime(raw[1:])
    from .vec_runtime import maybe_reexec_preferred_python

    maybe_reexec_preferred_python()
    args = build_parser().parse_args(raw)
    return serve(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        strict_port=args.strict_port,
        port_attempts=args.port_attempts,
        app_window=args.app_window,
    )
