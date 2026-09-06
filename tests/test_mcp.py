from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from backend.architectos.constants import SECRET_MASK
from backend.architectos.mcp import MCPClient, MCPError, MCPManager, MCPServerConfig

FAKE_MCP_SERVER = textwrap.dedent(
    """
    import json, sys
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        mid = msg.get("id")
        method = msg.get("method")
        if mid is None:
            continue
        if method == "initialize":
            res = {"protocolVersion": "2024-11-05", "serverInfo": {"name": "fake-mcp"}, "capabilities": {"tools": {}}}
        elif method == "tools/list":
            res = {"tools": [{"name": "echo", "description": "Echo tool"}]}
        elif method == "tools/call":
            args = (msg.get("params") or {}).get("arguments") or {}
            res = {"content": [{"type": "text", "text": "echo:" + json.dumps(args, sort_keys=True)}]}
        else:
            res = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\\n")
        sys.stdout.flush()
    """
)


class _Store:
    def __init__(self, servers):
        self.servers = servers

    def load(self):
        return self.servers

    def save(self, servers):
        self.servers = servers


class MCPClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script = self.root / "fake_mcp.py"
        self.script.write_text(FAKE_MCP_SERVER, encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _config(self, server_id="fake"):
        return MCPServerConfig.from_dict({
            "id": server_id,
            "label": "Fake MCP",
            "command": [sys.executable, str(self.script)],
            "enabled": True,
            "approval_required": False,
        })

    def test_client_initializes_lists_and_calls_tools(self) -> None:
        with MCPClient(self._config(), self.root) as client:
            info = client.initialize()
            self.assertEqual(info["serverInfo"]["name"], "fake-mcp")
            tools = client.list_tools()
            self.assertEqual(tools[0]["name"], "echo")
            result = client.call_tool("echo", {"value": 1})
            self.assertIn("echo:", result["content"][0]["text"])

    def test_manager_check_and_call(self) -> None:
        store = _Store([self._config().to_dict()])
        manager = MCPManager(self.root, store.load, store.save)
        check = manager.check("fake")
        self.assertTrue(check["ready"])
        self.assertEqual(check["tools"][0]["name"], "echo")
        called = manager.call_tool("fake", "echo", {"a": "b"})
        self.assertIn("echo:", called["result"]["content"][0]["text"])

    def test_close_session_unblocks_hung_initialize(self) -> None:
        started = threading.Event()
        closed = threading.Event()

        class SlowInitClient:
            def start(self) -> None:
                return None

            def initialize(self, timeout=None):
                started.set()
                if not closed.wait(30):
                    raise MCPError("still initializing")
                raise MCPError("session closed")

            def close(self) -> None:
                closed.set()

        store = _Store([self._config().to_dict()])
        manager = MCPManager(self.root, store.load, store.save)
        manager._client_for = lambda _config: SlowInitClient()  # type: ignore[method-assign]
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                manager.call_tool("fake", "echo", {})
            except BaseException as exc:  # noqa: BLE001 - thread boundary
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(started.wait(2))
        began = time.monotonic()
        manager.close_session("fake")
        thread.join(5)
        self.assertLess(time.monotonic() - began, 2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)

    def test_manager_rejects_missing_executable(self) -> None:
        store = _Store([{"id": "bad", "label": "Bad", "command": ["definitely-not-a-real-binary-xyz"], "enabled": True, "approval_required": False}])
        manager = MCPManager(self.root, store.load, store.save)
        check = manager.check("bad")
        self.assertFalse(check["ready"])
        self.assertEqual(check["status"], "missing_executable")

    def test_manager_supports_remote_http_transport(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or "0")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                method = payload.get("method")
                if method == "initialize":
                    result = {"protocolVersion": "2024-11-05", "serverInfo": {"name": "fake-http-mcp"}, "capabilities": {"tools": {}}}
                elif method == "tools/list":
                    result = {"tools": [{"name": "remote_echo", "description": "Remote echo"}]}
                elif method == "tools/call":
                    result = {"content": [{"type": "text", "text": "remote"}]}
                else:
                    result = {}
                body = json.dumps({"jsonrpc": "2.0", "id": payload.get("id"), "result": result}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            store = _Store([{
                "id": "remote",
                "label": "Remote MCP",
                "transport": "http",
                "allow_local": True,
                "url": f"http://127.0.0.1:{server.server_port}/mcp",
                "enabled": True,
                "approval_required": False,
            }])
            manager = MCPManager(self.root, store.load, store.save)
            check = manager.check("remote")
            self.assertTrue(check["ready"])
            self.assertEqual(check["tools"][0]["name"], "remote_echo")
            called = manager.call_tool("remote", "remote_echo", {})
            self.assertEqual(called["result"]["content"][0]["text"], "remote")
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_manager_supports_remote_http_event_stream_response(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or "0")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                method = payload.get("method")
                if method == "initialize":
                    result = {"protocolVersion": "2024-11-05", "serverInfo": {"name": "fake-sse-mcp"}, "capabilities": {"tools": {}}}
                elif method == "tools/list":
                    result = {"tools": [{"name": "stream_echo", "description": "Stream echo"}]}
                else:
                    result = {}
                message = json.dumps({"jsonrpc": "2.0", "id": payload.get("id"), "result": result})
                body = f"event: message\ndata: {message}\n\n".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            store = _Store([{
                "id": "remote-sse",
                "label": "Remote SSE MCP",
                "transport": "http",
                "allow_local": True,
                "url": f"http://127.0.0.1:{server.server_port}/mcp",
                "enabled": True,
                "approval_required": False,
            }])
            manager = MCPManager(self.root, store.load, store.save)
            check = manager.check("remote-sse")
            self.assertTrue(check["ready"])
            self.assertEqual(check["tools"][0]["name"], "stream_echo")
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_manager_oauth_flow_authorizes_remote_http_transport(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            registration_seen = False
            token_seen = False
            bearer_seen = False

            def _json(self, payload: dict, status: int = 200) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                base = f"http://127.0.0.1:{self.server.server_port}"
                if self.path == "/.well-known/oauth-protected-resource":
                    self._json({"authorization_servers": [base], "scopes_supported": ["mcp"]})
                elif self.path == "/.well-known/oauth-authorization-server":
                    self._json({
                        "issuer": base,
                        "authorization_endpoint": f"{base}/authorize",
                        "token_endpoint": f"{base}/token",
                        "registration_endpoint": f"{base}/register",
                    })
                elif self.path.startswith("/authorize"):
                    parsed = urlparse(self.path)
                    params = parse_qs(parsed.query)
                    self._json({"state": params.get("state", [""])[0], "client_id": params.get("client_id", [""])[0]})
                else:
                    self.send_error(404)

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or "0")
                raw = self.rfile.read(length).decode("utf-8")
                base = f"http://127.0.0.1:{self.server.server_port}"
                if self.path == "/mcp" and self.headers.get("Authorization") != "Bearer token-1":
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/mcp":
                    Handler.bearer_seen = True
                    payload = json.loads(raw)
                    method = payload.get("method")
                    if method == "initialize":
                        result = {"protocolVersion": "2024-11-05", "serverInfo": {"name": "authorized-mcp"}, "capabilities": {"tools": {}}}
                    elif method == "tools/list":
                        result = {"tools": [{"name": "secure_echo", "description": "Secure echo"}]}
                    else:
                        result = {}
                    self._json({"jsonrpc": "2.0", "id": payload.get("id"), "result": result})
                elif self.path == "/register":
                    Handler.registration_seen = True
                    self._json({"client_id": "client-1"})
                elif self.path == "/token":
                    params = parse_qs(raw)
                    Handler.token_seen = params.get("code") == ["code-1"] and bool(params.get("code_verifier"))
                    self._json({"access_token": "token-1", "token_type": "Bearer", "expires_in": 3600})
                else:
                    self.send_error(404)

            def log_message(self, *_args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            store = _Store([{
                "id": "secure",
                "label": "Secure MCP",
                "transport": "http",
                "allow_local": True,
                "url": f"http://127.0.0.1:{server.server_port}/mcp",
                "enabled": True,
                "approval_required": False,
            }])
            manager = MCPManager(self.root, store.load, store.save)
            started = manager.start_auth("secure", "http://127.0.0.1:8765")
            self.assertIn("/authorize?", started["auth_url"])
            self.assertIn("scope=mcp", started["auth_url"])
            state = MCPServerConfig.from_dict(store.servers[0]).auth["state"]
            completed = manager.complete_auth({"code": "code-1", "state": state})
            self.assertEqual(completed["auth_status"], "authorized")
            self.assertTrue(Handler.registration_seen)
            self.assertTrue(Handler.token_seen)
            public = manager.list_servers()[0]
            self.assertNotIn("access_token", public["auth"])
            check = manager.check("secure")
            self.assertTrue(check["ready"])
            self.assertTrue(Handler.bearer_seen)
            self.assertEqual(check["tools"][0]["name"], "secure_echo")
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


class MCPSecretMaskingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _store(self) -> _Store:
        return _Store([{
            "id": "secret-env",
            "label": "Secret MCP",
            "command": ["echo"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_real_token", "PLAIN": "value"},
            "headers": {"Authorization": "Bearer real_header"},
        }])

    def test_list_servers_masks_env_and_headers_values(self) -> None:
        store = self._store()
        manager = MCPManager(self.root, store.load, store.save)
        server = manager.list_servers()[0]
        self.assertEqual(server["env"], {"GITHUB_PERSONAL_ACCESS_TOKEN": SECRET_MASK, "PLAIN": SECRET_MASK})
        self.assertEqual(server["headers"], {"Authorization": SECRET_MASK})
        # The stored record keeps the real values.
        self.assertEqual(store.servers[0]["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"], "ghp_real_token")
        self.assertEqual(store.servers[0]["headers"]["Authorization"], "Bearer real_header")

    def test_upsert_masked_value_keeps_stored_secret(self) -> None:
        store = self._store()
        manager = MCPManager(self.root, store.load, store.save)
        manager.upsert_server({
            "id": "secret-env",
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": SECRET_MASK, "PLAIN": SECRET_MASK},
            "headers": {"Authorization": SECRET_MASK},
        })
        stored = MCPServerConfig.from_dict(store.servers[0])
        self.assertEqual(stored.env, {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_real_token", "PLAIN": "value"})
        self.assertEqual(stored.headers, {"Authorization": "Bearer real_header"})

    def test_upsert_new_value_overrides_stored_secret(self) -> None:
        store = self._store()
        manager = MCPManager(self.root, store.load, store.save)
        manager.upsert_server({
            "id": "secret-env",
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_new_token", "PLAIN": "value"},
            "headers": {"Authorization": "Bearer new_header"},
        })
        stored = MCPServerConfig.from_dict(store.servers[0])
        self.assertEqual(stored.env["GITHUB_PERSONAL_ACCESS_TOKEN"], "ghp_new_token")
        self.assertEqual(stored.headers["Authorization"], "Bearer new_header")

    def test_upsert_omitted_key_is_removed(self) -> None:
        store = self._store()
        manager = MCPManager(self.root, store.load, store.save)
        manager.upsert_server({
            "id": "secret-env",
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": SECRET_MASK},
            "headers": {},
        })
        stored = MCPServerConfig.from_dict(store.servers[0])
        self.assertEqual(stored.env, {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_real_token"})
        self.assertEqual(stored.headers, {})

    def test_upsert_new_server_drops_masked_entries(self) -> None:
        store = _Store([])
        manager = MCPManager(self.root, store.load, store.save)
        manager.upsert_server({
            "id": "fresh",
            "label": "Fresh MCP",
            "command": ["echo"],
            "env": {"TOKEN": SECRET_MASK, "REAL": "kept"},
            "headers": {"Authorization": SECRET_MASK},
        })
        stored = MCPServerConfig.from_dict(store.servers[0])
        self.assertEqual(stored.env, {"REAL": "kept"})
        self.assertEqual(stored.headers, {})

    def test_upsert_ignores_client_supplied_auth(self) -> None:
        store = _Store([{
            "id": "secure",
            "label": "Secure",
            "command": ["echo"],
            "auth": {"status": "authorized", "access_token": "keep-me", "refresh_token": "refresh-me"},
        }])
        manager = MCPManager(self.root, store.load, store.save)
        manager.upsert_server({
            "id": "secure",
            "label": "Secure",
            "command": ["echo"],
            "auth": {"status": "authorized", "access_token": "stolen", "refresh_token": "stolen"},
            "notes": "updated",
        })
        stored = MCPServerConfig.from_dict(store.servers[0])
        self.assertEqual(stored.notes, "updated")
        self.assertEqual(stored.auth.get("access_token"), "keep-me")
        self.assertEqual(stored.auth.get("refresh_token"), "refresh-me")

    def test_oauth_metadata_rejects_link_local(self) -> None:
        store = _Store([{
            "id": "remote",
            "label": "Remote",
            "transport": "http",
            "url": "https://example.com/mcp",
            "enabled": True,
        }])
        manager = MCPManager(self.root, store.load, store.save)
        with self.assertRaises(MCPError):
            manager._fetch_json("http://169.254.169.254/latest/meta-data")


class AzureDevOpsMCPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._prev_token = os.environ.pop("ADO_MCP_AUTH_TOKEN", None)
        self._prev_org = os.environ.pop("ADO_ORG", None)

    def tearDown(self) -> None:
        if self._prev_token is None:
            os.environ.pop("ADO_MCP_AUTH_TOKEN", None)
        else:
            os.environ["ADO_MCP_AUTH_TOKEN"] = self._prev_token
        if self._prev_org is None:
            os.environ.pop("ADO_ORG", None)
        else:
            os.environ["ADO_ORG"] = self._prev_org
        self.tmp.cleanup()

    def test_resolve_inserts_org_and_auth(self) -> None:
        os.environ["ADO_ORG"] = "Fsight1"
        os.environ["ADO_MCP_AUTH_TOKEN"] = "dummy-pat"
        store = _Store([{
            "id": "azure-devops",
            "label": "Azure DevOps",
            "command": ["npx", "-y", "@azure-devops/mcp"],
            "enabled": True,
            "approval_required": False,
            "transport": "stdio",
        }])
        manager = MCPManager(self.root, store.load, store.save)
        command = manager._resolve_command(manager.get_server("azure-devops"))
        self.assertEqual(command[:4], ["npx", "-y", "@azure-devops/mcp", "Fsight1"])
        self.assertIn("--authentication", command)
        self.assertIn("envvar", command)

    def test_check_reports_missing_token(self) -> None:
        os.environ["ADO_ORG"] = "Fsight1"
        store = _Store([{
            "id": "azure-devops-git",
            "label": "Azure DevOps Git",
            "command": ["npx", "-y", "@azure-devops/mcp", "$ADO_ORG", "--authentication", "envvar", "-d", "repositories"],
            "enabled": True,
            "approval_required": False,
            "transport": "stdio",
        }])
        manager = MCPManager(self.root, store.load, store.save)
        check = manager.check("azure-devops-git")
        self.assertFalse(check["ready"])
        self.assertEqual(check["status"], "auth_required")
        self.assertIn("ADO_MCP_AUTH_TOKEN", check["message"])

    def test_detect_azure_devops_ssh_remote(self) -> None:
        from backend.architectos.mcp import parse_azure_devops_remote_url

        detected = parse_azure_devops_remote_url("git@ssh.dev.azure.com:v3/Fsight1/E-AI/E-AI")
        self.assertEqual(detected["org"], "Fsight1")
        self.assertEqual(detected["project"], "E-AI")
        https = parse_azure_devops_remote_url("https://dev.azure.com/Fsight1/E-AI/_git/E-AI")
        self.assertEqual(https["org"], "Fsight1")
        self.assertEqual(https["project"], "E-AI")


class MCPTransportNormalizeTests(unittest.TestCase):
    def test_from_dict_maps_remote_http_label_to_http(self) -> None:
        config = MCPServerConfig.from_dict({
            "id": "granola",
            "label": "Granola",
            "transport": "remote http",
            "url": "https://mcp.granola.ai/mcp",
        })
        self.assertEqual(config.transport, "http")

    def test_from_dict_infers_http_when_url_and_blank_transport(self) -> None:
        config = MCPServerConfig.from_dict({
            "id": "granola",
            "label": "Granola",
            "transport": "",
            "url": "https://mcp.granola.ai/mcp",
        })
        self.assertEqual(config.transport, "http")
        self.assertEqual(config.url, "https://mcp.granola.ai/mcp")

    def test_oauth_scope_uses_scopes_supported(self) -> None:
        self.assertEqual(
            MCPManager._oauth_scope({"scopes_supported": ["mcp"]}, {}),
            "mcp",
        )
        self.assertEqual(
            MCPManager._oauth_scope({"scope": "openid profile"}, {"scopes_supported": ["mcp"]}),
            "openid profile",
        )


if __name__ == "__main__":
    unittest.main()
