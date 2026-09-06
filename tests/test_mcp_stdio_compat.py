"""Regression test: ArchitectOS MCP server speaks MCP stdio (ndjson JSON-RPC 2.0).

Spawns ``python -m backend.architectos.mcp_server`` as a real subprocess and
drives the exact handshake an official MCP client performs
(``initialize`` → ``notifications/initialized`` → ``tools/list`` → ``tools/call``),
plus JSON-RPC error semantics and the "stdout carries protocol frames only"
invariant. Verified manually against ``@modelcontextprotocol/sdk``'s
StdioClientTransport (see /tmp/mcp-compat-check/check.mjs).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _spawn_server(root: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["ARCHITECTOS_ROOT"] = str(root)
    return subprocess.Popen(
        [sys.executable, "-m", "backend.architectos.mcp_server"],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _run_session(messages: list[dict | str]) -> tuple[list[dict], str]:
    """Send raw lines, close stdin, collect parsed stdout frames + stderr."""
    with tempfile.TemporaryDirectory() as tmp:
        proc = _spawn_server(Path(tmp))
        payload = "".join(
            (line if isinstance(line, str) else json.dumps(line)) + "\n" for line in messages
        )
        stdout, stderr = proc.communicate(payload, timeout=60)
    frames = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    return frames, stderr


class MCPStdioCompatTests(unittest.TestCase):
    def test_handshake_tools_list_and_call_over_stdio(self) -> None:
        frames, stderr = _run_session([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "compat-test", "version": "0.0.1"},
            }},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "memory_list_projects", "arguments": {},
            }},
        ])
        # The notification must not produce a frame.
        self.assertEqual([frame["id"] for frame in frames], [1, 2, 3])

        init = frames[0]["result"]
        self.assertEqual(init["protocolVersion"], "2024-11-05")
        self.assertEqual(init["serverInfo"]["name"], "architectos-memory")
        self.assertIn("tools", init["capabilities"])
        self.assertTrue(init.get("instructions"))

        tools = frames[1]["result"]["tools"]
        self.assertIn("memory_search", {tool["name"] for tool in tools})
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")

        call = frames[2]["result"]
        self.assertFalse(call.get("isError"))
        self.assertEqual(call["content"][0]["type"], "text")
        self.assertIn("architectos", call["content"][0]["text"])

        # Logging must go to stderr only — every stdout line parsed as JSON-RPC above.
        self.assertIn("[architectos-mcp]", stderr)

    def test_protocol_version_negotiation(self) -> None:
        # Client asks for a newer version than the server implements: the server
        # must answer with its own newest supported version (2024-11-05), not echo.
        frames, _ = _run_session([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}},
        ])
        self.assertEqual(frames[0]["result"]["protocolVersion"], "2024-11-05")
        # Older/equal requested versions are echoed.
        frames, _ = _run_session([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        ])
        self.assertEqual(frames[0]["result"]["protocolVersion"], "2024-11-05")

    def test_jsonrpc_error_semantics(self) -> None:
        frames, _ = _run_session([
            "not json at all",
            {"jsonrpc": "2.0", "id": 7, "method": "no/such/method"},
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "no_such_tool"}},
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "memory_get", "arguments": {}}},
            {"id": 10, "method": "ping"},  # missing jsonrpc field
        ])
        self.assertEqual(frames[0]["error"]["code"], -32700)  # parse error
        self.assertEqual(frames[1]["error"]["code"], -32601)  # unknown method
        self.assertEqual(frames[2]["error"]["code"], -32601)  # unknown tool
        self.assertEqual(frames[3]["error"]["code"], -32602)  # invalid params (id required)
        self.assertEqual(frames[4]["error"]["code"], -32600)  # invalid request

    def test_tool_round_trip_memory_add_then_search(self) -> None:
        frames, _ = _run_session([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "memory_add", "arguments": {
                "label": "Stdio regression note",
                "text": "The MCP stdio transport must stay newline-delimited JSON-RPC.",
                "type": "Constraint",
                "project_id": "architectos",
            }}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "memory_search", "arguments": {
                "query": "newline-delimited JSON-RPC transport", "project_id": "architectos",
            }}},
        ])
        self.assertFalse(frames[1]["result"].get("isError"))
        search_text = frames[2]["result"]["content"][0]["text"]
        self.assertIn("Stdio regression note", search_text)


if __name__ == "__main__":
    unittest.main()
