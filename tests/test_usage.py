from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.architectos.adapters import _result_with_usage
from backend.architectos.service import ArchitectOSService
from backend.architectos.usage import estimate_cost_usd, normalize_usage, usage_from_result


class UsageNormalizeTests(unittest.TestCase):
    def test_openai_responses_usage(self) -> None:
        usage = normalize_usage({"usage": {"input_tokens": 120, "output_tokens": 40, "total_tokens": 160}})
        self.assertEqual(usage["prompt_tokens"], 120)
        self.assertEqual(usage["completion_tokens"], 40)
        self.assertEqual(usage["total_tokens"], 160)

    def test_openai_stream_completed_event(self) -> None:
        usage = normalize_usage({
            "type": "response.completed",
            "response": {"usage": {"input_tokens": 10, "output_tokens": 5}},
        })
        self.assertEqual(usage["prompt_tokens"], 10)
        self.assertEqual(usage["completion_tokens"], 5)
        self.assertEqual(usage["total_tokens"], 15)

    def test_anthropic_usage(self) -> None:
        usage = normalize_usage({"usage": {"input_tokens": 80, "output_tokens": 20}})
        self.assertEqual(usage["prompt_tokens"], 80)
        self.assertEqual(usage["completion_tokens"], 20)

    def test_openrouter_usage_with_cost(self) -> None:
        usage = normalize_usage({"usage": {"prompt_tokens": 50, "completion_tokens": 25, "cost": 0.00123}})
        self.assertEqual(usage["prompt_tokens"], 50)
        self.assertEqual(usage["completion_tokens"], 25)
        self.assertAlmostEqual(usage["cost_usd"], 0.00123)

    def test_ollama_usage(self) -> None:
        usage = normalize_usage({"prompt_eval_count": 33, "eval_count": 11, "done": True})
        self.assertEqual(usage["prompt_tokens"], 33)
        self.assertEqual(usage["completion_tokens"], 11)
        self.assertEqual(usage["total_tokens"], 44)

    def test_estimate_cost_from_table(self) -> None:
        cost = estimate_cost_usd("gpt-4.1-mini", 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 0.40 + 1.60)

    def test_result_with_usage_enriches_cost(self) -> None:
        result = _result_with_usage(
            {"provider_id": "openai", "status": "ok", "text": "hi", "raw": {"usage": {"input_tokens": 1000, "output_tokens": 500}}},
            model="gpt-4.1-mini",
        )
        self.assertIn("usage", result)
        self.assertEqual(result["usage"]["prompt_tokens"], 1000)
        self.assertEqual(result["usage"]["completion_tokens"], 500)
        self.assertGreater(result["usage"]["cost_usd"], 0)
        self.assertTrue(result["usage"]["cost_estimated"])

    def test_stream_done_result_keeps_usage(self) -> None:
        # Mimic what adapters put on stream done.result.
        done = {
            "type": "done",
            "result": _result_with_usage(
                {"provider_id": "openrouter", "status": "ok", "text": "answer", "raw": None},
                model="openai/gpt-4o-mini",
                usage_payload={"usage": {"prompt_tokens": 40, "completion_tokens": 12, "cost": 0.0004}},
            ),
        }
        usage = done["result"]["usage"]
        self.assertEqual(usage["prompt_tokens"], 40)
        self.assertEqual(usage["completion_tokens"], 12)
        self.assertAlmostEqual(usage["cost_usd"], 0.0004)
        self.assertFalse(usage.get("cost_estimated"))


class ProviderUsageAnalyticsTests(unittest.TestCase):
    def test_provider_run_stats_aggregates_usage(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            service = ArchitectOSService(Path(tmp))
            service.repository.set_setting("memory_retrieval", {"embeddings_enabled": False, "reindex_on_startup": False})
            service.repository.upsert_provider_run({
                "id": "run_a",
                "project_id": "architectos",
                "provider_id": "openai",
                "provider_label": "OpenAI",
                "status": "ok",
                "model": "gpt-4.1-mini",
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "cost_usd": 0.0002},
                "selected_provider": {"id": "openai", "model": "gpt-4.1-mini"},
            })
            service.repository.upsert_provider_run({
                "id": "run_b",
                "project_id": "architectos",
                "provider_id": "openai",
                "provider_label": "OpenAI",
                "status": "ok",
                "model": "gpt-4.1-mini",
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30, "cost_usd": 0.0001},
                "selected_provider": {"id": "openai", "model": "gpt-4.1-mini"},
            })
            analytics = service.analytics()
            usage = analytics["provider_usage"]
            self.assertEqual(usage["totals"]["prompt_tokens"], 120)
            self.assertEqual(usage["totals"]["completion_tokens"], 60)
            self.assertEqual(usage["totals"]["total_tokens"], 180)
            self.assertEqual(usage["totals"]["runs_with_usage"], 2)
            self.assertAlmostEqual(usage["totals"]["cost_usd"], 0.0003)
            self.assertEqual(len(usage["by_model"]), 1)
            self.assertEqual(usage["by_model"][0]["model"], "gpt-4.1-mini")

    def test_route_response_includes_usage(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            service = ArchitectOSService(Path(tmp))
            service.repository.set_setting("memory_retrieval", {"embeddings_enabled": False, "reindex_on_startup": False})
            routed = service._route_response(  # noqa: SLF001
                "architectos",
                "hello",
                "ctx",
                {
                    "provider_id": "openai",
                    "status": "ok",
                    "text": "world",
                    "selected_provider": {"id": "openai", "model": "gpt-4.1-mini", "label": "OpenAI"},
                    "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
                },
            )
            self.assertEqual(routed["usage"]["prompt_tokens"], 12)
            self.assertEqual(routed["usage"]["completion_tokens"], 3)
            self.assertIn("cost_usd", routed["usage"])

    def test_usage_from_result_reads_raw(self) -> None:
        usage = usage_from_result(
            {
                "selected_provider": {"model": "gpt-4o-mini"},
                "raw": {"usage": {"prompt_tokens": 5, "completion_tokens": 7}},
            }
        )
        self.assertEqual(usage["prompt_tokens"], 5)
        self.assertEqual(usage["completion_tokens"], 7)


if __name__ == "__main__":
    unittest.main()
