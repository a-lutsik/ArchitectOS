from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from backend.architectos.mcp_server import TOOLS, MemoryMCPServer


def _run(server: MemoryMCPServer, messages: list[dict]) -> list[dict]:
    stdin = io.StringIO("".join(json.dumps(message) + "\n" for message in messages))
    stdout = io.StringIO()
    server.serve(stdin=stdin, stdout=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


class MemoryMCPServerTests(unittest.TestCase):
    def _server(self, root: Path) -> MemoryMCPServer:
        return MemoryMCPServer(root)

    def test_initialize_and_tools_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(Path(tmp))
            responses = _run(server, [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ])
        self.assertEqual(len(responses), 2)  # notification produces no response
        init = responses[0]["result"]
        self.assertEqual(init["serverInfo"]["name"], "architectos-memory")
        self.assertIn("tools", init["capabilities"])
        tool_names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertEqual(tool_names, {tool["name"] for tool in TOOLS})
        self.assertIn("memory_search", tool_names)
        self.assertIn("memory_add", tool_names)

    def test_add_then_search_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(Path(tmp))
            responses = _run(server, [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "memory_add", "arguments": {
                    "label": "Router policy weighting",
                    "text": "The AI router blends quality, cost, speed, and availability into a weighted score.",
                    "type": "Decision",
                    "project_id": "architectos",
                }}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "memory_search", "arguments": {
                    "query": "router weighted score quality cost speed",
                    "project_id": "architectos",
                }}},
            ])
        add_result = responses[1]["result"]
        self.assertFalse(add_result.get("isError"))
        self.assertIn("Stored memory", add_result["content"][0]["text"])

        search_result = responses[2]["result"]
        self.assertFalse(search_result.get("isError"))
        text = search_result["content"][0]["text"]
        self.assertIn("Router policy weighting", text)
        payload = json.loads(text.split("```json", 1)[1].rsplit("```", 1)[0])
        self.assertGreaterEqual(len(payload["hits"]), 1)
        self.assertEqual(payload["hits"][0]["type"], "Decision")

    def test_context_tool_returns_briefing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(Path(tmp))
            responses = _run(server, [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "memory_add", "arguments": {
                    "label": "Local-first constraint",
                    "text": "ArchitectOS backend must use only the Python standard library.",
                }}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "memory_context", "arguments": {
                    "query": "python standard library constraint",
                }}},
            ])
        context_text = responses[2]["result"]["content"][0]["text"]
        self.assertIn("ArchitectOS Memory Context", context_text)

    def test_add_requires_label_and_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(Path(tmp))
            responses = _run(server, [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "memory_add", "arguments": {"label": "only label"}}},
            ])
        result = responses[1]["result"]
        self.assertTrue(result.get("isError"))
        self.assertIn("text is required", result["content"][0]["text"])

    def test_unknown_method_and_tool_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(Path(tmp))
            responses = _run(server, [
                {"jsonrpc": "2.0", "id": 1, "method": "does/not/exist"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "no_such_tool", "arguments": {}}},
            ])
        self.assertEqual(responses[0]["error"]["code"], -32601)
        self.assertEqual(responses[1]["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
