"""Regression tests for memory dedup on non-ASCII text and exact restatements."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from backend.architectos.memory_dedup import normalize_fact_text
from backend.architectos.memory_ingestion import (
    DUPLICATE_STOPWORDS,
    FINGERPRINT_VERSION,
    _token_set,
    candidate_fingerprint,
    fingerprint_from_tokens,
)
from backend.architectos.service import ArchitectOSService
from backend.architectos.storage import SQLiteMemoryRepository


def _atom(fact_key: str, text: str) -> dict[str, Any]:
    return {
        "label": text[:40],
        "text": text,
        "type": "Decision",
        "scope": "project",
        "project_id": "architectos",
        "source_type": "mcp",
        "metadata": {"template": "mcp_turn_atom", "fact_key": fact_key},
    }


class DedupTokenizerTests(unittest.TestCase):
    def test_cyrillic_words_are_tokens(self) -> None:
        tokens = _token_set("Проект использует PostgreSQL 16 для хранения данных.", DUPLICATE_STOPWORDS)
        self.assertEqual(tokens, {"использует", "postgresql", "хранения", "данных"})

    def test_yo_and_sentence_dot_are_folded(self) -> None:
        self.assertEqual(
            _token_set("Ёмкость кэша в SQLite.", DUPLICATE_STOPWORDS),
            _token_set("емкость кэша в sqlite", DUPLICATE_STOPWORDS),
        )
        # A dot inside a token is content (file names, versions).
        self.assertIn("file.py", _token_set("see file.py.", DUPLICATE_STOPWORDS))

    def test_short_fingerprint_keeps_readable_format(self) -> None:
        self.assertEqual(fingerprint_from_tokens({"beta", "alpha"}), "alpha|beta")

    def test_long_fingerprints_differ_when_only_the_tail_differs(self) -> None:
        shared = " ".join(f"aaa{i:02d}" for i in range(18))
        left = candidate_fingerprint("x", f"{shared} uses postgres")
        right = candidate_fingerprint("x", f"{shared} uses mysql")
        self.assertNotEqual(left, right)
        self.assertEqual(left, candidate_fingerprint("x", f"{shared} uses postgres"))


class DedupIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = ArchitectOSService(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_distinct_cyrillic_atoms_in_one_batch_are_all_kept(self) -> None:
        engine = self.service.ingestion_engine
        prepared = engine.prepare_candidates("architectos", [
            _atom("база данных sqlite", "Проект хранит данные в SQLite"),
            _atom("язык интерфейса русский", "Интерфейс по умолчанию на русском"),
            _atom("бэкенд без зависимостей", "Бэкенд работает только на stdlib"),
        ], 0)
        self.assertEqual(len(prepared), 3)
        self.assertEqual(engine.skipped_reviewed, 0)
        for item in prepared:
            self.assertTrue(item["metadata"]["fingerprint"])
            self.assertEqual(item["metadata"]["fingerprint_version"], FINGERPRINT_VERSION)

    def test_repeated_cyrillic_atom_in_one_batch_is_still_skipped(self) -> None:
        engine = self.service.ingestion_engine
        prepared = engine.prepare_candidates("architectos", [
            _atom("бэкенд без зависимостей", "Бэкенд работает только на stdlib"),
            _atom("бэкенд без зависимостей", "Бэкенд работает только на stdlib"),
        ], 0)
        self.assertEqual(len(prepared), 1)
        self.assertEqual(engine.skipped_reviewed, 1)

    def test_atoms_without_content_tokens_are_not_batch_duplicates(self) -> None:
        engine = self.service.ingestion_engine
        prepared = engine.prepare_candidates("architectos", [
            _atom("это так", "Это так"),
            _atom("все уже", "Все уже"),
        ], 0)
        self.assertEqual(len(prepared), 2)

    def test_restatement_with_case_punctuation_and_yo_is_merged(self) -> None:
        first = self.service.add_memory(
            {"label": "Хранилище", "text": "Всё хранится в локальной базе SQLite", "type": "Decision"}
        )
        again = self.service.add_memory(
            {"label": "хранилище", "text": "  все хранится в локальной базе sqlite. ", "type": "Decision"}
        )
        self.assertTrue(again.get("dedup_merged"))
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["metadata"]["dedup_hits"], 1)

    def test_facts_differing_by_a_number_are_not_merged(self) -> None:
        # Distinct labels: nodes with the same type+scope+label share a stable id
        # and replace each other in add_node, which is a separate (stage 1) issue.
        first = self.service.add_memory({"label": "Лимит API", "text": "Лимит запросов 5000 в час", "type": "Constraint"})
        second = self.service.add_memory({"label": "Лимит API.", "text": "Лимит запросов 9000 в час", "type": "Constraint"})
        self.assertFalse(second.get("dedup_merged"))
        self.assertNotEqual(first["id"], second["id"])

    def test_leading_minus_is_not_stripped_as_punctuation(self) -> None:
        normalize = normalize_fact_text
        self.assertEqual(normalize("«Данные в SQLite.»"), normalize("данные в sqlite"))
        self.assertNotEqual(normalize("-5 градусов"), normalize("5 градусов"))

    def test_same_fact_as_lesson_then_decision_keeps_the_decision(self) -> None:
        lesson = self.service.add_memory(
            {"label": "Деплой", "text": "Деплой идёт через GitHub Actions", "type": "Lesson"}
        )
        decision = self.service.add_memory(
            {"label": "Деплой", "text": "Деплой идёт через GitHub Actions.", "type": "Decision"}
        )
        self.assertTrue(decision.get("dedup_merged"))
        self.assertEqual(decision["id"], lesson["id"])
        self.assertEqual(decision["type"], "Decision")
        stored = self.service.repository.get_node(lesson["id"])
        assert stored is not None
        self.assertEqual(stored.type, "Decision")
        self.assertEqual(stored.metadata.get("dedup_type_from"), "Lesson")
        self.assertEqual(stored.metadata.get("memory_tier"), "long_term")
        events = self.service.repository.list_memory_events(lesson["id"])
        self.assertTrue(any(
            event["event_type"] == "dedup" and event["details"].get("type_to") == "Decision"
            for event in events
        ))

    def test_weaker_type_restatement_does_not_downgrade(self) -> None:
        decision = self.service.add_memory({"label": "CI", "text": "CI runs on three OSes", "type": "Decision"})
        lesson = self.service.add_memory({"label": "CI", "text": "CI runs on three OSes", "type": "Lesson"})
        self.assertTrue(lesson.get("dedup_merged"))
        self.assertEqual(lesson["id"], decision["id"])
        self.assertEqual(lesson["type"], "Decision")

    def test_non_fact_types_are_not_merged_across_types(self) -> None:
        artifact = self.service.add_memory({"label": "Readme", "text": "Project readme", "type": "Artifact"})
        lesson = self.service.add_memory({"label": "Readme", "text": "Project readme", "type": "Lesson"})
        self.assertFalse(lesson.get("dedup_merged"))
        self.assertNotEqual(artifact["id"], lesson["id"])


class DedupMigrationTests(unittest.TestCase):
    def test_v2_candidates_are_restamped_with_cyrillic_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteMemoryRepository(Path(tmp))
            candidate = {
                "id": "cand-ru",
                "project_id": "architectos",
                "source_type": "docs",
                "source_ref": "ru.md",
                "label": "Хранилище",
                "type": "Lesson",
                "scope": "project",
                "text": "Проект хранит данные в SQLite",
                "status": "candidate",
                # What the ASCII-only tokenizer stored for this text.
                "metadata": {"fingerprint": "sqlite", "tokens": ["sqlite"], "fingerprint_version": 2},
            }
            repository.upsert_memory_candidate(candidate)
            with repository._connect() as conn:
                conn.execute("PRAGMA user_version=5")
            before = repository.get_memory_candidate("cand-ru")
            assert before is not None

            repository._migrate()

            after = repository.get_memory_candidate("cand-ru")
            assert after is not None
            meta = after["metadata"]
            self.assertEqual(meta["fingerprint_version"], FINGERPRINT_VERSION)
            self.assertEqual(meta["fingerprint"], candidate_fingerprint(candidate["label"], candidate["text"], {}))
            self.assertIn("хранит", meta["tokens"])
            self.assertEqual(after["updated_at"], before["updated_at"])


if __name__ == "__main__":
    unittest.main()
