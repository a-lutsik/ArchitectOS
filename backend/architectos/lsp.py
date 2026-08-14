from __future__ import annotations

import json
import os
import ast
import platform
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# LSP SymbolKind numeric codes -> human labels (subset used for display).
SYMBOL_KINDS = {
    1: "File", 2: "Module", 3: "Namespace", 4: "Package", 5: "Class", 6: "Method",
    7: "Property", 8: "Field", 9: "Constructor", 10: "Enum", 11: "Interface",
    12: "Function", 13: "Variable", 14: "Constant", 15: "String", 16: "Number",
    17: "Boolean", 18: "Array", 19: "Object", 20: "Key", 21: "Null", 22: "EnumMember",
    23: "Struct", 24: "Event", 25: "Operator", 26: "TypeParameter",
}

# Stale prose values persisted before install_command became a real shell command.
STALE_INSTALL_COMMANDS = {
    "Install Eclipse JDT LS (jdtls) and add it to PATH",
}


def _which(executable: str, path: str | None = None) -> str | None:
    return shutil.which(executable, path=path)


def language_server_path_dirs() -> list[str]:
    """Extra dirs where package managers commonly drop language-server shims."""
    dirs: list[str] = []
    home = Path.home()
    candidates = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(home / ".local" / "bin"),
        str(home / ".npm-global" / "bin"),
        str(home / "AppData" / "Roaming" / "npm"),
        str(home / ".cargo" / "bin"),
    ]
    go_bin = _which("go")
    if go_bin:
        try:
            proc = subprocess.run([go_bin, "env", "GOPATH"], capture_output=True, text=True, timeout=5, check=False)
            gopath = (proc.stdout or "").strip()
            if gopath:
                candidates.append(str(Path(gopath) / "bin"))
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        candidates.append(str(home / "go" / "bin"))
    for item in candidates:
        if item and item not in dirs and Path(item).is_dir():
            dirs.append(item)
    return dirs


def augmented_path() -> str:
    current = os.environ.get("PATH") or ""
    parts = [item for item in current.split(os.pathsep) if item]
    for extra in language_server_path_dirs():
        if extra not in parts:
            parts.insert(0, extra)
    return os.pathsep.join(parts)


def find_executable(name: str) -> str | None:
    return _which(name, path=augmented_path())


def default_install_command(server_id: str) -> str:
    """Return a runnable shell command for the current OS/toolchain."""
    sid = (server_id or "").strip().lower()
    system = platform.system().lower()
    if sid == "python":
        if find_executable("npm"):
            return "npm install -g pyright"
        return "Install Node.js/npm, then run: npm install -g pyright"
    if sid == "typescript":
        if find_executable("npm"):
            return "npm install -g typescript-language-server typescript"
        return "Install Node.js/npm, then run: npm install -g typescript-language-server typescript"
    if sid == "go":
        if find_executable("go"):
            return "go install golang.org/x/tools/gopls@latest"
        return "Install Go, then run: go install golang.org/x/tools/gopls@latest"
    if sid == "rust":
        if find_executable("rustup"):
            return "rustup component add rust-analyzer"
        return "Install rustup, then run: rustup component add rust-analyzer"
    if sid == "java":
        if find_executable("brew"):
            return "HOMEBREW_NO_AUTO_UPDATE=1 brew install jdtls"
        if system == "windows":
            if find_executable("scoop"):
                return "scoop bucket add extras; scoop install jdtls"
            return "Install Scoop, then run: scoop bucket add extras; scoop install jdtls"
        if system == "linux":
            if find_executable("brew"):
                return "HOMEBREW_NO_AUTO_UPDATE=1 brew install jdtls"
            return "Install Homebrew, then run: brew install jdtls"
        return "Install Homebrew, then run: brew install jdtls"
    return ""


def is_runnable_install_command(command: str) -> bool:
    text = (command or "").strip()
    if not text:
        return False
    if text in STALE_INSTALL_COMMANDS:
        return False
    # Prose instructions start with "Install " and are not shell commands.
    if re.match(r"(?i)^install\s+[A-Za-z].* then ", text):
        return False
    if re.match(r"(?i)^install\s+(eclipse|node|go|rustup|homebrew|scoop)\b", text) and "npm " not in text and "brew " not in text and "go install" not in text and "rustup " not in text and "scoop " not in text:
        return False
    return True


