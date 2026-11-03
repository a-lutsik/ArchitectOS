"""Stage C: the LLM fact extractor and its fallbacks."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from backend.architectos.capture_extractor_service import CaptureExtractorMixin
from backend.architectos.service import ArchitectOSService

TURN = "давай лучше по-другому: вся конфигурация через переменные окружения, файлов конфигов не держим"


def _reply(facts: list[dict[str, Any]], provider: str = "fake-llm") -> dict[str, Any]:
    return {"text": "Sure, here you go:\n" + json.dumps({"facts": facts}), "selected_provider": {"id": provider}}


class LlmExtractorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = ArchitectOSService(Path(self._tmp.name))
        self.repo = self.service.repository

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _enable_llm(self, extractor: str = "llm") -> None:
        self.service.update_settings({"memory_capture": {"extractor": extractor}})

    def _route(self, reply: Any) -> Any:
        return mock.patch.object(self.service.provider_router, "route", return_value=reply)

    # -- availability ----------------------------------------------------------
    def test_heuristic_setting_never_calls_the_model(self) -> None:
        self._enable_llm("heuristic")
        with self._route(_reply([])) as route:
            turn = self.service.capture_memory_turn("Architecture decision: never commit directly to main.", project_id="architectos")
        route.assert_not_called()
        self.assertEqual(turn["extractor"], "heuristic")
        self.assertTrue(turn["kept"])

    def test_auto_without_a_real_provider_stays_heuristic(self) -> None:
        self.assertEqual(self.service.memory_capture_settings()["extractor"], "auto")
        with self._route(_reply([])) as route:
            turn = self.service.capture_memory_turn("Architecture decision: never commit directly to main.", project_id="architectos")
        route.assert_not_called()
        self.assertEqual(turn["extractor"], "heuristic")

    def test_auto_uses_the_model_when_a_provider_is_enabled(self) -> None:
        self.repo.upsert_provider({"id": "openai", "label": "OpenAI", "enabled": True, "status": "ok"})
        with self._route(_reply([])) as route:
            self.service.capture_memory_turn("Architecture decision: never commit directly to main.", project_id="architectos")
        route.assert_called_once()

    def test_chitchat_never_reaches_the_model(self) -> None:
        self._enable_llm()
        with self._route(_reply([])) as route:
            turn = self.service.capture_memory_turn("спасибо", project_id="architectos")
        route.assert_not_called()
        self.assertFalse(turn["kept"])

    def test_mode_off_never_reaches_the_model(self) -> None:
        self.service.update_settings({"memory_capture": {"mode": "off", "extractor": "llm"}})
        with self._route(_reply([{"kind": "project", "text": "x" * 30, "confidence": 0.9}])) as route:
            turn = self.service.capture_memory_turn("Architecture decision: never commit directly to main.", project_id="architectos")
        route.assert_not_called()
        self.assertEqual(turn["atoms"], [])

    # -- extraction ------------------------------------------------------------
    def test_model_finds_what_keywords_miss(self) -> None:
        self._enable_llm()
        self.service.update_settings({"memory_capture": {"extractor": "heuristic"}})
        heuristic = self.service.capture_memory_turn(TURN, project_id="architectos")
        # Rules see a generic statement: project kind, below the auto threshold.
        self.assertEqual(heuristic["auto_accepted"], [])
        self.assertEqual(heuristic["queued"][0]["metadata"]["kind"], "project")
        self.service.update_settings({"memory_capture": {"extractor": "llm"}})
        reply = _reply([{
            "kind": "feedback", "type": "Constraint",
            "text": "Configuration comes only from environment variables; no config files are kept.",
            "confidence": 0.88, "evidence": "вся конфигурация через переменные окружения, файлов конфигов не держим",
        }])
        with self._route(reply) as route:
            turn = self.service.capture_memory_turn(TURN, project_id="architectos")
        route.assert_called_once()
        self.assertEqual(turn["extractor"], "llm")
        self.assertTrue(turn["kept"])
        atom = turn["auto_accepted"][0]
        self.assertEqual((atom["metadata"]["kind"], atom["type"]), ("feedback", "Constraint"))
        self.assertEqual(atom["metadata"]["fact_source"], "llm_user")
        self.assertEqual(atom["confidence"], 0.88)
        self.assertIn("переменные окружения", atom["metadata"]["evidence"])
        prompt = route.call_args.args[1].message
        self.assertIn("ALREADY KNOWN", prompt)
        self.assertIn("USER:", prompt)

    def test_known_block_lists_stored_memory(self) -> None:
        self._enable_llm()
        self.service.add_memory({"label": "Хранилище", "text": "Данные проекта лежат в локальной базе SQLite", "type": "Decision"})
        with self._route(_reply([])) as route:
            self.service.capture_memory_turn("где у нас лежат данные проекта и почему именно так?", project_id="architectos")
        prompt = route.call_args.args[1].message
        self.assertIn("Хранилище", prompt)
        self.assertIn("SQLite", prompt)

    def test_empty_model_answer_means_nothing_durable(self) -> None:
        self._enable_llm()
        with self._route(_reply([])):
            turn = self.service.capture_memory_turn("Architecture decision: never commit directly to main.", project_id="architectos")
        self.assertEqual(turn["extractor"], "llm")
        self.assertFalse(turn["kept"])
        self.assertEqual(turn["reason"], "llm_nothing_durable")
        self.assertEqual(turn["atoms"], [])

    def test_garbage_or_stub_falls_back_to_heuristics(self) -> None:
        for reply in (
            {"text": "I cannot help with that.", "selected_provider": {"id": "fake-llm"}},
            {"text": "This is Local Memory fallback, not a language model.", "selected_provider": {"id": "local-memory"}},
            {"text": "", "selected_provider": {}},
        ):
            self._enable_llm()
            self.service._capture_llm_backoff_until = 0.0  # noqa: SLF001
            with self._route(reply):
                turn = self.service.capture_memory_turn("Architecture decision: never commit directly to main.", project_id="architectos")
            self.assertEqual(turn["extractor"], "heuristic", reply)
            self.assertTrue(turn["kept"], reply)

    def test_provider_error_backs_off_and_falls_back(self) -> None:
        self._enable_llm()
        with mock.patch.object(self.service.provider_router, "route", side_effect=RuntimeError("boom")) as route:
            first = self.service.capture_memory_turn("Architecture decision: never commit directly to main.", project_id="architectos")
            second = self.service.capture_memory_turn("Constraint: always use pytest for tests in this repository.", project_id="architectos")
        self.assertEqual(route.call_count, 1, "a failed provider is not retried on the very next turn")
        self.assertEqual((first["extractor"], second["extractor"]), ("heuristic", "heuristic"))
        self.assertTrue(first["kept"] and second["kept"])
        stats = self.service.memory_capture_stats(days=7)
        self.assertGreaterEqual(stats["llm_fallbacks"], 1)

    def test_model_facts_are_validated_and_rescored(self) -> None:
        facts = CaptureExtractorMixin._parse_llm_facts(  # noqa: SLF001
            json.dumps({"facts": [
                {"kind": "bogus", "type": "Whatever", "text": "The parse function is defined in backend/parser.py and returns a dict", "confidence": 0.95},
                {"kind": "user", "text": "The person prefers short answers in Russian", "confidence": "high", "evidence": "I prefer short answers in Russian"},
                {"kind": "project", "text": "short", "confidence": 0.9},
                {"kind": "project", "text": "Deploys happen for now by hand until the pipeline is ready", "confidence": 0.9},
                "not a dict",
            ]}),
            "I prefer short answers in Russian", "", 8,
        )
        assert facts is not None
        self.assertEqual(len(facts), 3)
        derivable, user, transient = facts
        self.assertEqual(derivable["kind"], "project")
        self.assertLessEqual(derivable["confidence"], 0.8)
        self.assertIn("derivable", derivable["signals"])
        self.assertEqual((user["kind"], user["type"], user["confidence"], user["source"]), ("user", "Preference", 0.7, "llm_user"))
        self.assertIn("transient", transient["signals"])
        self.assertEqual(transient["confidence"], 0.8)
        self.assertIsNone(CaptureExtractorMixin._parse_llm_facts("no json here", "", "", 8))  # noqa: SLF001

    def test_secret_in_dialog_is_redacted_before_the_prompt(self) -> None:
        self._enable_llm()
        with self._route(_reply([])) as route:
            self.service.capture_memory_turn(
                "Remember: deploy token is OPENAI_API_KEY=sk-proj-abcdef1234567890abcdef1234 and keep it", project_id="architectos",
            )
        self.assertNotIn("sk-proj", route.call_args.args[1].message)

    def test_session_summary_uses_one_model_call(self) -> None:
        self._enable_llm()
        posted = self.service.post_chat_message({
            "project_id": "architectos",
            "message": "давай лучше по-другому: конфиги не держим, всё через env\nи тесты гоняем только pytest",
            "provider_id": "local-memory",
        })
        reply = _reply([
            {"kind": "feedback", "type": "Constraint", "text": "Configuration comes only from environment variables, never from config files.", "confidence": 0.9},
            {"kind": "project", "type": "Decision", "text": "Tests run with pytest only.", "confidence": 0.9},
        ])
        with self._route(reply) as route:
            finalized = self.service.finalize_chat_session(posted["chat"]["id"], force=True, trigger="manual")
        # The summary's own narrative step may also ask the model; the extractor asks once.
        extractor_calls = [call for call in route.call_args_list if "ALREADY KNOWN" in call.args[1].message]
        self.assertEqual(len(extractor_calls), 1)
        self.assertEqual(finalized["summary"]["extractor"], "llm")
        self.assertEqual(len(finalized["atoms"]), 2)
        self.assertEqual({a["metadata"]["kind"] for a in finalized["atoms"]}, {"feedback", "project"})

    def test_conservative_keeps_only_model_facts_spoken_by_the_user(self) -> None:
        self.service.update_settings({"memory_capture": {"extractor": "llm", "aggressiveness": "conservative", "mode": "review"}})
        reply = _reply([
            {"kind": "feedback", "type": "Lesson", "text": "Configuration comes from environment variables only.", "confidence": 0.8, "evidence": "вся конфигурация через переменные окружения"},
            {"kind": "project", "type": "Lesson", "text": "The assistant suggested adding a config loader module.", "confidence": 0.8, "evidence": "I suggest a config loader module"},
        ])
        with self._route(reply):
            turn = self.service.capture_memory_turn(TURN, "I suggest a config loader module", project_id="architectos")
        self.assertEqual([a["metadata"]["fact_source"] for a in turn["atoms"]], ["llm_user"])

    def test_settings_expose_extractor_and_counters(self) -> None:
        self._enable_llm()
        with self._route(_reply([])):
            self.service.capture_memory_turn("Architecture decision: never commit directly to main.", project_id="architectos")
        capture = self.service.settings()["memory_capture"]
        self.assertEqual(capture["extractor"], "llm")
        self.assertGreaterEqual(capture["stats_7d"]["llm_calls"], 1)
        self.assertEqual(self.service.update_settings({"memory_capture": {"extractor": "nope"}})["memory_capture"]["extractor"], "auto")


if __name__ == "__main__":
    unittest.main()
