from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.architectos.service import ArchitectOSService
from backend.architectos.vec_runtime import (
    PIP_MISSING_ERROR,
    apply_vector_runtime_repair,
    is_embeddable_python,
    propose_repair,
    vector_runtime_status,
)
from backend.architectos.vecsql import sqlite_load_extension_supported, sqlite_vec_status


def _current(
    *,
    load_extension: bool = False,
    loaded: bool = False,
    installed: bool = False,
    frozen: bool = False,
    reason: str = "load-extension-disabled",
    pip: bool = True,
    writable: bool = True,
    embeddable: bool = False,
    executable: str = "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
) -> dict:
    return {
        "executable": executable,
        "version": "3.12.0",
        "load_extension": load_extension,
        "installed": installed,
        "loaded": loaded,
        "reason": reason,
        "frozen": frozen,
        "pip": pip,
        "writable": writable,
        "embeddable": embeddable,
        "current": True,
    }


class SqliteVecStatusTests(unittest.TestCase):
    def test_status_reports_load_extension_and_python(self) -> None:
        status = sqlite_vec_status()
        self.assertIn("load_extension", status)
        self.assertEqual(status["load_extension"], sqlite_load_extension_supported())
        self.assertTrue(status["python"])
        self.assertIn(status["reason"], {"ok", "not-installed", "load-extension-disabled", "probe-failed"})
        conn = sqlite3.connect(":memory:")
        try:
            self.assertEqual(status["load_extension"], hasattr(conn, "enable_load_extension"))
        finally:
            conn.close()


class VectorRuntimeStatusTests(unittest.TestCase):
    def test_settings_include_cheap_probe_without_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            with mock.patch("backend.architectos.vec_runtime.discover_capable_pythons", side_effect=AssertionError("must not discover on settings load")):
                settings = service.settings()
            runtime = settings["vector_runtime"]
            self.assertIn("current", runtime)
            self.assertIn("repair", runtime)
            self.assertEqual(runtime["current"]["load_extension"], sqlite_load_extension_supported())

    def test_disabled_python_needs_check_until_discovered(self) -> None:
        current = _current()
        with mock.patch("backend.architectos.vec_runtime.probe_current_python", return_value=current):
            with mock.patch("backend.architectos.vec_runtime.discover_capable_pythons", side_effect=AssertionError("discover")):
                report = vector_runtime_status(discover=False)
        self.assertFalse(report["ok"])
        self.assertTrue(report["repair"]["needs_check"])
        self.assertFalse(report["repair"]["fixable"])
        self.assertIsNone(report["repair"]["action"])

    def test_discover_proposes_capable_python(self) -> None:
        candidate = {
            "executable": "/opt/homebrew/bin/python3",
            "version": "3.13.0",
            "load_extension": True,
            "installed": False,
            "loaded": False,
            "reason": "not-installed",
            "current": False,
        }
        with mock.patch("backend.architectos.vec_runtime.probe_current_python", return_value=_current()):
            with mock.patch("backend.architectos.vec_runtime.discover_capable_pythons", return_value=[candidate]):
                with mock.patch("backend.architectos.vec_runtime._needs_isolated_venv", return_value=False):
                    report = vector_runtime_status(discover=True)
        self.assertEqual(report["repair"]["action"], "use_capable_python")
        self.assertTrue(report["repair"]["fixable"])
        self.assertIn("/opt/homebrew/bin/python3", report["repair"]["command_preview"])

    def test_brew_is_proposed_when_no_capable_python(self) -> None:
        with mock.patch("backend.architectos.vec_runtime.probe_current_python", return_value=_current()):
            with mock.patch("backend.architectos.vec_runtime.discover_capable_pythons", return_value=[]):
                with mock.patch("backend.architectos.vec_runtime._brew_bin", return_value="/opt/homebrew/bin/brew"):
                    with mock.patch("backend.architectos.vec_runtime.sys") as sys_mod:
                        sys_mod.platform = "darwin"
                        sys_mod.executable = "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
                        repair = propose_repair(_current(), candidates=[], discovered=True)
        self.assertEqual(repair["action"], "install_homebrew_python")
        self.assertTrue(repair["fixable"])
        self.assertIn("brew", repair["command_preview"])