def resolve_install_command(server_id: str, stored: str = "") -> str:
    stored = (stored or "").strip()
    preferred = default_install_command(server_id)
    if not stored:
        return preferred
    if not is_runnable_install_command(stored):
        return preferred
    # Drop cross-platform leftovers (e.g. brew command persisted on Windows).
    system = platform.system().lower()
    lowered = stored.lower()
    if system == "windows" and ("brew install" in lowered or lowered.startswith("homebrew_")):
        return preferred
    if system != "windows" and ("scoop " in lowered or "winget " in lowered):
        return preferred
    return stored


def install_prerequisites(server_id: str) -> dict[str, Any]:
    sid = (server_id or "").strip().lower()
    if sid in {"python", "typescript"}:
        ok = bool(find_executable("npm"))
        return {"ok": ok, "tool": "npm", "message": "" if ok else "Node.js/npm is required. Install Node.js, then retry."}
    if sid == "go":
        ok = bool(find_executable("go"))
        return {"ok": ok, "tool": "go", "message": "" if ok else "Go is required. Install Go, then retry."}
    if sid == "rust":
        ok = bool(find_executable("rustup"))
        return {"ok": ok, "tool": "rustup", "message": "" if ok else "rustup is required. Install Rust via rustup, then retry."}
    if sid == "java":
        if find_executable("brew") or find_executable("scoop"):
            return {"ok": True, "tool": "package-manager", "message": ""}
        system = platform.system().lower()
        if system == "windows":
            return {"ok": False, "tool": "scoop", "message": "Scoop is required to install jdtls automatically on Windows."}
        return {"ok": False, "tool": "brew", "message": "Homebrew is required to install jdtls automatically on macOS/Linux."}
    return {"ok": True, "tool": "", "message": ""}


INSTALL_COMMANDS = {
    "python": default_install_command("python"),
    "typescript": default_install_command("typescript"),
    "go": default_install_command("go"),
    "rust": default_install_command("rust"),
    "java": default_install_command("java"),
}


