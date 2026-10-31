"""Memory capture policy (stage A): one extractor, the mode decides a fact's fate."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from backend.architectos.memory_capture import (
    DEFAULT_MEMORY_CAPTURE,
    capture_from_legacy,
    filter_facts_for_policy,
    legacy_mirror,
    normalize_capture_settings,
)
from backend.architectos.service import ArchitectOSService

DECISION = "Architecture decision: never commit directly to main."
CONSTRAINT = "Constraint: always use pytest for tests in this repository."


class CapturePolicyUnitTests(unittest.TestCase):
    def test_defaults_are_auto_normal(self) -> None:
        policy = normalize_capture_settings(None)
        self.assertEqual((policy["mode"], policy["aggressiveness"], policy["min_confidence"]), ("auto", "normal", 0.7))
        self.assertEqual(policy["chat_memory_mode"], "strict")

    def test_bogus_values_fall_back(self) -> None:
        policy = normalize_capture_settings({"mode": "yolo", "aggressiveness": "max", "min_confidence": "x"})
        self.assertEqual(policy["mode"], DEFAULT_MEMORY_CAPTURE["mode"])
        self.assertEqual(policy["aggressiveness"], "normal")
        self.assertEqual(policy["min_confidence"], 0.7)
        self.assertEqual(normalize_capture_settings({"min_confidence": 7})["min_confidence"], 1.0)

    def test_legacy_mapping(self) -> None:
        self.assertEqual(capture_from_legacy({"chat_memory_mode": "off"})["mode"], "off")
        review = capture_from_legacy({"chat_memory_mode": "strict", "chat_auto_accept": False})
        self.assertEqual((review["mode"], review["aggressiveness"]), ("review", "normal"))
        eager = capture_from_legacy({"chat_memory_mode": "aggressive", "chat_auto_accept": True})
        self.assertEqual((eager["mode"], eager["aggressiveness"], eager["chat_memory_mode"]), ("auto", "eager", "aggressive"))
        self.assertEqual(capture_from_legacy({})["mode"], "auto")

    def test_mirror_round_trips(self) -> None:
        for raw in ({"mode": "off"}, {"mode": "review", "aggressiveness": "eager"}, {"mode": "auto", "aggressiveness": "conservative"}):
            policy = normalize_capture_settings(raw)
            back = capture_from_legacy(legacy_mirror(policy))
            self.assertEqual(back["mode"], policy["mode"])
            self.assertEqual(back["chat_memory_mode"], policy["chat_memory_mode"])

    def test_conservative_keeps_only_user_facts(self) -> None:
        facts = [
            {"text": "a", "source": "user_statement"},
            {"text": "b", "source": "assistant_extract"},
            {"text": "c", "source": "turn_compact"},
        ]
        self.assertEqual(filter_facts_for_policy(facts, {"aggressiveness": "conservative"}), facts[:1])
        self.assertEqual(filter_facts_for_policy(facts, {"aggressiveness": "normal"}), facts)


class CapturePolicyServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = ArchitectOSService(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _finalize(self, message: str) -> dict[str, Any]:
        result = self.service.post_chat_message({"project_id": "architectos", "message": message, "provider_id": "local-memory"})
        return self.service.finalize_chat_session(result["chat"]["id"], force=True, trigger="manual")

    def _atoms(self, status: str | None) -> list[dict[str, Any]]:
        items = self.service.list_memory_candidates("architectos", status=status)["candidates"]
        return [item for item in items if (item.get("metadata") or {}).get("template") == "chat_session_atom"]

    def test_fresh_install_defaults_to_auto_and_exposes_settings(self) -> None:
        settings = self.service.settings()
        capture = settings["memory_capture"]
        self.assertEqual(capture["mode"], "auto")
        self.assertIn("stats_7d", capture)
        self.assertTrue(settings["memory_lifecycle"]["chat_auto_accept"])

    def test_legacy_install_without_policy_is_derived(self) -> None:
        self.service.repository.set_setting("memory_lifecycle", {
            **self.service.repository.get_setting("memory_lifecycle"), "chat_memory_mode": "aggressive", "chat_auto_accept": False,
        })
        self.service.repository.set_setting("memory_capture", {})
        policy = self.service.memory_capture_settings()
        self.assertEqual((policy["mode"], policy["aggressiveness"]), ("review", "eager"))

    def test_policy_patch_mirrors_into_legacy_keys(self) -> None:
        self.service.update_settings({"memory_capture": {"mode": "review", "aggressiveness": "eager"}})
        life = self.service.repository.get_setting("memory_lifecycle")
        self.assertEqual((life["chat_memory_mode"], life["chat_auto_accept"]), ("aggressive", False))
        self.service.update_settings({"memory_capture": {"mode": "off"}})
        self.assertEqual(self.service.repository.get_setting("memory_lifecycle")["chat_memory_mode"], "off")

    def test_legacy_patch_rederives_policy(self) -> None:
        self.service.update_settings({"memory_lifecycle": {"chat_auto_accept": False}})
        self.assertEqual(self.service.memory_capture_settings()["mode"], "review")
        self.service.update_settings({"memory_lifecycle": {"chat_memory_mode": "off"}})
        self.assertEqual(self.service.memory_capture_settings()["mode"], "off")

    def test_auto_mode_writes_decisions_and_constraints_at_once(self) -> None:
        finalized = self._finalize(f"{DECISION}\n{CONSTRAINT}")
        atoms = finalized["atoms"]
        self.assertGreaterEqual(len(atoms), 2)
        self.assertTrue(all(item["status"] == "promoted" for item in atoms))
        self.assertEqual(self._atoms("candidate"), [])
        types = {item["type"] for item in atoms}
        self.assertTrue(types & {"Decision", "Constraint"})
        for item in atoms:
            node = self.service.repository.get_node(item["promoted_node_id"])
            assert node is not None
            self.assertEqual(node.status, "active")
            self.assertTrue(item["metadata"].get("auto_accepted"))

    def test_review_mode_queues_everything(self) -> None:
        self.service.update_settings({"memory_capture": {"mode": "review"}})
        finalized = self._finalize(f"{DECISION}\n{CONSTRAINT}")
        self.assertTrue(all(item["status"] == "candidate" for item in finalized["atoms"]))
        self.assertGreaterEqual(len(self._atoms("candidate")), 2)

    def test_off_mode_captures_nothing_but_explicit_saves(self) -> None:
        self.service.update_settings({"memory_capture": {"mode": "off"}})
        finalized = self._finalize(DECISION)
        self.assertEqual(finalized.get("atoms") or [], [])
        self.assertEqual(self.service.list_memory_candidates("architectos", status=None)["candidates"], [])
        turn = self.service.capture_memory_turn(DECISION, project_id="architectos")
        self.assertFalse(turn["kept"])
        self.assertEqual(turn["capture_mode"], "off")
        explicit = self.service.add_memory({"label": "Branching", "text": DECISION, "type": "Decision"})
        self.assertTrue(explicit.get("id"))

    def test_min_confidence_holds_low_confidence_facts_for_review(self) -> None:
        self.service.update_settings({"memory_capture": {"mode": "auto", "min_confidence": 0.9}})
        finalized = self._finalize(DECISION)
        self.assertTrue(finalized["atoms"])
        self.assertTrue(all(item["status"] == "candidate" for item in finalized["atoms"]))

    def test_conservative_drops_assistant_mined_facts(self) -> None:
        # A short, non-durable user question; the facts come from the assistant reply only.
        user = "what did we agree on for the cache?"
        assistant = "Decision: we will use Redis for session cache.\n- Constraint: never store secrets in the database."
        self.service.update_settings({"memory_capture": {"mode": "review", "aggressiveness": "conservative"}})
        turn = self.service.capture_memory_turn(user, assistant, project_id="architectos")
        self.assertTrue(turn["kept"])
        self.assertEqual(turn["atoms"], [])
        self.service.update_settings({"memory_capture": {"aggressiveness": "normal"}})
        turn = self.service.capture_memory_turn(user, assistant, project_id="architectos")
        self.assertTrue(turn["atoms"])

    def test_undo_archives_node_and_rejects_candidate(self) -> None:
        turn = self.service.capture_memory_turn(DECISION, project_id="architectos")
        saved = turn["auto_accepted"][0]
        undone = self.service.undo_auto_capture({"candidate_id": saved["id"]})
        self.assertEqual(undone["candidate"]["status"], "rejected")
        node = self.service.repository.get_node(saved["promoted_node_id"])
        assert node is not None
        self.assertEqual(node.status, "archived")
        self.assertEqual(node.metadata.get("archived_reason"), "auto_capture_undo")
        with self.assertRaises(ValueError):
            self.service.undo_auto_capture({"candidate_id": saved["id"]})
        with self.assertRaises(ValueError):
            self.service.undo_auto_capture({"candidate_id": "missing"})

    def test_undo_refuses_manually_promoted_candidate(self) -> None:
        self.service.update_settings({"memory_capture": {"mode": "review"}})
        turn = self.service.capture_memory_turn(DECISION, project_id="architectos")
        queued = turn["queued"][0]
        self.service.promote_memory_candidate(queued["id"])
        with self.assertRaises(ValueError):
            self.service.undo_auto_capture({"candidate_id": queued["id"]})

    def test_stats_count_recent_policy_outcomes(self) -> None:
        turn = self.service.capture_memory_turn(DECISION, project_id="architectos")
        self.service.undo_auto_capture({"candidate_id": turn["auto_accepted"][0]["id"]})
        self.service.update_settings({"memory_capture": {"mode": "review"}})
        self.service.capture_memory_turn(CONSTRAINT, project_id="architectos")
        stats = self.service.memory_capture_stats(days=7)
        self.assertGreaterEqual(stats["auto_saved"], 1)
        self.assertGreaterEqual(stats["undone"], 1)
        self.assertGreaterEqual(stats["queued"], 1)
        self.assertEqual(stats["days"], 7)


if __name__ == "__main__":
    unittest.main()
