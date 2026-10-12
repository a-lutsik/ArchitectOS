"""Stage 2: the dedup gate reads an index instead of scanning every node."""
from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path
from typing import Any

from backend.architectos.memory_dedup import (
    MemoryDedupGate,
    fact_features,
    incoming_features,
)
from backend.architectos.service import ArchitectOSService
from backend.architectos.storage_dedup import dedup_index_eligible

_LABELS = ["Лимит API", "Кэш", "Деплой", "Порт", "Cache", "Queue"]
_WORDS = (
    "сервис база кэш лимит запрос порт очередь деплой релиз узел агент "
    "service cache limit request port queue deploy release node agent"
).split()


def _random_fact(rng: random.Random) -> tuple[str, str, str]:
    words = " ".join(rng.choice(_WORDS) for _ in range(rng.randint(3, 7)))
    negation = "не " if rng.random() < 0.2 else ""
    number = f" {rng.choice([10, 20, 5000])}" if rng.random() < 0.5 else ""
    return rng.choice(_LABELS), f"{negation}{words}{number}", rng.choice(["Lesson", "Decision", "Concept"])


class DedupIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = ArchitectOSService(Path(self._tmp.name))
        self.repo = self.service.repository
        self.gate: MemoryDedupGate = self.service.memory_dedup

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _indexed_ids(self) -> set[str]:
        with self.repo._connect() as conn:
            return {row[0] for row in conn.execute("SELECT node_id FROM memory_dedup_index")}

    def _brute_force(self, label: str, text: str, node_type: str) -> Any:
        """The stage 1 scan: compare against every eligible node."""
        incoming = incoming_features(label, text)
        rank = {"duplicate": 3, "update": 2, "conflict": 1}
        best_action, best_rank, best_score, best_target, best_near = "new", 0, 0.0, "", 0.0
        for node in sorted(self.repo.list_nodes(), key=lambda item: item.id):
            if not dedup_index_eligible(node) or node.scope != "project" or node.project_id != "architectos":
                continue
            if not self.gate.type_matches(node.type, node_type):
                continue
            verdict = self.gate.compare(incoming, fact_features(node.label, node.text))
            best_near = max(best_near, verdict.near_score)
            if verdict.action == "duplicate" and verdict.reason == "exact restatement":
                return "duplicate", node.id, 1.0
            r = rank.get(verdict.action, 0)
            if r and (r > best_rank or (r == best_rank and verdict.score > best_score)):
                best_action, best_rank, best_score, best_target = verdict.action, r, verdict.score, node.id
        return best_action, best_target, best_near

    def test_index_matches_full_scan_on_random_corpus(self) -> None:
        rng = random.Random(20261012)
        for i in range(250):
            label, text, node_type = _random_fact(rng)
            # Distinct ids even under equal labels; same as the gate's salting.
            self.repo.add_node(node_type, label, "project", text, "architectos", id_salt=str(i))
        for _ in range(120):
            label, text, node_type = _random_fact(rng)
            decision = self.gate.classify(label, text, node_type, "project", "architectos")
            action, target, near = self._brute_force(label, text, node_type)
            self.assertEqual(decision.action, action, (label, text))
            if action != "new":
                self.assertEqual(decision.target_id, target, (label, text))
            elif near >= self.gate.NEAR_DUPLICATE_SIMILARITY:
                self.assertAlmostEqual(decision.near_score, near, places=9, msg=(label, text))
            else:
                # Below the flag threshold the index may skip weak neighbours.
                self.assertLess(decision.near_score, self.gate.NEAR_DUPLICATE_SIMILARITY, (label, text))

    def test_near_min_shared_never_drops_a_flagged_neighbour(self) -> None:
        rng = random.Random(7)
        universe = [f"w{i}" for i in range(14)]
        for _ in range(4000):
            left = frozenset(rng.sample(universe, rng.randint(1, 10)))
            right = frozenset(rng.sample(universe, rng.randint(1, 10)))
            a = fact_features("x", "x")
            score = self.gate.near_score(
                type(a)(a.key, a.label_core, left, a.numbers, a.negated, a.identifiers),
                type(a)(a.key, a.label_core, right, a.numbers, a.negated, a.identifiers),
            )
            if score >= self.gate.NEAR_DUPLICATE_SIMILARITY:
                self.assertGreaterEqual(len(left & right), self.gate.near_min_shared(len(left)))

    def test_rows_follow_node_lifecycle(self) -> None:
        node = self.service.add_memory({"label": "Порт", "text": "Сервер слушает порт 8765", "type": "Decision"})
        self.assertIn(node["id"], self._indexed_ids())
        stored = self.repo.get_node(node["id"])
        assert stored is not None
        stored.status = "archived"
        self.repo.upsert_node(stored)
        self.assertNotIn(node["id"], self._indexed_ids())
        stored.status = "active"
        self.repo.upsert_node(stored)
        self.assertIn(node["id"], self._indexed_ids())

    def test_superseded_fact_leaves_the_index_until_undo(self) -> None:
        old = self.service.add_memory({"label": "Лимит API", "text": "Лимит запросов 5000 в час", "type": "Constraint"})
        new = self.service.add_memory({"label": "Лимит API", "text": "Лимит запросов 9000 в час", "type": "Constraint"})
        self.assertEqual(new["dedup"]["action"], "update")
        self.assertNotIn(old["id"], self._indexed_ids())
        self.service.undo_memory_dedup({"event_id": new["dedup"]["event_id"]})
        self.assertIn(old["id"], self._indexed_ids())

    def test_near_duplicate_under_another_label_is_flagged(self) -> None:
        base = self.service.add_memory({
            "label": "Хранилище", "text": "Данные проекта лежат в локальной базе SQLite", "type": "Decision",
        })
        # Same text filed under a different subject: not folded, only flagged.
        other = self.service.add_memory({
            "label": "Хранилище данных", "text": "Данные проекта лежат в локальной базе SQLite", "type": "Decision",
        })
        self.assertEqual(other["dedup"]["action"], "new")
        self.assertEqual(other["metadata"].get("possible_duplicate_of"), base["id"])

    def test_startup_rebuilds_missing_or_outdated_index(self) -> None:
        node = self.service.add_memory({"label": "Кэш", "text": "Используем Redis для кэша сессий", "type": "Decision"})
        with self.repo._connect() as conn:
            conn.execute("DELETE FROM memory_dedup_stems")
            conn.execute("DELETE FROM memory_dedup_index")
        self.repo._migrate()
        self.assertIn(node["id"], self._indexed_ids())
        with self.repo._connect() as conn:
            conn.execute("UPDATE memory_dedup_index SET version = 0, features = '{}'")
        self.repo._migrate()
        again = self.service.add_memory({"label": "Кэш", "text": "Для кэша сессий используем Redis", "type": "Decision"})
        self.assertTrue(again.get("dedup_merged"))

    def test_manual_reindex_reports_indexed_nodes(self) -> None:
        node = self.service.add_memory({"label": "Порт", "text": "Сервер слушает порт 8765", "type": "Decision"})
        result = self.service.rebuild_memory_dedup_index()
        self.assertGreaterEqual(result["indexed"], 1)
        self.assertIn(node["id"], self._indexed_ids())

    def test_wipe_clears_index_rows(self) -> None:
        project = self.service.create_project({"name": "Wipe dedup", "description": "dedup index wipe"})
        node = self.service.add_memory({
            "project_id": project["id"], "label": "Порт", "type": "Decision", "scope": "project",
            "text": "Сервер слушает порт 8765",
        })
        self.assertIn(node["id"], self._indexed_ids())
        self.service.wipe_project_memory(project["id"], confirm=True)
        self.assertNotIn(node["id"], self._indexed_ids())
        with self.repo._connect() as conn:
            stems = conn.execute("SELECT count(*) FROM memory_dedup_stems WHERE node_id = ?", (node["id"],)).fetchone()[0]
        self.assertEqual(stems, 0)


if __name__ == "__main__":
    unittest.main()
