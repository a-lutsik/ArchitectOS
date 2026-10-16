from __future__ import annotations

import io
import json
import re
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
        self.assertIn("memory_list_projects", init["instructions"])
        self.assertIn("memory_use_project", init["instructions"])
        self.assertIn("project_create", init["instructions"])
        self.assertIn("memory_turn", init["instructions"])
        self.assertIn("memory_search", init["instructions"])
        tool_names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertEqual(tool_names, {tool["name"] for tool in TOOLS})
        self.assertIn("memory_search", tool_names)
        self.assertIn("memory_turn", tool_names)
        self.assertIn("memory_add", tool_names)
        self.assertIn("memory_use_project", tool_names)

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
                    "project_id": "architectos",
                }}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "memory_context", "arguments": {
                    "query": "python standard library constraint",
                    "project_id": "architectos",
                }}},
            ])
        context_text = responses[2]["result"]["content"][0]["text"]
        self.assertIn("ArchitectOS Memory Context", context_text)
        self.assertTrue(
            "Stable memory" in context_text or "Retrieved for this query:" in context_text
        )

    def test_add_requires_label_and_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(Path(tmp))
            responses = _run(server, [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "memory_add", "arguments": {"label": "only label", "project_id": "architectos"}}},
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

    def _add_sample(self, server: MemoryMCPServer) -> str:
        responses = _run(server, [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_add", "arguments": {
                "label": "Discount rounding bug",
                "text": "Discount was calculated before taxes; it must apply to the net amount after tax removal.",
                "type": "Lesson",
                "project_id": "architectos",
            }}},
        ])
        payload = json.loads(responses[0]["result"]["content"][0]["text"].split("```json", 1)[1].rsplit("```", 1)[0])
        return payload["id"]

    def test_memory_get_returns_full_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(Path(tmp))
            node_id = self._add_sample(server)
            responses = _run(server, [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_get", "arguments": {"id": node_id}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "memory_get", "arguments": {"id": "missing-id"}}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "memory_get", "arguments": {}}},
            ])
        result = responses[0]["result"]
        self.assertFalse(result.get("isError"))
        text = result["content"][0]["text"]
        self.assertIn("Discount rounding bug", text)
        self.assertIn("net amount after tax removal", text)
        payload = json.loads(text.split("```json", 1)[1].rsplit("```", 1)[0])
        self.assertEqual(payload["id"], node_id)
        self.assertEqual(payload["type"], "Lesson")
        self.assertIn("neighbors", payload)
        self.assertIn("not found", responses[1]["result"]["content"][0]["text"])
        self.assertEqual(responses[2]["error"]["code"], -32602)

    def test_memory_feedback_records_rating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(Path(tmp))
            node_id = self._add_sample(server)
            responses = _run(server, [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_feedback", "arguments": {
                    "rating": 1, "hit_ids": [node_id], "query": "discount tax", "note": "exactly what was needed",
                    "project_id": "architectos",
                }}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "memory_feedback", "arguments": {"rating": 5}}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "memory_feedback", "arguments": {}}},
            ])
        result = responses[0]["result"]
        self.assertFalse(result.get("isError"))
        self.assertIn("useful", result["content"][0]["text"])
        payload = json.loads(result["content"][0]["text"].split("```json", 1)[1].rsplit("```", 1)[0])
        self.assertEqual(payload["hit_count"], 1)
        self.assertEqual(responses[1]["error"]["code"], -32602)
        self.assertEqual(responses[2]["error"]["code"], -32602)

    def test_explain_path_between_connected_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(Path(tmp))
            source = server.service.add_memory({
                "project_id": "architectos", "label": "Path source", "type": "Decision",
                "scope": "project", "text": "Zephyr routing decision source anchor.",
            })["id"]
            target = server.service.add_memory({
                "project_id": "architectos", "label": "Path target", "type": "Lesson",
                "scope": "project", "text": "Zephyr routing lesson target anchor.",
            })["id"]
            server.service.repository.add_edge(source, target, "SUPPORTS", "project")
            responses = _run(server, [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_explain_path", "arguments": {
                    "source_id": source, "target_id": target,
                }}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "memory_explain_path", "arguments": {
                    "source_id": source,
                }}},
            ])
        result = responses[0]["result"]
        self.assertFalse(result.get("isError"))
        payload = json.loads(result["content"][0]["text"].split("```json", 1)[1].rsplit("```", 1)[0])
        self.assertTrue(payload["found"])
        self.assertIn("SUPPORTS", payload["edge_types"])
        self.assertEqual(responses[1]["error"]["code"], -32602)

    def test_themes_tool_lists_communities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(Path(tmp))
            ids: dict[str, str] = {}
            for label, text in [
                ("Theme A", "Redis cache eviction policy alpha marker"),
                ("Theme B", "Redis cache eviction policy beta marker"),
                ("Theme C", "Redis cache eviction policy gamma marker"),
            ]:
                ids[label] = server.service.add_memory({
                    "project_id": "architectos", "label": label, "type": "Concept",
                    "scope": "project", "text": text,
                })["id"]
            for a, b in [("Theme A", "Theme B"), ("Theme B", "Theme C"), ("Theme A", "Theme C")]:
                server.service.repository.add_edge(ids[a], ids[b], "DEPENDS_ON", "project")
            responses = _run(server, [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_themes", "arguments": {
                    "query": "redis cache", "project_id": "architectos",
                }}},
            ])
        result = responses[0]["result"]
        self.assertFalse(result.get("isError"))
        payload = json.loads(result["content"][0]["text"].split("```json", 1)[1].rsplit("```", 1)[0])
        self.assertIn("themes", payload)
        self.assertTrue(any("redis" in (t.get("keywords") or []) for t in payload["themes"]))

    def _seed_code_graph(self, server: MemoryMCPServer) -> None:
        repo = server.service.repository
        helpers = repo.add_node(
            "Artifact", "pkg/helpers.py", "project", "file helpers", "architectos",
            metadata={"source": "project_scan", "path": "pkg/helpers.py"},
        )
        main = repo.add_node(
            "Artifact", "pkg/main.py", "project", "file main", "architectos",
            metadata={"source": "project_scan", "path": "pkg/main.py"},
        )
        server.service.code_graph.ingest_files(
            [
                (helpers, "pkg/helpers.py", ".py", "def helper():\n    '''Reusable.'''\n    return 1\n"),
                (main, "pkg/main.py", ".py",
                 "from .helpers import helper\n\nclass App:\n    def run(self):\n        return helper()\n"),
            ],
            "architectos",
        )

    def test_code_neighbors_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(Path(tmp))
            self._seed_code_graph(server)
            responses = _run(server, [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "code_neighbors", "arguments": {
                    "symbol": "helper", "project_id": "architectos",
                }}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "code_neighbors", "arguments": {}}},
            ])
        result = responses[0]["result"]
        self.assertFalse(result.get("isError"))
        payload = json.loads(result["content"][0]["text"].split("```json", 1)[1].rsplit("```", 1)[0])
        self.assertTrue(payload["found"])
        self.assertIn("pkg/main.py::App.run", {c["label"] for c in payload["callers"]})
        self.assertEqual(responses[1]["error"]["code"], -32602)

    def test_code_impact_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(Path(tmp))
            self._seed_code_graph(server)
            responses = _run(server, [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "code_impact", "arguments": {
                    "symbol": "helper", "depth": 3, "project_id": "architectos",
                }}},
            ])
        result = responses[0]["result"]
        self.assertFalse(result.get("isError"))
        payload = json.loads(result["content"][0]["text"].split("```json", 1)[1].rsplit("```", 1)[0])
        self.assertTrue(payload["found"])
        labels = {item["label"] for item in payload["impacted"]}
        self.assertIn("pkg/main.py::App.run", labels)

    def test_resources_list_and_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = MemoryMCPServer(Path(tmp), project_id="architectos")
            node_id = self._add_sample(server)
            responses = _run(server, [
                {"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
                {"jsonrpc": "2.0", "id": 2, "method": "resources/templates/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": "memory://projects"}},
                {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": "memory://review"}},
                {"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {"uri": f"memory://nodes/{node_id}"}},
                {"jsonrpc": "2.0", "id": 6, "method": "resources/read", "params": {"uri": "memory://unknown"}},
            ])
        uris = {r["uri"] for r in responses[0]["result"]["resources"]}
        self.assertIn("memory://projects", uris)
        self.assertIn("memory://review", uris)
        self.assertIn("memory://briefing", uris)
        templates = responses[1]["result"]["resourceTemplates"]
        self.assertEqual(templates[0]["uriTemplate"], "memory://nodes/{id}")
        projects = json.loads(responses[2]["result"]["contents"][0]["text"])
        self.assertIn("projects", projects)
        review = json.loads(responses[3]["result"]["contents"][0]["text"])
        self.assertIn("candidates", review)
        node = json.loads(responses[4]["result"]["contents"][0]["text"])
        self.assertEqual(node["id"], node_id)
        self.assertIn("net amount after tax removal", node["text"])
        self.assertEqual(responses[5]["error"]["code"], -32602)

    def test_briefing_resource_after_constraint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = MemoryMCPServer(Path(tmp), project_id="architectos")
            _run(server, [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_add", "arguments": {
                    "label": "Never commit to main",
                    "text": "Do not commit directly to the main branch. Use pull requests.",
                    "type": "Constraint",
                    "project_id": "architectos",
                }}},
            ])
            responses = _run(server, [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": "memory://briefing"}},
            ])
        instructions = responses[0]["result"]["instructions"]
        briefing = responses[1]["result"]["contents"][0]["text"]
        self.assertIn("Never commit to main", instructions)
        self.assertIn("Never commit to main", briefing)
        self.assertIn("Stable memory", briefing)

    def test_memory_turn_packs_and_queues_constraint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(Path(tmp))
            responses = _run(server, [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_turn", "arguments": {
                    "user_text": "Architecture decision: never commit directly to main.",
                    "project_id": "architectos",
                }}},
            ])
        result = responses[0]["result"]
        self.assertFalse(result.get("isError"))
        text = result["content"][0]["text"]
        payload = json.loads(text.split("```json", 1)[1].rsplit("```", 1)[0])
        self.assertTrue(payload.get("kept"))
        self.assertGreaterEqual(len(payload.get("queued") or []), 1)
        self.assertEqual(payload["queued"][0]["type"], "Decision")

    def test_memory_turn_auto_writes_low_risk_lesson(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(Path(tmp))
            responses = _run(server, [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_turn", "arguments": {
                    "user_text": "Remember: the team prefers pytest for unit tests in this repository.",
                    "project_id": "architectos",
                }}},
            ])
        result = responses[0]["result"]
        self.assertFalse(result.get("isError"))
        payload = json.loads(result["content"][0]["text"].split("```json", 1)[1].rsplit("```", 1)[0])
        self.assertTrue(payload.get("kept"))
        self.assertGreaterEqual(len(payload.get("auto_accepted") or []), 1)

    def test_memory_add_splits_multi_fact_blob(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = self._server(Path(tmp))
            responses = _run(server, [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_add", "arguments": {
                    "label": "Repo rules",
                    "text": "Never commit directly to main.\nRemember: the team prefers pytest for unit tests.",
                    "project_id": "architectos",
                }}},
            ])
        result = responses[0]["result"]
        self.assertFalse(result.get("isError"))
        text = result["content"][0]["text"]
        self.assertIn("Split into", text)
        payload = json.loads(text.split("```json", 1)[1].rsplit("```", 1)[0])
        self.assertGreaterEqual(len(payload.get("also_created") or []), 1)


