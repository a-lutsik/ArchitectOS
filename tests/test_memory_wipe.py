"""Hard-delete wipe: clear source memory and wipe project memory."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.architectos.service import ArchitectOSService


class MemoryWipeTests(unittest.TestCase):
    def test_clear_source_preview_does_not_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            project = service.create_project({"id": "wipe-demo", "name": "Wipe Demo", "root_path": str(tmp)})
            sources = service.list_project_sources(project["id"])
            docs = next(item for item in sources if item["kind"] == "docs")
            service.add_memory({
                "project_id": project["id"],
                "label": "Docs fact",
                "type": "Decision",
                "scope": "project",
                "text": "Docs source owns this durable memory node.",
                "source_id": docs["id"],
            })
            preview = service.clear_source_memory(docs["id"], confirm=False)
            self.assertFalse(preview.get("confirmed"))
            self.assertGreaterEqual(preview["nodes"], 1)
            still = service.repository.list_nodes(project_id=project["id"])
            self.assertTrue(any(n.label == "Docs fact" for n in still))
            self.assertIsNotNone(service.repository.get_source(docs["id"]))

    def test_clear_source_removes_nodes_keeps_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "readme.md").write_text("# Hello\n\nOwned by docs source.", encoding="utf-8")
            service = ArchitectOSService(root)
            project = service.create_project({"id": "wipe-docs", "name": "Wipe Docs", "root_path": str(root)})
            sources = service.list_project_sources(project["id"])
            docs = next(item for item in sources if item["kind"] == "docs")
            adr = next(item for item in sources if item["kind"] == "adr")
            service.add_memory({
                "project_id": project["id"],
                "label": "Docs only",
                "type": "Doc",
                "scope": "project",
                "text": "This memory belongs to the docs binding only.",
                "source_id": docs["id"],
            })
            service.add_memory({
                "project_id": project["id"],
                "label": "ADR only",
                "type": "Decision",
                "scope": "project",
                "text": "This memory belongs to the adr binding only.",
                "source_id": adr["id"],
            })
            # Candidate tied to docs
            service.repository.upsert_memory_candidate({
                "id": "cand-docs-1",
                "project_id": project["id"],
                "source_type": "docs",
                "status": "candidate",
                "label": "Docs candidate",
                "text": "Pending docs candidate text here.",
                "metadata": {"source_id": docs["id"]},
            })

            result = service.clear_source_memory(docs["id"], confirm=True)
            self.assertTrue(result.get("confirmed"))
            self.assertGreaterEqual(result["nodes"], 1)
            self.assertGreaterEqual(result["candidates"], 1)

            nodes = service.repository.list_nodes(project_id=project["id"])
            labels = {n.label for n in nodes}
            self.assertNotIn("Docs only", labels)
            self.assertIn("ADR only", labels)
            self.assertIsNotNone(service.repository.get_source(docs["id"]))
            candidates = service.repository.list_memory_candidates(project["id"], status=None, limit=None)
            self.assertFalse(any(c.get("id") == "cand-docs-1" for c in candidates))

    def test_delete_source_with_purge_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            project = service.create_project({"id": "wipe-http", "name": "Wipe HTTP", "root_path": str(tmp)})
            created = service.create_source({
                "project_id": project["id"],
                "name": "Remote wiki",
                "kind": "docs",
                "config": {
                    "driver": "http",
                    "enabled": True,
                    "http": {"url": "https://example.com/wiki.json", "method": "GET"},
                },
            })
            source_id = created["id"]
            service.add_memory({
                "project_id": project["id"],
                "label": "HTTP fact",
                "type": "Doc",
                "scope": "project",
                "text": "Ingested from the HTTP wiki source binding.",
                "source_id": source_id,
            })
            deleted = service.delete_project_source(source_id, purge_memory=True)
            self.assertTrue(deleted.get("deleted"))
            self.assertTrue(deleted.get("purge_memory"))
            self.assertIsNone(service.repository.get_source(source_id))
            nodes = service.repository.list_nodes(project_id=project["id"])
            self.assertFalse(any(n.label == "HTTP fact" for n in nodes))

    def test_wipe_project_keeps_sources_and_chats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root_a = root / "a"
            root_b = root / "b"
            root_a.mkdir()
            root_b.mkdir()
            service = ArchitectOSService(root)
            a = service.create_project({"id": "wipe-a", "name": "Project A", "root_path": str(root_a)})
            b = service.create_project({"id": "wipe-b", "name": "Project B", "root_path": str(root_b)})
            self.assertNotEqual(a["id"], b["id"])
            service.add_memory({
                "project_id": a["id"],
                "label": "A memory",
                "type": "Decision",
                "scope": "project",
                "text": "Memory that belongs only to project A.",
            })
            service.add_memory({
                "project_id": b["id"],
                "label": "B memory",
                "type": "Decision",
                "scope": "project",
                "text": "Memory that belongs only to project B.",
            })
            chat = service.create_chat({"project_id": a["id"], "title": "Keep me"})
            sources_before = service.list_project_sources(a["id"])
            self.assertGreater(len(sources_before), 0)

            preview = service.wipe_project_memory(a["id"], confirm=False)
            self.assertFalse(preview.get("confirmed"))
            self.assertGreaterEqual(preview["nodes"], 1)

            result = service.wipe_project_memory(a["id"], confirm=True)
            self.assertTrue(result.get("confirmed"))
            self.assertGreaterEqual(result["nodes"], 1)

            nodes_a = service.repository.list_nodes(project_id=a["id"])
            content_a = [
                n for n in nodes_a
                if n.status == "active" and n.type != "Project" and not dict(n.metadata or {}).get("structural")
            ]
            self.assertEqual(content_a, [])
            self.assertTrue(any(n.type == "Project" and n.status == "active" for n in nodes_a))
            nodes_b = service.repository.list_nodes(project_id=b["id"])
            self.assertTrue(any(n.label == "B memory" for n in nodes_b))
            self.assertEqual(len(service.list_project_sources(a["id"])), len(sources_before))
            self.assertIsNotNone(service.repository.get_chat(chat["id"]))

    def test_wipe_and_graph_commands_cannot_remove_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            service = ArchitectOSService(Path(tmp))
            project = service.create_project({"name": "Keep Root", "root_path": str(root)})
            roots = [n for n in service.repository.list_nodes(project_id=project["id"]) if n.type == "Project"]
            self.assertEqual(len(roots), 1)
            root_id = roots[0].id
            with self.assertRaises(ValueError):
                service.apply_graph_command({"action": "delete", "node_id": root_id})
            other = service.add_memory({
                "project_id": project["id"],
                "label": "Leaf",
                "type": "Decision",
                "scope": "project",
                "text": "A leaf that must not absorb the project root.",
            })
            with self.assertRaises(ValueError):
                service.merge_graph_nodes(root_id, {"target_id": other["id"]})
            still = service.repository.get_node(root_id)
            self.assertIsNotNone(still)
            self.assertEqual(still.status, "active")
            self.assertEqual(still.type, "Project")


if __name__ == "__main__":
    unittest.main()