@dataclass(slots=True)
class LSPServerConfig:
    id: str
    label: str
    language_id: str
    extensions: list[str]
    command: list[str]
    enabled: bool = True
    status: str = "planned"
    notes: str = ""
    install_command: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LSPServerConfig":
        command = data.get("command")
        if isinstance(command, str):
            command = command.split()
        server_id = str(data.get("id") or "")
        return cls(
            id=server_id,
            label=str(data.get("label") or data.get("id") or ""),
            language_id=str(data.get("language_id") or data.get("id") or ""),
            extensions=[str(item).lower() for item in (data.get("extensions") or [])],
            command=[str(part) for part in (command or []) if str(part)],
            enabled=bool(data.get("enabled", True)),
            status=str(data.get("status") or "planned"),
            notes=str(data.get("notes") or ""),
            install_command=resolve_install_command(server_id, str(data.get("install_command") or "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "language_id": self.language_id,
            "extensions": list(self.extensions),
            "command": list(self.command),
            "enabled": self.enabled,
            "status": self.status,
            "notes": self.notes,
            "install_command": resolve_install_command(self.id, self.install_command),
            "install_runnable": is_runnable_install_command(resolve_install_command(self.id, self.install_command)),
        }



class LSPError(RuntimeError):
    pass


class LSPClient:
    """JSON-RPC client speaking the LSP stdio transport (Content-Length framed messages)."""

    def __init__(self, command: list[str], root: Path, timeout: float = 25.0) -> None:
        self.command = command
        self.root = root
        self.timeout = timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._responses: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._stderr: list[str] = []
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._next_id = 0

    def __enter__(self) -> "LSPClient":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def start(self) -> None:
        if not self.command:
            raise LSPError("LSP command is not configured.")
        executable = find_executable(self.command[0])
        if not executable:
            raise LSPError(f"Language server not found on PATH: {self.command[0]}")
        env = dict(os.environ)
        env["PATH"] = augmented_path()
        try:
            self._process = subprocess.Popen(
                [executable, *self.command[1:]],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.root),
                env=env,
                shell=False,
            )
        except OSError as exc:
            raise LSPError(f"Failed to launch language server: {exc}") from exc
        self._stdout_thread = threading.Thread(target=self._pump_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._pump_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _pump_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        stream = self._process.stdout
        while True:
            headers: dict[str, str] = {}
            while True:
                line = stream.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").strip()
                if text == "":
                    break
                if ":" in text:
                    key, _, value = text.partition(":")
                    headers[key.strip().lower()] = value.strip()
            length = int(headers.get("content-length") or 0)
            if length <= 0:
                continue
            body = stream.read(length)
            if not body:
                return
            try:
                message = json.loads(body.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            message_id = message.get("id")
            if isinstance(message_id, int):
                with self._lock:
                    self._responses[message_id] = message
                self._event.set()

    def _pump_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        for line in self._process.stderr:
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self._stderr.append(text)
                del self._stderr[:-40]

    def _write(self, message: dict[str, Any]) -> None:
        assert self._process is not None and self._process.stdin is not None
        body = json.dumps(message).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        try:
            self._process.stdin.write(header + body)
            self._process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise LSPError(f"Failed to write to language server: {exc}") from exc

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict[str, Any]) -> Any:
        self._next_id += 1
        message_id = self._next_id
        self._write({"jsonrpc": "2.0", "id": message_id, "method": method, "params": params})
        end = time.monotonic() + self.timeout
        response: dict[str, Any] | None = None
        while time.monotonic() < end:
            with self._lock:
                if message_id in self._responses:
                    response = self._responses.pop(message_id)
                    break
            if self._process and self._process.poll() is not None:
                raise LSPError(f"Language server exited: {self._stderr[-1] if self._stderr else 'no stderr'}")
            self._event.wait(0.1)
            self._event.clear()
        if response is None:
            raise LSPError(f"LSP request '{method}' timed out after {self.timeout}s.")
        if response.get("error"):
            error = response["error"]
            raise LSPError(f"LSP error {error.get('code')}: {error.get('message')}")
        return response.get("result")

    def initialize(self) -> dict[str, Any]:
        root_uri = self.root.as_uri()
        result = self.request("initialize", {
            "processId": os.getpid(),
            "clientInfo": {"name": "ArchitectOS", "version": "1.0.0"},
            "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": self.root.name}],
            "capabilities": {
                "textDocument": {
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    "hover": {"contentFormat": ["plaintext", "markdown"]},
                    "references": {"dynamicRegistration": False},
                    "synchronization": {"didSave": True},
                },
            },
        })
        self.notify("initialized", {})
        return result or {}

    def did_open(self, path: Path, language_id: str, text: str) -> None:
        self.notify("textDocument/didOpen", {
            "textDocument": {"uri": path.as_uri(), "languageId": language_id, "version": 1, "text": text},
        })

    def document_symbols(self, path: Path) -> list[dict[str, Any]]:
        result = self.request("textDocument/documentSymbol", {"textDocument": {"uri": path.as_uri()}})
        return list(result or [])

    def hover(self, path: Path, line: int, character: int) -> dict[str, Any]:
        result = self.request("textDocument/hover", {
            "textDocument": {"uri": path.as_uri()},
            "position": {"line": max(0, line), "character": max(0, character)},
        })
        return dict(result or {})

    def references(self, path: Path, line: int, character: int) -> list[dict[str, Any]]:
        result = self.request("textDocument/references", {
            "textDocument": {"uri": path.as_uri()},
            "position": {"line": max(0, line), "character": max(0, character)},
            "context": {"includeDeclaration": True},
        })
        return list(result or [])

    def close(self) -> None:
        if not self._process:
            return
        process = self._process
        try:
            if process.poll() is None:
                try:
                    self.request("shutdown", {})
                    self.notify("exit", {})
                except LSPError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.terminate()
        finally:
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            self._close_process_pipes(process)
            self._join_reader_threads()
            self._process = None

    def _close_process_pipes(self, process: subprocess.Popen[bytes]) -> None:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass

    def _join_reader_threads(self) -> None:
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread and thread.is_alive():
                thread.join(timeout=1)

    @property
    def stderr(self) -> str:
        return "\n".join(self._stderr[-8:])