def _tool_payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"].split("```json", 1)[1].rsplit("```", 1)[0])


class ProjectPinTests(unittest.TestCase):
    def test_pinned_project_id_scopes_writes_and_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / "store"
            shop_dir = root / "shop"
            blog_dir = root / "blog"
            shop_dir.mkdir()
            blog_dir.mkdir()
            setup = MemoryMCPServer(store, cwd=root)
            created = _run(setup, [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "project_create", "arguments": {"name": "Shop", "root_path": str(shop_dir)}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "project_create", "arguments": {"name": "Blog", "root_path": str(blog_dir)}}},
            ])
            shop_id = _tool_payload(created[0]["result"])["id"]
            blog_id = _tool_payload(created[1]["result"])["id"]

            shop = MemoryMCPServer(store, project_id=shop_id, cwd=shop_dir)
            blog = MemoryMCPServer(store, project_id=blog_id, cwd=blog_dir)
            shop_add = _run(shop, [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_add", "arguments": {
                    "label": "Shop checkout rule",
                    "text": "Shop checkout rounds tax only after discounts are applied to the net amount.",
                    "type": "Decision",
                }}},
            ])
            _run(blog, [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_add", "arguments": {
                    "label": "Blog publish rule",
                    "text": "Blog posts publish only from the main branch after editorial review.",
                    "type": "Decision",
                }}},
            ])
            shop_search = _run(shop, [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_search", "arguments": {
                    "query": "checkout rounds tax discounts",
                }}},
            ])
            blog_search = _run(blog, [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_search", "arguments": {
                    "query": "checkout rounds tax discounts",
                }}},
            ])
            shop_init = _run(shop, [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            ])

        self.assertEqual(_tool_payload(shop_add[0]["result"])["project_id"], shop_id)
        shop_hits = _tool_payload(shop_search[0]["result"])["hits"]
        self.assertTrue(shop_hits)
        self.assertTrue(all(hit["project_id"] == shop_id for hit in shop_hits))
        self.assertIn("Shop checkout rule", shop_search[0]["result"]["content"][0]["text"])
        self.assertNotIn("Shop checkout rule", blog_search[0]["result"]["content"][0]["text"])
        self.assertIn(shop_id, shop_init[0]["result"]["instructions"])

    def test_explicit_project_id_overrides_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / "store"
            shop_dir = root / "shop"
            blog_dir = root / "blog"
            shop_dir.mkdir()
            blog_dir.mkdir()
            setup = MemoryMCPServer(store, cwd=root)
            created = _run(setup, [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "project_create", "arguments": {"name": "Shop", "root_path": str(shop_dir)}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "project_create", "arguments": {"name": "Blog", "root_path": str(blog_dir)}}},
            ])
            shop_id = _tool_payload(created[0]["result"])["id"]
            blog_id = _tool_payload(created[1]["result"])["id"]
            shop = MemoryMCPServer(store, project_id=shop_id, cwd=shop_dir)
            added = _run(shop, [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory_add", "arguments": {
                    "label": "Blog only note",
                    "text": "Editorial calendar locks the blog homepage every Friday afternoon.",
                    "type": "Decision",
                    "project_id": blog_id,
                }}},
            ])
        self.assertEqual(_tool_payload(added[0]["result"])["project_id"], blog_id)

    def test_resolve_project_id_from_env_and_cli(self) -> None:
        from backend.architectos.mcp_server import resolve_mcp_project_id

        self.assertEqual(resolve_mcp_project_id([], {"ARCHITECTOS_PROJECT_ID": "blog"}), "blog")
        self.assertEqual(resolve_mcp_project_id(["--project-id", "shop"], {"ARCHITECTOS_PROJECT_ID": "blog"}), "shop")
        self.assertEqual(resolve_mcp_project_id(["--project-id=shop"], {}), "shop")
        self.assertEqual(resolve_mcp_project_id([], {}), "")


