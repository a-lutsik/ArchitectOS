from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from backend.architectos.adapters import AzureOpenAIResponsesAdapter, ProviderRequest
from backend.architectos.adapters_base import LOCAL_MEMORY_CANNOT_INSTALL, LocalMemoryAdapter
from backend.architectos.routing import RouterPolicy, provider_is_ready
from backend.architectos.service import ArchitectOSService


class LocalMemoryAskTests(unittest.TestCase):
    def test_install_npx_mcp_intent_is_honest(self) -> None:
        adapter = LocalMemoryAdapter()
        request = ProviderRequest(
            message="please execute command or just install Atlassian Confluence mcp and Jira mcp servers",
            context="",
            project_id="architectos",
        )
        result = adapter.run({}, request, Path("."))
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["text"], LOCAL_MEMORY_CANNOT_INSTALL)
        self.assertNotIn("no matching memory", result["text"])
        self.assertIn("SETUP → MCP", result["text"])
        self.assertIn("Run command", result["text"])

    def test_npx_confluence_command_is_honest(self) -> None:
        adapter = LocalMemoryAdapter()
        request = ProviderRequest(
            message="need to install mcp server - npx -y mcp-confluence",
            context="- [Lesson] Something: not a command",
            project_id="architectos",
        )
        result = adapter.run({}, request, Path("."))
        self.assertEqual(result["text"], LOCAL_MEMORY_CANNOT_INSTALL)
        self.assertIn("npx -y mcp-confluence", result["text"])


class ProviderReadyRuleTests(unittest.TestCase):
    def test_enabled_missing_credentials_is_not_ready(self) -> None:
        openai = {
            "id": "openai",
            "enabled": True,
            "status": "configured",
            "last_check": {"ready": False, "status": "missing_credentials", "message": "OPENAI_API_KEY is not set."},
        }
        anthropic = {
            "id": "anthropic",
            "enabled": True,
            "status": "configured",
            "last_check": {"ready": False, "status": "missing_credentials", "message": "ANTHROPIC_API_KEY is not set."},
        }
        azure = {
            "id": "azure-openai",
            "enabled": True,
            "status": "configured",
            "last_check": {"ready": True, "status": "ok"},
        }
        disabled = {"id": "ollama", "enabled": False, "status": "unreachable"}
        providers = [openai, anthropic, azure, disabled]
        ready = [item for item in providers if provider_is_ready(item)]
        self.assertEqual([item["id"] for item in ready], ["azure-openai"])
        self.assertEqual(len(ready), 1)
        self.assertEqual(len(providers), 4)


class AzureProbeTests(unittest.TestCase):
    def test_check_without_deployment_is_not_ready(self) -> None:
        os.environ["AZURE_OPENAI_API_KEY"] = "test-key"
        os.environ["AZURE_OPENAI_ENDPOINT"] = "https://example.invalid/openai/v1"
        previous_deployment = os.environ.pop("AZURE_OPENAI_DEPLOYMENT", None)
        try:
            result = AzureOpenAIResponsesAdapter().check(
                {"base_url": "https://example.invalid/openai/v1", "model": "", "allow_local": True},
                Path("."),
            )
            self.assertFalse(result["ready"])
            self.assertEqual(result["status"], "missing_model")
            self.assertIn("deployment name", result["message"].lower())
        finally:
            os.environ.pop("AZURE_OPENAI_API_KEY", None)
            os.environ.pop("AZURE_OPENAI_ENDPOINT", None)
            if previous_deployment is None:
                os.environ.pop("AZURE_OPENAI_DEPLOYMENT", None)
            else:
                os.environ["AZURE_OPENAI_DEPLOYMENT"] = previous_deployment

    def test_check_probe_404_is_not_ready(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = json.dumps({"error": {"code": "DeploymentNotFound", "message": "missing"}}).encode("utf-8")
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        os.environ["ARCHITECTOS_TEST_AZURE_KEY"] = "test-key"
        os.environ["ARCHITECTOS_ALLOW_LOCAL_URLS"] = "1"
        try:
            result = AzureOpenAIResponsesAdapter().check(
                {
                    "base_url": f"http://127.0.0.1:{server.server_port}/openai/v1",
                    "api_key_env": "ARCHITECTOS_TEST_AZURE_KEY",
                    "model": "gpt-4.1-mini",
                    "allow_local": True,
                },
                Path("."),
            )
            self.assertFalse(result["ready"])
            self.assertEqual(result["status"], "missing_model")
            self.assertIn("deployment was not found", result["message"])
        finally:
            os.environ.pop("ARCHITECTOS_TEST_AZURE_KEY", None)
            os.environ.pop("ARCHITECTOS_ALLOW_LOCAL_URLS", None)
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_ask_404_persists_last_check(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.dumps({"error": {"code": "DeploymentNotFound", "message": "missing"}}).encode("utf-8")
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        os.environ["ARCHITECTOS_TEST_AZURE_KEY"] = "test-key"
        os.environ["ARCHITECTOS_ALLOW_LOCAL_URLS"] = "1"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                service = ArchitectOSService(Path(tmp))
                service.update_provider("azure-openai", {
                    "base_url": f"http://127.0.0.1:{server.server_port}/openai/v1",
                    "api_key_env": "ARCHITECTOS_TEST_AZURE_KEY",
                    "model": "wrong-deployment",
                    "enabled": True,
                })
                service.run_ai({"project_id": "architectos", "message": "hello", "provider_id": "azure-openai"})
                stored = next(item for item in service.providers()["providers"] if item["id"] == "azure-openai")
                self.assertFalse(provider_is_ready(stored))
                self.assertEqual(stored["last_check"]["status"], "missing_model")
        finally:
            os.environ.pop("ARCHITECTOS_TEST_AZURE_KEY", None)
            os.environ.pop("ARCHITECTOS_ALLOW_LOCAL_URLS", None)
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


class RoutingAskTests(unittest.TestCase):
    def test_ready_azure_beats_local_memory_for_agent_install(self) -> None:
        policy = RouterPolicy({"strategy": "balanced"})
        plan = policy.select(
            [
                {"id": "azure-openai", "label": "Azure OpenAI", "enabled": True, "status": "configured", "last_check": {"ready": True, "status": "ok"}},
                {"id": "local-memory", "label": "Local Memory", "enabled": True, "status": "fallback"},
            ],
            "auto",
            role="code",
        )
        self.assertEqual(plan["provider"]["id"], "azure-openai")


if __name__ == "__main__":
    unittest.main()