class VectorRuntimeRepairTests(unittest.TestCase):
    def test_repair_requires_confirm(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            apply_vector_runtime_repair({"action": "install_sqlite_vec"})
        self.assertIn("confirm", str(ctx.exception))

    def test_unknown_action_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            apply_vector_runtime_repair({"confirm": True, "action": "rm -rf /"})

    def test_install_sqlite_vec_rejected_when_extensions_disabled(self) -> None:
        with mock.patch("backend.architectos.vec_runtime.probe_current_python", return_value=_current()):
            with mock.patch("backend.architectos.vec_runtime.discover_capable_pythons", return_value=[]):
                with mock.patch("backend.architectos.vec_runtime._brew_bin", return_value=None):
                    with mock.patch("backend.architectos.vec_runtime.is_frozen", return_value=False):
                        with self.assertRaises(ValueError) as ctx:
                            apply_vector_runtime_repair({"confirm": True, "action": "install_sqlite_vec"})
        self.assertIn("not valid", str(ctx.exception))

    def test_pep668_python_proposes_local_venv(self) -> None:
        candidate = {
            "executable": "/usr/local/bin/python3.14",
            "version": "3.14.7",
            "load_extension": True,
            "installed": False,
            "loaded": False,
            "reason": "not-installed",
            "current": False,
        }
        with mock.patch("backend.architectos.vec_runtime.probe_current_python", return_value=_current()):
            with mock.patch("backend.architectos.vec_runtime.discover_capable_pythons", return_value=[candidate]):
                with mock.patch("backend.architectos.vec_runtime._needs_isolated_venv", return_value=True):
                    report = vector_runtime_status(discover=True)
        repair = report["repair"]
        self.assertEqual(repair["action"], "use_capable_python")
        self.assertTrue(repair["isolate_venv"])
        self.assertIn("-m venv", repair["command_preview"])
        self.assertIn("data/python", repair["command_preview"])

    def test_use_capable_python_installs_into_target_without_real_pip(self) -> None:
        candidate = {
            "executable": "/opt/homebrew/bin/python3",
            "version": "3.13.0",
            "load_extension": True,
            "installed": False,
            "loaded": False,
            "reason": "not-installed",
            "current": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            pref = Path(tmp) / "architectos.python.json"
            with mock.patch("backend.architectos.vec_runtime.probe_current_python", return_value=_current()):
                with mock.patch("backend.architectos.vec_runtime.discover_capable_pythons", return_value=[candidate]):
                    with mock.patch("backend.architectos.vec_runtime.is_frozen", return_value=False):
                        with mock.patch("backend.architectos.vec_runtime._ensure_pip", return_value={"ok": True, "skipped": True, "command": [], "returncode": 0, "stdout": "ok", "stderr": "", "error": ""}):
                            with mock.patch("backend.architectos.vec_runtime._pip_install", return_value={"ok": True, "command": [], "returncode": 0, "stdout": "ok", "stderr": "", "error": ""}):
                                with mock.patch("backend.architectos.vec_runtime.preferred_python_path", return_value=pref):
                                    with mock.patch("backend.architectos.vec_runtime._needs_isolated_venv", return_value=False):
                                        with mock.patch("backend.architectos.vec_runtime.retarget_macos_launch_agent", return_value={"ok": False, "skipped": True}):
                                            result = apply_vector_runtime_repair({"confirm": True, "action": "use_capable_python"})
            self.assertTrue(result["ok"])
            self.assertTrue(result["restart_required"])
            self.assertIn("architectos", result["restart_command"])
            saved = json.loads(pref.read_text(encoding="utf-8"))
            self.assertEqual(saved["executable"], "/opt/homebrew/bin/python3")

    def test_install_sqlite_vec_when_current_python_can_load_extensions(self) -> None:
        before = _current(load_extension=True, installed=False, loaded=False, reason="not-installed")
        after = _current(load_extension=True, installed=True, loaded=True, reason="ok")
        with mock.patch("backend.architectos.vec_runtime.probe_current_python", side_effect=[before, after, after]):
            with mock.patch("backend.architectos.vec_runtime.is_frozen", return_value=False):
                with mock.patch("backend.architectos.vec_runtime._ensure_pip", return_value={"ok": True, "skipped": True, "command": [], "returncode": 0, "stdout": "ok", "stderr": "", "error": ""}):
                    with mock.patch("backend.architectos.vec_runtime._pip_install", return_value={"ok": True, "command": ["pip"], "returncode": 0, "stdout": "ok", "stderr": "", "error": ""}):
                        with mock.patch("backend.architectos.vec_runtime.reset_sqlite_vec_probe"):
                            with mock.patch("backend.architectos.vec_runtime._needs_isolated_venv", return_value=False):
                                result = apply_vector_runtime_repair({"confirm": True, "action": "install_sqlite_vec"})
        self.assertTrue(result["ok"])
        self.assertFalse(result["restart_required"])

    def test_homebrew_install_uses_allowlisted_argv(self) -> None:
        brew_proc = mock.Mock(returncode=0, stdout="python poured", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            pref = Path(tmp) / "architectos.python.json"
            with mock.patch("backend.architectos.vec_runtime.probe_current_python", return_value=_current()):
                with mock.patch("backend.architectos.vec_runtime.discover_capable_pythons", return_value=[]):
                    with mock.patch("backend.architectos.vec_runtime._brew_bin", return_value="/opt/homebrew/bin/brew"):
                        with mock.patch("backend.architectos.vec_runtime.sys") as sys_mod:
                            sys_mod.platform = "darwin"
                            sys_mod.executable = "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
                            with mock.patch("backend.architectos.vec_runtime.is_frozen", return_value=False):
                                with mock.patch("backend.architectos.vec_runtime._run", return_value=brew_proc) as run:
                                    with mock.patch("backend.architectos.vec_runtime._resolve_homebrew_python", return_value="/opt/homebrew/bin/python3"):
                                        with mock.patch("backend.architectos.vec_runtime._ensure_pip", return_value={"ok": True, "skipped": True, "command": [], "returncode": 0, "stdout": "", "stderr": "", "error": ""}):
                                            with mock.patch("backend.architectos.vec_runtime._pip_install", return_value={"ok": True, "command": [], "returncode": 0, "stdout": "", "stderr": "", "error": ""}):
                                                with mock.patch("backend.architectos.vec_runtime.preferred_python_path", return_value=pref):
                                                    with mock.patch("backend.architectos.vec_runtime._prepare_target_python", return_value=("/opt/homebrew/bin/python3", [])):
                                                        with mock.patch("backend.architectos.vec_runtime.retarget_macos_launch_agent", return_value={"ok": False, "skipped": True}):
                                                            result = apply_vector_runtime_repair({"confirm": True, "action": "install_homebrew_python"})
            self.assertTrue(result["ok"])
            self.assertEqual(run.call_args.args[0], ["/opt/homebrew/bin/brew", "install", "python"])
            self.assertTrue(run.call_args.kwargs.get("extra_env", {}).get("HOMEBREW_NO_AUTO_UPDATE"))


class VectorRuntimeCliTests(unittest.TestCase):
    def test_install_report_explains_why_and_fallback(self) -> None:
        from backend.architectos.vec_runtime import format_vector_runtime_report

        report = {
            "ok": False,
            "current": _current(frozen=True),
            "repair": {"fixable": False},
        }
        text = format_vector_runtime_report(report)
        self.assertIn("Memory search backend: python", text)
        self.assertIn("Ask/Search stay fast", text)
        self.assertIn("cannot be pip-patched", text)

    def test_cli_vector_runtime_json_does_not_discover_by_default(self) -> None:
        from backend.architectos.cli import main as cli_main

        buf = __import__("io").StringIO()
        with mock.patch("backend.architectos.vec_runtime.discover_capable_pythons", side_effect=AssertionError("discover")):
            with mock.patch("sys.stdout", buf):
                code = cli_main(["vector-runtime", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertIn("current", payload)
        self.assertIn("repair", payload)

    def test_require_env_fails_builder_probe_when_not_loaded(self) -> None:
        from backend.architectos.vec_runtime import emit_builder_probe

        with mock.patch("backend.architectos.vec_runtime.vector_runtime_status", return_value={"ok": False, "current": _current()}):
            with mock.patch.dict("os.environ", {"ARCHITECTOS_REQUIRE_SQLITE_VEC": "1"}):
                with mock.patch("sys.stderr", __import__("io").StringIO()):
                    self.assertEqual(emit_builder_probe(), 1)

    def test_frozen_repair_is_not_fixable(self) -> None:
        frozen = _current(frozen=True)
        repair = propose_repair(frozen, candidates=[], discovered=True)
        self.assertFalse(repair["fixable"])
        self.assertIsNone(repair["action"])
        self.assertEqual(repair["reason"], "frozen")
        self.assertIn("build time", repair["summary"])


class EmbeddablePipTests(unittest.TestCase):
    def test_embed_path_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            python_dir = Path(tmp) / "runtime" / "python"
            python_dir.mkdir(parents=True)
            exe = python_dir / "python.exe"
            exe.write_text("", encoding="utf-8")
            (python_dir / "python312._pth").write_text("python312.zip\n.\nimport site\n", encoding="ascii")
            self.assertTrue(is_embeddable_python(str(exe)))

    def test_missing_pip_not_writable_does_not_propose_doomed_pip(self) -> None:
        current = _current(
            load_extension=True,
            installed=False,
            loaded=False,
            reason="not-installed",
            pip=False,
            writable=False,
            embeddable=True,
            executable=r"C:\Users\1040755\AppData\Local\ArchitectOS\bin\runtime\python\python.exe",
        )
        with mock.patch("backend.architectos.vec_runtime._needs_isolated_venv", return_value=False):
            repair = propose_repair(current, candidates=[], discovered=True)
        self.assertFalse(repair["fixable"])
        self.assertIsNone(repair["action"])
        self.assertEqual(repair["reason"], "pip-missing")
        self.assertNotIn("-m pip", repair.get("command_preview") or "")

    def test_missing_pip_writable_proposes_ensurepip_then_install(self) -> None:
        current = _current(
            load_extension=True,
            installed=False,
            loaded=False,
            reason="not-installed",
            pip=False,
            writable=True,
            embeddable=True,
        )
        with mock.patch("backend.architectos.vec_runtime._needs_isolated_venv", return_value=False):
            repair = propose_repair(current, candidates=[], discovered=True)
        self.assertTrue(repair["fixable"])
        self.assertEqual(repair["action"], "install_sqlite_vec")
        self.assertTrue(repair["bootstrap_pip"])
        self.assertIn("ensurepip", repair["command_preview"])
        self.assertIn("sqlite-vec", repair["command_preview"])

    def test_missing_pip_repair_does_not_run_pip_when_bootstrap_fails(self) -> None:
        before = _current(
            load_extension=True,
            installed=False,
            loaded=False,
            reason="not-installed",
            pip=False,
            writable=True,
            embeddable=True,
        )
        ensure_fail = {
            "ok": False,
            "skipped": False,
            "command": ["python", "-m", "ensurepip", "--upgrade"],
            "returncode": 1,
            "stdout": "",
            "stderr": "No module named ensurepip",
            "error": PIP_MISSING_ERROR,
        }
        with mock.patch("backend.architectos.vec_runtime.probe_current_python", return_value=before):
            with mock.patch("backend.architectos.vec_runtime.is_frozen", return_value=False):
                with mock.patch("backend.architectos.vec_runtime._needs_isolated_venv", return_value=False):
                    with mock.patch("backend.architectos.vec_runtime._ensure_pip", return_value=ensure_fail):
                        with mock.patch("backend.architectos.vec_runtime._pip_install") as pip_install:
                            result = apply_vector_runtime_repair({"confirm": True, "action": "install_sqlite_vec"})
        self.assertFalse(result["ok"])
        self.assertIn("pip", result["error"].lower())
        self.assertNotIn("No module named pip", result["error"])
        pip_install.assert_not_called()

    def test_pip_install_refuses_when_pip_module_missing(self) -> None:
        from backend.architectos.vec_runtime import _pip_install

        with mock.patch("backend.architectos.vec_runtime._pip_module_available", return_value=False):
            with mock.patch("backend.architectos.vec_runtime._run") as run:
                result = _pip_install("/missing/python.exe")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], PIP_MISSING_ERROR)
        run.assert_not_called()

    def test_pep668_pip_error_is_explained(self) -> None:
        from backend.architectos.vec_runtime import PEP668_ERROR, _friendly_pip_error

        message = _friendly_pip_error("", "error: externally-managed-environment\nEXTERNALLY-MANAGED")
        self.assertEqual(message, PEP668_ERROR)

    def test_retarget_launch_agent_swaps_python_only(self) -> None:
        from backend.architectos.vec_runtime import retarget_macos_launch_agent

        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / "com.architectos.server.plist"
            target = Path(tmp) / "venv" / "bin" / "python"
            target.parent.mkdir(parents=True)
            plist.write_bytes(
                b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.architectos.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/python3.12</string>
        <string>/tmp/run_architectos.py</string>
        <string>--port</string>
        <string>8893</string>
    </array>
</dict>
</plist>
"""
            )
            with mock.patch("backend.architectos.vec_runtime.sys") as sys_mod:
                sys_mod.platform = "darwin"
                result = retarget_macos_launch_agent(str(target), plist_path=plist)
            self.assertTrue(result["ok"])
            self.assertEqual(result["previous"], "/usr/local/bin/python3.12")
            loaded = __import__("plistlib").loads(plist.read_bytes())
            self.assertEqual(loaded["ProgramArguments"][0], result["executable"])
            self.assertEqual(loaded["ProgramArguments"][1], "/tmp/run_architectos.py")

    def test_maybe_reexec_switches_to_preferred_python(self) -> None:
        from backend.architectos.vec_runtime import maybe_reexec_preferred_python

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "python"
            dest.write_text("#!/bin/sh\n", encoding="utf-8")
            dest.chmod(0o755)
            with mock.patch("backend.architectos.vec_runtime.is_frozen", return_value=False):
                with mock.patch("backend.architectos.vec_runtime._read_preferred_python", return_value=str(dest)):
                    with mock.patch("backend.architectos.vec_runtime.os.path.samefile", return_value=False):
                        with mock.patch("backend.architectos.vec_runtime.os.execv") as execv:
                            maybe_reexec_preferred_python()
        execv.assert_called_once()
        self.assertEqual(execv.call_args.args[0], str(dest))


if __name__ == "__main__":
    unittest.main()
