"""Stage 4: compaction of stored memory and the Duplicates review queue."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from backend.architectos.mcp_server import MemoryMCPServer
from backend.architectos.service import ArchitectOSService

SHORT = "Деплой через GitHub Actions"
FULL = "Деплой через GitHub Actions на каждый push в main ветку"


class CompactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = ArchitectOSService(Path(self._tmp.name))
        self.repo = self.service.repository

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _legacy(self, text: str, node_type: str = "Decision", label: str = "Деплой", **meta: Any) -> Any:
        """A node written before the gate existed (no dedup on the way in)."""
        node = self.repo.add_node(node_type, label, "project", text, "architectos", id_salt=text + node_type)
        if meta:
            node.metadata.update(meta)
            node = self.repo.upsert_node(node)
        return node

    def _status(self, node: Any) -> str:
        stored = self.repo.get_node(node.id)
        assert stored is not None
        return stored.status

    def test_dry_run_reports_without_changing_anything(self) -> None:
        keeper = self._legacy(FULL)
        short = self._legacy(SHORT, "Lesson")
        report = self.service.compact_memory({})
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["would_merge"], 1)
        self.assertEqual(report["clusters"][0]["keeper"]["id"], keeper.id)
        self.assertEqual([m["id"] for m in report["clusters"][0]["members"]], [short.id])
        self.assertEqual(self._status(short), "active")

    def test_apply_merges_covered_facts_into_the_fullest_and_keeps_its_text(self) -> None:
        keeper = self._legacy(FULL, "Lesson")
        exact = self._legacy(SHORT, "Decision")
        restated = self._legacy("деплой через github actions.", "Lesson")
        other = self._legacy("Деплой занимает 5 минут")
        report = self.service.compact_memory({"dry_run": False})
        self.assertEqual(report["merged"], 2)
        for member in (exact, restated):
            stored = self.repo.get_node(member.id)
            assert stored is not None
            self.assertEqual((stored.status, stored.metadata.get("merged_into")), ("merged", keeper.id))
        survivor = self.repo.get_node(keeper.id)
        assert survivor is not None
        self.assertEqual(survivor.text, FULL)
        self.assertEqual(survivor.type, "Decision")
        self.assertEqual(self._status(other), "active")

    def test_changed_values_and_pinned_nodes_are_never_merged(self) -> None:
        self._legacy("Лимит запросов 5000 в час", label="Лимит")
        changed = self._legacy("Лимит запросов 9000 в час", label="Лимит")
        self._legacy(FULL)
        pinned = self._legacy(SHORT, "Lesson", pinned=True)
        report = self.service.compact_memory({"dry_run": False})
        self.assertEqual(report["merged"], 0)
        self.assertEqual(self._status(changed), "active")
        self.assertEqual(self._status(pinned), "active")

    def test_undo_reactivates_member_and_compaction_leaves_the_pair_alone(self) -> None:
        keeper = self._legacy(FULL)
        short = self._legacy(SHORT)
        neighbour = self._legacy("Документация лежит в docs/", label="Документы")
        self.service.create_graph_edge({"source": short.id, "target": neighbour.id, "type": "RELATED_TO"})
        report = self.service.compact_memory({"dry_run": False})
        event_id = report["clusters"][0]["members"][0]["event_id"]
        copied = [e for e in self.repo.list_edges_touching([keeper.id]) if e.target == neighbour.id]
        self.assertEqual(len(copied), 1)

        undo = self.service.undo_memory_dedup({"event_id": event_id})
        self.assertEqual(undo["action"], "merge")
        self.assertEqual(self._status(short), "active")
        self.assertEqual([e for e in self.repo.list_edges_touching([keeper.id]) if e.target == neighbour.id], [])
        self.assertEqual(self.service.compact_memory({"dry_run": False})["merged"], 0)

    def test_suggest_mode_never_applies(self) -> None:
        self._legacy(FULL)
        short = self._legacy(SHORT)
        self.service.update_settings({"memory_dedup": {"mode": "suggest"}})
        report = self.service.compact_memory({"dry_run": False})
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["would_merge"], 1)
        self.assertEqual(self._status(short), "active")

    def test_scheduled_run_records_summary_and_respects_switch(self) -> None:
        self._legacy(FULL)
        self._legacy(SHORT)
        report = self.service.run_scheduled_compaction()
        assert report is not None
        self.assertEqual(report["merged"], 1)
        last = self.repo.get_setting("memory_dedup_compaction_last")
        self.assertEqual(last["merged"], 1)
        self.service.update_settings({"memory_dedup": {"compaction_enabled": False}})
        self.assertIsNone(self.service.run_scheduled_compaction())

    def test_oversized_subject_group_is_capped(self) -> None:
        self._legacy(FULL)
        spellings = ["Деплой через GitHub Actions", "деплой через github actions", "ДЕПЛОЙ через GitHub Actions.",
                     "Деплой  через GitHub Actions!", "деплой через GITHUB actions", "Деплой через Github Actions…"]
        for index, text in enumerate(spellings):
            # stable_id lower-cases its parts, so case-only variants need their own salt.
            self.repo.add_node("Lesson", "Деплой", "project", text, "architectos", id_salt=f"spelling-{index}")
        full_report = self.service.compact_memory({})
        self.assertEqual(sum(len(cluster["members"]) for cluster in full_report["clusters"]), 6)
        self.service.MAX_GROUP_NODES = 3  # type: ignore[misc]
        capped = self.service.compact_memory({})
        self.assertEqual(capped["clusters"][0]["keeper"]["text"], FULL)
        self.assertEqual(sum(len(cluster["members"]) for cluster in capped["clusters"]), 2)

    def test_keep_best_merge_does_not_concatenate_text(self) -> None:
        keeper = self._legacy(FULL)
        short = self._legacy(SHORT)
        result = self.service.merge_graph_nodes(short.id, {"target_id": keeper.id, "strategy": "keep_best"})
        self.assertEqual(result["target"]["text"], FULL)
        with self.assertRaises(ValueError):
            self.service.merge_graph_nodes(keeper.id, {"target_id": short.id, "strategy": "bogus"})


class DuplicatesReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = ArchitectOSService(Path(self._tmp.name))
        self.repo = self.service.repository

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _flagged_pair(self) -> tuple[dict[str, Any], dict[str, Any]]:
        base = self.service.add_memory({"label": "Хранилище", "type": "Decision", "text": "Данные проекта лежат в локальной базе SQLite"})
        flagged = self.service.add_memory({"label": "Хранилище данных", "type": "Decision", "text": "Данные проекта лежат в локальной базе SQLite"})
        self.assertEqual(flagged["metadata"]["possible_duplicate_of"], base["id"])
        return base, flagged

    def _items(self) -> list[dict[str, Any]]:
        return self.service.list_memory_duplicates("architectos")["items"]

    def test_flagged_pair_is_listed(self) -> None:
        base, flagged = self._flagged_pair()
        items = self._items()
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["kind"], items[0]["node"]["id"], items[0]["target"]["id"]), ("possible_duplicate", flagged["id"], base["id"]))

    def test_distinct_clears_flag_and_remembers_pair(self) -> None:
        base, flagged = self._flagged_pair()
        self.service.resolve_memory_duplicate({"node_id": flagged["id"], "target_id": base["id"], "action": "distinct"})
        self.assertEqual(self._items(), [])
        stored = self.repo.get_node(flagged["id"])
        assert stored is not None
        self.assertNotIn("possible_duplicate_of", stored.metadata)
        self.assertIn(tuple(sorted((base["id"], flagged["id"]))), self.repo.dedup_rejected_pairs_for([base["id"]]))

    def test_merge_from_review_is_listed_and_undoable(self) -> None:
        base, flagged = self._flagged_pair()
        result = self.service.resolve_memory_duplicate(
            {"node_id": flagged["id"], "target_id": base["id"], "action": "merge", "keep": "target"}
        )
        self.assertEqual(result["keeper"]["id"], base["id"])
        self.assertEqual(self.repo.get_node(flagged["id"]).status, "merged")  # type: ignore[union-attr]
        recent = self.service.list_memory_duplicates("architectos")["recent"]
        self.assertEqual((recent[0]["action"], recent[0]["undone"]), ("merge", False))
        self.service.undo_memory_dedup({"event_id": result["event_id"]})
        self.assertEqual(self.repo.get_node(flagged["id"]).status, "active")  # type: ignore[union-attr]
        self.assertTrue(self.service.list_memory_duplicates("architectos")["recent"][0]["undone"])

    def test_conflict_is_listed_first_and_can_be_dismissed(self) -> None:
        self._flagged_pair()
        self.service.add_memory({"label": "Кэш", "type": "Decision", "text": "Используем Redis для кэша сессий"})
        conflict = self.service.add_memory({"label": "Кэш", "type": "Decision", "text": "Не используем Redis для кэша сессий"})
        items = self._items()
        self.assertEqual(items[0]["kind"], "conflict")
        self.service.resolve_memory_duplicate({"node_id": conflict["id"], "action": "dismiss"})
        self.assertNotIn("conflict", [item["kind"] for item in self._items()])

    def test_resolve_rejects_unknown_action(self) -> None:
        base, flagged = self._flagged_pair()
        with self.assertRaises(ValueError):
            self.service.resolve_memory_duplicate({"node_id": flagged["id"], "target_id": base["id"], "action": "explode"})

    def test_wipe_drops_rejected_pairs(self) -> None:
        project = self.service.create_project({"name": "Wipe pairs", "description": "rejected pairs"})
        a = self.service.add_memory({"project_id": project["id"], "label": "A", "type": "Decision", "text": "Первый факт проекта"})
        b = self.service.add_memory({"project_id": project["id"], "label": "B", "type": "Decision", "text": "Второй факт проекта"})
        self.repo.add_dedup_rejected_pair(a["id"], b["id"])
        self.service.wipe_project_memory(project["id"], confirm=True)
        self.assertEqual(self.repo.dedup_rejected_pairs_for([a["id"], b["id"]]), set())


class CompactionMcpTests(unittest.TestCase):
    def test_memory_compact_tool_is_a_read_only_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = MemoryMCPServer(Path(tmp))
            repo = server.service.repository
            repo.add_node("Decision", "Деплой", "project", FULL, "architectos", id_salt="full")
            short = repo.add_node("Decision", "Деплой", "project", SHORT, "architectos", id_salt="short")
            stdin = io.StringIO(json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "memory_compact", "arguments": {"project_id": "architectos"}},
            }) + "\n")
            stdout = io.StringIO()
            server.serve(stdin=stdin, stdout=stdout)
            response = json.loads(stdout.getvalue().splitlines()[0])
            self.assertIn("Nothing was merged", response["result"]["content"][0]["text"])
            stored = repo.get_node(short.id)
            assert stored is not None
            self.assertEqual(stored.status, "active")


if __name__ == "__main__":
    unittest.main()
