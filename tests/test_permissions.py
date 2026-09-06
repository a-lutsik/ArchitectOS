from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from backend.architectos.models import Project
from backend.architectos.permissions import PermissionBroker, PermissionRequired, path_covered
from backend.architectos.service import ArchitectOSService
from backend.architectos.tool_gateway import ToolGateway

from tests.test_tool_gateway import _FakeMCP


class PermissionBrokerTests(unittest.TestCase):
    def test_wait_and_allow_once(self) -> None:
        broker = PermissionBroker()
        broker.bind_run("run-1", chat_id="chat-1")
        request = broker.create_request("sandbox", "read", "/tmp/secret.txt", "outside")
        decision_box: dict = {}

        def allow() -> None:
            time.sleep(0.05)
            decision_box["value"] = broker.decide(request["request_id"], allow=True, scope="once")

        threading.Thread(target=allow, daemon=True).start()
        decision = broker.wait(request["request_id"], timeout=2)
        self.assertTrue(decision["allow"])
        self.assertTrue(broker.is_allowed("sandbox", "read", "/tmp/secret.txt", run_id="run-1"))

    def test_session_grant_covers_child_path(self) -> None:
        broker = PermissionBroker()
        broker.bind_run("run-a", chat_id="chat-z")
        broker.grant("sandbox", "read", "/tmp/proj", run_id="run-a", chat_id="chat-z", scope="session")
        broker.bind_run("run-b", chat_id="chat-z")
        self.assertTrue(broker.is_allowed("sandbox", "read", "/tmp/proj/notes.md"))
        self.assertFalse(path_covered("/tmp/proj", "/var/log/syslog"))

    def test_unknown_request_raises(self) -> None:
        broker = PermissionBroker()
        with self.assertRaises(ValueError):
            broker.decide("missing", allow=True)


class SandboxFsPermissionTests(unittest.TestCase):
    def test_outside_read_raises_until_granted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir = root / "repo"
            project_dir.mkdir()
            (project_dir / "src").mkdir()
            (project_dir / "src" / "App.java").write_text("class App {}", encoding="utf-8")
            service = ArchitectOSService(root)
            project = service.repository.upsert_project(
                Project(id="proj1", name="Repo", root_path=str(project_dir), description="")
            )
            outside = root / "sibling.txt"
            outside.write_text("secret-note", encoding="utf-8")
            with self.assertRaises(PermissionRequired) as raised:
                service._tool_fs_read({"project_id": project.id, "path": str(outside)})
            self.assertEqual(raised.exception.kind, "sandbox")
            service.permissions.bind_run("run-1")
            service.permissions.grant("sandbox", "read", str(outside), run_id="run-1", scope="run")
            result = service._tool_fs_read({"project_id": project.id, "path": str(outside)})
            self.assertIn("secret-note", result["text"])
            self.assertTrue(result.get("outside"))

    def test_gateway_write_asks_permission(self) -> None:
        mcp = _FakeMCP({"filesystem"})
        gateway = ToolGateway(
            mcp,
            native_handlers={"fs_write": lambda args: {"ok": True, "path": args.get("path")}},
        )
        blocked = gateway.execute("fs_write", {"path": "a.txt", "text": "hi"}, include_writes=False)
        self.assertFalse(blocked["ok"])
        self.assertTrue(blocked.get("needs_permission"))
        self.assertEqual(blocked["permission"]["kind"], "write")
        allowed = gateway.execute("fs_write", {"path": "a.txt", "text": "hi"}, include_writes=True)
        self.assertTrue(allowed["ok"])


if __name__ == "__main__":
    unittest.main()
