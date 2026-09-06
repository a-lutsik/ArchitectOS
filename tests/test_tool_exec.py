from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.architectos.models import MemoryNode, Project
from backend.architectos.service import ArchitectOSService


class ToolExecFilesystemTests(unittest.TestCase):
    def _service(self, tmp: str, relative: str = "src/App.java", text: str | None = None) -> tuple[ArchitectOSService, str]:
        root = Path(tmp)
        project_dir = root / "repo"
        target = project_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        body = text if text is not None else "\n".join(f"line-{index}" for index in range(1, 80))
        target.write_text(body, encoding="utf-8")
        service = ArchitectOSService(root)
        project = service.repository.upsert_project(
            Project(id="proj1", name="Repo", root_path=str(project_dir), description="")
        )
        return service, project.id

    def test_fs_read_windows_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, project_id = self._service(tmp)
            first = service._tool_fs_read({
                "project_id": project_id,
                "path": "src/App.java",
                "start_line": 1,
                "end_line": 10,
            })
            self.assertEqual(first["start_line"], 1)
            self.assertEqual(first["end_line"], 10)
            self.assertTrue(first["truncated"])
            self.assertEqual(first["next_start_line"], 11)
            self.assertIn("1|", first["text"])

    def test_fs_read_resolves_class_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, project_id = self._service(
                tmp,
                "src/main/java/com/acme/FooBuilder.java",
                "class FooBuilder {}",
            )
            result = service._tool_fs_read({
                "project_id": project_id,
                "path": "com.acme.FooBuilder",
            })
            self.assertTrue(str(result["path"]).endswith("FooBuilder.java"))

    def test_fs_list_filters_by_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, project_id = self._service(tmp, "src/a.txt", "a")
            docs = Path(tmp) / "repo" / "docs"
            docs.mkdir(parents=True, exist_ok=True)
            (docs / "b.txt").write_text("b", encoding="utf-8")
            listed = service._tool_fs_list({"project_id": project_id, "path": "src", "limit": 50})
            paths = [item["path"] for item in listed["files"]]
            self.assertTrue(any(path == "src/a.txt" or path.startswith("src/") for path in paths))
            self.assertFalse(any(str(path).startswith("docs/") for path in paths))

    def test_fs_search_derives_class_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, project_id = self._service(
                tmp,
                "src/main/java/com/acme/FooBuilder.java",
                "class FooBuilder {}",
            )
            result = service._tool_fs_search({
                "project_id": project_id,
                "query": "com.acme.FooBuilder",
                "mode": "name",
            })
            self.assertEqual(result.get("resolved_query"), "FooBuilder")
            self.assertTrue(result.get("hits"))

    def test_fs_write_and_memory_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, project_id = self._service(tmp, "README.md", "hello")
            written = service._tool_fs_write({
                "project_id": project_id,
                "path": "note.txt",
                "text": "saved",
            })
            self.assertTrue((Path(tmp) / "repo" / "note.txt").exists() or "path" in written)
            node = MemoryNode(
                id="n1",
                type="note",
                label="Chart work #78167",
                scope="project",
                text="Primary energy chart",
                project_id=project_id,
                status="active",
                metadata={"work_item_id": "78167", "source": "azure-boards"},
            )
            service.repository.upsert_node(node)
            searched = service._tool_memory_search({"query": "energy", "project_id": project_id, "limit": 5})
            self.assertGreaterEqual(searched["count"], 1)
            got = service._tool_memory_get({"id": "n1"})
            self.assertEqual(got["id"], "n1")
            self.assertEqual(got["label"], "Chart work #78167")

    def test_boards_ids_from_memory_hits(self) -> None:
        hits = [
            {"node": {"label": "Item #12345", "metadata": {"source": "azure"}, "source": "azure"}},
            {"node": {"metadata": {"work_item_id": "99"}}},
            {"node": "broken"},
        ]
        ids = ArchitectOSService._boards_ids_from_memory_hits(hits)
        self.assertEqual(ids, ["12345", "99"])

    def test_memory_search_requires_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _project_id = self._service(tmp)
            with self.assertRaises(ValueError):
                service._tool_memory_search({})


