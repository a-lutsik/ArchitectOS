from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from backend.architectos.server import ArchitectOSHandler
from backend.architectos.service import ArchitectOSService


class ArchitectOSE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.tempdir.name)
        (self.project_root / "README.md").write_text("# E2E Project\n\nDecision: keep tests local.", encoding="utf-8")
        (self.project_root / "app.py").write_text("def run():\n    return 'e2e'\n", encoding="utf-8")

        service = ArchitectOSService(self.project_root)
        self.service = service

        class TestHandler(ArchitectOSHandler):
            pass

        TestHandler.service = service
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tempdir.cleanup()

    def request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "X-ArchitectOS-Token": self.service.auth_token},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def request_text(self, path: str) -> str:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=10) as response:
            return response.read().decode("utf-8")

    def request_error(self, method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        """request_json variant that also returns (status, body) for non-2xx."""
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "X-ArchitectOS-Token": self.service.auth_token},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_validation_error_returns_400(self) -> None:
        # create_project raises ValueError when root_path is missing.
        status, body = self.request_error("POST", "/api/projects", {"name": "NoRoot"})
        self.assertEqual(status, 400)
        self.assertEqual(body["type"], "ValueError")
        self.assertIn("error", body)

    def test_internal_error_returns_500(self) -> None:
        with mock.patch.object(self.service, "create_project", side_effect=RuntimeError("boom")):
            status, body = self.request_error("POST", "/api/projects", {"name": "X", "root_path": "/nonexistent"})
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "Internal server error")
        self.assertEqual(body["type"], "InternalError")
        self.assertIn("error_id", body)
        self.assertNotIn("boom", json.dumps(body))

    def test_http_end_to_end_local_memory_security_and_bundle_flow(self) -> None:
        index = self.request_text("/")
        self.assertIn("ArchitectOS", index)

        health = self.request_json("GET", "/api/health")
        self.assertTrue(health["ok"])
        self.assertEqual(health["root"], str(self.project_root))

        memory = self.request_json("POST", "/api/memory", {
            "project_id": "architectos",
            "label": "Provider token rule",
            "type": "Constraint",
            "scope": "project",
            "text": "Never persist token=abc123456789xyz in durable memory.",
        })
        self.assertIn("[REDACTED]", memory["text"])

        query = urllib.parse.quote("provider token")
        search = self.request_json("GET", f"/api/memory/search?project_id=architectos&query={query}")
        self.assertGreaterEqual(len(search["hits"]), 1)

        files = self.request_json("GET", "/api/project/files?project_id=architectos&limit=10")
        self.assertTrue(any(item["path"] == "README.md" for item in files["files"]))
        languages = self.request_json("GET", "/api/code/languages?project_id=architectos")
        self.assertTrue(any(item["extension"] == ".py" for item in languages["languages"]))

        terminal = self.request_json("POST", "/api/terminal/run", {
            "project_id": "architectos",
            "command": "echo TerminalMarker",
            "timeout_seconds": 5,
        })
        self.assertEqual(terminal["status"], "ok")
        self.assertIn("TerminalMarker", terminal["stdout"])
        blocked = self.request_json("POST", "/api/terminal/run", {
            "project_id": "architectos",
            "command": "rm -rf .",
            "timeout_seconds": 5,
        })
        self.assertEqual(blocked["status"], "blocked")

        project_root = self.project_root / "external"
        project_root.mkdir()
        (project_root / "README.md").write_text("# External\n\nExternalProjectMemory marker.", encoding="utf-8")
        project = self.request_json("POST", "/api/projects", {"name": "External", "root_path": str(project_root)})
        scan = self.request_json("POST", "/api/project/scan", {"project_id": project["id"], "root_path": str(project_root), "limit": 5})
        self.assertEqual(scan["project_id"], project["id"])
        self.assertGreaterEqual(scan["count"], 1)
        external_query = urllib.parse.quote("ExternalProjectMemory")
        external_search = self.request_json("GET", f"/api/memory/search?project_id={project['id']}&query={external_query}")
        self.assertTrue(any(hit["node"]["project_id"] == project["id"] for hit in external_search["hits"]))

        encoded = "data:text/plain;base64,RmlsZU1lbW9yeUltcG9ydE1hcmtlciBmcm9tIHVwbG9hZGVkIGZpbGUu"
        upload = self.request_json("POST", "/api/files", {
            "project_id": project["id"],
            "name": "memory-note.txt",
            "content": encoded,
        })
        memory_files = self.request_json("POST", "/api/memory/files", {
            "project_id": project["id"],
            "file_ids": [upload["file"]["id"]],
            "type": "Doc",
            "scope": "project",
        })
        self.assertEqual(memory_files["count"], 1)
        file_query = urllib.parse.quote("FileMemoryImportMarker")
        file_search = self.request_json("GET", f"/api/memory/search?project_id={project['id']}&query={file_query}")
        self.assertTrue(any(hit["node"]["metadata"].get("source") == "memory_file_upload" for hit in file_search["hits"]))

        chat = self.request_json("POST", "/api/chat/message", {
            "project_id": "architectos",
            "provider_id": "local-memory",
            "message": "Use token=abc123456789xyz while summarizing memory",
            "remember": True,
        })
        serialized_chat = json.dumps(chat)
        self.assertNotIn("abc123456789xyz", serialized_chat)
        self.assertIn("[REDACTED]", serialized_chat)

        preview = self.request_json("POST", "/api/security/preview", {
            "text": "authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        })
        self.assertTrue(preview["redacted"])
        self.assertEqual(preview["text"], "[REDACTED]")

        bundle = self.request_json("GET", "/api/bundle/export?project_id=architectos")
        serialized_bundle = json.dumps(bundle)
        self.assertEqual(bundle["format"], "architectos.bundle")
        self.assertNotIn("abc123456789xyz", serialized_bundle)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", serialized_bundle)

    def test_project_memory_is_isolated_but_shared_memory_is_available(self) -> None:
        project_a_root = self.project_root / "ProjectA"
        project_b_root = self.project_root / "ProjectB"
        project_a_root.mkdir()
        project_b_root.mkdir()
        electric_file = project_a_root / "electricity.md"
        water_file = project_b_root / "water.md"
        electric_file.write_text("# Electricity\n\nProjectA-Electric-Marker", encoding="utf-8")
        water_file.write_text("# Water\n\nProjectB-Water-Marker", encoding="utf-8")

        project_a = self.request_json("POST", "/api/projects", {"name": "ProjectA", "root_path": str(project_a_root)})
        project_b = self.request_json("POST", "/api/projects", {"name": "ProjectB", "root_path": str(project_b_root)})
        self.request_json("POST", "/api/project/scan", {"project_id": project_a["id"], "limit": 10, "reindex": True})
        self.request_json("POST", "/api/project/scan", {"project_id": project_b["id"], "limit": 10, "reindex": True})

        self.request_json("POST", "/api/memory", {
            "project_id": project_a["id"],
            "label": "Shared Java baseline",
            "type": "Concept",
            "scope": "shared",
            "text": "Shared-Java-SpringBoot-Marker applies across projects.",
        })

        electric_query = urllib.parse.quote("ProjectA-Electric-Marker")
        shared_query = urllib.parse.quote("Shared-Java-SpringBoot-Marker")
        a_search = self.request_json("GET", f"/api/memory/search?project_id={project_a['id']}&query={electric_query}")
        b_search = self.request_json("GET", f"/api/memory/search?project_id={project_b['id']}&query={electric_query}")
        b_shared_search = self.request_json("GET", f"/api/memory/search?project_id={project_b['id']}&query={shared_query}")

        self.assertTrue(any(hit["node"]["project_id"] == project_a["id"] for hit in a_search["hits"]))
        self.assertFalse(any(hit["node"]["project_id"] == project_a["id"] for hit in b_search["hits"]))
        self.assertTrue(any(hit["node"]["scope"] == "shared" and hit["node"]["project_id"] is None for hit in b_shared_search["hits"]))

        graph = self.request_json("GET", f"/api/graph?project_id={project_b['id']}")
        self.assertTrue(any(node["scope"] == "shared" and node["label"] == "Shared Java baseline" for node in graph["nodes"]))
        self.assertFalse(any(node.get("project_id") == project_a["id"] and node["type"] != "Project" for node in graph["nodes"]))

        electric_file.unlink()
        reindex = self.request_json("POST", "/api/project/scan", {"project_id": project_a["id"], "limit": 10, "reindex": True})
        self.assertGreaterEqual(reindex["archived"], 1)
        after_reindex = self.request_json("GET", f"/api/memory/search?project_id={project_a['id']}&query={electric_query}")
        self.assertFalse(any(hit["node"]["label"] == "electricity.md" for hit in after_reindex["hits"]))

    def test_startup_scripts_configuration_docs_and_help_are_consistent(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for relative in [
            "run_architectos.py",
            "start-architectos.ps1",
            "start-architectos.bat",
            "start-architectos-app.ps1",
            "start-architectos-app.bat",
            "docs/STARTUP.md",
            "docs/CONFIGURATION.md",
        ]:
            self.assertTrue((root / relative).exists(), relative)

        help_result = subprocess.run(
            [sys.executable, "run_architectos.py", "--help"],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=10,
            shell=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--strict-port", help_result.stdout)
        self.assertIn("--no-browser", help_result.stdout)
        self.assertIn("--app-window", help_result.stdout)

        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["scripts"]["start"], "python run_architectos.py")
        self.assertEqual(package["scripts"]["dev"], "python backend/app.py")
        self.assertEqual(package["scripts"]["app"], "python run_architectos.py --app-window")
        self.assertIn("unittest", package["scripts"]["test"])

        startup = (root / "docs" / "STARTUP.md").read_text(encoding="utf-8")
        config = (root / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
        self.assertIn("data\\architectos.runtime.json", startup)
        self.assertIn("--app-window", startup)
        self.assertIn("OPENAI_API_KEY", config)
        self.assertIn("No Secrets", config)
        index_html = (root / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("Open Work", index_html)
        self.assertNotIn("workspace-tasks", index_html)
        self.assertNotIn('data-view="graph"', index_html)
        self.assertNotIn("graph-view", index_html)
        self.assertNotIn('data-view="workflows"', index_html)
        self.assertNotIn("workflows-view", index_html)
        self.assertIn("project-root-path", index_html)
        self.assertIn("projectFolderModal", index_html)
        self.assertIn("browse-project-folder", index_html)
        self.assertIn("open-project-folder-modal", index_html)
        self.assertIn("project-folder-summary", index_html)
        self.assertIn("Connect folder", index_html)
        self.assertIn("Connect &amp; index", index_html)
        self.assertIn("Add Files to Memory", index_html)
        self.assertIn("memory-file-input", index_html)
        self.assertIn("data-tab=\"voice\"", index_html)
        self.assertIn("voice-memory-form", index_html)
        self.assertIn("voice-start", index_html)
        self.assertIn("Save Voice Memory", index_html)
        self.assertIn("Memory Graph", index_html)
        self.assertIn("Code context", index_html)
        self.assertIn("code-language-support", index_html)
        self.assertIn("code-page", index_html)
        self.assertIn("code-setup-panel", index_html)
        self.assertIn("code-use-selected-file", index_html)
        self.assertIn("code-tab-hover", index_html)
        self.assertIn("code-tab-diagnostics", index_html)
        self.assertIn("code-tab-references", index_html)
        self.assertIn("code-run-insight", index_html)
        self.assertIn('data-view="terminal"', index_html)
        self.assertIn("terminal-view", index_html)
        self.assertIn("terminal-command", index_html)
        self.assertIn("terminal-allow-destructive", index_html)
        self.assertIn("language-select", index_html)
        self.assertIn("language-menu", index_html)
        self.assertIn("data-language-option", index_html)
        self.assertIn("🇺🇸", index_html)
        self.assertIn("🇷🇺", index_html)
        self.assertIn("🇺🇦", index_html)
        self.assertIn("🇮🇱", index_html)
        self.assertIn("settings-language-select", index_html)
        self.assertIn("project-switcher", index_html)
        self.assertIn("project-select-label", index_html)
        self.assertIn("Developer memory OS", index_html)
        self.assertIn("workspace-files-title", index_html)
        app_js = (root / "frontend" / "app.js").read_text(encoding="utf-8")
        frontend_js = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((root / "frontend").glob("*.js"))
        )
        self.assertNotIn("workspace-tasks", frontend_js)
        self.assertNotIn('"view.graph"', frontend_js)
        self.assertNotIn('"view.workflows"', frontend_js)
        self.assertNotIn("loadWorkflows", frontend_js)
        self.assertIn("System Workspace", frontend_js)
        self.assertIn("displayProjectName", frontend_js)
        self.assertIn("syncProjectTerminology", frontend_js)
        self.assertIn("browseProjectFolder", frontend_js)
        self.assertIn("/api/system/folder-picker", frontend_js)
        self.assertIn("Scan Workspace", frontend_js)
        self.assertIn("scheduleGraphLoad", frontend_js)
        self.assertIn("/api/memory/files", frontend_js)
        self.assertIn("ensureVoiceRecognition", frontend_js)
        self.assertIn("saveVoiceMemory", frontend_js)
        self.assertIn("voiceRecognitionLanguage", frontend_js)
        self.assertIn("/api/code/languages", frontend_js)
        self.assertIn("/api/code/diagnostics", frontend_js)
        self.assertIn("/api/code/references", frontend_js)
        self.assertIn("fillCodeFileFromSelection", frontend_js)
        self.assertIn("/api/terminal/run", frontend_js)
        self.assertIn("/api/terminal/open", frontend_js)
        self.assertIn("runTerminalCommand", frontend_js)
        self.assertIn("data-terminal-install-run", frontend_js)
        self.assertIn("data-terminal-install-copy", frontend_js)
        self.assertIn("data-agent-install-command", frontend_js)
        self.assertIn("code-server-card", frontend_js)
        self.assertIn("install-actions", frontend_js)
        self.assertIn("runInstallCommandInTerminal", frontend_js)
        self.assertIn("installCodeLanguageServer", frontend_js)
        self.assertIn("/api/code/servers/", frontend_js)
        self.assertIn("copyInstallCommand", frontend_js)
        self.assertIn("askAgentInstallCommand", frontend_js)
        self.assertIn("LANGUAGE_META", frontend_js)
        self.assertIn("syncLanguageMenu", frontend_js)
        self.assertIn("openGraphNodeModal", frontend_js)
        self.assertIn("graphNodeRelatedFiles", frontend_js)
        self.assertIn("graphEdgeLabel", frontend_js)
        self.assertIn("confidence", frontend_js)
        self.assertIn("translations", frontend_js)
        self.assertIn("RTL_LANGUAGES", frontend_js)
        self.assertIn("Русский", frontend_js)
        self.assertIn("Українська", frontend_js)
        self.assertIn("עברית", frontend_js)
        self.assertTrue(app_js.strip(), "frontend/app.js should remain the bootstrap entrypoint")
        styles = (root / "frontend" / "styles.css").read_text(encoding="utf-8")
        self.assertNotIn('data-view="graph"', styles)
        self.assertNotIn('data-view="workflows"', styles)
        styles_compact = "".join(styles.split())
        self.assertIn(".graph-stage{width:100%;height:640px", styles_compact)
        self.assertIn(".graph-node-dialog", styles_compact)
        self.assertIn(".code-grid{grid-template-columns:minmax(280px,360px)minmax(0,1fr)", styles_compact)
        self.assertIn("#code-server-list{grid-template-columns:repeat(auto-fit,minmax(440px,1fr))", styles_compact)
        self.assertIn("white-space:nowrap", styles_compact)
        self.assertIn(".voice-memory-container{display:grid", styles_compact)
        self.assertIn(".voice-record-btn{background:linear-gradient", styles_compact)
        self.assertIn(".project-folder-modal{position:fixed", styles_compact)
        self.assertIn(".project-folder-summary{display:flex", styles_compact)
        self.assertIn(".language-menu{position:absolute", styles_compact)
        self.assertIn(".language-dropdown.open.language-menu", styles_compact)
        self.assertIn("@media(max-width:1300px)", styles_compact)
        server_py = (root / "backend" / "architectos" / "server.py").read_text(encoding="utf-8")
        service_py = (root / "backend" / "architectos" / "service.py").read_text(encoding="utf-8")
        self.assertIn("/api/system/folder-picker", server_py)
        self.assertIn("system_folder_picker", service_py)


if __name__ == "__main__":
    unittest.main()