def flatten_symbols(symbols: list[dict[str, Any]], depth: int = 0) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for symbol in symbols:
        # DocumentSymbol has "range"/"selectionRange"; SymbolInformation has "location".
        location = symbol.get("location") or {}
        rng = symbol.get("range") or location.get("range") or {}
        start = (rng.get("start") or {})
        flat.append({
            "name": symbol.get("name") or "",
            "kind": SYMBOL_KINDS.get(int(symbol.get("kind") or 0), str(symbol.get("kind") or "")),
            "detail": symbol.get("detail") or "",
            "line": int(start.get("line") or 0) + 1,
            "depth": depth,
            "container": symbol.get("containerName") or "",
        })
        for child in flatten_symbols(symbol.get("children") or [], depth + 1):
            flat.append(child)
    return flat


class CodeIntelligenceManager:
    """Owns the language server registry and short-lived code-intelligence sessions."""

    def __init__(self, project_root: Path, load_servers, save_servers) -> None:
        self.project_root = project_root
        self._load_servers = load_servers
        self._save_servers = save_servers

    def list_servers(self) -> list[dict[str, Any]]:
        servers = [LSPServerConfig.from_dict(item).to_dict() for item in self._load_servers()]
        self._persist_resolved_install_commands(servers)
        return servers

    def _persist_resolved_install_commands(self, servers: list[dict[str, Any]]) -> None:
        raw = self._load_servers()
        changed = False
        resolved_by_id = {str(item.get("id") or ""): str(item.get("install_command") or "") for item in servers}
        for item in raw:
            server_id = str(item.get("id") or "")
            resolved = resolved_by_id.get(server_id) or resolve_install_command(server_id, str(item.get("install_command") or ""))
            if str(item.get("install_command") or "") != resolved:
                item["install_command"] = resolved
                changed = True
        if changed:
            self._save_servers(raw)

    def get_server(self, server_id: str) -> LSPServerConfig | None:
        for item in self._load_servers():
            config = LSPServerConfig.from_dict(item)
            if config.id == server_id:
                return config
        return None

    def server_for_extension(self, extension: str) -> LSPServerConfig | None:
        extension = extension.lower()
        for item in self._load_servers():
            config = LSPServerConfig.from_dict(item)
            if config.enabled and extension in config.extensions:
                return config
        return None

    def upsert_server(self, payload: dict[str, Any]) -> dict[str, Any]:
        server_id = str(payload.get("id") or "").strip()
        if not server_id:
            raise LSPError("Language server id is required.")
        servers = self._load_servers()
        updated = False
        for index, item in enumerate(servers):
            if str(item.get("id")) == server_id:
                servers[index] = LSPServerConfig.from_dict({**item, **payload}).to_dict()
                updated = True
                break
        if not updated:
            servers.append(LSPServerConfig.from_dict(payload).to_dict())
        self._save_servers(servers)
        return self.get_server(server_id).to_dict()  # type: ignore[union-attr]

    def check(self, server_id: str) -> dict[str, Any]:
        config = self.get_server(server_id)
        if not config:
            raise LSPError("Language server not found.")
        install_command = resolve_install_command(config.id, config.install_command)
        if not config.command:
            return {
                "id": server_id,
                "ready": False,
                "status": "missing_command",
                "message": "No command configured.",
                "install_command": install_command,
                "install_runnable": is_runnable_install_command(install_command),
            }
        executable = find_executable(config.command[0])
        if not executable:
            self._mark_status(server_id, "missing_executable")
            return {
                "id": server_id,
                "ready": False,
                "status": "missing_executable",
                "message": f"Language server not found: {config.command[0]}",
                "hint": config.notes,
                "install_command": install_command,
                "install_runnable": is_runnable_install_command(install_command),
            }
        self._mark_status(server_id, "available")
        return {
            "id": server_id,
            "ready": True,
            "status": "available",
            "message": f"Language server available: {executable}",
            "path": executable,
            "install_command": install_command,
            "install_runnable": is_runnable_install_command(install_command),
        }

    def install(self, server_id: str, *, timeout_seconds: int = 600) -> dict[str, Any]:
        config = self.get_server(server_id)
        if not config:
            raise LSPError("Language server not found.")
        command = resolve_install_command(config.id, config.install_command)
        if not is_runnable_install_command(command):
            raise LSPError(f"No runnable install command for {server_id}. {command or 'Configure install_command first.'}")
        prereq = install_prerequisites(config.id)
        if not prereq.get("ok"):
            return {
                "id": server_id,
                "status": "missing_prerequisite",
                "ready": False,
                "command": command,
                "message": prereq.get("message") or f"Missing prerequisite: {prereq.get('tool')}",
                "install_command": command,
                "stdout": "",
                "stderr": "",
            }
        # Persist resolved command so UI / advanced panel stay in sync.
        if config.install_command != command:
            self.upsert_server({"id": server_id, "install_command": command})

        if os.name == "nt":
            shell = ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]
        else:
            shell_bin = find_executable("bash") or find_executable("sh") or "/bin/sh"
            shell = [shell_bin, "-lc"]
        env = dict(os.environ)
        env["PATH"] = augmented_path()
        timeout = min(max(int(timeout_seconds or 600), 30), 900)
        try:
            proc = subprocess.run(
                [*shell, command],
                cwd=str(self.project_root),
                text=True,
                capture_output=True,
                timeout=timeout,
                shell=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            check = self.check(server_id)
            return {
                "id": server_id,
                "status": "timeout",
                "ready": bool(check.get("ready")),
                "command": command,
                "message": f"Install timed out after {timeout}s.",
                "stdout": (exc.stdout or "")[:40_000] if isinstance(exc.stdout, str) else "",
                "stderr": (exc.stderr or "")[:20_000] if isinstance(exc.stderr, str) else "",
                "install_command": command,
                "check": check,
            }
        except OSError as exc:
            return {
                "id": server_id,
                "status": "unavailable",
                "ready": False,
                "command": command,
                "message": f"Failed to run install command: {exc}",
                "stdout": "",
                "stderr": str(exc),
                "install_command": command,
            }

        check = self.check(server_id)
        ready = bool(check.get("ready"))
        if proc.returncode == 0 and ready:
            status = "ok"
            message = f"Installed and detected {config.command[0]}."
        elif proc.returncode == 0 and not ready:
            status = "installed_not_on_path"
            message = (
                f"Install finished, but `{config.command[0]}` is still not on PATH for ArchitectOS. "
                "Restart ArchitectOS or add the package-manager bin directory to PATH."
            )
        else:
            status = "error"
            message = f"Install command failed with exit {proc.returncode}."
        return {
            "id": server_id,
            "status": status,
            "ready": ready,
            "command": command,
            "message": message,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[:40_000],
            "stderr": (proc.stderr or "")[:20_000],
            "install_command": command,
            "check": check,
        }

    def symbols(self, root: Path, relative_path: str, text: str, extension: str) -> dict[str, Any]:
        config = self.server_for_extension(extension)
        if not config or not config.command or not find_executable(config.command[0]):
            fallback = fallback_symbols(relative_path, text, extension)
            if fallback is None:
                if not config:
                    raise LSPError(f"No enabled language server handles '{extension}' files.")
                raise LSPError(f"Language server not installed: {config.command[0]}")
            message = "Fallback parser used because no language server is configured."
            if config and config.command:
                message = f"Fallback parser used because '{config.command[0]}' is not installed."
            return {
                "language": fallback["language"],
                "server": config.id if config else "fallback",
                "path": relative_path,
                "symbols": fallback["symbols"],
                "count": len(fallback["symbols"]),
                "source": "fallback",
                "fallback": True,
                "message": message,
                "install_command": config.install_command if config else "",
            }
        path = (root / relative_path).resolve()
        with LSPClient(config.command, root) as client:
            client.initialize()
            client.did_open(path, config.language_id, text)
            symbols = client.document_symbols(path)
        flat = flatten_symbols(symbols)
        return {"language": config.language_id, "server": config.id, "path": relative_path, "symbols": flat, "count": len(flat), "source": "lsp", "fallback": False}

    def hover(self, root: Path, relative_path: str, text: str, extension: str, line: int, character: int) -> dict[str, Any]:
        config = self.server_for_extension(extension)
        if not config or not config.command or not find_executable(config.command[0]):
            hover = fallback_hover(relative_path, text, extension, line, character)
            if hover is None:
                if not config:
                    raise LSPError(f"No enabled language server handles '{extension}' files.")
                raise LSPError(f"Language server not installed: {config.command[0]}")
            return {**hover, "server": config.id if config else "fallback", "fallback": True, "source": "fallback", "install_command": config.install_command if config else ""}
        path = (root / relative_path).resolve()
        with LSPClient(config.command, root) as client:
            client.initialize()
            client.did_open(path, config.language_id, text)
            hover = client.hover(path, line, character)
        contents = hover.get("contents")
        rendered = _render_hover(contents)
        return {"language": config.language_id, "server": config.id, "path": relative_path, "line": line, "character": character, "hover": rendered, "source": "lsp", "fallback": False}

    def diagnostics(self, relative_path: str, text: str, extension: str) -> dict[str, Any]:
        fallback = fallback_diagnostics(relative_path, text, extension)
        if fallback is None:
            config = self.server_for_extension(extension)
            return {
                "language": config.language_id if config else extension.lstrip("."),
                "server": config.id if config else "none",
                "path": relative_path,
                "diagnostics": [],
                "count": 0,
                "source": "basic",
                "message": "No fallback diagnostics are available for this language.",
            }
        return fallback

    def references(self, root: Path, relative_path: str, text: str, extension: str, line: int, character: int, query: str = "") -> dict[str, Any]:
        config = self.server_for_extension(extension)
        if config and config.command and find_executable(config.command[0]):
            path = (root / relative_path).resolve()
            with LSPClient(config.command, root) as client:
                client.initialize()
                client.did_open(path, config.language_id, text)
                refs = client.references(path, line, character)
            references = []
            for ref in refs:
                uri = str(ref.get("uri") or "")
                rng = ref.get("range") or {}
                start = rng.get("start") or {}
                references.append({
                    "path": uri.rsplit("/", 1)[-1] if uri else relative_path,
                    "line": int(start.get("line") or 0) + 1,
                    "character": int(start.get("character") or 0),
                    "preview": "",
                })
            return {"language": config.language_id, "server": config.id, "path": relative_path, "references": references, "count": len(references), "source": "lsp", "fallback": False}
        fallback = fallback_references(relative_path, text, extension, line, character, query)
        if fallback is None:
            raise LSPError(f"No references provider handles '{extension}' files.")
        return {**fallback, "server": config.id if config else "fallback", "fallback": True, "source": "fallback", "install_command": config.install_command if config else ""}

    def _mark_status(self, server_id: str, status: str) -> None:
        servers = self._load_servers()
        for item in servers:
            if str(item.get("id")) == server_id:
                item["status"] = status
        self._save_servers(servers)


def _render_hover(contents: Any) -> str:
    if contents is None:
        return ""
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        return str(contents.get("value") or "")
    if isinstance(contents, list):
        parts = []
        for item in contents:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("value") or ""))
        return "\n\n".join(part for part in parts if part)
    return str(contents)


