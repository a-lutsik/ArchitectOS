from __future__ import annotations

import atexit
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .constants import SECRET_MASK
from .netutil import outbound_policy, validate_outbound_url
from .ssl_util import urlopen

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "ArchitectOS", "version": "1.0.0"}
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


@dataclass(slots=True)
class MCPServerConfig:
    id: str
    label: str
    command: list[str]
    url: str = ""
    enabled: bool = False
    approval_required: bool = True
    allow_local: bool = False
    transport: str = "stdio"
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    auth: dict[str, Any] = field(default_factory=dict)
    status: str = "planned"
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MCPServerConfig":
        command = data.get("command")
        if isinstance(command, str):
            command = command.split()
        transport = str(data.get("transport") or "stdio").strip().lower()
        if transport in {"remote http", "remote-http"}:
            transport = "http"
        return cls(
            id=str(data.get("id") or ""),
            label=str(data.get("label") or data.get("id") or ""),
            command=[str(part) for part in (command or []) if str(part)],
            url=str(data.get("url") or ""),
            enabled=bool(data.get("enabled")),
            approval_required=bool(data.get("approval_required", True)),
            allow_local=bool(data.get("allow_local")),
            transport=transport,
            headers={str(key): str(value) for key, value in dict(data.get("headers") or {}).items()},
            env={str(key): str(value) for key, value in dict(data.get("env") or {}).items()},
            auth=dict(data.get("auth") or {}),
            status=str(data.get("status") or "planned"),
            notes=str(data.get("notes") or ""),
        )

    def to_dict(self, include_secrets: bool = True) -> dict[str, Any]:
        if include_secrets:
            headers = dict(self.headers)
            env = dict(self.env)
        else:
            # Keys stay visible so editors can round-trip the config; values are masked.
            headers = {key: SECRET_MASK for key in self.headers}
            env = {key: SECRET_MASK for key in self.env}
        payload = {
            "id": self.id,
            "label": self.label,
            "command": list(self.command),
            "url": self.url,
            "enabled": self.enabled,
            "approval_required": self.approval_required,
            "allow_local": self.allow_local,
            "transport": self.transport,
            "headers": headers,
            "env": env,
            "status": self.status,
            "notes": self.notes,
        }
        if include_secrets:
            payload["auth"] = dict(self.auth)
        elif self.auth:
            auth = dict(self.auth)
            payload["auth"] = {
                "status": auth.get("status") or ("authorized" if auth.get("access_token") else ""),
                "expires_at": auth.get("expires_at") or 0,
                "has_access_token": bool(auth.get("access_token")),
                "has_refresh_token": bool(auth.get("refresh_token")),
            }
        else:
            payload["auth"] = {}
        return payload


class MCPError(RuntimeError):
    pass


def _allow_local_remote_urls(config: MCPServerConfig) -> bool:
    if config.allow_local:
        return True
    return os.environ.get("ARCHITECTOS_MCP_ALLOW_LOCAL", "").strip().lower() in {"1", "true", "yes"}


def _validate_remote_url(config: MCPServerConfig) -> None:
    """SSRF guard: remote MCP URLs must not reach loopback/link-local hosts unless opted in."""
    try:
        validate_outbound_url(config.url, allow_local=_allow_local_remote_urls(config))
    except ValueError as exc:
        raise MCPError(f"Remote MCP URL is not allowed: {exc}") from exc


