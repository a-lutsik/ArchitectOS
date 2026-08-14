from __future__ import annotations

import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


def _load_build_share_package():
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "build_share_package.py"
    spec = importlib.util.spec_from_file_location("build_share_package", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SharePackageTests(unittest.TestCase):
    def test_guide_and_autostart_scripts_exist(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for relative in [
            "docs/GUIDE_RU.md",
            "docs/INSTALLERS.md",
            "scripts/install_autostart.sh",
            "scripts/install_autostart.ps1",
            "scripts/uninstall_autostart.sh",
            "scripts/uninstall_autostart.ps1",
            "scripts/build_share_package.py",
        ]:
            self.assertTrue((root / relative).is_file(), relative)

        guide = (root / "docs" / "GUIDE_RU.md").read_text(encoding="utf-8")
        self.assertIn("Full", guide)
        self.assertIn("MCP", guide)
        self.assertIn("IDE", guide)
        self.assertIn("install.sh", guide)
        self.assertIn("LaunchAgent", guide)

    def test_build_share_package_stages_zip_with_fake_sidecar(self) -> None:
        bsp = _load_build_share_package()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "architectos-server"
            fake_bin.write_bytes(b"#!/bin/sh\necho fake\n")
            fake_bin.chmod(0o755)
            out_dir = tmp_path / "out"

            with mock.patch.object(bsp, "find_existing_server", return_value=fake_bin):
                code = bsp.main(["--skip-build", "--output-dir", str(out_dir)])
            self.assertEqual(code, 0)

            archives = list(out_dir.glob("ArchitectOS_Full_*.zip"))
            self.assertEqual(len(archives), 1, archives)
            with zipfile.ZipFile(archives[0]) as zf:
                names = zf.namelist()
            joined = "\n".join(names)
            self.assertIn("docs/GUIDE_RU.md", joined)
            self.assertIn("README-SHARE.txt", joined)
            self.assertTrue(
                any(n.endswith("/install.sh") or n.endswith("/install.bat") for n in names),
                names,
            )
            self.assertTrue(
                any("architectos-server" in n for n in names),
                names,
            )


if __name__ == "__main__":
    unittest.main()