def fallback_symbols(relative_path: str, text: str, extension: str) -> dict[str, Any] | None:
    if extension == ".py":
        return {"language": "python", "symbols": _python_symbols(text)}
    if extension in {".js", ".jsx", ".ts", ".tsx"}:
        return {"language": "javascript" if extension in {".js", ".jsx"} else "typescript", "symbols": _regex_symbols(text, _JS_SYMBOL_PATTERNS)}
    if extension == ".java":
        return {"language": "java", "symbols": _regex_symbols(text, _JAVA_SYMBOL_PATTERNS)}
    return None


def fallback_hover(relative_path: str, text: str, extension: str, line: int, character: int) -> dict[str, Any] | None:
    symbols = fallback_symbols(relative_path, text, extension)
    if symbols is None:
        return None
    word = _word_at(text, line, character)
    line_text = _line_at(text, line).strip()
    hover = word or line_text
    if word and line_text:
        hover = f"{word}\n{line_text}"
    return {"language": symbols["language"], "path": relative_path, "line": line, "character": character, "hover": hover}


def fallback_diagnostics(relative_path: str, text: str, extension: str) -> dict[str, Any] | None:
    language = _language_for_extension(extension)
    if not language:
        return None
    diagnostics: list[dict[str, Any]] = []
    if extension == ".py":
        try:
            ast.parse(text or "\n", filename=relative_path)
        except SyntaxError as exc:
            diagnostics.append({
                "severity": "error",
                "message": exc.msg,
                "line": int(exc.lineno or 1),
                "character": int(exc.offset or 1) - 1,
            })
    return {"language": language, "server": "fallback", "path": relative_path, "diagnostics": diagnostics, "count": len(diagnostics), "source": "fallback"}