class MCPClient:
    """Minimal JSON-RPC 2.0 client speaking the MCP stdio transport (newline-delimited JSON)."""

    def __init__(self, config: MCPServerConfig, cwd: Path, timeout: float = 90.0) -> None:
        self.config = config
        self.cwd = cwd
        self.timeout = timeout
        self._process: subprocess.Popen[str] | None = None
        self._responses: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._event = threading.Event()
        self._reader: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr: list[str] = []
        self._next_id = 0

    def __enter__(self) -> "MCPClient":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def start(self) -> None:
        if not self.config.command:
            raise MCPError("MCP command is not configured.")
        env = prefer_modern_node_env(dict(os.environ))
        env.update({key: value for key, value in self.config.env.items() if value})
        # Re-resolve against the modern-node PATH so npx/node are not the stale
        # /usr/local Node 18 that breaks @azure-devops/mcp (needs Node >= 20).
        executable = shutil.which(self.config.command[0], path=env.get("PATH"))
        if not executable:
            executable = shutil.which(self.config.command[0])
        if not executable:
            raise MCPError(f"MCP executable not found on PATH: {self.config.command[0]}")
        command = [executable, *self.config.command[1:]]
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self.cwd),
                env=env,
                bufsize=1,
                shell=False,
            )
        except OSError as exc:
            raise MCPError(f"Failed to launch MCP server: {exc}") from exc
        self._reader = threading.Thread(target=self._pump_stdout, daemon=True)
        self._reader.start()
        self._stderr_thread = threading.Thread(target=self._pump_stderr, daemon=True)
        self._stderr_thread.start()

    def _pump_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
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
            if line.strip():
                self._stderr.append(line.strip())
                del self._stderr[:-40]

    def _send(self, method: str, params: dict[str, Any] | None = None, notify: bool = False) -> int | None:
        assert self._process is not None and self._process.stdin is not None
        with self._send_lock:
            message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                message["params"] = params
            message_id: int | None = None
            if not notify:
                self._next_id += 1
                message_id = self._next_id
                message["id"] = message_id
            try:
                self._process.stdin.write(json.dumps(message) + "\n")
                self._process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise MCPError(f"Failed to write to MCP server: {exc}") from exc
            return message_id

    def _await(self, message_id: int, timeout: float | None = None) -> dict[str, Any]:
        effective_timeout = timeout if timeout and timeout > 0 else self.timeout
        end = time.monotonic() + effective_timeout
        response: dict[str, Any] | None = None
        while time.monotonic() < end:
            with self._lock:
                if message_id in self._responses:
                    response = self._responses.pop(message_id)
                    break
            if self._process and self._process.poll() is not None:
                raise MCPError(f"MCP server exited: {self._stderr[-1] if self._stderr else 'no stderr'}")
            self._event.wait(0.1)
            self._event.clear()
        if response is None:
            raise MCPError(f"MCP request {message_id} timed out after {effective_timeout:g}s.")
        if response.get("error"):
            error = response["error"]
            raise MCPError(f"MCP error {error.get('code')}: {error.get('message')}")
        return response.get("result") or {}

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        message_id = self._send(method, params)
        assert message_id is not None
        return self._await(message_id, timeout=timeout)

    def initialize(self) -> dict[str, Any]:
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}, "resources": {}},
            "clientInfo": CLIENT_INFO,
        })
        self._send("notifications/initialized", {}, notify=True)
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        result = self.request("tools/list", {})
        return list(result.get("tools") or [])

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)

    def close(self) -> None:
        if not self._process:
            return
        process = self._process
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
        finally:
            self._close_process_pipes(process)
            self._join_reader_threads()
            self._process = None

    def _close_process_pipes(self, process: subprocess.Popen[str]) -> None:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass

    def _join_reader_threads(self) -> None:
        for thread in (self._reader, self._stderr_thread):
            if thread and thread.is_alive():
                thread.join(timeout=1)

    @property
    def stderr(self) -> str:
        return "\n".join(self._stderr[-8:])


class MCPRemoteHTTPClient:
    """Minimal MCP JSON-RPC client for remote HTTP endpoints."""

    def __init__(self, config: MCPServerConfig, timeout: float = 90.0) -> None:
        self.config = config
        self.timeout = timeout
        self._next_id = 0
        if self.config.url:
            _validate_remote_url(config)

    def __enter__(self) -> "MCPRemoteHTTPClient":
        if not self.config.url:
            raise MCPError("MCP remote URL is not configured.")
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        self._next_id += 1
        message_id = self._next_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id, "method": method}
        if params is not None:
            payload["params"] = params
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            **self.config.headers,
        }
        access_token = str((self.config.auth or {}).get("access_token") or "")
        if access_token and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {access_token}"
        request = urllib.request.Request(self.config.url, data=body, method="POST", headers=headers)
        effective_timeout = timeout if timeout and timeout > 0 else self.timeout
        allow_local = _allow_local_remote_urls(self.config)
        try:
            with outbound_policy(allow_local=allow_local):
                with urlopen(request, timeout=effective_timeout, allow_local=allow_local, validate=False) as response:
                    raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise MCPError(f"Remote MCP HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise MCPError(f"Remote MCP connection failed: {exc.reason}") from exc
        message = self._parse_response(raw, message_id)
        if message.get("error"):
            error = message["error"]
            raise MCPError(f"MCP error {error.get('code')}: {error.get('message')}")
        return message.get("result") or {}

    def _parse_response(self, raw: str, message_id: int) -> dict[str, Any]:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            message = self._parse_sse_response(raw, message_id)
        if not isinstance(message, dict):
            raise MCPError("Remote MCP returned an invalid JSON-RPC response.")
        return message

    def _parse_sse_response(self, raw: str, message_id: int) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        for event in raw.replace("\r\n", "\n").split("\n\n"):
            data_lines = []
            for line in event.splitlines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if not data_lines:
                continue
            data = "\n".join(data_lines).strip()
            if not data or data == "[DONE]":
                continue
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                candidates.append(payload)
        for payload in candidates:
            if payload.get("id") == message_id:
                return payload
        if candidates:
            return candidates[-1]
        raise MCPError("Remote MCP returned an event-stream without a JSON-RPC response.")

    def initialize(self) -> dict[str, Any]:
        return self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}, "resources": {}},
            "clientInfo": CLIENT_INFO,
        })

    def list_tools(self) -> list[dict[str, Any]]:
        result = self.request("tools/list", {})
        return list(result.get("tools") or [])

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)


