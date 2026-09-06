"""stdio and remote HTTP MCP JSON-RPC clients."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .mcp_config import CLIENT_INFO, PROTOCOL_VERSION, MCPError, MCPServerConfig
from .mcp_detect import prefer_modern_node_env
from .netutil import validate_outbound_url
from .ssl_util import urlopen


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

    def initialize(self, timeout: float | None = 20.0) -> dict[str, Any]:
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}, "resources": {}},
            "clientInfo": CLIENT_INFO,
        }, timeout=timeout)
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
            # config.url passed _validate_remote_url in __init__, so skip a second
            # DNS lookup here; redirect hops are still checked against allow_local.
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

    def initialize(self, timeout: float | None = 20.0) -> dict[str, Any]:
        return self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}, "resources": {}},
            "clientInfo": CLIENT_INFO,
        }, timeout=timeout)

    def list_tools(self) -> list[dict[str, Any]]:
        result = self.request("tools/list", {})
        return list(result.get("tools") or [])

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)
