from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.architectos.models import Project
from backend.architectos.service import ArchitectOSService


def _make_service(tmp: str) -> ArchitectOSService:
    return ArchitectOSService(Path(tmp))


def _scan_nodes(service: ArchitectOSService) -> list:
    return [
        node for node in service.repository.list_nodes()
        if dict(node.metadata or {}).get("source") == "project_scan"
    ]


class ProjectScanServiceTests(unittest.TestCase):
    def test_scan_project_imports_code_and_doc_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _make_service(tmp)
            tree = Path(tmp) / "sample_project"
            (tree / "src").mkdir(parents=True)
            (tree / "src" / "app.py").write_text(
                "import os\n\n\nclass App:\n    pass\n\n\ndef main():\n    return 1\n",
                encoding="utf-8",
            )
            (tree / "docs").mkdir()
            (tree / "docs" / "guide.md").write_text("# Guide\n\nHow to operate the sample project.\n", encoding="utf-8")

            result = service.scan_project({"root_path": str(tree), "name": "Sample", "limit": 25})

            self.assertEqual(result["count"], 2)
            self.assertEqual(result["mode"], "scan")
            self.assertEqual({item["label"] for item in result["imported"]}, {"src/app.py", "docs/guide.md"})
            nodes = _scan_nodes(service)
            self.assertEqual(len(nodes), 2)
            by_label = {node.label: node for node in nodes}
            code = by_label["src/app.py"]
            self.assertEqual(code.type, "Artifact")
            self.assertEqual(code.project_id, result["project_id"])
            self.assertEqual(code.metadata["path"], "src/app.py")
            self.assertIn("structure", code.text.lower())
            self.assertIn("class App", code.text)
            self.assertIn("def main()", code.text)
            doc = by_label["docs/guide.md"]
            self.assertEqual(doc.type, "Doc")
            self.assertIn("Guide", doc.text)
            stored = service.repository.get_node(code.id)
            self.assertIsNotNone(stored)

    def test_scan_project_skips_excluded_dirs_noise_and_binary_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _make_service(tmp)
            tree = Path(tmp) / "sample_project"
            tree.mkdir()
            (tree / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
            # DEFAULT_EXCLUDES directories are never walked into.
            (tree / "node_modules" / "pkg").mkdir(parents=True)
            (tree / "node_modules" / "pkg" / "index.js").write_text("module.exports = {};\n", encoding="utf-8")
            (tree / ".git").mkdir()
            (tree / ".git" / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
            # Lockfiles and logs are noise even though they decode as text.
            (tree / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
            (tree / "debug.log").write_text("noise line\n", encoding="utf-8")
            # Binary payloads fail the text sniff.
            (tree / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

            result = service.scan_project({"root_path": str(tree), "name": "Sample", "limit": 25})

            self.assertEqual(result["count"], 1)
            self.assertEqual([item["label"] for item in result["imported"]], ["app.py"])
            self.assertEqual([node.label for node in _scan_nodes(service)], ["app.py"])

    def test_scan_project_twice_does_not_duplicate_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _make_service(tmp)
            tree = Path(tmp) / "sample_project"
            tree.mkdir()
            (tree / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
            (tree / "README.md").write_text("# Sample\n\nA tiny fixture project.\n", encoding="utf-8")
            payload = {"root_path": str(tree), "name": "Sample", "limit": 25}

            first = service.scan_project(payload)
            total_after_first = len(service.repository.list_nodes())
            second = service.scan_project(payload)
            total_after_second = len(service.repository.list_nodes())

            self.assertEqual(first["count"], 2)
            self.assertEqual(second["count"], 2)
            self.assertEqual(total_after_second, total_after_first)
            self.assertEqual(
                {item["id"] for item in first["imported"]},
                {item["id"] for item in second["imported"]},
            )
            self.assertEqual(len(_scan_nodes(service)), 2)

    def test_scan_project_reindex_archives_nodes_for_removed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _make_service(tmp)
            tree = Path(tmp) / "sample_project"
            tree.mkdir()
            (tree / "keep.py").write_text("def keep():\n    return 1\n", encoding="utf-8")
            (tree / "drop.py").write_text("def drop():\n    return 2\n", encoding="utf-8")
            payload = {"root_path": str(tree), "name": "Sample", "limit": 25}

            first = service.scan_project(payload)
            self.assertEqual(first["count"], 2)
            dropped_id = next(item["id"] for item in first["imported"] if item["label"] == "drop.py")
            (tree / "drop.py").unlink()

            second = service.scan_project({**payload, "reindex": True})

            self.assertEqual(second["mode"], "reindex")
            self.assertEqual(second["count"], 1)
            self.assertEqual(second["archived"], 1)
            dropped = service.repository.get_node(dropped_id)
            self.assertIsNotNone(dropped)
            assert dropped is not None
            self.assertEqual(dropped.status, "archived")
            self.assertEqual(dropped.metadata["archive_reason"], "project_reindex_missing_source")
            active_labels = {node.label for node in _scan_nodes(service) if node.status == "active"}
            self.assertEqual(active_labels, {"keep.py"})

    def test_scan_project_rejects_missing_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _make_service(tmp)
            with self.assertRaises(ValueError) as raised:
                service.scan_project({"root_path": str(Path(tmp) / "does-not-exist")})
            self.assertIn("project path does not exist", str(raised.exception))

    def test_inbox_candidates_from_drop_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _make_service(tmp)
            inbox = service._inbox_dir()
            self.assertEqual(inbox, service.repository.data_dir / "inbox")
            (inbox / "notes.md").write_text("# Release notes\n\nWorth remembering.\n", encoding="utf-8")
            (inbox / "snippet.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
            # Too short to be useful, not text-like, and hidden inputs are all skipped.
            (inbox / "tiny.txt").write_text("ab\n", encoding="utf-8")
            (inbox / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
            (inbox / ".hidden").mkdir()
            (inbox / ".hidden" / "secret.md").write_text("# Hidden\n\nShould never be ingested.\n", encoding="utf-8")

            candidates = service._ingest_inbox_candidates("architectos", 10)

            self.assertEqual({item["label"] for item in candidates}, {"Inbox: notes.md", "Inbox: snippet.py"})
            for item in candidates:
                self.assertEqual(item["source_type"], "inbox")
                self.assertEqual(item["type"], "Doc")
                self.assertEqual(item["scope"], "project")
                self.assertEqual(item["metadata"]["template"], "inbox")
            notes = next(item for item in candidates if item["label"] == "Inbox: notes.md")
            self.assertIn("Dropped file: notes.md", notes["text"])
            self.assertIn("Worth remembering.", notes["text"])
            # The drop folder contract is read-only: files stay in place for re-ingest.
            self.assertTrue((inbox / "notes.md").is_file())
            self.assertTrue((inbox / "snippet.py").is_file())

            result = service.ingest_memory({"project_id": "architectos", "sources": ["inbox"], "limit": 10})
            self.assertEqual(result["warnings"], [])
            self.assertEqual(result["count"], 2)
            self.assertEqual({item["source_type"] for item in result["candidates"]}, {"inbox"})
            pending = service.repository.list_memory_candidates("architectos")
            self.assertEqual(len(pending), 2)

    def test_project_file_save_delete_confined_to_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _make_service(tmp)
            project_dir = Path(tmp) / "proj"
            project_dir.mkdir()
            service.repository.upsert_project(Project(id="proj1", name="Proj", root_path=str(project_dir), description=""))

            saved = service.save_project_file({"project_id": "proj1", "path": "docs/notes.md", "text": "hello memory"})
            self.assertTrue(saved["success"])
            # save_project_file returns OS-native separators; normalize for comparison.
            self.assertEqual(saved["path"].replace("\\", "/"), "docs/notes.md")
            self.assertEqual((project_dir / "docs" / "notes.md").read_text(encoding="utf-8"), "hello memory")

            with self.assertRaises(ValueError) as save_escape:
                service.save_project_file({"project_id": "proj1", "path": "../escape.txt", "text": "x"})
            self.assertIn("outside project root", str(save_escape.exception))
            self.assertFalse((Path(tmp) / "escape.txt").exists())
            with self.assertRaises(ValueError):
                service.save_project_file({"project_id": "proj1", "path": "/etc/architectos-must-not-write.txt", "text": "x"})
            with self.assertRaises(ValueError):
                service.save_project_file({"project_id": "proj1", "path": "", "text": "x"})

            deleted = service.delete_project_file({"project_id": "proj1", "path": "docs/notes.md"})
            self.assertTrue(deleted["success"])
            self.assertFalse((project_dir / "docs" / "notes.md").exists())
            with self.assertRaises(ValueError) as missing:
                service.delete_project_file({"project_id": "proj1", "path": "docs/notes.md"})
            self.assertIn("does not exist", str(missing.exception))
            with self.assertRaises(ValueError):
                service.delete_project_file({"project_id": "proj1", "path": "../../etc/passwd"})


if __name__ == "__main__":
    unittest.main()
