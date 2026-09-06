from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
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
        install_sh = (root / "scripts" / "install_autostart.sh").read_text(encoding="utf-8")
        self.assertIn("vector-runtime", install_sh)
        self.assertIn("ARCHITECTOS_SKIP_AUTOSTART", install_sh)
        self.assertIn("ARCHITECTOS_SKIP_PAUSE", install_sh)
        self.assertIn("wait_if_interactive", install_sh)
        self.assertIn("SUCCESS / УСПЕХ", install_sh)
        self.assertIn("xattr -dr com.apple.quarantine", install_sh)
        install_ps1 = (root / "scripts" / "install_autostart.ps1").read_text(encoding="utf-8")
        self.assertIn("vector-runtime", install_ps1)
        self.assertIn("ARCHITECTOS_SKIP_AUTOSTART", install_ps1)
        self.assertIn("Unblock-File", install_ps1)
        self.assertIn("runtime", install_ps1)
        self.assertIn("SUCCESS / УСПЕХ", install_ps1)
        self.assertIn("ERROR / ОШИБКА", install_ps1)

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
            with zipfile.ZipFile(archives[0]) as zf:
                for info in zf.infolist():
                    if info.filename.endswith("/install.sh") or info.filename.endswith("/architectos-server"):
                        mode = (info.external_attr >> 16) & 0o777
                        self.assertTrue(mode & stat.S_IXUSR, f"{info.filename} mode={oct(mode)}")

    def test_windows_cross_pack_includes_runtime_and_install_bat(self) -> None:
        bsp = _load_build_share_package()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            embed = tmp_path / "python-embed.zip"
            with zipfile.ZipFile(embed, "w") as zf:
                zf.writestr("python.exe", b"MZ")
                zf.writestr("python312.zip", b"PK")
                zf.writestr("python312._pth", "python312.zip\n.\n")
            launcher = tmp_path / "architectos-server.exe"
            launcher.write_bytes(b"MZ fake-pe")
            out_dir = tmp_path / "out"

            with (
                mock.patch.object(bsp, "download_windows_embed", return_value=embed),
                mock.patch.object(bsp, "compile_windows_launcher", return_value=launcher),
                mock.patch.object(bsp, "vendor_windows_vector_wheels", return_value=tmp_path / "site"),
            ):
                code = bsp.main(
                    ["--platform", "windows", "--with-mcp", "--with-ide", "--output-dir", str(out_dir)]
                )
            self.assertEqual(code, 0)
            archives = list(out_dir.glob("ArchitectOS_Full_*_windows.zip"))
            self.assertEqual(len(archives), 1, archives)
            with zipfile.ZipFile(archives[0]) as zf:
                names = "\n".join(zf.namelist())
            self.assertIn("/install.bat", names)
            self.assertIn("/install.ps1", names)
            self.assertIn("/architectos-server.exe", names)
            self.assertIn("/architectos-mcp.exe", names)
            self.assertIn("/runtime/python/python.exe", names)
            self.assertIn("/runtime/backend/architectos/", names)
            self.assertIn("/runtime/frontend/index.html", names)
            self.assertIn("/runtime/python/python312._pth", names)
            with zipfile.ZipFile(archives[0]) as zf:
                pth_name = next(n for n in zf.namelist() if n.endswith("/python312._pth"))
                pth = zf.read(pth_name).decode("ascii")
            self.assertIn("Lib/site-packages", pth)
            self.assertIn("import site", pth)
            with zipfile.ZipFile(archives[0]) as zf:
                bat_name = next(n for n in zf.namelist() if n.endswith("/install.bat"))
                bat = zf.read(bat_name).decode("utf-8")
            self.assertIn("cmdcmdline", bat)
            self.assertIn("pause", bat)
            self.assertIn("INSTALL OK", bat)
            self.assertIn("exit /b %ERR%", bat)
            self.assertNotIn("if errorlevel 1 pause", bat)

    def test_windows_launcher_bat_pauses_when_double_clicked(self) -> None:
        bsp = _load_build_share_package()
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            bsp.copy_autostart_scripts(stage, "windows")
            bat = (stage / "install.bat").read_text(encoding="utf-8")
            self.assertIn("cmdcmdline", bat)
            self.assertIn("pause", bat)
            self.assertIn("INSTALL OK", bat)
            self.assertIn("INSTALL FAILED", bat)
            self.assertIn("exit /b %ERR%", bat)
            self.assertNotIn("if errorlevel 1 pause", bat)
            unbat = (stage / "uninstall.bat").read_text(encoding="utf-8")
            self.assertIn("cmdcmdline", unbat)
            self.assertIn("pause", unbat)
            self.assertIn("UNINSTALL OK", unbat)

    def test_install_sh_copies_companions_without_autostart(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            package = tmp_path / "pkg"
            dest_root = tmp_path / "home" / "ArchitectOS"
            package.mkdir()
            (package / "docs").mkdir()
            (package / "ide").mkdir()
            (package / "docs" / "GUIDE_RU.md").write_text("guide\n", encoding="utf-8")
            (package / "README-SHARE.txt").write_text("readme\n", encoding="utf-8")
            (package / "ide" / "plugin.zip").write_bytes(b"pk")
            fake_server = package / "architectos-server"
            fake_server.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "vector-runtime" ]; then echo "Memory search backend: python"; exit 0; fi\n'
                'echo "unexpected: $*" >&2; exit 1\n',
                encoding="utf-8",
            )
            fake_server.chmod(0o755)
            fake_mcp = package / "architectos-mcp"
            fake_mcp.write_text("#!/bin/sh\necho mcp\n", encoding="utf-8")
            fake_mcp.chmod(0o755)
            install_src = root / "scripts" / "install_autostart.sh"
            uninstall_src = root / "scripts" / "uninstall_autostart.sh"
            (package / "install.sh").write_text(install_src.read_text(encoding="utf-8"), encoding="utf-8")
            (package / "uninstall.sh").write_text(uninstall_src.read_text(encoding="utf-8"), encoding="utf-8")
            (package / "install.sh").chmod(0o755)
            (package / "uninstall.sh").chmod(0o755)

            env = os.environ.copy()
            env["ARCHITECTOS_ROOT"] = str(dest_root)
            env["ARCHITECTOS_SKIP_AUTOSTART"] = "1"
            env["ARCHITECTOS_PROBE_TIMEOUT"] = "10"
            proc = subprocess.run(
                [str(package / "install.sh")],
                cwd=package,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue((dest_root / "bin" / "architectos-server").is_file())
            self.assertTrue(os.access(dest_root / "bin" / "architectos-server", os.X_OK))
            self.assertTrue((dest_root / "bin" / "architectos-mcp").is_file())
            self.assertTrue((dest_root / "data").is_dir())
            self.assertTrue((dest_root / "logs").is_dir())
            self.assertTrue((dest_root / "uninstall.sh").is_file())
            self.assertTrue((dest_root / "docs" / "GUIDE_RU.md").is_file())
            self.assertTrue((dest_root / "README-SHARE.txt").is_file())
            self.assertTrue((dest_root / "ide" / "plugin.zip").is_file())
            self.assertIn("Skipped OS autostart", proc.stdout)
            self.assertIn("SUCCESS / УСПЕХ", proc.stdout)
            self.assertNotIn("Press Enter to close", proc.stdout)

    def test_install_sh_survives_hung_vector_runtime_probe(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            package = tmp_path / "pkg"
            dest_root = tmp_path / "home" / "ArchitectOS"
            package.mkdir()
            hung = package / "architectos-server"
            hung.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
            hung.chmod(0o755)
            install_src = root / "scripts" / "install_autostart.sh"
            (package / "install.sh").write_text(install_src.read_text(encoding="utf-8"), encoding="utf-8")
            (package / "install.sh").chmod(0o755)

            env = os.environ.copy()
            env["ARCHITECTOS_ROOT"] = str(dest_root)
            env["ARCHITECTOS_SKIP_AUTOSTART"] = "1"
            env["ARCHITECTOS_PROBE_TIMEOUT"] = "1"
            proc = subprocess.run(
                [str(package / "install.sh")],
                cwd=package,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue((dest_root / "bin" / "architectos-server").is_file())
            combined = proc.stdout + proc.stderr
            self.assertTrue(
                "timed out" in combined or "unavailable" in combined,
                combined,
            )

    def test_vendor_windows_vector_wheels_unpacks_into_site_packages(self) -> None:
        bsp = _load_build_share_package()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            python_dir = tmp_path / "python"
            python_dir.mkdir()
            work = tmp_path / "work"

            def fake_download(cmd, check=True, **_kwargs):
                dest = Path(cmd[cmd.index("--dest") + 1])
                dest.mkdir(parents=True, exist_ok=True)
                wheel = dest / "sqlite_vec-0.1.6-py3-none-win_amd64.whl"
                with zipfile.ZipFile(wheel, "w") as zf:
                    zf.writestr("sqlite_vec/__init__.py", "loaded = True\n")
                self.assertIn("win_amd64", cmd)
                self.assertIn("sqlite-vec>=0.1.6", cmd)
                return mock.Mock(returncode=0)

            with mock.patch.object(bsp.subprocess, "run", side_effect=fake_download):
                site = bsp.vendor_windows_vector_wheels(python_dir, work)
            self.assertEqual(site, python_dir / "Lib" / "site-packages")
            self.assertTrue((site / "sqlite_vec" / "__init__.py").is_file())
            self.assertIn("loaded = True", (site / "sqlite_vec" / "__init__.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
