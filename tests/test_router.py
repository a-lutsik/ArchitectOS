from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.architectos.adapters import ProviderRequest, ProviderRouter
from backend.architectos.routing import RouterPolicy, classify_role


def _providers() -> list[dict]:
    return [
        {"id": "openai", "label": "OpenAI", "enabled": True, "status": "configured"},
        {"id": "anthropic", "label": "Anthropic", "enabled": True, "status": "configured"},
        {"id": "ollama", "label": "Ollama", "enabled": True, "status": "configured"},
        {"id": "openrouter", "label": "OpenRouter", "enabled": False, "status": "planned"},
        {"id": "local-memory", "label": "Local Memory", "enabled": True, "status": "fallback"},
    ]


class RouterPolicyTests(unittest.TestCase):
    def test_quality_strategy_prefers_highest_quality_provider(self) -> None:
        policy = RouterPolicy({"strategy": "quality"})
        plan = policy.select(_providers(), "auto", role=None)
        self.assertEqual(plan["decision"]["mode"], "auto")
        self.assertEqual(plan["provider"]["id"], "anthropic")

    def test_cost_strategy_prefers_cheaper_local_provider(self) -> None:
        policy = RouterPolicy({"strategy": "cost"})
        plan = policy.select(_providers(), "auto", role=None)
        self.assertIn(plan["provider"]["id"], {"ollama", "local-memory"})

    def test_explicit_provider_is_respected(self) -> None:
        policy = RouterPolicy({"strategy": "balanced"})
        plan = policy.select(_providers(), "openrouter", role=None)
        self.assertEqual(plan["decision"]["mode"], "explicit")
        self.assertEqual(plan["provider"]["id"], "openrouter")

    def test_disabled_provider_is_not_auto_selected(self) -> None:
        policy = RouterPolicy({"strategy": "balanced"})
        ranked = policy.rank(_providers())
        openrouter = next(item for item in ranked if item["provider_id"] == "openrouter")
        self.assertEqual(openrouter["availability"], 0.0)

    def test_fallback_to_local_memory_when_nothing_enabled(self) -> None:
        policy = RouterPolicy({})
        disabled = [{"id": "openai", "label": "OpenAI", "enabled": False, "status": "planned"}]
        plan = policy.select(disabled, "auto", role=None)
        self.assertEqual(plan["decision"]["mode"], "fallback")
        self.assertEqual(plan["provider"]["id"], "local-memory")

    def test_balanced_ready_azure_beats_local_memory_for_code(self) -> None:
        policy = RouterPolicy({"strategy": "balanced"})
        providers = [
            {"id": "azure-openai", "label": "Azure OpenAI", "enabled": True, "status": "configured", "last_check": {"ready": True, "status": "ok"}},
            {"id": "local-memory", "label": "Local Memory", "enabled": True, "status": "fallback"},
        ]
        plan = policy.select(providers, "auto", role="code")
        self.assertEqual(plan["decision"]["mode"], "auto")
        self.assertEqual(plan["provider"]["id"], "azure-openai")

    def test_missing_credentials_last_check_is_not_ready(self) -> None:
        from backend.architectos.routing import provider_is_ready

        provider = {
            "id": "anthropic",
            "label": "Anthropic",
            "enabled": True,
            "status": "configured",
            "last_check": {"ready": False, "status": "missing_credentials", "message": "ANTHROPIC_API_KEY is not set."},
        }
        self.assertFalse(provider_is_ready(provider))
        policy = RouterPolicy({"strategy": "balanced"})
        plan = policy.select(
            [provider, {"id": "local-memory", "label": "Local Memory", "enabled": True, "status": "fallback"}],
            "auto",
            role="code",
        )
        self.assertEqual(plan["provider"]["id"], "local-memory")

    def test_role_classification(self) -> None:
        self.assertEqual(classify_role("please implement this function and add a test"), "code")
        self.assertEqual(classify_role("review this for security risks"), "review")
        self.assertEqual(classify_role("evaluate the architecture trade-offs"), "architecture")
        self.assertEqual(classify_role("write documentation and a readme guide"), "docs")


class ProviderRouterRoutingTests(unittest.TestCase):
    def test_route_attaches_routing_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = ProviderRouter(Path(tmp), {"strategy": "quality"})
            request = ProviderRequest(message="design the architecture", context="", project_id="architectos")
            result = router.route(_providers(), request, "auto")
            self.assertIn("routing", result)
            self.assertIn(result["routing"]["mode"], {"auto", "fallback"})
            self.assertEqual(result["routing"]["role"], "architecture")

    def test_plan_ranks_providers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = ProviderRouter(Path(tmp))
            plan = router.plan(_providers(), "auto", "code")
            self.assertTrue(plan["decision"]["ranked"])


if __name__ == "__main__":
    unittest.main()
