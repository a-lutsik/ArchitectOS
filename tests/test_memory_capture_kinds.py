"""Stage B: what counts as important — kinds, correction signals, risk-based auto-save."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from backend.architectos.chat_memory import evaluate_chat_turn, has_durable_signal, is_chitchat_user_message
from backend.architectos.fact_signals import classify_fact_kind, describe_fact, detect_signals, has_capture_signal
from backend.architectos.memory_capture import annotate_fact, auto_threshold, normalize_capture_settings
from backend.architectos.service import ArchitectOSService


class FactSignalTests(unittest.TestCase):
    def test_corrections_are_kept_without_project_keywords(self) -> None:
        for text in ("Используй pnpm, а не npm", "No, don't add a semicolon after every statement", "Use tabs instead of spaces here"):
            self.assertTrue(has_capture_signal(text), text)
            self.assertTrue(has_durable_signal(text), text)
            self.assertEqual(classify_fact_kind(text), "feedback", text)

    def test_confirmation_is_feedback(self) -> None:
        self.assertEqual(classify_fact_kind("Да, так и оставь — коммиты только через PR"), "feedback")
        self.assertEqual(classify_fact_kind("Yes, keep it that way for the API layer"), "feedback")

    def test_first_person_preference_is_user_kind(self) -> None:
        for text in ("Я предпочитаю отвечать на русском и короткими абзацами", "I prefer pytest over unittest for new tests", "Call me Anton"):
            self.assertEqual(classify_fact_kind(text), "user", text)

    def test_team_preference_stays_project(self) -> None:
        described = describe_fact("Remember: the team prefers pytest for unit tests in this repository.", "user_statement")
        self.assertEqual(described["kind"], "project")
        self.assertGreaterEqual(described["confidence"], 0.85)

    def test_reference_kind(self) -> None:
        self.assertEqual(classify_fact_kind("Баги ведём в Jira, доска PLAT, дашборд в Grafana"), "reference")
        self.assertEqual(classify_fact_kind("Runbooks live at https://wiki.example.com/ops"), "reference")

    def test_rule_with_never_is_not_a_correction(self) -> None:
        signals = detect_signals("Architecture decision: never commit directly to main.")
        self.assertFalse(signals["correction"])
        self.assertTrue(signals["marker"] and signals["imperative"])

    def test_no_thanks_is_chitchat_not_a_correction(self) -> None:
        self.assertTrue(is_chitchat_user_message("No, thanks"))
        self.assertFalse(detect_signals("No, thanks").get("correction"))
        self.assertFalse(evaluate_chat_turn("No, thanks", "Sure.").keep)

    def test_confidence_orders_sources_and_signals(self) -> None:
        plain = describe_fact("We use Redis for the session cache", "user_statement")["confidence"]
        marked = describe_fact("Decision: we always use Redis for the session cache", "user_statement")["confidence"]
        mined = describe_fact("We use Redis for the session cache", "assistant_extract")["confidence"]
        derivable = describe_fact("The parse function is defined in backend/parser.py and returns a dict", "assistant_extract")["confidence"]
        transient = describe_fact("Пока что деплой делаем вручную", "user_statement")["confidence"]
        self.assertGreater(marked, plain)
        self.assertGreater(plain, mined)
        self.assertLess(derivable, 0.6)
        self.assertLess(transient, plain)
        self.assertTrue(all(0.5 <= value <= 0.95 for value in (plain, marked, mined, derivable, transient)))

    def test_annotate_sets_scope_type_and_evidence(self) -> None:
        user = annotate_fact({"text": "Я предпочитаю короткие ответы без вступлений", "source": "user_statement"})
        self.assertEqual((user["kind"], user["type"], user["scope"]), ("user", "Preference", "global"))
        self.assertEqual(user["evidence"], "Я предпочитаю короткие ответы без вступлений")
        fix = annotate_fact({"text": "No, use pnpm instead of npm for installs", "source": "user_line", "type": "Lesson"})
        self.assertEqual((fix["kind"], fix["scope"]), ("feedback", "project"))
        explicit = annotate_fact({"text": "anything", "source": "user_line", "kind": "reference", "confidence": 0.9})
        self.assertEqual((explicit["kind"], explicit["confidence"]), ("reference", 0.9))

    def test_auto_threshold_is_higher_for_project_facts(self) -> None:
        policy = normalize_capture_settings({"min_confidence": 0.7, "auto_project_min_confidence": 0.85})
        self.assertEqual(auto_threshold(policy, "project"), 0.85)
        self.assertEqual(auto_threshold(policy, "feedback"), 0.7)
        self.assertEqual(auto_threshold(policy, "user"), 0.7)
        low = normalize_capture_settings({"min_confidence": 0.9, "auto_project_min_confidence": 0.6})
        self.assertEqual(auto_threshold(low, "project"), 0.9)


class CaptureKindsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = ArchitectOSService(Path(self._tmp.name))
        self.repo = self.service.repository

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _turn(self, user: str, assistant: str = "") -> dict[str, Any]:
        return self.service.capture_memory_turn(user, assistant, project_id="architectos")

    def test_correction_is_captured_and_written_in_auto_mode(self) -> None:
        turn = self._turn("Используй pnpm, а не npm")
        self.assertTrue(turn["kept"])
        self.assertEqual(len(turn["auto_accepted"]), 1)
        atom = turn["auto_accepted"][0]
        self.assertEqual(atom["metadata"]["kind"], "feedback")
        self.assertIn("correction", atom["metadata"]["capture_signals"])
        node = self.repo.get_node(atom["promoted_node_id"])
        assert node is not None
        self.assertEqual(node.metadata.get("kind"), "feedback")
        self.assertEqual(node.scope, "project")

    def test_user_preference_becomes_global_long_term_preference(self) -> None:
        turn = self._turn("Я предпочитаю отвечать на русском и короткими абзацами")
        atom = turn["auto_accepted"][0]
        self.assertEqual((atom["type"], atom["scope"], atom["metadata"]["kind"]), ("Preference", "global", "user"))
        node = self.repo.get_node(atom["promoted_node_id"])
        assert node is not None
        self.assertEqual(node.type, "Preference")
        self.assertEqual(node.scope, "global")
        self.assertIsNone(node.project_id)
        self.assertEqual(node.metadata.get("memory_tier"), "long_term")
        self.assertEqual(node.metadata.get("evidence"), "Я предпочитаю отвечать на русском и короткими абзацами")

    def test_global_preference_is_visible_from_another_project(self) -> None:
        self._turn("I prefer pytest over unittest for new tests")
        other = self.service.create_project({"name": "Other", "description": "second project"})
        pack = self.service.context("which test framework do I prefer", project_id=other["id"])
        self.assertTrue(any("pytest" in str(hit.get("node", hit).get("text") or hit.get("text") or "") for hit in pack.get("hits") or []))

    def test_reference_kind_is_recorded(self) -> None:
        turn = self._turn("Баги ведём в Jira, доска PLAT, дашборд в Grafana")
        atom = (turn["auto_accepted"] or turn["queued"])[0]
        self.assertEqual(atom["metadata"]["kind"], "reference")

    def test_project_decision_with_marker_auto_saves_but_plain_statement_waits(self) -> None:
        marked = self._turn("Architecture decision: never commit directly to main.")
        self.assertEqual(len(marked["auto_accepted"]), 1)
        self.assertEqual(marked["auto_accepted"][0]["metadata"]["kind"], "project")
        plain = self._turn("We decided to use Redis for the session cache in this service")
        self.assertTrue(plain["kept"])
        self.assertEqual(plain["auto_accepted"], [])
        self.assertEqual(len(plain["queued"]), 1)
        self.assertLess(float(plain["queued"][0]["confidence"]), 0.85)

    def test_explicit_remember_writes_project_fact_at_once(self) -> None:
        turn = self._turn("Remember: UniqueBirchFormatter is the required Python formatter in this repo.")
        self.assertEqual(len(turn["auto_accepted"]), 1)
        self.assertIn("explicit_save", turn["auto_accepted"][0]["metadata"]["capture_signals"])
        self.assertEqual(auto_threshold(normalize_capture_settings(None), "project", ["explicit_save"]), 0.7)

    def test_project_threshold_is_configurable(self) -> None:
        self.service.update_settings({"memory_capture": {"auto_project_min_confidence": 0.7}})
        plain = self._turn("We decided to use Redis for the session cache in this service")
        self.assertEqual(len(plain["auto_accepted"]), 1)

    def test_code_derivable_fact_waits_for_review(self) -> None:
        turn = self._turn(
            "where is the parser?",
            "Decision: the parse function is defined in backend/parser.py and returns a dict.",
        )
        self.assertTrue(turn["kept"])
        self.assertEqual(turn["auto_accepted"], [])
        self.assertEqual(len(turn["queued"]), 1)
        self.assertIn("derivable", turn["queued"][0]["metadata"]["capture_signals"])

    def test_review_card_carries_kind_and_evidence(self) -> None:
        self.service.update_settings({"memory_capture": {"mode": "review"}})
        turn = self._turn("No, don't add a semicolon after every statement")
        card = turn["queued"][0]
        self.assertEqual(card["metadata"]["kind"], "feedback")
        self.assertEqual(card["metadata"]["evidence"], "No, don't add a semicolon after every statement")
        self.assertGreaterEqual(float(card["confidence"]), 0.7)


if __name__ == "__main__":
    unittest.main()