def fallback_references(relative_path: str, text: str, extension: str, line: int, character: int, query: str = "") -> dict[str, Any] | None:
    language = _language_for_extension(extension)
    if not language:
        return None
    needle = (query or _word_at(text, line, character)).strip()
    if not needle:
        return {"language": language, "path": relative_path, "query": "", "references": [], "count": 0}
    pattern = re.compile(rf"\b{re.escape(needle)}\b")
    references = []
    for index, value in enumerate(text.splitlines(), start=1):
        for match in pattern.finditer(value):
            references.append({"path": relative_path, "line": index, "character": match.start(), "preview": value.strip()[:240]})
            if len(references) >= 80:
                break
        if len(references) >= 80:
            break
    return {"language": language, "path": relative_path, "query": needle, "references": references, "count": len(references)}


def _python_symbols(text: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(text or "\n")
    except SyntaxError:
        return []
    symbols: list[dict[str, Any]] = []

    def visit(node: ast.AST, depth: int = 0) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "Class" if isinstance(child, ast.ClassDef) else "Function"
                symbols.append({"name": child.name, "kind": kind, "detail": "", "line": child.lineno, "depth": depth, "container": ""})
                visit(child, depth + 1)

    visit(tree)
    return symbols


def _regex_symbols(text: str, patterns: list[tuple[re.Pattern[str], str]]) -> list[dict[str, Any]]:
    symbols: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines(), start=1):
        for pattern, kind in patterns:
            match = pattern.search(line)
            if match:
                name = match.group("name")
                symbols.append({"name": name, "kind": kind, "detail": line.strip()[:120], "line": index, "depth": 0, "container": ""})
                break
    return symbols


