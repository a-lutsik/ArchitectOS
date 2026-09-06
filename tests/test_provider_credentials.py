from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from backend.architectos.service import ArchitectOSService


def _pop_env(*names: str) -> dict[str, str | None]:
    saved = {name: os.environ.pop(name, None) for name in names}
    return saved


def _restore_env(saved: dict[str, str | None]) -> None:
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


class ProviderCredentialsTests(unittest.TestCase):
    def test_save_provider_credentials_writes_env_local_not_sqlite(self) -> None:
        saved = _pop_env("OPENAI_API_KEY", "OPENAI_BASE_URL")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                service = ArchitectOSService(Path(tmp))
                result = service.save_provider_credentials("openai", {
                    "api_key": "sk-from-ui",
                    "base_url": "https://api.openai.com/v1",
                    "test": False,
                })
                self.assertTrue(result["credentials_set"])
                self.assertNotIn("sk-from-ui", json.dumps(result))
                env_local = (Path(tmp) / ".env.local").read_text(encoding="utf-8")
                self.assertIn("OPENAI_API_KEY=sk-from-ui", env_local)
                self.assertIn("OPENAI_BASE_URL=https://api.openai.com/v1", env_local)
                self.assertEqual(os.environ.get("OPENAI_API_KEY"), "sk-from-ui")
                stored = next(item for item in service.repository.list_providers() if item["id"] == "openai")
                self.assertNotIn("sk-from-ui", json.dumps(stored))
                self.assertTrue(stored["enabled"])
                listed = service.providers()["providers"]
                openai = next(item for item in listed if item["id"] == "openai")
                self.assertTrue(openai["credentials_set"])
                self.assertTrue(openai["credentials"]["api_key_set"])
                self.assertEqual(openai["credentials"]["base_url"], "https://api.openai.com/v1")
        finally:
            _restore_env(saved)

    def test_save_azure_credentials_sets_endpoint_and_deployment(self) -> None:
        saved = _pop_env("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                service = ArchitectOSService(Path(tmp))
                result = service.save_provider_credentials("azure-openai", {
                    "api_key": "azure-from-ui",
                    "endpoint": "https://example.cognitiveservices.azure.com/openai/v1",
                    "deployment": "gpt-4.1",
                    "test": False,
                })
                self.assertTrue(result["credentials_set"])
                provider = result["provider"]
                self.assertEqual(provider["base_url"], "https://example.cognitiveservices.azure.com/openai/v1")
                self.assertEqual(provider["model"], "gpt-4.1")
                env_local = (Path(tmp) / ".env.local").read_text(encoding="utf-8")
                self.assertIn("AZURE_OPENAI_API_KEY=azure-from-ui", env_local)
                self.assertIn("AZURE_OPENAI_ENDPOINT=https://example.cognitiveservices.azure.com/openai/v1", env_local)
                self.assertIn("AZURE_OPENAI_DEPLOYMENT=gpt-4.1", env_local)
                self.assertNotIn("azure-from-ui", json.dumps(service.repository.list_providers()))
        finally:
            _restore_env(saved)

    def test_blank_api_key_keeps_existing_secret(self) -> None:
        saved = _pop_env("ANTHROPIC_API_KEY")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                service = ArchitectOSService(Path(tmp))
                service.save_provider_credentials("anthropic", {"api_key": "keep-me", "test": False})
                service.save_provider_credentials("anthropic", {"api_key": "  ", "test": False})
                self.assertEqual(os.environ.get("ANTHROPIC_API_KEY"), "keep-me")
                env_local = (Path(tmp) / ".env.local").read_text(encoding="utf-8")
                self.assertIn("ANTHROPIC_API_KEY=keep-me", env_local)
                self.assertEqual(env_local.count("ANTHROPIC_API_KEY="), 1)
        finally:
            _restore_env(saved)


class IntegrationCredentialsTests(unittest.TestCase):
    def test_save_azure_devops_without_persisting_secrets(self) -> None:
        saved = _pop_env("ADO_ORG", "ADO_MCP_AUTH_TOKEN")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                service = ArchitectOSService(Path(tmp))
                empty = service.integration_credentials()
                self.assertFalse(empty["azure_devops"]["ready"])
                self.assertNotIn("teams", empty)

                ado = service.save_integration_credentials({
                    "kind": "azure_devops",
                    "org": "Contoso",
                    "token": "pat-from-ui",
                })
                self.assertTrue(ado["azure_devops"]["ready"])
                self.assertEqual(ado["azure_devops"]["org"], "Contoso")
                self.assertTrue(ado["azure_devops"]["token_set"])
                self.assertNotIn("pat-from-ui", json.dumps(ado))

                env_local = (Path(tmp) / ".env.local").read_text(encoding="utf-8")
                self.assertIn("ADO_ORG=Contoso", env_local)
                self.assertIn("ADO_MCP_AUTH_TOKEN=pat-from-ui", env_local)
                settings = service.settings()
                self.assertTrue(settings["integrations"]["azure_devops"]["ready"])
                self.assertNotIn("pat-from-ui", json.dumps(settings))
        finally:
            _restore_env(saved)

    def test_unknown_integration_kind_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            with self.assertRaises(ValueError):
                service.save_integration_credentials({"kind": "teams"})


if __name__ == "__main__":
    unittest.main()
