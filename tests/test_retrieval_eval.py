"""Retrieval quality + safety gate over the public eval fixture (evals/retrieval).

Runs the full offline eval once (hash embeddings, ~10 s) and checks it against
evals/retrieval/thresholds.json. Quality floors are a ratchet; safety checks
are strict zeros. Run the human-readable report with:

    python -m evals.retrieval_eval --show-misses
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.architectos.service import ArchitectOSService
from evals.retrieval_eval import CORPUS_PATH, EVAL_DIR, load_fixture, run_eval, score_ranking


class ScoreRankingTests(unittest.TestCase):
    def test_perfect_and_empty_rankings(self) -> None:
        relevant = {"a": 2, "b": 1}
        perfect = score_ranking(["a", "b", None], relevant)
        self.assertEqual(perfect["answer@1"], 1.0)
        self.assertEqual(perfect["recall@10"], 1.0)
        self.assertAlmostEqual(perfect["ndcg@10"], 1.0)
        empty = score_ranking([], relevant)
        self.assertEqual(empty["recall@10"], 0.0)
        self.assertEqual(empty["mrr"], 0.0)

    def test_mrr_uses_first_answer_not_supporting_node(self) -> None:
        scores = score_ranking(["b", None, "a"], {"a": 2, "b": 1})
        self.assertEqual(scores["answer@1"], 0.0)
        self.assertAlmostEqual(scores["mrr"], 1 / 3)
        self.assertLess(scores["ndcg@10"], 1.0)


class SupersededStableLayerTests(unittest.TestCase):
    """Regression: the always-on pack layer used to resurrect superseded decisions."""

    def test_superseded_decision_not_in_stable_or_rule_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            old = service.add_memory({"project_id": "p", "label": "Jobs run on one scheduler", "type": "Decision", "text": "Single scheduler node runs jobs.", "source": "adr"})
            new = service.add_memory({"project_id": "p", "label": "Jobs run on a work queue", "type": "Decision", "text": "Workers consume jobs from a queue.", "source": "adr"})
            service.create_graph_edge({"source": new["id"], "target": old["id"], "type": "SUPERSEDES"})
            stable_ids = {hit["node"]["id"] for hit in service.list_stable_memory(project_id="p", query="scheduler jobs")}
            rule_ids = {hit["node"]["id"] for hit in service.list_rule_layer_memory(project_id="p", query="scheduler jobs")}
            self.assertIn(new["id"], stable_ids)
            self.assertNotIn(old["id"], stable_ids)
            self.assertNotIn(old["id"], rule_ids)
            packed = service.context("scheduler jobs", project_id="p")["context"]
            self.assertNotIn(f"id={old['id']}", packed)


class RetrievalEvalGateTests(unittest.TestCase):
    report: dict

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = run_eval(provider="hash")
        cls.thresholds = json.loads((EVAL_DIR / "thresholds.json").read_text(encoding="utf-8"))

    def test_quality_floors(self) -> None:
        failures = []
        for mode, spec in self.thresholds["floors"].items():
            data = self.report["modes"][mode]
            for metric, floor in (spec.get("overall") or {}).items():
                actual = data["overall"][metric]
                if actual < floor:
                    failures.append(f"{mode}.overall.{metric} = {actual} < {floor}")
            for category, metrics in (spec.get("by_category") or {}).items():
                for metric, floor in metrics.items():
                    actual = data["by_category"].get(category, {}).get(metric, 0.0)
                    if actual < floor:
                        failures.append(f"{mode}.{category}.{metric} = {actual} < {floor}")
            if "context_recall" in spec:
                actual = data["context"]["context_recall"]
                if actual < spec["context_recall"]:
                    failures.append(f"{mode}.context_recall = {actual} < {spec['context_recall']}")
        self.assertFalse(failures, "retrieval quality regressed (python -m evals.retrieval_eval --show-misses):\n" + "\n".join(failures))

    def test_no_superseded_or_cross_project_results(self) -> None:
        violations = {mode: data["safety"]["forbidden_violations"] for mode, data in self.report["modes"].items() if data["safety"]["forbidden_violations"]}
        self.assertEqual(violations, {}, "superseded or other-project memory reached results/packs")

    def test_noise_queries_return_nothing(self) -> None:
        for mode, data in self.report["modes"].items():
            self.assertEqual(data["safety"]["noise_false_positive_rate"], 0.0, mode)

    def test_packs_stay_within_budget(self) -> None:
        self.assertEqual(self.report["modes"]["context"]["context"]["over_budget"], 0)

    def test_no_unexpected_secret_leaks(self) -> None:
        for mode, data in self.report["modes"].items():
            self.assertEqual(data["safety"]["unexpected_secret_leaks"], [], mode)

    def test_known_secret_gaps_are_still_gaps(self) -> None:
        """Ratchet: once the redactor learns a format, drop its known_gap flag in corpus.json."""
        known = {secret["node"] for secret in load_fixture(CORPUS_PATH)["secrets"] if secret.get("known_gap")}
        leaked = {leak["node"] for leak in self.report["modes"]["hybrid"]["safety"]["secret_storage_leaks"]}
        fixed = sorted(known - leaked)
        self.assertEqual(fixed, [], f"redaction now covers {fixed}: remove known_gap for them in evals/retrieval/corpus.json")


if __name__ == "__main__":
    unittest.main()
