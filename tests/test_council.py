from __future__ import annotations

import unittest
from typing import Any

from backend.architectos.council import CouncilOrchestrator


class CouncilOrchestratorTests(unittest.TestCase):
    def _panel(self) -> list[dict[str, Any]]:
        return [
            {"id": "auto", "label": "Auto (router)", "ready": True},
            {"id": "openai", "label": "OpenAI", "ready": True},
            {"id": "anthropic", "label": "Anthropic", "ready": True},
            {"id": "azure-openai", "label": "Azure OpenAI", "ready": False},
        ]

    def _fake_run_ai(self):
        calls: list[dict[str, Any]] = []

        def run_ai(payload: dict[str, Any]) -> dict[str, Any]:
            calls.append(payload)
            provider_id = payload.get("provider_id")
            return {
                "text": f"[{payload.get('role')}] answer from {provider_id}",
                "provider": {"id": provider_id, "status": "ok"},
                "run_id": f"run-{len(calls)}",
            }

        return run_ai, calls

    def test_runs_each_model_and_judges(self) -> None:
        run_ai, calls = self._fake_run_ai()
        council = CouncilOrchestrator(run_ai, self._panel)
        result = council.run({
            "project_id": "architectos",
            "message": "improve the parser",
            "models": ["openai", "anthropic"],
        })
        self.assertEqual(len(result["answers"]), 2)
        self.assertEqual({a["provider_id"] for a in result["answers"]}, {"openai", "anthropic"})
        # 2 member calls + 1 judge call
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["role"], "council")
        self.assertEqual(calls[-1]["role"], "review")
        self.assertTrue(result["synthesis"])

    def test_single_model_skips_judge_and_uses_answer(self) -> None:
        run_ai, calls = self._fake_run_ai()
        council = CouncilOrchestrator(run_ai, self._panel)
        result = council.run({
            "project_id": "architectos",
            "message": "audit",
            "models": ["openai"],
        })
        self.assertEqual(len(result["answers"]), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["synthesis"], result["answers"][0]["text"])

    def test_default_panel_uses_ready_models_only(self) -> None:
        run_ai, calls = self._fake_run_ai()
        council = CouncilOrchestrator(run_ai, self._panel)
        self.assertEqual(council.default_models(), ["openai", "anthropic"])
        result = council.run({"project_id": "architectos", "message": "help"})
        self.assertEqual(result["models"], ["openai", "anthropic"])

    def test_dedupes_and_caps_panel(self) -> None:
        run_ai, _calls = self._fake_run_ai()
        council = CouncilOrchestrator(run_ai, self._panel)
        result = council.run({
            "project_id": "architectos",
            "message": "x",
            "models": ["openai", "openai", "anthropic"],
        })
        self.assertEqual(result["models"], ["openai", "anthropic"])

    def test_empty_message_raises(self) -> None:
        run_ai, _calls = self._fake_run_ai()
        council = CouncilOrchestrator(run_ai, self._panel)
        with self.assertRaises(ValueError):
            council.run({"project_id": "architectos", "message": "  "})


if __name__ == "__main__":
    unittest.main()
