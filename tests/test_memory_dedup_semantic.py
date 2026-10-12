"""Stage 3: enrich, semantic neighbours, optional LLM judge, gate modes."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from backend.architectos.service import ArchitectOSService


def _llm(text: str) -> dict[str, Any]:
    return {"text": text, "selected_provider": {"id": "fake-llm"}}


class SemanticDedupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = ArchitectOSService(Path(self._tmp.name))
        self.repo = self.service.repository

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add(self, label: str, text: str, node_type: str = "Decision", **extra: Any) -> dict[str, Any]:
        return self.service.add_memory({"label": label, "text": text, "type": node_type, **extra})

    def _settings(self, **values: Any) -> None:
        self.service.update_settings({"memory_dedup": values})

    def _semantic(self, scores: dict[str, float]) -> Any:
        """Pretend a real embedding provider is configured and returns these neighbours."""
        self.service.memory_embeddings.provider.provider_id = "fake-embedder"  # type: ignore[misc]
        return mock.patch.object(
            self.service, "_vector_search_memory_timed", return_value=(scores, 1.0, False),
        )

    # -- enrich / covered -------------------------------------------------
    def test_enrich_replaces_text_and_undo_restores_both_facts(self) -> None:
        base = self._add("Деплой", "Деплой через GitHub Actions")
        fuller = self._add("Деплой", "Деплой через GitHub Actions на каждый push в main ветку", source="mcp")
        self.assertTrue(fuller["dedup_merged"])
        self.assertEqual(fuller["id"], base["id"])
        self.assertEqual(fuller["dedup"]["action"], "enrich")
        self.assertEqual(fuller["text"], "Деплой через GitHub Actions на каждый push в main ветку")
        self.assertEqual(fuller["metadata"]["dedup_enrichments"], 1)

        undo = self.service.undo_memory_dedup({"event_id": fuller["dedup"]["event_id"]})
        stored = self.repo.get_node(base["id"])
        assert stored is not None
        self.assertEqual(stored.text, "Деплой через GitHub Actions")
        self.assertEqual(undo["restored"]["text"], "Деплой через GitHub Actions на каждый push в main ветку")
        self.assertNotEqual(undo["restored"]["id"], base["id"])

    def test_enrich_undo_refuses_after_later_edit(self) -> None:
        self._add("Деплой", "Деплой через GitHub Actions")
        fuller = self._add("Деплой", "Деплой через GitHub Actions на каждый push в main ветку")
        stored = self.repo.get_node(fuller["id"])
        assert stored is not None
        stored.text = "Отредактировано вручную после слияния"
        self.repo.upsert_node(stored)
        with self.assertRaises(ValueError):
            self.service.undo_memory_dedup({"event_id": fuller["dedup"]["event_id"]})

    def test_shorter_restatement_keeps_the_fuller_text(self) -> None:
        base = self._add("Деплой", "Деплой через GitHub Actions на каждый push в main ветку")
        short = self._add("Деплой", "Деплой через GitHub Actions")
        self.assertTrue(short["dedup_merged"])
        self.assertEqual(short["dedup"]["reason"], "already covered by a fuller fact")
        self.assertEqual(short["text"], base["text"])

    def test_bulk_counts_enrichments(self) -> None:
        self._add("Деплой", "Деплой через GitHub Actions")
        result = self.service.add_memory_bulk({"items": [
            {"label": "Деплой", "text": "Деплой через GitHub Actions на каждый push", "type": "Decision"},
        ]})
        self.assertEqual(result["stats"]["enriched"], 1)
        self.assertEqual(result["stats"]["deduplicated"], 1)

    # -- semantic neighbours ----------------------------------------------
    def test_semantic_neighbour_sets_possible_duplicate_flag(self) -> None:
        base = self._add("Хранилище", "Данные лежат в SQLite")
        with self._semantic({base["id"]: 0.91}):
            other = self._add("БД", "Проект держит всё в локальной базе")
        self.assertEqual(other["dedup"]["action"], "new")
        self.assertEqual(other["metadata"]["possible_duplicate_of"], base["id"])
        self.assertEqual(other["metadata"]["possible_duplicate_kind"], "semantic")

    def test_weak_semantic_neighbour_is_not_flagged(self) -> None:
        base = self._add("Хранилище", "Данные лежат в SQLite")
        with self._semantic({base["id"]: 0.7}):
            other = self._add("БД", "Проект держит всё в локальной базе")
        self.assertNotIn("possible_duplicate_of", other["metadata"])

    def test_hash_embedder_is_not_used_as_semantics(self) -> None:
        self._add("Хранилище", "Данные лежат в SQLite")
        with mock.patch.object(self.service, "_vector_search_memory_timed") as search:
            self._add("БД", "Проект держит всё в локальной базе")
        search.assert_not_called()

    def test_semantic_off_skips_vector_search(self) -> None:
        base = self._add("Хранилище", "Данные лежат в SQLite")
        self._settings(semantic="off")
        with self._semantic({base["id"]: 0.95}) as search:
            other = self._add("БД", "Проект держит всё в локальной базе")
        search.assert_not_called()
        self.assertNotIn("possible_duplicate_of", other["metadata"])

    # -- LLM judge ---------------------------------------------------------
    def test_llm_judge_folds_paraphrase(self) -> None:
        base = self._add("Хранилище", "Данные лежат в SQLite")
        self._settings(llm_judge=True)
        verdict = '{"action": "duplicate", "confidence": 0.92, "reason": "same storage fact"}'
        with self._semantic({base["id"]: 0.88}), mock.patch.object(
            self.service.provider_router, "route", return_value=_llm(verdict)
        ) as route:
            other = self._add("БД", "Проект держит всё в локальной базе")
        route.assert_called_once()
        self.assertTrue(other["dedup_merged"])
        self.assertEqual(other["id"], base["id"])
        self.assertTrue(other["dedup"]["reason"].startswith("llm judge"))
        self.assertEqual(other["dedup"]["details"]["judge"], "llm")
        self.assertEqual(other["dedup"]["details"]["provider"], "fake-llm")

    def test_llm_judge_enrich_replaces_text(self) -> None:
        base = self._add("Хранилище", "Данные лежат в SQLite")
        self._settings(llm_judge=True)
        verdict = '{"action": "enrich", "confidence": 0.9, "reason": "adds WAL"}'
        with self._semantic({base["id"]: 0.9}), mock.patch.object(
            self.service.provider_router, "route", return_value=_llm(verdict)
        ):
            other = self._add("БД", "Все данные проекта в SQLite с журналом WAL")
        self.assertEqual(other["dedup"]["action"], "enrich")
        self.assertEqual(other["text"], "Все данные проекта в SQLite с журналом WAL")

    def test_llm_low_confidence_or_garbage_keeps_a_new_node(self) -> None:
        base = self._add("Хранилище", "Данные лежат в SQLite")
        self._settings(llm_judge=True)
        cases = [
            ('{"action": "duplicate", "confidence": 0.4}', "Проект держит всё в локальной базе"),
            ("I think they are the same", "Всё хранится локально на диске"),
            ("{not json", "Файл базы лежит рядом с приложением"),
        ]
        for answer, text in cases:
            with self._semantic({base["id"]: 0.9}), mock.patch.object(
                self.service.provider_router, "route", return_value=_llm(answer)
            ):
                other = self._add(f"БД {answer[:6]}", text)
            self.assertFalse(other.get("dedup_merged"), answer)
            self.assertEqual(other["metadata"]["possible_duplicate_of"], base["id"], answer)

    def test_llm_is_not_asked_when_protected_details_differ(self) -> None:
        base = self._add("Лимит", "Лимит запросов 5000 в час")
        self._settings(llm_judge=True)
        with self._semantic({base["id"]: 0.97}), mock.patch.object(self.service.provider_router, "route") as route:
            other = self._add("Квота API", "Квота на запросы 9000 в час")
        route.assert_not_called()
        self.assertFalse(other.get("dedup_merged"))

    def test_llm_judge_is_off_by_default(self) -> None:
        base = self._add("Хранилище", "Данные лежат в SQLite")
        with self._semantic({base["id"]: 0.9}), mock.patch.object(self.service.provider_router, "route") as route:
            self._add("БД", "Проект держит всё в локальной базе")
        route.assert_not_called()

    def test_preview_can_skip_the_llm(self) -> None:
        base = self._add("Хранилище", "Данные лежат в SQLite")
        self._settings(llm_judge=True)
        with self._semantic({base["id"]: 0.9}), mock.patch.object(self.service.provider_router, "route") as route:
            preview = self.service.preview_memory_dedup(
                {"label": "БД", "text": "Проект держит всё в локальной базе", "type": "Decision", "use_llm": False}
            )
        route.assert_not_called()
        self.assertEqual(preview["dedup"]["near_id"], base["id"])

    # -- modes & settings ----------------------------------------------------
    def test_suggest_mode_stores_new_node_with_the_verdict(self) -> None:
        base = self._add("Порт", "Сервер слушает порт 8765")
        self._settings(mode="suggest")
        again = self._add("Порт", "Сервер слушает порт 8765.")
        self.assertFalse(again.get("dedup_merged"))
        self.assertNotEqual(again["id"], base["id"])
        self.assertEqual(again["dedup"]["details"]["suggested_action"], "duplicate")
        self.assertEqual(again["metadata"]["dedup_suggestion"]["action"], "duplicate")
        self.assertEqual(again["metadata"]["possible_duplicate_of"], base["id"])

    def test_off_mode_skips_the_gate_but_never_overwrites(self) -> None:
        base = self._add("Порт", "Сервер слушает порт 8765")
        self._settings(mode="off")
        again = self._add("Порт", "Сервер слушает порт 8765")
        self.assertNotEqual(again["id"], base["id"])
        self.assertEqual(again["dedup"], {"action": "new"})
        self.assertIsNotNone(self.repo.get_node(base["id"]))

    def test_settings_are_merged_and_normalized(self) -> None:
        self._settings(llm_judge=True)
        self._settings(mode="bogus", semantic_flag_score=3)
        current = self.service.settings()["memory_dedup"]
        self.assertTrue(current["llm_judge"])
        self.assertEqual(current["mode"], "auto")
        self.assertEqual(current["semantic_flag_score"], 1.0)

    def test_chat_auto_accept_holds_semantic_and_suggested_facts(self) -> None:
        base = self._add("Хранилище", "Данные лежат в SQLite")
        candidate = {
            "label": "БД", "text": "Проект держит всё в локальной базе", "type": "Decision",
            "scope": "project", "project_id": "architectos", "metadata": {},
        }
        with self._semantic({base["id"]: 0.9}):
            self.assertTrue(self.service._auto_accept_near_duplicate(candidate))
        self._settings(mode="suggest")
        repeat = dict(candidate, label="Хранилище", text="Данные лежат в SQLite")
        self.assertTrue(self.service._auto_accept_near_duplicate(repeat))

    def test_promote_in_suggest_mode_does_not_fold(self) -> None:
        existing = self._add("Архитектура", "Сервер и UI работают локально")
        self._settings(mode="suggest")
        self.repo.upsert_memory_candidate({
            "id": "cand-same", "project_id": "architectos", "source_type": "docs", "source_ref": "a.md",
            "label": "Архитектура", "type": "Decision", "scope": "project",
            "text": "Сервер и UI работают локально.", "status": "candidate", "metadata": {},
        })
        promoted = self.service.promote_memory_candidate("cand-same")
        self.assertNotEqual(promoted["memory"]["id"], existing["id"])


if __name__ == "__main__":
    unittest.main()
