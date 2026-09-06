"""Tests for the platform-integration MCP tools (T0.A2-A7).

Covers project_create, memory_link, source_create/source_list, memory_history,
memory_add_bulk, source_id propagation, and allowed_scopes enforcement — driven
through the in-process MCP server harness used by test_mcp_server.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from backend.architectos.mcp_server import MemoryMCPServer


def _run(server: MemoryMCPServer, messages: list[dict]) -> list[dict]:
    stdin = io.StringIO("".join(json.dumps(message) + "\n" for message in messages))
    stdout = io.StringIO()
    server.serve(stdin=stdin, stdout=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"].split("```json", 1)[1].rsplit("```", 1)[0])


def _call(name: str, arguments: dict, call_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": call_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


class ProjectCreateToolTests(unittest.TestCase):
    def test_create_knowledge_project_without_root_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = MemoryMCPServer(Path(tmp))
            responses = _run(server, [
                _call("project_create", {"name": "Crypto Intel", "description": "News pipeline"}, 1),
                _call("project_create", {"name": "Crypto Intel"}, 2),  # idempotent by name
                _call("project_create", {}, 3),
                _call("memory_list_projects", {}, 4),
            ])
        created = _payload(responses[0]["result"])
        self.assertTrue(created["created"])
        self.assertEqual(created["root_path"], "")
        again = _payload(responses[1]["result"])
        self.assertFalse(again["created"])
        self.assertEqual(again["id"], created["id"])
        self.assertEqual(responses[2]["error"]["code"], -32602)
        projects = _payload(responses[3]["result"])["projects"]
        self.assertIn(created["id"], {p["id"] for p in projects})

    def test_create_project_with_root_path_still_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = MemoryMCPServer(Path(tmp))
            responses = _run(server, [
                _call("project_create", {"name": "Bad", "root_path": "/no/such/dir-xyz"}, 1),
                _call("project_create", {"name": "Ws", "root_path": tmp}, 2),
            ])
        self.assertTrue(responses[0]["result"].get("isError"))
        created = _payload(responses[1]["result"])
        self.assertTrue(created["created"])
        self.assertTrue(created["root_path"])

    def test_knowledge_project_survives_service_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = MemoryMCPServer(Path(tmp))
            project_id = _payload(_run(first, [
                _call("project_create", {"name": "Pipeline Knowledge"}, 1),
            ])[0]["result"])["id"]
            # A fresh service instance runs seed_if_empty (startup cleanup).
            second = MemoryMCPServer(Path(tmp))
            projects = _payload(_run(second, [_call("memory_list_projects", {}, 1)])[0]["result"])["projects"]
        self.assertIn(project_id, {p["id"] for p in projects})


class MemoryLinkToolTests(unittest.TestCase):
    def _two_nodes(self, server: MemoryMCPServer) -> tuple[str, str]:
        svc = server.service
        a = svc.add_memory({"project_id": "architectos", "label": "Link alpha", "type": "Decision",
                            "scope": "project", "text": "Alpha anchor fact for link tests."})["id"]
        b = svc.add_memory({"project_id": "architectos", "label": "Link beta", "type": "Lesson",
                            "scope": "project", "text": "Beta anchor fact for link tests."})["id"]
        return a, b

    def test_link_creates_edge_and_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = MemoryMCPServer(Path(tmp))
            a, b = self._two_nodes(server)
            responses = _run(server, [
                _call("memory_link", {"source": a, "target": b, "type": "supports"}, 1),
                _call("memory_link", {"source": a, "target": b, "type": "NOPE"}, 2),
                _call("memory_link", {"source": a, "type": "SUPPORTS"}, 3),
                _call("memory_link", {"source": a, "target": "node_missing", "type": "SUPPORTS"}, 4),
            ])
            linked = _payload(responses[0]["result"])
            edge_id = linked["edge_id"]
            stored = [e for e in server.service.repository.list_edges_touching([a]) if e.id == edge_id]
        self.assertEqual(linked["type"], "SUPPORTS")
        self.assertTrue(edge_id)
        self.assertTrue(stored and stored[0].target == b)
        self.assertEqual(responses[1]["error"]["code"], -32602)
        self.assertIn("one of", responses[1]["error"]["message"])
        self.assertEqual(responses[2]["error"]["code"], -32602)
        self.assertTrue(responses[3]["result"].get("isError"))

    def test_link_supersedes_retires_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = MemoryMCPServer(Path(tmp))
            a, b = self._two_nodes(server)
            responses = _run(server, [
                _call("memory_link", {"source": a, "target": b, "type": "SUPERSEDES"}, 1),
            ])
            target = server.service.repository.get_node(b)
            superseded_by = target.metadata.get("superseded_by")
            invalid_at = target.metadata.get("invalid_at")
        self.assertFalse(responses[0]["result"].get("isError"))
        self.assertEqual(superseded_by, a)
        self.assertTrue(invalid_at)


class SourceToolTests(unittest.TestCase):
    def test_source_create_list_and_memory_add_with_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = MemoryMCPServer(Path(tmp))
            project_id = _payload(_run(server, [
                _call("project_create", {"name": "Sources Proj"}, 1),
            ])[0]["result"])["id"]
            responses = _run(server, [
                _call("source_create", {"project_id": project_id, "name": "News RSS", "kind": "rss",
                                        "config": {"url": "https://example.com/feed"}}, 1),
                _call("source_create", {"project_id": project_id, "name": "News RSS"}, 2),  # idempotent
                _call("source_list", {"project_id": project_id}, 3),
                _call("source_list", {}, 4),
                _call("source_create", {"name": "No project"}, 5),
                _call("source_create", {"project_id": "proj_missing", "name": "Ghost"}, 6),
            ])
            source = _payload(responses[0]["result"])
            self.assertTrue(source["created"])
            self.assertEqual(source["kind"], "rss")
            dup = _payload(responses[1]["result"])
            self.assertFalse(dup["created"])
            self.assertEqual(dup["id"], source["id"])
            listed = _payload(responses[2]["result"])["sources"]
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["config"]["url"], "https://example.com/feed")
            self.assertGreaterEqual(len(_payload(responses[3]["result"])["sources"]), 1)
            self.assertEqual(responses[4]["error"]["code"], -32602)
            self.assertTrue(responses[5]["result"].get("isError"))

            add = _run(server, [
                _call("memory_add", {"label": "BTC rally", "text": "Bitcoin rallied on ETF inflows.",
                                     "project_id": project_id, "source_id": source["id"]}, 1),
                _call("memory_add", {"label": "Bad source", "text": "Node with a missing source id.",
                                     "project_id": project_id, "source_id": "src_missing"}, 2),
            ])
            added = _payload(add[0]["result"])
            node = server.service.repository.get_node(added["id"])
            node_source_id = node.metadata.get("source_id")
        self.assertEqual(added.get("source_id"), source["id"])
        self.assertEqual(node_source_id, source["id"])
        self.assertTrue(add[1]["result"].get("isError"))

    def test_search_filters_by_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = MemoryMCPServer(Path(tmp))
            svc = server.service
            src_a = svc.create_source({"project_id": "architectos", "name": "Feed A"})["id"]
            src_b = svc.create_source({"project_id": "architectos", "name": "Feed B"})["id"]
            svc.add_memory({"project_id": "architectos", "label": "Marker alpha", "type": "Lesson",
                            "scope": "project", "text": "Shared marker text alpha.", "source_id": src_a})
            svc.add_memory({"project_id": "architectos", "label": "Marker beta", "type": "Lesson",
                            "scope": "project", "text": "Shared marker text beta.", "source_id": src_b})
            responses = _run(server, [
                _call("memory_search", {"mode": "list", "query": "", "filters": {"source_id": src_a}}, 1),
            ])
        hits = _payload(responses[0]["result"])["hits"]
        labels = {h["label"] for h in hits}
        self.assertIn("Marker alpha", labels)
        self.assertNotIn("Marker beta", labels)


class MemoryHistoryToolTests(unittest.TestCase):
    def test_history_records_lifecycle_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = MemoryMCPServer(Path(tmp))
            node_id = _payload(_run(server, [
                _call("memory_add", {"label": "History subject", "text": "A fact worth tracking over time.",
                                     "project_id": "architectos"}, 1),
            ])[0]["result"])["id"]
            svc = server.service
            other = svc.add_memory({"project_id": "architectos", "label": "Newer fact",
                                    "type": "Lesson", "scope": "project",
                                    "text": "The replacement fact for the history subject."})["id"]
            svc.create_graph_edge({"source": other, "target": node_id, "type": "SUPERSEDES"})
            svc._tool_memory_get({"id": node_id, "include_neighbors": False})
            responses = _run(server, [
                _call("memory_history", {"id": node_id}, 1),
                _call("memory_history", {"id": "node_missing"}, 2),
                _call("memory_history", {}, 3),
            ])
        events = _payload(responses[0]["result"])["events"]
        kinds = [e["event_type"] for e in events]
        self.assertEqual(kinds[0], "created")
        self.assertIn("superseded", kinds)
        self.assertIn("accessed", kinds)
        superseded = next(e for e in events if e["event_type"] == "superseded")
        self.assertEqual(superseded["details"].get("superseded_by"), other)
        self.assertTrue(responses[1]["result"].get("isError"))
        self.assertEqual(responses[2]["error"]["code"], -32602)


class MemoryAddBulkToolTests(unittest.TestCase):
    def test_bulk_add_creates_dedupes_and_isolates_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = MemoryMCPServer(Path(tmp))
            svc = server.service
            src = svc.create_source({"project_id": "architectos", "name": "Bulk feed"})["id"]
            responses = _run(server, [
                _call("memory_add_bulk", {
                    "project_id": "architectos",
                    "source_id": src,
                    "items": [
                        {"label": "Item one", "text": "First bulk item text.", "type": "Lesson"},
                        {"label": "Item two", "text": "Second bulk item text."},
                        {"label": "Item one", "text": "First bulk item text.", "type": "Lesson"},  # exact dup
                        {"label": "", "text": "no label here"},  # per-item error
                        {"label": "No text"},
                    ],
                }, 1),
                _call("memory_add_bulk", {"items": "not-a-list"}, 2),
            ])
            result = _payload(responses[0]["result"])
            # Bulk path does not split multi-fact blobs: one item → one node.
            first_node = svc.repository.get_node(result["ids"][0])
            first_source_id = first_node.metadata.get("source_id")
        self.assertEqual(result["stats"]["created"], 2)
        self.assertEqual(result["stats"]["deduplicated"], 1)
        self.assertEqual(result["stats"]["errors"], 2)
        self.assertEqual(len(result["ids"]), 2)
        error_entries = [r for r in result["results"] if r.get("status") == "error"]
        self.assertEqual(len(error_entries), 2)
        self.assertTrue(all("index" in r and "error" in r for r in error_entries))
        self.assertEqual(responses[1]["error"]["code"], -32602)
        self.assertEqual(first_source_id, src)


class AllowedScopesTests(unittest.TestCase):
    def test_allowed_scopes_excludes_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = MemoryMCPServer(Path(tmp))
            svc = server.service
            svc.add_memory({"project_id": "architectos", "label": "Public fact", "type": "Lesson",
                            "scope": "public_knowledge", "text": "Visibility marker shared fact."})
            svc.add_memory({"project_id": "architectos", "label": "Private fact", "type": "Lesson",
                            "scope": "user_private", "text": "Visibility marker private fact."})
            # New scopes are accepted by VALID_SCOPES.
            from backend.architectos.models import VALID_SCOPES
            self.assertTrue({"public_knowledge", "project_shared", "user_private", "system_internal"} <= VALID_SCOPES)

            open_search = svc.search_memory("Visibility marker", project_id="architectos")
            open_labels = {h["node"]["label"] for h in open_search["hits"]}
            self.assertIn("Public fact", open_labels)
            self.assertIn("Private fact", open_labels)

            restricted = svc.search_memory(
                "Visibility marker", project_id="architectos",
                allowed_scopes=["public_knowledge", "project_shared"],
            )
            labels = {h["node"]["label"] for h in restricted["hits"]}
            self.assertIn("Public fact", labels)
            self.assertNotIn("Private fact", labels)

            list_mode = svc.search_memory("", project_id="architectos", mode="list",
                                          filters={"type": "Lesson"},
                                          allowed_scopes=["user_private"])
            list_labels = {h["node"]["label"] for h in list_mode["hits"]}
            self.assertIn("Private fact", list_labels)
            self.assertNotIn("Public fact", list_labels)

    def test_allowed_scopes_via_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = MemoryMCPServer(Path(tmp))
            svc = server.service
            svc.add_memory({"project_id": "architectos", "label": "MCP visible", "type": "Lesson",
                            "scope": "project", "text": "Scope gate marker visible."})
            svc.add_memory({"project_id": "architectos", "label": "MCP hidden", "type": "Lesson",
                            "scope": "system_internal", "text": "Scope gate marker hidden."})
            responses = _run(server, [
                _call("memory_search", {"query": "Scope gate marker", "project_id": "architectos",
                                        "allowed_scopes": ["project", "shared"]}, 1),
            ])
        hits = _payload(responses[0]["result"])["hits"]
        labels = {h["label"] for h in hits}
        self.assertIn("MCP visible", labels)
        self.assertNotIn("MCP hidden", labels)


if __name__ == "__main__":
    unittest.main()
