from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from backend.architectos.lsp import CodeIntelligenceManager, LSPClient, flatten_symbols

FAKE_LSP_SERVER = textwrap.dedent(
    """
    import json, sys

    def read_message():
        headers = {}
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            text = line.decode("utf-8", "replace").strip()
            if text == "":
                break
            if ":" in text:
                key, _, value = text.partition(":")
                headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length", 0))
        if length <= 0:
            return {}
        return json.loads(sys.stdin.buffer.read(length).decode("utf-8", "replace"))

    def write_message(obj):
        body = json.dumps(obj).encode("utf-8")
        sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode() + b"\\r\\n\\r\\n" + body)
        sys.stdout.buffer.flush()

    while True:
        msg = read_message()
        if msg is None:
            break
        mid = msg.get("id")
        method = msg.get("method")
        if mid is None:
            if method == "exit":
                break
            continue
        if method == "initialize":
            res = {"capabilities": {"documentSymbolProvider": True}, "serverInfo": {"name": "fake-lsp"}}
        elif method == "textDocument/documentSymbol":
            res = [{"name": "main", "kind": 12, "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}},
                    "children": [{"name": "inner", "kind": 13, "range": {"start": {"line": 1, "character": 2}, "end": {"line": 1, "character": 7}}}]}]
        elif method == "textDocument/hover":
            res = {"contents": {"kind": "plaintext", "value": "hover text"}}
        else:
            res = None
        write_message({"jsonrpc": "2.0", "id": mid, "result": res})
    """
)


class _Store:
    def __init__(self, servers):
        self.servers = servers

    def load(self):
        return self.servers

    def save(self, servers):
        self.servers = servers


class LSPClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script = self.root / "fake_lsp.py"
        self.script.write_text(FAKE_LSP_SERVER, encoding="utf-8")
        self.doc = self.root / "sample.py"
        self.doc.write_text("def main():\n    inner = 1\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _command(self):
        return [sys.executable, str(self.script)]

    def test_client_returns_document_symbols(self) -> None:
        with LSPClient(self._command(), self.root) as client:
            info = client.initialize()
            self.assertEqual(info["serverInfo"]["name"], "fake-lsp")
            client.did_open(self.doc, "python", self.doc.read_text())
            symbols = client.document_symbols(self.doc)
            flat = flatten_symbols(symbols)
            self.assertEqual(flat[0]["name"], "main")
            self.assertEqual(flat[0]["kind"], "Function")
            self.assertEqual(flat[1]["name"], "inner")
            self.assertEqual(flat[1]["depth"], 1)

    def test_client_hover(self) -> None:
        with LSPClient(self._command(), self.root) as client:
            client.initialize()
            client.did_open(self.doc, "python", self.doc.read_text())
            hover = client.hover(self.doc, 0, 4)
            self.assertEqual(hover.get("contents", {}).get("value"), "hover text")

    def test_manager_symbols_selects_server_by_extension(self) -> None:
        store = _Store([
            {"id": "python", "label": "Python", "language_id": "python", "extensions": [".py"], "command": self._command(), "enabled": True},
        ])
        manager = CodeIntelligenceManager(self.root, store.load, store.save)
        result = manager.symbols(self.root, "sample.py", self.doc.read_text(), ".py")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["symbols"][0]["name"], "main")

    def test_manager_rejects_unknown_extension(self) -> None:
        store = _Store([{"id": "python", "label": "Python", "language_id": "python", "extensions": [".py"], "command": self._command(), "enabled": True}])
        manager = CodeIntelligenceManager(self.root, store.load, store.save)
        with self.assertRaises(Exception):
            manager.symbols(self.root, "sample.rs", "fn main() {}", ".rs")

    def test_manager_check_reports_missing_executable(self) -> None:
        store = _Store([{"id": "rust", "label": "Rust", "language_id": "rust", "extensions": [".rs"], "command": ["definitely-not-real-analyzer-xyz"], "enabled": True}])
        manager = CodeIntelligenceManager(self.root, store.load, store.save)
        check = manager.check("rust")
        self.assertFalse(check["ready"])
        self.assertEqual(check["status"], "missing_executable")

    def test_manager_uses_python_fallback_when_lsp_is_missing(self) -> None:
        store = _Store([{"id": "python", "label": "Python", "language_id": "python", "extensions": [".py"], "command": ["definitely-not-real-pyright-xyz"], "enabled": True}])
        manager = CodeIntelligenceManager(self.root, store.load, store.save)
        result = manager.symbols(self.root, "sample.py", "class App:\n    def run(self):\n        return 1\n", ".py")
        self.assertTrue(result["fallback"])
        self.assertEqual(result["source"], "fallback")
        self.assertEqual([item["name"] for item in result["symbols"]], ["App", "run"])

    def test_manager_fallback_diagnostics_reports_python_syntax_error(self) -> None:
        manager = CodeIntelligenceManager(self.root, lambda: [], lambda _servers: None)
        result = manager.diagnostics("broken.py", "def broken(:\n    pass\n", ".py")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["diagnostics"][0]["severity"], "error")

    def test_manager_fallback_references_searches_java_python_js(self) -> None:
        manager = CodeIntelligenceManager(self.root, lambda: [], lambda _servers: None)
        java = "class Demo {\n  void run() {}\n  void call() { run(); }\n}\n"
        refs = manager.references(self.root, "Demo.java", java, ".java", 1, 7)
        self.assertTrue(refs["fallback"])
        self.assertEqual(refs["query"], "run")
        self.assertEqual(refs["count"], 2)


if __name__ == "__main__":
    unittest.main()