_JS_SYMBOL_PATTERNS = [
    (re.compile(r"\bclass\s+(?P<name>[A-Za-z_$][\w$]*)"), "Class"),
    (re.compile(r"\bfunction\s+(?P<name>[A-Za-z_$][\w$]*)\s*\("), "Function"),
    (re.compile(r"\b(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"), "Function"),
    (re.compile(r"\bexport\s+(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\s*\("), "Function"),
]

_JAVA_SYMBOL_PATTERNS = [
    (re.compile(r"\b(?:class|interface|enum|record)\s+(?P<name>[A-Za-z_]\w*)"), "Class"),
    (re.compile(r"\b(?:public|private|protected)?\s*(?:static\s+)?(?:final\s+)?[\w<>\[\], ?]+\s+(?P<name>[A-Za-z_]\w*)\s*\([^;]*\)\s*(?:throws\s+[\w.,\s]+)?\{?"), "Method"),
]


def _language_for_extension(extension: str) -> str:
    return {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".java": "java",
    }.get(extension.lower(), "")


def _line_at(text: str, line: int) -> str:
    lines = text.splitlines()
    if 0 <= line < len(lines):
        return lines[line]
    return ""


def _word_at(text: str, line: int, character: int) -> str:
    value = _line_at(text, line)
    if not value:
        return ""
    character = max(0, min(character, len(value)))
    left = character
    right = character
    while left > 0 and re.match(r"[\w$]", value[left - 1]):
        left -= 1
    while right < len(value) and re.match(r"[\w$]", value[right]):
        right += 1
    return value[left:right]
