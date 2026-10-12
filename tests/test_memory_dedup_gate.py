"""Stage 1 dedup gate: one classifier for every memory write path."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from backend.architectos.memory_dedup import MemoryDedupGate, fact_features, stem
from backend.architectos.service import ArchitectOSService


class GateClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = MemoryDedupGate(repository=None)

    def _compare(self, old: tuple[str, str], new: tuple[str, str]) -> Any:
        return self.gate.compare(fact_features(*new), fact_features(*old))

    def test_light_stemming_folds_inflections(self) -> None:
        self.assertEqual({stem(w) for w in ("хранит", "хранится", "хранят")}, {"хран"})
        self.assertEqual({stem(w) for w in ("данные", "данных", "данными")}, {"данн"})
        self.assertEqual(stem("file.py"), "file.py")

    def test_reordered_inflected_restatement_is_duplicate(self) -> None:
        verdict = self._compare(("Кэш", "Мы используем Redis для кэша"), ("Кэш", "Для кэша мы используем Redis"))
        self.assertEqual(verdict.action, "duplicate")

    def test_changed_number_is_update(self) -> None:
        verdict = self._compare(("Таймаут", "Таймаут сессии 30 минут"), ("Таймаут", "Таймаут сессии 45 минут"))
        self.assertEqual(verdict.action, "update")
        self.assertEqual(verdict.details, {"values_from": ["30"], "values_to": ["45"]})

    def test_flipped_negation_is_conflict(self) -> None:
        verdict = self._compare(("Кэш", "Используем Redis для кэша сессий"), ("Кэш", "Не используем Redis для кэша сессий"))
        self.assertEqual(verdict.action, "conflict")

    def test_different_identifiers_are_different_facts(self) -> None:
        verdict = self._compare(("Config", "Config lives in settings.py"), ("Config", "Config lives in settings.yaml"))
        self.assertEqual(verdict.action, "new")

    def test_short_names_keep_similar_facts_apart(self) -> None:
        verdict = self._compare(("Порты", "Сервис A слушает порт 8080"), ("Порты", "Сервис B слушает порт 8080"))
        self.assertEqual(verdict.action, "new")

    def test_different_subjects_are_never_updated_or_merged(self) -> None:
        verdict = self._compare(("Сервис A", "Слушает порт 8080"), ("Сервис B", "Слушает порт 8081"))
        self.assertEqual(verdict.action, "new")

    def test_added_detail_under_same_subject_is_enrich(self) -> None:
        verdict = self._compare(
            ("Деплой", "Деплой через GitHub Actions"),
            ("Деплой", "Деплой через GitHub Actions на каждый push в main ветку"),
        )
        self.assertEqual(verdict.action, "enrich")
        self.assertGreaterEqual(verdict.near_score, MemoryDedupGate.NEAR_DUPLICATE_SIMILARITY)

    def test_shorter_restatement_is_covered_duplicate(self) -> None:
        verdict = self._compare(
            ("Деплой", "Деплой через GitHub Actions на каждый push в main ветку"),
            ("Деплой", "Деплой через GitHub Actions"),
        )
        self.assertEqual((verdict.action, verdict.reason), ("duplicate", "already covered by a fuller fact"))

    def test_added_detail_with_changed_value_is_not_enrich(self) -> None:
        verdict = self._compare(
            ("Лимит", "Лимит запросов 5000 в час"),
            ("Лимит", "Лимит запросов 9000 в час и 10 в секунду"),
        )
        self.assertNotIn(verdict.action, {"enrich", "duplicate"})

    def test_added_identifier_is_enrich_changed_identifier_is_not(self) -> None:
        added = self._compare(("Config", "Config lives in settings.py"), ("Config", "Config lives in settings.py and settings.yaml"))
        changed = self._compare(("Config", "Config lives in settings.py"), ("Config", "Config lives in settings.yaml"))
        self.assertEqual(added.action, "enrich")
        self.assertEqual(changed.action, "new")


class GateWritePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = ArchitectOSService(Path(self._tmp.name))
        self.repo = self.service.repository

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add(self, label: str, text: str, node_type: str = "Decision", **extra: Any) -> dict[str, Any]:
        return self.service.add_memory({"label": label, "text": text, "type": node_type, **extra})

    def _edges(self, source: str, target: str, edge_type: str) -> list[Any]:
        return [
            edge for edge in self.repo.list_edges_touching([source])
            if edge.source == source and edge.target == target and edge.type == edge_type
        ]

    def test_second_fact_under_same_label_does_not_overwrite_the_first(self) -> None:
        first = self._add("Решение", "Бэкенд работает только на stdlib")
        second = self._add("Решение", "Фронтенд собирается без бандлера")
        self.assertNotEqual(first["id"], second["id"])
        kept = self.repo.get_node(first["id"])
        assert kept is not None
        self.assertEqual(kept.text, "Бэкенд работает только на stdlib")
        self.assertEqual(second["dedup"]["action"], "new")

    def test_reordered_restatement_folds_into_existing_node(self) -> None:
        first = self._add("Кэш", "Мы используем Redis для кэша")
        again = self._add("Кэш", "Для кэша мы используем Redis", source="mcp")
        self.assertTrue(again["dedup_merged"])
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["dedup"]["action"], "duplicate")
        self.assertEqual(again["metadata"]["dedup_sources"][-1]["source"], "mcp")

    def test_update_supersedes_old_value_and_undo_restores_it(self) -> None:
        old = self._add("Лимит API", "Лимит запросов 5000 в час", "Constraint")
        new = self._add("Лимит API", "Лимит запросов 9000 в час", "Constraint")
        self.assertNotEqual(old["id"], new["id"])
        self.assertEqual(new["dedup"]["action"], "update")
        self.assertEqual(new["dedup"]["target_id"], old["id"])
        stored_old = self.repo.get_node(old["id"])
        assert stored_old is not None
        self.assertTrue(stored_old.metadata.get("invalid_at"))
        self.assertEqual(stored_old.metadata.get("superseded_by"), new["id"])
        self.assertEqual(len(self._edges(new["id"], old["id"], "SUPERSEDES")), 1)

        undo = self.service.undo_memory_dedup({"event_id": new["dedup"]["event_id"]})
        self.assertEqual(undo["action"], "update")
        restored_old = self.repo.get_node(old["id"])
        assert restored_old is not None
        self.assertFalse(restored_old.metadata.get("invalid_at"))
        self.assertEqual(self._edges(new["id"], old["id"], "SUPERSEDES"), [])
        with self.assertRaises(ValueError):
            self.service.undo_memory_dedup({"event_id": new["dedup"]["event_id"]})

    def test_conflict_links_contradiction_and_flags_review(self) -> None:
        old = self._add("Кэш", "Используем Redis для кэша сессий")
        new = self._add("Кэш", "Не используем Redis для кэша сессий")
        self.assertEqual(new["dedup"]["action"], "conflict")
        self.assertEqual(new["metadata"]["needs_review"], "conflict")
        self.assertEqual(len(self._edges(new["id"], old["id"], "CONTRADICTS")), 1)
        stored_old = self.repo.get_node(old["id"])
        assert stored_old is not None
        self.assertFalse(stored_old.metadata.get("invalid_at"))

        self.service.undo_memory_dedup({"event_id": new["dedup"]["event_id"]})
        self.assertEqual(self._edges(new["id"], old["id"], "CONTRADICTS"), [])
        stored_new = self.repo.get_node(new["id"])
        assert stored_new is not None
        self.assertNotIn("needs_review", stored_new.metadata)

    def test_undo_duplicate_restores_swallowed_fact_and_type(self) -> None:
        lesson = self._add("Деплой", "Деплой идёт через GitHub Actions", "Lesson")
        decision = self._add("Деплой", "деплой идет через github actions", "Decision")
        self.assertTrue(decision["dedup_merged"])
        self.assertEqual(decision["type"], "Decision")

        undo = self.service.undo_memory_dedup({"event_id": decision["dedup"]["event_id"]})
        survivor = self.repo.get_node(lesson["id"])
        assert survivor is not None
        self.assertEqual(survivor.type, "Lesson")
        self.assertEqual(survivor.metadata.get("dedup_hits"), 0)
        restored = undo["restored"]
        self.assertIsNotNone(restored)
        self.assertNotEqual(restored["id"], lesson["id"])
        self.assertEqual(restored["type"], "Decision")
        self.assertEqual(restored["text"], "деплой идет через github actions")

    def test_undo_snapshot_is_redacted(self) -> None:
        text = "Ключ OPENAI_API_KEY=sk-proj-abcdef1234567890abcdef1234 для тестов"
        self._add("Ключи", text)
        again = self._add("Ключи", text)
        event = self.repo.get_memory_event(again["dedup"]["event_id"])
        assert event is not None
        self.assertNotIn("sk-proj", event["details"]["incoming"]["text"])

    def test_unknown_event_cannot_be_undone(self) -> None:
        with self.assertRaises(ValueError):
            self.service.undo_memory_dedup({"event_id": "event_missing"})

    def test_preview_does_not_write(self) -> None:
        old = self._add("Лимит API", "Лимит запросов 5000 в час", "Constraint")
        before = len(self.repo.list_nodes())
        preview = self.service.preview_memory_dedup(
            {"label": "Лимит API", "text": "Лимит запросов 7000 в час", "type": "Constraint"}
        )
        self.assertEqual(preview["dedup"]["action"], "update")
        self.assertEqual(preview["dedup"]["target_id"], old["id"])
        self.assertEqual(len(self.repo.list_nodes()), before)

    def test_bulk_reports_gate_actions_and_dedups_within_batch(self) -> None:
        self._add("Лимит API", "Лимит запросов 5000 в час", "Constraint")
        result = self.service.add_memory_bulk({"items": [
            {"label": "Лимит API", "text": "Лимит запросов 9000 в час", "type": "Constraint"},
            {"label": "Порт", "text": "Сервер слушает порт 8765", "type": "Decision"},
            {"label": "Порт", "text": "Сервер слушает порт 8765.", "type": "Decision"},
        ]})
        stats = result["stats"]
        self.assertEqual(stats["created"], 2)
        self.assertEqual(stats["deduplicated"], 1)
        self.assertEqual(stats["superseded"], 1)
        self.assertEqual(result["results"][0]["dedup_action"], "update")

    def _candidate(self, cid: str, label: str, text: str) -> dict[str, Any]:
        return self.repo.upsert_memory_candidate({
            "id": cid,
            "project_id": "architectos",
            "source_type": "docs",
            "source_ref": f"{cid}.md",
            "label": label,
            "type": "Decision",
            "scope": "project",
            "text": text,
            "status": "candidate",
            "metadata": {},
        })

    def test_promote_keeps_existing_node_under_same_label(self) -> None:
        existing = self._add("Архитектура", "Сервер и UI работают локально")
        self._candidate("cand-other", "Архитектура", "Эмбеддинги считаются офлайн")
        promoted = self.service.promote_memory_candidate("cand-other")
        self.assertNotEqual(promoted["memory"]["id"], existing["id"])
        kept = self.repo.get_node(existing["id"])
        assert kept is not None
        self.assertEqual(kept.text, "Сервер и UI работают локально")

    def test_promote_of_identical_fact_folds_into_existing_node(self) -> None:
        existing = self._add("Архитектура", "Сервер и UI работают локально")
        self._candidate("cand-same", "Архитектура", "сервер и UI работают локально.")
        promoted = self.service.promote_memory_candidate("cand-same")
        self.assertEqual(promoted["memory"]["id"], existing["id"])
        self.assertTrue(promoted["memory"]["dedup_merged"])
        self.assertEqual(promoted["candidate"]["status"], "promoted")
        self.assertEqual(promoted["candidate"]["promoted_node_id"], existing["id"])

    def test_chat_auto_accept_holds_value_updates_for_review(self) -> None:
        self._add("Лимит API", "Лимит запросов 5000 в час", "Constraint")
        update = {
            "label": "Лимит API", "text": "Лимит запросов 9000 в час", "type": "Constraint",
            "scope": "project", "project_id": "architectos", "metadata": {},
        }
        fresh = dict(update, label="Шрифт", text="Интерфейс использует шрифт Inter")
        self.assertTrue(self.service._auto_accept_near_duplicate(update))
        self.assertFalse(self.service._auto_accept_near_duplicate(fresh))


if __name__ == "__main__":
    unittest.main()
