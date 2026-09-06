from __future__ import annotations

import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from backend.architectos.adapters import ProviderAdapter, ProviderRequest, ProviderRouter


class FakeProviderAdapter(ProviderAdapter):
    provider_id = "fake-test"

    def run(self, provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "status": "ok",
            "text": f"fake:{request.project_id}:{request.message}:{project_root.name}",
            "raw": {"approved": request.approved, "model": provider.get("model") or "fake-model"},
        }

    def stream(self, provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> Iterator[dict[str, Any]]:
        yield {"type": "delta", "text": "fake "}
        yield {"type": "delta", "text": request.message}
        yield {"type": "done", "result": self.run(provider, request, project_root)}

    def check(self, provider: dict[str, Any], project_root: Path) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "ready": True,
            "status": "ok",
            "message": f"{provider['label']} is ready in {project_root.name}.",
            "hint": "Fake adapter is deterministic.",
            "actions": ["Use in tests"],
            "details": {"model": provider.get("model")},
        }

    def list_models(self, provider: dict[str, Any], project_root: Path) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "ready": True,
            "status": "ok",
            "models": [{"name": "fake-small"}, {"name": "fake-large"}],
            "message": "Fake models ready.",
        }


class ProviderRouterFakeAdapterTests(unittest.TestCase):
    def test_router_uses_fake_adapter_for_run_stream_check_and_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            router = ProviderRouter(root)
            router.adapters["fake-test"] = FakeProviderAdapter()
            providers = [{"id": "fake-test", "label": "Fake Test", "provider_type": "test", "enabled": True, "model": "fake-large"}]
            request = ProviderRequest(message="hello", context="ctx", project_id="architectos", approved=True)

            result = router.route(providers, request, "fake-test")
            self.assertEqual(result["provider_id"], "fake-test")
            self.assertEqual(result["requested_provider_id"], "fake-test")
            self.assertEqual(result["selected_provider"]["label"], "Fake Test")
            self.assertIn("fake:architectos:hello", result["text"])

            events = list(router.stream(providers, request, "fake-test"))
            self.assertEqual(events[0]["type"], "start")
            self.assertEqual("".join(event.get("text", "") for event in events if event["type"] == "delta"), "fake hello")
            self.assertEqual(events[-1]["result"]["status"], "ok")

            check = router.check(providers, "fake-test")
            self.assertTrue(check["ready"])
            self.assertEqual(check["status"], "ok")

            models = router.list_models(providers, "fake-test")
            self.assertEqual([model["name"] for model in models["models"]], ["fake-small", "fake-large"])

    def test_router_falls_back_to_local_memory_when_no_provider_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            router = ProviderRouter(Path(tmp))
            request = ProviderRequest(message="memory", context="- [Lesson] Test: local fallback", project_id="architectos")
            result = router.route([], request, "auto")
            self.assertEqual(result["provider_id"], "local-memory")
            self.assertEqual(result["selected_provider"]["id"], "local-memory")
            self.assertIn("Local Memory", result["text"])

    def test_router_falls_back_when_auto_selected_provider_fails(self) -> None:
        class FailingProviderAdapter(ProviderAdapter):
            provider_id = "anthropic"

            def run(self, provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> dict[str, Any]:
                return {"provider_id": self.provider_id, "status": "error", "text": "forced failure", "raw": None}

            def stream(self, provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> Iterator[dict[str, Any]]:
                yield {"type": "done", "result": self.run(provider, request, project_root)}

        with tempfile.TemporaryDirectory() as tmp:
            router = ProviderRouter(Path(tmp), {"strategy": "quality"})
            router.adapters["anthropic"] = FailingProviderAdapter()
            providers = [
                {"id": "anthropic", "label": "Anthropic", "enabled": True, "status": "configured"},
                {"id": "local-memory", "label": "Local Memory", "enabled": True, "status": "fallback"},
            ]
            request = ProviderRequest(message="hello", context="- [Lesson] Test: fallback", project_id="architectos")
            result = router.route(providers, request, "auto")
            self.assertEqual(result["provider_id"], "anthropic")
            self.assertEqual(result["status"], "error")
            self.assertIn("forced failure", result["text"])

            events = list(router.stream(providers, request, "auto"))
            self.assertEqual(events[0]["type"], "start")
            self.assertEqual(events[-1]["result"]["provider_id"], "anthropic")
            self.assertEqual(events[-1]["result"]["status"], "error")


if __name__ == "__main__":
    unittest.main()