class ToolExecTerminalExtrasTests(unittest.TestCase):
    def test_empty_command_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            with self.assertRaises(ValueError):
                service.terminal_run({"project_id": "architectos", "command": "  "})

    def test_timeout_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            proc = mock.Mock()
            expired = subprocess.TimeoutExpired(cmd="sleep", timeout=1, output="partial", stderr="")
            proc.communicate.side_effect = [expired, ("", "")]
            proc.poll.return_value = None
            proc.pid = 4242
            with mock.patch("backend.architectos.tool_exec_service.subprocess.Popen", return_value=proc):
                result = service.terminal_run({
                    "project_id": "architectos",
                    "command": "echo ok",
                    "timeout_seconds": 1,
                })
            self.assertEqual(result["status"], "timeout")
            proc.kill.assert_called()

    def test_timeout_explains_silent_mcp_stdio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            proc = mock.Mock()
            expired = subprocess.TimeoutExpired(cmd="npx", timeout=1, output="", stderr="")
            proc.communicate.side_effect = [expired, ("", "")]
            proc.poll.return_value = None
            proc.pid = 4242
            with mock.patch("backend.architectos.tool_exec_service.subprocess.Popen", return_value=proc):
                result = service.terminal_run({
                    "project_id": "architectos",
                    "command": "npx -y @modelcontextprotocol/server-filesystem .",
                    "timeout_seconds": 1,
                })
            self.assertEqual(result["status"], "timeout")
            self.assertIn("JSON-RPC", result["stderr"])

    def test_timeout_returns_when_child_keeps_pipes_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            proc = mock.Mock()
            expired = subprocess.TimeoutExpired(cmd="npx", timeout=1, output="", stderr="")
            proc.communicate.side_effect = [
                expired,
                subprocess.TimeoutExpired(cmd="npx", timeout=2),
            ]
            proc.poll.return_value = None
            proc.pid = 4242
            proc.stdout = mock.Mock()
            proc.stderr = mock.Mock()
            with mock.patch("backend.architectos.tool_exec_service.subprocess.Popen", return_value=proc):
                result = service.terminal_run({
                    "project_id": "architectos",
                    "command": "npx -y @modelcontextprotocol/server-filesystem .",
                    "timeout_seconds": 1,
                })
            self.assertEqual(result["status"], "timeout")
            proc.stdout.close.assert_called()

    def test_real_sleep_hits_timeout(self) -> None:
        hang = "ping -n 30 127.0.0.1 >nul" if os.name == "nt" else "sleep 30"
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            result = service.terminal_run({
                "project_id": "architectos",
                "command": hang,
                "timeout_seconds": 1,
            })
            self.assertEqual(result["status"], "timeout")
            self.assertLessEqual(result["duration_ms"], 15_000)

    def test_shell_id_and_open_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            self.assertIn(service._terminal_shell_id("auto"), {"bash", "sh", "cmd", "powershell"})
            self.assertEqual(service._terminal_shell_id("bash"), "bash")
            commands = service._terminal_open_commands(Path(tmp))
            self.assertIsInstance(commands, list)
            with mock.patch("backend.architectos.tool_exec_service.subprocess.Popen") as popen:
                popen.return_value = mock.Mock()
                with mock.patch.object(service, "_terminal_open_commands", return_value=[["open", "-a", "Terminal", tmp]]):
                    opened = service.terminal_open({"project_id": "architectos"})
            self.assertEqual(opened["status"], "opened")
            with mock.patch("backend.architectos.tool_exec_service.subprocess.Popen", side_effect=OSError("nope")):
                unavailable = service.terminal_open({"project_id": "architectos"})
            self.assertEqual(unavailable["status"], "unavailable")

    def test_memory_get_neighbors_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, project_id = ToolExecFilesystemTests()._service(tmp)
            parent = MemoryNode(
                id="parent",
                type="note",
                label="Parent",
                scope="project",
                text="parent text",
                project_id=project_id,
            )
            child = MemoryNode(
                id="child",
                type="note",
                label="Child",
                scope="project",
                text="child text",
                project_id=project_id,
            )
            service.repository.upsert_node(parent)
            service.repository.upsert_node(child)
            service.repository.add_edge("parent", "child", "related", "project")
            got = service._tool_memory_get({"id": "parent"})
            self.assertEqual(got["neighbors"][0]["id"], "child")
            with self.assertRaises(ValueError):
                service._tool_memory_get({"id": "missing"})
            with self.assertRaises(ValueError):
                service._tool_fs_search({"project_id": project_id, "query": ""})


if __name__ == "__main__":
    unittest.main()
