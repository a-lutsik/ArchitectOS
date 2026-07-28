from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from backend.architectos.release import build_release_archive, release_manifest


class ReleasePackagingTests(unittest.TestCase):
    def test_release_manifest_is_ready_and_excludes_runtime_state(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest = release_manifest(root)
        self.assertTrue(manifest["ready"], manifest)
        self.assertTrue(manifest["checks"]["required_files"])
        self.assertTrue(manifest["checks"]["visual_qa"])
        self.assertFalse(any(item.startswith("data/") for item in manifest["files"]))
        self.assertFalse(any(item.startswith("memory/") for item in manifest["files"]))
        self.assertFalse(any("__pycache__" in item or item.endswith(".pyc") for item in manifest["files"]))
        for required in [
            "docs/RELEASE_QA.md",
            "docs/CONFIGURATION.md",
            "frontend/index.html",
            "frontend/styles.css",
            "tests/test_e2e.py",
            "tests/test_ops.py",
            "docs/PRODUCTION.md",
            "start-architectos-app.ps1",
            "start-architectos-app.bat",
        ]:
            self.assertIn(required, manifest["files"])

    def test_build_release_archive_contains_manifest_and_portable_files(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            result = build_release_archive(root, Path(tmp))
            archive = Path(result["archive"])
            self.assertTrue(archive.exists())
            self.assertGreater(result["archive_size"], 1000)
            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
                self.assertIn("architectos-1.0.0/release-manifest.json", names)
                self.assertIn("architectos-1.0.0/run_architectos.py", names)
                self.assertIn("architectos-1.0.0/start-architectos-app.ps1", names)
                self.assertIn("architectos-1.0.0/start-architectos-app.bat", names)
                self.assertIn("architectos-1.0.0/frontend/index.html", names)
                self.assertFalse(any("/data/" in name or "/memory/" in name or "/backups/" in name for name in names))
                manifest = json.loads(zf.read("architectos-1.0.0/release-manifest.json").decode("utf-8"))
                self.assertTrue(manifest["ready"])

    def test_release_script_check_only_reports_ready_manifest(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "scripts/build_release.py", "--check-only"],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=15,
            shell=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)
        self.assertTrue(manifest["ready"])
        self.assertTrue(manifest["checks"]["startup_scripts"])

    def test_release_excludes_dotenv_files(self) -> None:
        from backend.architectos.release import should_include

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root / ".env"
            secret.write_text("OPENAI_API_KEY=sk-test", encoding="utf-8")
            local = root / ".env.local"
            local.write_text("TOKEN=abc", encoding="utf-8")
            example = root / ".env.example"
            example.write_text("OPENAI_API_KEY=", encoding="utf-8")
            self.assertFalse(should_include(secret, root))
            self.assertFalse(should_include(local, root))
            self.assertTrue(should_include(example, root))

        project_root = Path(__file__).resolve().parents[1]
        names = [Path(item).name for item in release_manifest(project_root)["files"]]
        self.assertNotIn(".env", names)
        self.assertNotIn(".env.local", names)


if __name__ == "__main__":
    unittest.main()