def _read_bind_file(folder: Path) -> dict:
    return json.loads((folder / ".architectos.json").read_text(encoding="utf-8"))


def _write_bind_file(folder: Path, project_id: str, name: str) -> None:
    (folder / ".architectos.json").write_text(
        json.dumps({"project_id": project_id, "name": name}, indent=2) + "\n",
        encoding="utf-8",
    )


def _call_tool(name: str, arguments: dict, call_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": call_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


class ProjectBindTests(unittest.TestCase):
    def _layout(self, tmp: str) -> tuple[Path, Path, Path, Path, Path]:
        root = Path(tmp)
        store = root / "store"
        shop_dir = root / "shop"
        blog_dir = root / "blog"
        workspace = root / "workspace"
        shop_dir.mkdir()
        blog_dir.mkdir()
        workspace.mkdir()
        return root, store, shop_dir, blog_dir, workspace

    def _seed_shop_blog(self, store: Path, shop_dir: Path, blog_dir: Path) -> tuple[str, str]:
        setup_ws = store.parent / "setup-ws"
        setup_ws.mkdir()
        setup = MemoryMCPServer(store, cwd=setup_ws)
        shop = setup.service.create_project({"name": "Shop", "root_path": str(shop_dir)})
        blog = setup.service.create_project({"name": "Blog", "root_path": str(blog_dir)})
        return shop["id"], blog["id"]

    def test_unbound_memory_add_errors_with_project_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, store, shop_dir, blog_dir, workspace = self._layout(tmp)
            shop_id, _blog_id = self._seed_shop_blog(store, shop_dir, blog_dir)
            server = MemoryMCPServer(store, cwd=workspace)
            responses = _run(server, [
                _call_tool("memory_add", {
                    "label": "Unbound note",
                    "text": "This must not be stored until the folder is bound to a project.",
                    "type": "Decision",
                }),
            ])
            bind_exists = (workspace / ".architectos.json").exists()
        result = responses[0]["result"]
        self.assertTrue(result.get("isError"))
        text = result["content"][0]["text"]
        self.assertIn("memory_list_projects", text)
        self.assertIn("memory_use_project", text)
        self.assertIn("project_create", text)
        self.assertIn("Shop", text)
        self.assertIn(shop_id, text)
        self.assertFalse(bind_exists)

    def test_memory_use_project_by_name_writes_file_and_scopes_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, store, shop_dir, blog_dir, workspace = self._layout(tmp)
            shop_id, blog_id = self._seed_shop_blog(store, shop_dir, blog_dir)
            server = MemoryMCPServer(store, cwd=workspace)
            listed = _run(server, [_call_tool("memory_list_projects", {})])
            list_payload = _tool_payload(listed[0]["result"])
            bound = _run(server, [
                _call_tool("memory_use_project", {"name": "Shop"}),
            ])
            added = _run(server, [
                _call_tool("memory_add", {
                    "label": "Shop checkout rule",
                    "text": "Shop checkout rounds tax only after discounts are applied to the net amount.",
                    "type": "Decision",
                }),
            ])
            searched = _run(server, [
                _call_tool("memory_search", {"query": "checkout rounds tax discounts"}),
            ])
            init = _run(server, [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}])
            bind = _read_bind_file(workspace)
            store_has_bind = (store / ".architectos.json").exists()

        self.assertTrue(list_payload.get("create_new"))
        self.assertFalse(bound[0]["result"].get("isError"))
        self.assertEqual(bind["project_id"], shop_id)
        self.assertEqual(bind["name"], "Shop")
        self.assertFalse(store_has_bind)
        self.assertEqual(_tool_payload(added[0]["result"])["project_id"], shop_id)
        hits = _tool_payload(searched[0]["result"])["hits"]
        self.assertTrue(hits)
        self.assertTrue(all(hit["project_id"] == shop_id for hit in hits))
        self.assertNotIn(blog_id, searched[0]["result"]["content"][0]["text"])
        self.assertIn(shop_id, init[0]["result"]["instructions"])

    def test_new_folder_without_bind_file_is_unbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, store, shop_dir, blog_dir, workspace = self._layout(tmp)
            shop_id, _blog_id = self._seed_shop_blog(store, shop_dir, blog_dir)
            server = MemoryMCPServer(store, cwd=workspace)
            _run(server, [_call_tool("memory_use_project", {"name": "Shop"})])
            other = store.parent / "other-app"
            other.mkdir()
            fresh = MemoryMCPServer(store, cwd=other)
            responses = _run(fresh, [
                _call_tool("memory_add", {
                    "label": "Other folder note",
                    "text": "A new folder must ask which ArchitectOS project to use.",
                    "type": "Decision",
                }),
            ])
            init = _run(fresh, [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}])
        self.assertTrue(responses[0]["result"].get("isError"))
        self.assertIn("memory_list_projects", responses[0]["result"]["content"][0]["text"])
        self.assertIn(shop_id, responses[0]["result"]["content"][0]["text"])
        instructions = init[0]["result"]["instructions"]
        self.assertIn("memory_list_projects", instructions)
        self.assertIn("memory_use_project", instructions)
        self.assertIn("project_create", instructions)
        self.assertNotIn(shop_id, instructions)

    def test_project_create_from_unbound_server_binds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, store, _shop_dir, _blog_dir, workspace = self._layout(tmp)
            server = MemoryMCPServer(store, cwd=workspace)
            created = _run(server, [
                _call_tool("project_create", {"name": "New App"}),
            ])
            payload = _tool_payload(created[0]["result"])
            added = _run(server, [
                _call_tool("memory_add", {
                    "label": "New app rule",
                    "text": "The new app stores session cookies only on the same site.",
                    "type": "Decision",
                }),
            ])
            stored = server.service.repository.get_project(payload["id"])
            bind = _read_bind_file(workspace)
            store_has_bind = (store / ".architectos.json").exists()
            stored_root = Path(stored.root_path).resolve()
            payload_root = Path(payload["root_path"]).resolve()
            workspace_root = workspace.resolve()
        self.assertFalse(created[0]["result"].get("isError"))
        self.assertEqual(bind["project_id"], payload["id"])
        self.assertEqual(bind["name"], "New App")
        self.assertFalse(store_has_bind)
        self.assertEqual(_tool_payload(added[0]["result"])["project_id"], payload["id"])
        self.assertEqual(stored_root, workspace_root)
        self.assertEqual(payload_root, workspace_root)

    def test_env_pin_beats_bind_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, store, shop_dir, blog_dir, workspace = self._layout(tmp)
            shop_id, blog_id = self._seed_shop_blog(store, shop_dir, blog_dir)
            _write_bind_file(workspace, blog_id, "Blog")
            pinned = MemoryMCPServer(store, project_id=shop_id, cwd=workspace)
            added = _run(pinned, [
                _call_tool("memory_add", {
                    "label": "Pinned shop note",
                    "text": "Environment pin must win over the folder bind file.",
                    "type": "Decision",
                }),
            ])
        self.assertEqual(_tool_payload(added[0]["result"])["project_id"], shop_id)

    def test_bind_file_walks_up_from_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, store, shop_dir, blog_dir, workspace = self._layout(tmp)
            shop_id, _blog_id = self._seed_shop_blog(store, shop_dir, blog_dir)
            _write_bind_file(workspace, shop_id, "Shop")
            nested = workspace / "src" / "app"
            nested.mkdir(parents=True)
            server = MemoryMCPServer(store, cwd=nested)
            added = _run(server, [
                _call_tool("memory_add", {
                    "label": "Nested folder note",
                    "text": "Bind file in a parent folder applies to nested working directories.",
                    "type": "Decision",
                }),
            ])
        self.assertEqual(_tool_payload(added[0]["result"])["project_id"], shop_id)

    def test_path_root_binds_when_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, store, shop_dir, blog_dir, _workspace = self._layout(tmp)
            shop_id, _blog_id = self._seed_shop_blog(store, shop_dir, blog_dir)
            nested = shop_dir / "src"
            nested.mkdir()
            server = MemoryMCPServer(store, cwd=nested)
            added = _run(server, [
                _call_tool("memory_add", {
                    "label": "Path bind note",
                    "text": "A project whose root_path contains cwd binds without a file.",
                    "type": "Decision",
                }),
            ])
        self.assertEqual(_tool_payload(added[0]["result"])["project_id"], shop_id)

    def test_ambiguous_name_lists_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, store, shop_dir, blog_dir, workspace = self._layout(tmp)
            extra = store.parent / "shop-two"
            extra.mkdir()
            setup_ws = store.parent / "setup-ws"
            setup_ws.mkdir()
            setup = MemoryMCPServer(store, cwd=setup_ws)
            first = setup.service.create_project({"name": "Shop", "root_path": str(shop_dir)})
            second = setup.service.create_project({"name": "Shop", "root_path": str(extra)})
            server = MemoryMCPServer(store, cwd=workspace)
            responses = _run(server, [
                _call_tool("memory_use_project", {"name": "Shop"}),
            ])
            bind_exists = (workspace / ".architectos.json").exists()
        result = responses[0]["result"]
        self.assertTrue(result.get("isError"))
        text = result["content"][0]["text"]
        self.assertIn(first["id"], text)
        self.assertIn(second["id"], text)
        self.assertFalse(bind_exists)

    def test_use_project_does_not_overwrite_existing_root_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, store, shop_dir, blog_dir, workspace = self._layout(tmp)
            shop_id, _blog_id = self._seed_shop_blog(store, shop_dir, blog_dir)
            server = MemoryMCPServer(store, cwd=workspace)
            _run(server, [_call_tool("memory_use_project", {"name": "Shop", "cwd": str(workspace)})])
            stored = server.service.repository.get_project(shop_id)
        self.assertEqual(Path(stored.root_path).resolve(), shop_dir.resolve())

    def test_use_project_fills_empty_root_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, store, _shop_dir, _blog_dir, workspace = self._layout(tmp)
            setup_ws = store.parent / "setup-ws"
            setup_ws.mkdir()
            setup = MemoryMCPServer(store, cwd=setup_ws)
            created = setup.service.create_project({"name": "Intel"})
            server = MemoryMCPServer(store, cwd=workspace)
            _run(server, [_call_tool("memory_use_project", {"name": "Intel"})])
            stored = server.service.repository.get_project(created["id"])
        self.assertEqual(Path(stored.root_path).resolve(), workspace.resolve())

    def test_unbound_initialize_asks_user_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, store, _shop_dir, _blog_dir, workspace = self._layout(tmp)
            server = MemoryMCPServer(store, cwd=workspace)
            init = _run(server, [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}])
        instructions = init[0]["result"]["instructions"]
        self.assertIn("memory_list_projects", instructions)
        self.assertIn("memory_use_project", instructions)
        self.assertIn("project_create", instructions)
        self.assertIn("create a new project", instructions.lower())


class ResolveRootTests(unittest.TestCase):
    def test_architectos_root_env(self) -> None:
        import os
        from unittest import mock

        from backend.architectos.mcp_server import _resolve_root

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"ARCHITECTOS_ROOT": tmp}):
                self.assertEqual(_resolve_root(), Path(tmp).resolve())

    def test_frozen_defaults_to_home_architectos(self) -> None:
        import os
        import sys
        from unittest import mock

        from backend.architectos.mcp_server import _resolve_root

        env = {k: v for k, v in os.environ.items() if k != "ARCHITECTOS_ROOT"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(sys, "frozen", True, create=True):
                self.assertEqual(_resolve_root(), (Path.home() / "ArchitectOS").resolve())


if __name__ == "__main__":
    unittest.main()