class MCPManager:
    """Owns the MCP server registry and short-lived stdio sessions."""

    def __init__(self, project_root: Path, load_servers, save_servers, filesystem_root=None) -> None:
        self.project_root = project_root
        self._load_servers = load_servers
        self._save_servers = save_servers
        self._filesystem_root = filesystem_root
        self._sessions: dict[str, tuple[Any, dict[str, Any]]] = {}
        self._sessions_lock = threading.Lock()
        atexit.register(self.close_all_sessions)

    def close_session(self, server_id: str) -> None:
        """Drop a cached MCP client so the next call starts a fresh process/session."""
        with self._sessions_lock:
            cached = self._sessions.pop(server_id, None)
        if not cached:
            return
        try:
            if hasattr(cached[0], "close"):
                cached[0].close()
        except Exception:
            pass

    def close_all_sessions(self) -> None:
        with self._sessions_lock:
            for _server_id, (client, _) in list(self._sessions.items()):
                try:
                    if hasattr(client, "close"):
                        client.close()
                except Exception:
                    pass
            self._sessions.clear()

    def list_servers(self) -> list[dict[str, Any]]:
        return [MCPServerConfig.from_dict(item).to_dict(include_secrets=False) for item in self._load_servers()]

    def get_server(self, server_id: str) -> MCPServerConfig | None:
        for item in self._load_servers():
            config = MCPServerConfig.from_dict(item)
            if config.id == server_id:
                return config
        return None

    @staticmethod
    def _restore_masked_secrets(payload: dict[str, Any], stored: dict[str, Any]) -> None:
        """Resolve SECRET_MASK placeholders in payload env/headers against the stored record.

        A masked value means "keep the stored secret"; masked entries without a
        stored counterpart are dropped so the placeholder is never persisted.
        Keys absent from the payload stay absent (the user deleted them).
        """
        for name in ("env", "headers"):
            incoming = payload.get(name)
            if not isinstance(incoming, dict):
                continue
            existing = dict(stored.get(name) or {})
            resolved: dict[str, Any] = {}
            for key, value in incoming.items():
                if value == SECRET_MASK:
                    if key in existing:
                        resolved[key] = existing[key]
                    continue
                resolved[key] = value
            payload[name] = resolved

    def upsert_server(self, payload: dict[str, Any]) -> dict[str, Any]:
        server_id = str(payload.get("id") or "").strip()
        if not server_id:
            raise MCPError("MCP server id is required.")
        servers = self._load_servers()
        payload = dict(payload)
        # Auth tokens are written only by the OAuth flow — never accept client-supplied auth.
        payload.pop("auth", None)
        updated = False
        for index, item in enumerate(servers):
            if str(item.get("id")) == server_id:
                self._restore_masked_secrets(payload, item)
                merged = {**item, **payload}
                # Preserve stored OAuth material even if a stale client field slipped through.
                merged["auth"] = dict(item.get("auth") or {})
                servers[index] = MCPServerConfig.from_dict(merged).to_dict()
                updated = True
                break
        if not updated:
            self._restore_masked_secrets(payload, {})
            payload["auth"] = {}
            servers.append(MCPServerConfig.from_dict(payload).to_dict())
        self._save_servers(servers)
        return self.get_server(server_id).to_dict(include_secrets=False)  # type: ignore[union-attr]

    def start_auth(self, server_id: str, base_url: str) -> dict[str, Any]:
        config = self.get_server(server_id)
        if not config:
            raise MCPError("MCP server not found.")
        if config.transport not in {"http", "remote", "streamable-http"}:
            raise MCPError("OAuth is only available for remote MCP servers.")
        if not config.url:
            raise MCPError("MCP remote URL is not configured.")
        # The OAuth flow dereferences config.url directly, so validate it here too.
        _validate_remote_url(config)
        redirect_uri = f"{base_url.rstrip('/')}/api/mcp/oauth/callback"
        resource_metadata, auth_metadata = self._discover_oauth_metadata(config)
        client = self._register_oauth_client(config, auth_metadata, redirect_uri)
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        authorization_endpoint = str(auth_metadata.get("authorization_endpoint") or "")
        if not authorization_endpoint:
            raise MCPError("OAuth authorization endpoint was not advertised by the MCP server.")
        params = {
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": config.url,
        }
        scope = str(resource_metadata.get("scope") or auth_metadata.get("scope") or "").strip()
        if scope:
            params["scope"] = scope
        # Start a clean auth session — never keep an expired access token mid-flow.
        config.auth = {
            "status": "pending",
            "state": state,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "resource": config.url,
            "client_id": client["client_id"],
            "client_secret": client.get("client_secret") or "",
            "authorization_endpoint": authorization_endpoint,
            "token_endpoint": str(auth_metadata.get("token_endpoint") or ""),
            "issuer": str(auth_metadata.get("issuer") or ""),
            "resource_metadata_url": str(resource_metadata.get("_metadata_url") or ""),
        }
        if not config.auth["token_endpoint"]:
            raise MCPError("OAuth token endpoint was not advertised by the MCP server.")
        self._save_config(config)
        auth_url = f"{authorization_endpoint}?{urllib.parse.urlencode(params)}"
        return {
            "id": server_id,
            "auth_url": auth_url,
            "auth_status": "pending",
            "message": "Complete Granola sign-in in the browser window, then return here and click Test.",
        }

    def complete_auth(self, query: dict[str, Any]) -> dict[str, Any]:
        error = self._query_value(query, "error")
        if error:
            raise MCPError(f"MCP authorization failed: {error}")
        code = self._query_value(query, "code")
        state = self._query_value(query, "state")
        if not code or not state:
            raise MCPError("OAuth callback is missing code or state.")
        servers = self._load_servers()
        for index, item in enumerate(servers):
            config = MCPServerConfig.from_dict(item)
            auth = dict(config.auth or {})
            if auth.get("state") != state:
                continue
            form = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": str(auth.get("redirect_uri") or ""),
                "client_id": str(auth.get("client_id") or ""),
                "code_verifier": str(auth.get("code_verifier") or ""),
                "resource": str(auth.get("resource") or config.url),
            }
            client_secret = str(auth.get("client_secret") or "")
            if client_secret:
                form["client_secret"] = client_secret
            token = self._post_form(str(auth.get("token_endpoint") or ""), form, config=config)
            access_token = str(token.get("access_token") or "")
            if not access_token:
                raise MCPError("OAuth token response did not include an access_token.")
            now = int(time.time())
            auth.update({
                "status": "authorized",
                "access_token": access_token,
                "refresh_token": str(token.get("refresh_token") or auth.get("refresh_token") or ""),
                "token_type": str(token.get("token_type") or "Bearer"),
                "expires_at": now + int(token.get("expires_in") or 3600),
            })
            for key in ("state", "code_verifier", "client_secret"):
                auth.pop(key, None)
            config.auth = auth
            config.enabled = True
            config.status = "configured"
            servers[index] = config.to_dict()
            self._save_servers(servers)
            return {"id": config.id, "label": config.label, "auth_status": "authorized", "message": "MCP authorization completed. Return to ArchitectOS and click Test."}
        raise MCPError("OAuth callback state did not match any pending MCP authorization.")

    def _resolve_command(self, config: MCPServerConfig) -> list[str]:
        command = list(config.command)
        if config.id == "filesystem" and command:
            root = str(self._resolve_filesystem_root())
            last = command[-1]
            if last == "." or Path(str(last)).is_absolute():
                command[-1] = root
            else:
                command.append(root)
        if self._is_azure_devops_mcp(config):
            command = self._resolve_azure_devops_command(command, config)
        return command

    def _resolve_filesystem_root(self) -> Path:
        if callable(self._filesystem_root):
            try:
                resolved = self._filesystem_root()
                if resolved:
                    return Path(resolved).expanduser().resolve()
            except Exception:
                pass
        return Path(self.project_root).expanduser().resolve()

    @staticmethod
    def _is_azure_devops_mcp(config: MCPServerConfig) -> bool:
        if config.id in {"azure-devops", "azure-devops-git"}:
            return True
        return any(part == ADO_MCP_PACKAGE or part.endswith(f"/{ADO_MCP_PACKAGE}") for part in config.command)

    def _resolve_azure_devops_command(self, command: list[str], config: MCPServerConfig) -> list[str]:
        resolved = list(command)
        if not resolved:
            raise MCPError("Azure DevOps MCP command is not configured.")
        org = self._azure_devops_org(config, resolved)
        if not org:
            raise MCPError(
                "Azure DevOps organization is required. Set ADO_ORG in .env, "
                "put the org name in the MCP command, or open a project with an Azure DevOps git remote."
            )
        package_idx = next((i for i, part in enumerate(resolved) if ADO_MCP_PACKAGE in part), -1)
        if package_idx < 0:
            raise MCPError(f"Azure DevOps MCP command must include {ADO_MCP_PACKAGE}.")
        next_idx = package_idx + 1
        if next_idx >= len(resolved) or resolved[next_idx].startswith("-"):
            resolved.insert(next_idx, org)
        elif resolved[next_idx] in ADO_ORG_PLACEHOLDERS:
            resolved[next_idx] = org
        if "--authentication" not in resolved and "-a" not in resolved:
            resolved.extend(["--authentication", "envvar"])
        self._ensure_azure_devops_auth(config, resolved)
        return resolved

    def _azure_devops_org(self, config: MCPServerConfig, command: list[str] | None = None) -> str:
        for key in ("ADO_ORG", "AZURE_DEVOPS_ORG", "ado_mcp_org"):
            value = str(config.env.get(key) or os.environ.get(key) or "").strip()
            if value and value not in ADO_ORG_PLACEHOLDERS:
                return value
        command = list(command or config.command)
        package_idx = next((i for i, part in enumerate(command) if ADO_MCP_PACKAGE in part), -1)
        if package_idx >= 0 and package_idx + 1 < len(command):
            candidate = command[package_idx + 1]
            if candidate and not candidate.startswith("-") and candidate not in ADO_ORG_PLACEHOLDERS:
                return candidate
        detected = detect_azure_devops_from_git(self.project_root)
        if detected.get("org"):
            return detected["org"]
        return ""

    def _ensure_azure_devops_auth(self, config: MCPServerConfig, command: list[str]) -> None:
        auth_mode = "envvar"
        for flag in ("--authentication", "-a"):
            if flag in command:
                idx = command.index(flag)
                if idx + 1 < len(command):
                    auth_mode = command[idx + 1].strip().lower()
                break
        env = {**dict(os.environ), **{k: v for k, v in config.env.items() if v}}
        if auth_mode in {"envvar", "env"}:
            if not str(env.get("ADO_MCP_AUTH_TOKEN") or "").strip():
                raise MCPError(
                    "Azure DevOps auth requires ADO_MCP_AUTH_TOKEN in .env (raw PAT), then restart ArchitectOS."
                )
        elif auth_mode == "pat":
            if not str(env.get("PERSONAL_ACCESS_TOKEN") or "").strip():
                raise MCPError(
                    "Azure DevOps PAT auth requires PERSONAL_ACCESS_TOKEN in .env "
                    "(base64 of ':<pat>'), then restart ArchitectOS."
                )

    def _client_for(self, config: MCPServerConfig):
        if config.transport in {"http", "remote", "streamable-http"}:
            return MCPRemoteHTTPClient(config)
        return MCPClient(config, self.project_root)

    def _save_config(self, config: MCPServerConfig) -> None:
        servers = self._load_servers()
        updated = False
        for index, item in enumerate(servers):
            if str(item.get("id")) == config.id:
                servers[index] = config.to_dict()
                updated = True
                break
        if not updated:
            servers.append(config.to_dict())
        self._save_servers(servers)

    def _discover_oauth_metadata(self, config: MCPServerConfig) -> tuple[dict[str, Any], dict[str, Any]]:
        metadata_url = self._protected_resource_metadata_url(config)
        resource_metadata = self._fetch_json(metadata_url, config=config)
        resource_metadata["_metadata_url"] = metadata_url
        auth_servers = resource_metadata.get("authorization_servers") or []
        if not auth_servers:
            raise MCPError("MCP server did not advertise an OAuth authorization server.")
        auth_metadata_url = self._authorization_server_metadata_url(str(auth_servers[0]))
        return resource_metadata, self._fetch_json(auth_metadata_url, config=config)

    def _protected_resource_metadata_url(self, config: MCPServerConfig) -> str:
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
        }).encode("utf-8")
        request = urllib.request.Request(
            config.url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": PROTOCOL_VERSION,
                **config.headers,
            },
        )
        try:
            allow_local = _allow_local_remote_urls(config)
            with urlopen(request, timeout=20.0, allow_local=allow_local, validate=False):
                raise MCPError("MCP server did not request OAuth authorization.")
        except urllib.error.HTTPError as exc:
            header = exc.headers.get("WWW-Authenticate") or ""
            metadata_url = self._www_authenticate_param(header, "resource_metadata")
            if metadata_url:
                return metadata_url
            if exc.code != 401:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                raise MCPError(f"Remote MCP HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise MCPError(f"Remote MCP connection failed: {exc.reason}") from exc
        parsed = urllib.parse.urlparse(config.url)
        return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/.well-known/oauth-protected-resource", "", "", ""))

    def _register_oauth_client(self, config: MCPServerConfig, auth_metadata: dict[str, Any], redirect_uri: str) -> dict[str, str]:
        registration_endpoint = str(auth_metadata.get("registration_endpoint") or "")
        existing = dict(config.auth or {})
        if not registration_endpoint:
            client_id = str(existing.get("client_id") or "")
            if not client_id:
                raise MCPError("OAuth dynamic client registration is unavailable and no client_id is configured.")
            return {"client_id": client_id, "client_secret": str(existing.get("client_secret") or "")}
        payload = {
            "client_name": CLIENT_INFO["name"],
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        registered = self._post_json(registration_endpoint, payload, config=config)
        client_id = str(registered.get("client_id") or "")
        if not client_id:
            raise MCPError("OAuth registration did not return a client_id.")
        return {"client_id": client_id, "client_secret": str(registered.get("client_secret") or "")}

    def _require_outbound_url(self, url: str, *, context: str, config: MCPServerConfig | None = None) -> str:
        """SSRF-check OAuth discovery / token / registration URLs from remote metadata.

        Link-local is always rejected. Loopback is allowed only when the MCP
        server opted into allow_local (local-first OAuth against a loopback MCP).
        """
        if not url:
            raise MCPError(f"{context} URL is empty.")
        allow_local = _allow_local_remote_urls(config) if config is not None else False
        try:
            return validate_outbound_url(url, allow_local=allow_local)
        except ValueError as exc:
            raise MCPError(f"{context} URL is not allowed: {exc}") from exc

    def _fetch_json(self, url: str, *, config: MCPServerConfig | None = None) -> dict[str, Any]:
        safe = self._require_outbound_url(url, context="OAuth metadata", config=config)
        allow_local = _allow_local_remote_urls(config) if config is not None else False
        request = urllib.request.Request(safe, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=20.0, allow_local=allow_local) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise MCPError(f"OAuth metadata HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise MCPError(f"OAuth metadata request failed: {exc.reason}") from exc
        try:
            return dict(json.loads(raw))
        except (TypeError, json.JSONDecodeError) as exc:
            raise MCPError("OAuth metadata response was not valid JSON.") from exc

    def _post_json(self, url: str, payload: dict[str, Any], *, config: MCPServerConfig | None = None) -> dict[str, Any]:
        safe = self._require_outbound_url(url, context="OAuth registration", config=config)
        allow_local = _allow_local_remote_urls(config) if config is not None else False
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(safe, data=body, method="POST", headers={"Content-Type": "application/json", "Accept": "application/json"})
        return self._read_json_response(request, "OAuth JSON request", allow_local=allow_local)

    def _post_form(self, url: str, form: dict[str, str], *, config: MCPServerConfig | None = None) -> dict[str, Any]:
        safe = self._require_outbound_url(url, context="OAuth token", config=config)
        allow_local = _allow_local_remote_urls(config) if config is not None else False
        body = urllib.parse.urlencode(form).encode("utf-8")
        request = urllib.request.Request(safe, data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        return self._read_json_response(request, "OAuth token request", allow_local=allow_local)

    def _read_json_response(self, request: urllib.request.Request, context: str, *, allow_local: bool = False) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=20.0, allow_local=allow_local, validate=False) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise MCPError(f"{context} HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise MCPError(f"{context} failed: {exc.reason}") from exc
        try:
            return dict(json.loads(raw))
        except (TypeError, json.JSONDecodeError) as exc:
            raise MCPError(f"{context} returned invalid JSON.") from exc

    @staticmethod
    def _authorization_server_metadata_url(issuer: str) -> str:
        if "/.well-known/oauth-authorization-server" in issuer:
            return issuer
        return f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server"

    @staticmethod
    def _www_authenticate_param(header: str, name: str) -> str:
        marker = f"{name}="
        start = header.find(marker)
        if start < 0:
            return ""
        value = header[start + len(marker):].strip()
        if value.startswith('"'):
            value = value[1:]
            return value.split('"', 1)[0]
        return value.split(",", 1)[0].strip()

    @staticmethod
    def _query_value(query: dict[str, Any], key: str) -> str:
        value = query.get(key)
        if isinstance(value, list):
            value = value[0] if value else ""
        return str(value or "")

    def _get_client(self, server_id: str) -> Any:
        with self._sessions_lock:
            config = self._require_launchable(server_id)
            if config.transport in {"http", "remote", "streamable-http"}:
                config = self._ensure_fresh_auth(config)
            command = self._resolve_command(config) if config.transport not in {"http", "remote", "streamable-http"} else list(config.command)
            probe = MCPServerConfig.from_dict({**config.to_dict(), "command": command})
            current_config_dict = probe.to_dict()

            cached = self._sessions.get(server_id)
            if cached:
                cached_client, cached_config = cached
                valid = True
                if config.transport not in {"http", "remote", "streamable-http"}:
                    if not cached_client._process or cached_client._process.poll() is not None:
                        valid = False
                if cached_config != current_config_dict:
                    valid = False

                if valid:
                    return cached_client
                else:
                    try:
                        if hasattr(cached_client, "close"):
                            cached_client.close()
                    except Exception:
                        pass
                    self._sessions.pop(server_id, None)

            client = self._client_for(probe)
            if hasattr(client, "start"):
                client.start()
            client.initialize()
            self._sessions[server_id] = (client, current_config_dict)
            return client

    def check(self, server_id: str) -> dict[str, Any]:
        with self._sessions_lock:
            cached = self._sessions.pop(server_id, None)
            if cached:
                try:
                    if hasattr(cached[0], "close"):
                        cached[0].close()
                except Exception:
                    pass

        config = self.get_server(server_id)
        if not config:
            raise MCPError("MCP server not found.")
        if config.transport in {"http", "remote", "streamable-http"}:
            if not config.url:
                return {"id": server_id, "ready": False, "status": "missing_url", "message": "No remote MCP URL configured.", "tools": []}
            if (config.auth or {}).get("status") == "pending" and not (config.auth or {}).get("access_token"):
                return {
                    "id": server_id,
                    "ready": False,
                    "status": "auth_required",
                    "message": "Authorization is pending. Finish sign-in in the browser, then click Test again.",
                    "tools": [],
                }
            command = list(config.command)
            try:
                config = self._ensure_fresh_auth(config)
            except MCPError as exc:
                self._mark_status(server_id, "auth_required")
                return {"id": server_id, "ready": False, "status": "auth_required", "message": str(exc), "tools": []}
        else:
            try:
                command = self._resolve_command(config)
            except MCPError as exc:
                status = "auth_required" if "ADO_MCP_AUTH_TOKEN" in str(exc) or "PERSONAL_ACCESS_TOKEN" in str(exc) else "error"
                self._mark_status(server_id, status)
                return {"id": server_id, "ready": False, "status": status, "message": str(exc), "tools": []}
            if not command:
                return {"id": server_id, "ready": False, "status": "missing_command", "message": "No command configured.", "tools": []}
            if not shutil.which(command[0]):
                return {"id": server_id, "ready": False, "status": "missing_executable", "message": f"Executable not found: {command[0]}", "hint": f"Install {command[0]} or update the command.", "tools": []}
        if config.approval_required and not config.enabled:
            return {"id": server_id, "ready": False, "status": "approval_required", "message": "Enable this MCP server before launching it.", "tools": []}
        probe = MCPServerConfig.from_dict({**config.to_dict(), "command": command})
        try:
            with self._client_for(probe) as client:
                info = client.initialize()
                tools = client.list_tools()
            status = "ready"
            self._mark_status(server_id, "ready")
            return {
                "id": server_id,
                "ready": True,
                "status": status,
                "message": f"Connected to {info.get('serverInfo', {}).get('name', server_id)}.",
                "server_info": info.get("serverInfo") or {},
                "capabilities": info.get("capabilities") or {},
                "tools": [{"name": tool.get("name"), "description": tool.get("description") or ""} for tool in tools],
            }
        except MCPError as exc:
            if config.transport in {"http", "remote", "streamable-http"} and "HTTP 401" in str(exc):
                refreshed = self._try_refresh_after_unauthorized(config)
                if refreshed:
                    try:
                        probe = MCPServerConfig.from_dict({**refreshed.to_dict(), "command": command})
                        with self._client_for(probe) as client:
                            info = client.initialize()
                            tools = client.list_tools()
                        self._mark_status(server_id, "ready")
                        return {
                            "id": server_id,
                            "ready": True,
                            "status": "ready",
                            "message": f"Connected to {info.get('serverInfo', {}).get('name', server_id)}.",
                            "server_info": info.get("serverInfo") or {},
                            "capabilities": info.get("capabilities") or {},
                            "tools": [{"name": tool.get("name"), "description": tool.get("description") or ""} for tool in tools],
                        }
                    except MCPError as retry_exc:
                        exc = retry_exc
                status = "auth_required"
                message = str(exc)
                if "Session expired" in message or "401" in message:
                    message = f"{message} Click Authorize and sign in again."
                self._mark_status(server_id, status)
                return {"id": server_id, "ready": False, "status": status, "message": message, "tools": []}
            status = "error"
            self._mark_status(server_id, status)
            return {"id": server_id, "ready": False, "status": status, "message": str(exc), "tools": []}

    def _token_expired(self, auth: dict[str, Any]) -> bool:
        expires_at = int(auth.get("expires_at") or 0)
        if expires_at <= 0:
            return False
        return time.time() >= (expires_at - 60)

    def _ensure_fresh_auth(self, config: MCPServerConfig) -> MCPServerConfig:
        auth = dict(config.auth or {})
        if not auth.get("access_token"):
            return config
        if auth.get("status") == "pending":
            return config
        if self._token_expired(auth):
            if not auth.get("refresh_token"):
                raise MCPError("MCP access token expired. Click Authorize and sign in again.")
            return self._refresh_access_token(config)
        return config

    def _try_refresh_after_unauthorized(self, config: MCPServerConfig) -> MCPServerConfig | None:
        auth = dict(config.auth or {})
        if not auth.get("refresh_token") or auth.get("status") == "pending":
            return None
        try:
            return self._refresh_access_token(config)
        except MCPError:
            return None

    def _refresh_access_token(self, config: MCPServerConfig) -> MCPServerConfig:
        auth = dict(config.auth or {})
        refresh_token = str(auth.get("refresh_token") or "")
        token_endpoint = str(auth.get("token_endpoint") or "")
        if not refresh_token or not token_endpoint:
            raise MCPError("MCP refresh token is unavailable. Click Authorize and sign in again.")
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": str(auth.get("client_id") or ""),
            "resource": str(auth.get("resource") or config.url),
        }
        client_secret = str(auth.get("client_secret") or "")
        if client_secret:
            form["client_secret"] = client_secret
        token = self._post_form(token_endpoint, form, config=config)
        access_token = str(token.get("access_token") or "")
        if not access_token:
            raise MCPError("MCP token refresh did not return an access_token. Click Authorize and sign in again.")
        now = int(time.time())
        auth.update({
            "status": "authorized",
            "access_token": access_token,
            "refresh_token": str(token.get("refresh_token") or refresh_token),
            "token_type": str(token.get("token_type") or "Bearer"),
            "expires_at": now + int(token.get("expires_in") or 3600),
        })
        config.auth = auth
        self._save_config(config)
        return self.get_server(config.id) or config

    def list_tools(self, server_id: str) -> dict[str, Any]:
        client = self._get_client(server_id)
        tools = client.list_tools()
        return {"id": server_id, "tools": tools, "count": len(tools)}

    def call_tool(self, server_id: str, tool: str, arguments: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        if not tool:
            raise MCPError("MCP tool name is required.")
        client = self._get_client(server_id)
        result = client.call_tool(tool, arguments or {}, timeout=timeout)
        return {"id": server_id, "tool": tool, "result": result}

    def _require_launchable(self, server_id: str) -> MCPServerConfig:
        config = self.get_server(server_id)
        if not config:
            raise MCPError("MCP server not found.")
        if not config.enabled:
            raise MCPError("MCP server is disabled. Enable it before launching.")
        if config.transport in {"http", "remote", "streamable-http"}:
            if not config.url:
                raise MCPError("MCP remote URL is not configured.")
            return config
        if not config.command or not shutil.which(
            self._resolve_command(config)[0],
            path=prefer_modern_node_env().get("PATH"),
        ):
            raise MCPError("MCP executable is not available on PATH.")
        return config

    def _mark_status(self, server_id: str, status: str) -> None:
        servers = self._load_servers()
        for item in servers:
            if str(item.get("id")) == server_id:
                item["status"] = status
        self._save_servers(servers)
