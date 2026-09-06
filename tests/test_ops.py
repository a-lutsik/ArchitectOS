from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from backend.architectos.config import APP_VERSION
from backend.architectos.server import ArchitectOSHandler
from backend.architectos.service import ArchitectOSService

# These requests hit an in-process server on loopback, so the timeout only guards
# against a hang. It has to absorb a machine running the whole suite under
# coverage, where a 10s budget intermittently expired on the readiness check.
LOCAL_HTTP_TIMEOUT = 60


class ProductionOpsTests(unittest.TestCase):
    def test_version_readiness_and_backup_service(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        root_service = ArchitectOSService(app_root)
        version = root_service.version()
        self.assertEqual(version["version"], APP_VERSION)
        self.assertTrue(version["release"]["ready"])

        ready = root_service.readiness()
        self.assertTrue(ready["ok"], ready)
        self.assertEqual(ready["checks"]["database"]["status"], "ok")
        self.assertEqual(ready["checks"]["security"]["status"], "ok")
        self.assertEqual(ready["checks"]["frontend"]["status"], "ok")

        with tempfile.TemporaryDirectory() as tmp:
            temp_service = ArchitectOSService(Path(tmp))
            temp_ready = temp_service.readiness()
            self.assertFalse(temp_ready["ok"], temp_ready)
            self.assertEqual(temp_ready["checks"]["database"]["status"], "ok")
            self.assertEqual(temp_ready["checks"]["frontend"]["status"], "error")

            backup = temp_service.create_backup({"label": "unit"})
            self.assertTrue(Path(backup["path"]).exists())
            self.assertGreater(backup["size"], 0)
            backups = temp_service.list_backups()["backups"]
            self.assertEqual(backups[0]["path"], backup["path"])

    def test_packaged_readiness_uses_bundled_frontend_and_skips_source_manifest(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        frontend_root = app_root / "frontend"
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            with mock.patch("backend.architectos.service.is_frozen", return_value=True), mock.patch(
                "backend.architectos.service.resolve_frontend_root",
                return_value=frontend_root,
            ):
                ready = service.readiness()
            self.assertTrue(ready["ok"], ready)
            self.assertEqual(ready["checks"]["frontend"]["status"], "ok")
            self.assertEqual(ready["checks"]["frontend"]["root"], str(frontend_root))
            self.assertEqual(ready["checks"]["release"]["status"], "ok")
            self.assertEqual(ready["checks"]["release"]["mode"], "packaged")
            self.assertTrue(ready["checks"]["release"]["checks"]["packaged_runtime"])

    def test_http_ops_endpoints_and_security_headers(self) -> None:
        service = ArchitectOSService(Path(__file__).resolve().parents[1])

        class TestHandler(ArchitectOSHandler):
            pass

        TestHandler.service = service
        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        token_headers = {"X-ArchitectOS-Token": service.auth_token}
        try:
            ready_response = urllib.request.urlopen(urllib.request.Request(f"{base_url}/api/ready", headers=token_headers), timeout=LOCAL_HTTP_TIMEOUT)
            ready = json.loads(ready_response.read().decode("utf-8"))
            self.assertTrue(ready["ok"], ready)
            self.assertEqual(ready_response.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(ready_response.headers["X-Frame-Options"], "DENY")
            self.assertEqual(ready_response.headers["Cache-Control"], "no-store, no-cache, must-revalidate, max-age=0")
            self.assertIn("default-src 'self'", ready_response.headers["Content-Security-Policy"])

            index_response = urllib.request.urlopen(base_url, timeout=LOCAL_HTTP_TIMEOUT)
            index_html = index_response.read().decode("utf-8")
            self.assertEqual(index_response.headers["Cache-Control"], "no-store, no-cache, must-revalidate, max-age=0")
            self.assertNotIn('data-view="graph"', index_html)
            self.assertIn("Memory Graph", index_html)
            self.assertIn(f'<meta name="architectos-token" content="{service.auth_token}">', index_html)

            request = urllib.request.Request(
                f"{base_url}/api/ops/backup",
                data=b"{}",
                method="POST",
                headers={"Content-Type": "application/json", **token_headers},
            )
            backup = json.loads(urllib.request.urlopen(request, timeout=LOCAL_HTTP_TIMEOUT).read().decode("utf-8"))
            self.assertTrue(Path(backup["path"]).exists())
            backups = json.loads(urllib.request.urlopen(urllib.request.Request(f"{base_url}/api/ops/backups", headers=token_headers), timeout=LOCAL_HTTP_TIMEOUT).read().decode("utf-8"))
            self.assertGreaterEqual(len(backups["backups"]), 1)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_http_api_requires_token_and_local_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))

            class TestHandler(ArchitectOSHandler):
                pass

            TestHandler.service = service
            server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"

            def status_of(request: urllib.request.Request) -> int:
                try:
                    with urllib.request.urlopen(request, timeout=LOCAL_HTTP_TIMEOUT) as response:
                        return response.status
                except urllib.error.HTTPError as exc:
                    return exc.code

            try:
                # Static shell stays reachable without a token so the SPA can boot.
                self.assertEqual(status_of(urllib.request.Request(base_url)), 200)
                # API without a token is rejected.
                self.assertEqual(status_of(urllib.request.Request(f"{base_url}/api/health")), 403)
                # Wrong token is rejected.
                self.assertEqual(status_of(urllib.request.Request(f"{base_url}/api/health", headers={"X-ArchitectOS-Token": "wrong"})), 403)
                # Cross-origin browser request is rejected even with the token.
                self.assertEqual(status_of(urllib.request.Request(f"{base_url}/api/health", headers={"X-ArchitectOS-Token": service.auth_token, "Origin": "https://evil.example"})), 403)
                # Foreign Host header (DNS rebinding) is rejected.
                self.assertEqual(status_of(urllib.request.Request(f"{base_url}/api/health", headers={"X-ArchitectOS-Token": service.auth_token, "Host": "evil.example"})), 403)
                # OAuth callback is token-exempt, but STILL Host/Origin-guarded (no rebinding bypass).
                self.assertEqual(status_of(urllib.request.Request(f"{base_url}/api/mcp/oauth/callback", headers={"Host": "evil.example"})), 403)
                # Token remains waived for the loopback OAuth callback (handler runs, not 403).
                callback_status = status_of(urllib.request.Request(f"{base_url}/api/mcp/oauth/callback"))
                self.assertNotEqual(callback_status, 403)
                # Same-origin request with the token passes.
                self.assertEqual(status_of(urllib.request.Request(f"{base_url}/api/health", headers={"X-ArchitectOS-Token": service.auth_token, "Origin": base_url})), 200)
                self.assertEqual(status_of(urllib.request.Request(f"{base_url}/api/health", headers={"X-ArchitectOS-Token": service.auth_token})), 200)
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()

    def test_project_file_save_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            project_root = root / "proj"
            project_root.mkdir()
            project = service.create_project({"name": "Proj", "root_path": str(project_root)})
            project_id = project["id"]
            with self.assertRaises(ValueError):
                service.save_project_file({"project_id": project_id, "path": "../escape.txt", "text": "x"})
            self.assertFalse((root / "escape.txt").exists())
            saved = service.save_project_file({"project_id": project_id, "path": "ok/nested.txt", "text": "hello"})
            self.assertTrue(saved["success"])
            self.assertEqual((project_root / "ok" / "nested.txt").read_text(encoding="utf-8"), "hello")

    def test_export_bundle_strips_mcp_oauth_secrets(self) -> None:
        from backend.architectos.models import Project
        from backend.architectos.storage import SQLiteMemoryRepository

        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteMemoryRepository(Path(tmp))
            repository.upsert_project(Project(id="architectos", name="ArchitectOS"))
            repository.set_setting("mcp_servers", {
                "servers": [
                    {
                        "id": "granola",
                        "label": "Granola",
                        "auth": {
                            "status": "authorized",
                            "access_token": "access-token-SECRET-123",
                            "refresh_token": "refresh-token-SECRET-456",
                            "client_secret": "client-secret-SECRET-789",
                            "expires_at": 4102444800,
                        },
                    }
                ]
            })
            bundle = repository.export_bundle("architectos")
            serialized = json.dumps(bundle)
            self.assertNotIn("access-token-SECRET-123", serialized)
            self.assertNotIn("refresh-token-SECRET-456", serialized)
            self.assertNotIn("client-secret-SECRET-789", serialized)
            server_auth = bundle["settings"]["mcp_servers"]["servers"][0]["auth"]
            self.assertEqual(server_auth["status"], "authorized")
            self.assertEqual(server_auth["expires_at"], 4102444800)

    def test_export_bundle_masks_mcp_env_and_headers(self) -> None:
        from backend.architectos.constants import SECRET_MASK
        from backend.architectos.models import Project
        from backend.architectos.storage import SQLiteMemoryRepository

        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteMemoryRepository(Path(tmp))
            repository.upsert_project(Project(id="architectos", name="ArchitectOS"))
            repository.set_setting("mcp_servers", {
                "servers": [
                    {
                        "id": "github",
                        "label": "GitHub",
                        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_testsecret123"},
                        "headers": {"Authorization": "Bearer hdr_testsecret456"},
                    }
                ]
            })
            bundle = repository.export_bundle("architectos")
            serialized = json.dumps(bundle)
            self.assertNotIn("ghp_testsecret123", serialized)
            self.assertNotIn("hdr_testsecret456", serialized)
            server = bundle["settings"]["mcp_servers"]["servers"][0]
            # Key names survive so the config stays editable; values are masked.
            self.assertEqual(server["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"], SECRET_MASK)
            self.assertEqual(server["headers"]["Authorization"], SECRET_MASK)

    def test_system_folder_picker_macos_uses_osascript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            completed = subprocess.CompletedProcess(args=["osascript"], returncode=0, stdout=f"{tmp}\n", stderr="")
            with mock.patch("backend.architectos.folder_picker.pick_folder_subprocess", return_value={"supported": True, "cancelled": False, "path": tmp}):
                result = service.system_folder_picker({"initial_path": tmp})
            self.assertTrue(result["supported"])
            self.assertFalse(result["cancelled"])
            self.assertEqual(result["path"], tmp)

    def test_system_folder_picker_reports_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            with mock.patch("backend.architectos.folder_picker.pick_folder_subprocess", return_value={"supported": True, "cancelled": True, "path": ""}):
                result = service.system_folder_picker({"initial_path": tmp})
            self.assertTrue(result["supported"])
            self.assertTrue(result["cancelled"])
            self.assertEqual(result["path"], "")

    def test_system_folder_picker_linux_without_tools_is_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            with mock.patch("backend.architectos.folder_picker.pick_folder_subprocess", return_value={"supported": False, "cancelled": True, "path": "", "message": "Install zenity or kdialog"}):
                result = service.system_folder_picker({"initial_path": tmp})
            self.assertFalse(result["supported"])
            self.assertIn("zenity", result["message"])

    def test_unhandled_api_error_hides_exception_text(self) -> None:
        service = ArchitectOSService(Path(__file__).resolve().parents[1])

        class TestHandler(ArchitectOSHandler):
            pass

        TestHandler.service = service
        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        headers = {"X-ArchitectOS-Token": service.auth_token, "Content-Type": "application/json"}
        try:
            with mock.patch.object(service, "health", side_effect=RuntimeError("/Users/secret/path boom")):
                try:
                    urllib.request.urlopen(
                        urllib.request.Request(f"{base_url}/api/health", headers=headers),
                        timeout=LOCAL_HTTP_TIMEOUT,
                    )
                    self.fail("expected HTTPError")
                except urllib.error.HTTPError as exc:
                    self.assertEqual(exc.code, 500)
                    body = json.loads(exc.read().decode("utf-8"))
                    self.assertEqual(body["error"], "Internal server error")
                    self.assertIn("error_id", body)
                    self.assertNotIn("/Users/secret", body["error"])
                    self.assertNotIn("boom", body["error"])
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
