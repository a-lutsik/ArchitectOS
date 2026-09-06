from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.architectos import ArchitectOS, __version__
from backend.architectos.cli import main as cli_main


class SdkSurfaceTests(unittest.TestCase):
    def test_search_add_and_capture_share_one_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = ArchitectOS(tmp)
            added = mem.add(
                "UniqueBirchFormatter is the required Python formatter in this repo.",
                label="Formatter",
                type="Constraint",
            )
            self.assertTrue(added.get("id"))
            found = mem.search("UniqueBirchFormatter", project_id="architectos", limit=5)
            self.assertIn("UniqueBirchFormatter", json.dumps(found.get("hits") or []))
            captured = mem.capture_turn(
                "Remember: UniqueBirchFormatter is required.",
                "Noted, we use UniqueBirchFormatter.",
                project_id="architectos",
            )
            self.assertEqual(captured["project_id"], "architectos")
            self.assertIn("context", captured)
            briefing = mem.briefing(project_id="architectos")
            self.assertEqual(briefing["project_id"], "architectos")
            self.assertEqual(__version__, "1.0.0")

    def test_cli_help_and_hook_doctor(self) -> None:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli_main(["help"]), 0)
        self.assertIn("hook", buf.getvalue())
        self.assertIn("vector-runtime", buf.getvalue())
        with tempfile.TemporaryDirectory() as tmp:
            mem = ArchitectOS(tmp)
            status = mem.hook_status()
            self.assertTrue(status["entry_script_exists"])
            self.assertIn("clients", status)
