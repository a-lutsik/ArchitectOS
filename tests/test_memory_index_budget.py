"""Stage D: the start-of-session index has a budget and says when it is over."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from backend.architectos.mcp_server import MemoryMCPServer
from backend.architectos.memory_capture import normalize_capture_settings
from backend.architectos.service import ArchitectOSService


class IndexBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = ArchitectOSService(Path(self._tmp.name))
        self.repo = self.service.repository

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _stable(self, count: int, node_type: str = "Decision") -> None:
        for index in range(count):
            self.repo.add_node(node_type, f"Решение {index}", "project", f"Стабильный факт номер {index} про сервис", "architectos", id_salt=str(index))

    def test_budget_is_normalized(self) -> None:
        self.assertEqual(normalize_capture_settings(None)["budget_index_lines"], 200)
        self.assertEqual(normalize_capture_settings({"budget_index_lines": 3})["budget_index_lines"], 20)
        self.assertEqual(normalize_capture_settings({"budget_index_lines": "x"})["budget_index_lines"], 200)

    def test_status_counts_only_the_stable_layer(self) -> None:
        self._stable(3)
        self.repo.add_node("Lesson", "Урок", "project", "Короткоживущий урок без закрепления", "architectos")
        pinned = self.repo.add_node("Lesson", "Закреплённый урок", "project", "Закреплённый урок попадает в стартовый слой", "architectos")
        pinned.metadata["pinned"] = True
        self.repo.upsert_node(pinned)
        status = self.service.memory_index_status("architectos")
        self.assertEqual(status["size"], 4)
        self.assertEqual(status["pinned"], 1)
        self.assertEqual(status["by_type"].get("Decision"), 3)
        self.assertEqual((status["budget"], status["level"], status["over_by"]), (200, "ok", 0))
        self.assertEqual(status["briefing_items"], 4)

    def test_global_preferences_count_for_every_project(self) -> None:
        self.service.capture_memory_turn("Я предпочитаю короткие ответы без вступлений", project_id="architectos")
        other = self.service.create_project({"name": "Other", "description": "second"})
        # A Preference is long-term but not a stable-layer type; pin it to put it in the start layer.
        node = next(n for n in self.repo.list_nodes() if n.type == "Preference")
        node.metadata["pinned"] = True
        self.repo.upsert_node(node)
        self.assertEqual(self.service.memory_index_status(other["id"])["size"], 1)

    def test_levels_follow_the_budget(self) -> None:
        self._stable(18)
        self.service.update_settings({"memory_capture": {"budget_index_lines": 20}})
        self.assertEqual(self.service.memory_index_status("architectos")["level"], "near")
        self._stable(5, "Constraint")
        status = self.service.memory_index_status("architectos")
        self.assertEqual((status["level"], status["over_by"], status["briefing_items"]), ("over", 3, 12))

    def test_preview_reports_compactable_facts(self) -> None:
        self.repo.add_node("Decision", "Деплой", "project", "Деплой через GitHub Actions на каждый push в main ветку", "architectos", id_salt="full")
        self.repo.add_node("Decision", "Деплой", "project", "Деплой через GitHub Actions", "architectos", id_salt="short")
        status = self.service.memory_index_status("architectos", preview=True)
        self.assertEqual(status["compactable"], 1)
        self.assertNotIn("compactable", self.service.memory_index_status("architectos"))

    def test_briefing_carries_index_status(self) -> None:
        self._stable(2)
        briefing = self.service.memory_briefing(project_id="architectos")
        self.assertEqual(briefing["index"]["size"], 2)
        self.assertEqual(briefing["index"]["level"], "ok")

    def test_mcp_instructions_warn_only_when_over_budget(self) -> None:
        def instructions(root: Path) -> str:
            # Bound explicitly: an unbound server only tells the agent to pick a project.
            server = MemoryMCPServer(root, project_id="architectos")
            stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n")
            stdout = io.StringIO()
            server.serve(stdin=stdin, stdout=stdout)
            return json.loads(stdout.getvalue().splitlines()[0])["result"]["instructions"]

        root = Path(self._tmp.name)
        self._stable(2)
        text = instructions(root)
        self.assertNotIn("over budget", text)
        self.assertIn("memory capture policy", text)
        self.service.update_settings({"memory_capture": {"budget_index_lines": 20}})
        self._stable(25, "Constraint")
        text = instructions(root)
        self.assertIn("over budget", text)
        self.assertIn("memory_compact", text)


if __name__ == "__main__":
    unittest.main()
