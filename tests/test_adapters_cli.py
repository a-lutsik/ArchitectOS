from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.architectos.adapters_base import ProviderRequest
from backend.architectos.adapters_cli import (
    CliAdapter,
    _cli_command_with_prompt_arg,
    _cli_guard,
    _codex_session_auth_state,
    _ensure_gemini_oauth_personal_setting,
    _gemini_session_auth_state,
    _jwt_email_claim,
    _launch_cli_login_terminal,
    _normalize_cli_command,
    _provider_command,
    _provider_workdir,
    _which_cli,
)


def _jwt(email: str) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"{header}.{payload}.sig"


class JwtEmailClaimTests(unittest.TestCase):
    def test_extracts_email_from_id_token(self) -> None:
        self.assertEqual(_jwt_email_claim(_jwt("anton@example.com")), "anton@example.com")

    def test_rejects_garbage_and_non_email_claims(self) -> None:
        self.assertEqual(_jwt_email_claim(""), "")
        self.assertEqual(_jwt_email_claim("not.a.jwt"), "")
        self.assertEqual(_jwt_email_claim("a.b"), "")
        bad = base64.urlsafe_b64encode(b'{"preferred_username":"local"}').decode().rstrip("=")
        self.assertEqual(_jwt_email_claim(f"x.{bad}.y"), "")


class WhichCliTests(unittest.TestCase):
    def test_finds_agy_in_user_local_bin_when_path_is_short(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            agy = bin_dir / "agy"
            agy.write_text("#!/bin/sh\n", encoding="utf-8")
            agy.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
                with mock.patch(
                    "backend.architectos.adapters_cli._cli_extra_path_dirs",
                    return_value=[str(bin_dir)],
                ):
                    self.assertEqual(_which_cli("agy"), str(agy))

    def test_accepts_absolute_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agy = Path(tmp) / "agy"
            agy.write_text("#!/bin/sh\n", encoding="utf-8")
            agy.chmod(0o755)
            self.assertEqual(_which_cli(str(agy)), str(agy))


class ProviderCommandTests(unittest.TestCase):
    def test_list_and_string_commands(self) -> None:
        self.assertEqual(_provider_command({"command": ["agy", "-p"]}, ["default"]), ["agy", "-p"])
        self.assertEqual(_provider_command({"command": "agy -p"}, ["default"]), ["agy", "-p"])
        self.assertEqual(_provider_command({}, ["agy", "-p"]), ["agy", "-p"])

    def test_gemini_command_migrates_to_agy_when_present(self) -> None:
        with mock.patch("backend.architectos.adapters_cli.shutil.which", return_value="/bin/agy"):
            self.assertEqual(_normalize_cli_command(["gemini", "-p", "-"], ["agy"]), ["agy", "-p"])
            self.assertEqual(_normalize_cli_command(["gemini-cli", "--print"], ["agy"]), ["agy", "--print"])

    def test_gemini_command_stays_when_agy_missing(self) -> None:
        with mock.patch("backend.architectos.adapters_cli.shutil.which", return_value=None):
            self.assertEqual(_normalize_cli_command(["gemini", "-p"], ["agy"]), ["gemini", "-p"])


class PromptArgBuilderTests(unittest.TestCase):
    def test_attaches_prompt_and_print_timeout(self) -> None:
        with mock.patch("backend.architectos.adapters_cli.shutil.which", return_value=None):
            out = _cli_command_with_prompt_arg(["agy", "-p"], "hello", 90)
        self.assertEqual(out[:3], ["agy", "-p", "hello"])
        self.assertIn("--print-timeout", out)
        self.assertEqual(out[out.index("--print-timeout") + 1], "90s")

    def test_replaces_existing_prompt_argument(self) -> None:
        with mock.patch("backend.architectos.adapters_cli.shutil.which", return_value=None):
            out = _cli_command_with_prompt_arg(["agy", "--print", "old", "--flag"], "new", 30)
        self.assertEqual(out[:4], ["agy", "--print", "new", "--flag"])

    def test_migrates_legacy_gemini_executable(self) -> None:
        with mock.patch("backend.architectos.adapters_cli.shutil.which", return_value="/usr/bin/agy"):
            out = _cli_command_with_prompt_arg(["gemini", "-"], "prompt", 180)
        self.assertEqual(out[0], "agy")
        self.assertIn("prompt", out)


class WorkdirPolicyTests(unittest.TestCase):
    def test_default_policy_uses_project_root(self) -> None:
        root = Path("/tmp/project")
        workdir, error = _provider_workdir({}, root)
        self.assertEqual(workdir, root)
        self.assertEqual(error, "")

    def test_custom_workdir_must_stay_inside_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "src"
            nested.mkdir()
            ok, err = _provider_workdir({"workdir_policy": "custom", "workdir": str(nested)}, root)
            self.assertEqual(ok, nested.resolve())
            self.assertEqual(err, "")
            rejected, message = _provider_workdir(
                {"workdir_policy": "custom", "workdir": "/tmp"},
                root,
            )
            self.assertEqual(rejected, root.resolve())
            self.assertIn("outside", message)

    def test_missing_custom_workdir_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "nope"
            _, message = _provider_workdir({"workdir_policy": "custom", "workdir": str(missing)}, root)
            self.assertIn("does not exist", message)


class CliGuardTests(unittest.TestCase):
    def test_antigravity_print_mode_skips_approval(self) -> None:
        request = ProviderRequest(message="hi", context="", project_id="p", approved=False)
        with mock.patch("backend.architectos.adapters_cli.shutil.which", return_value="/bin/agy"):
            guard = _cli_guard({"id": "gemini-cli", "command": ["agy", "-p"], "approval_required": True}, request, Path("."))
        self.assertFalse(guard["error"])
        self.assertEqual(guard["command"][0], "/bin/agy")

    def test_requires_approval_by_default(self) -> None:
        request = ProviderRequest(message="hi", context="", project_id="p", approved=False)
        guard = _cli_guard({"command": ["echo"]}, request, Path("."))
        self.assertTrue(guard["error"])
        self.assertEqual(guard["status"], "approval_required")

    def test_missing_command_and_executable(self) -> None:
        request = ProviderRequest(message="hi", context="", project_id="p", approved=True)
        missing = _cli_guard({}, request, Path("."))
        self.assertEqual(missing["status"], "error")
        with mock.patch("backend.architectos.adapters_cli.shutil.which", return_value=None):
            no_exe = _cli_guard({"command": ["no-such-cli"], "approval_required": False}, request, Path("."))
        self.assertIn("not found", no_exe["text"])

    def test_ready_guard_resolves_executable_and_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = ProviderRequest(message="hi", context="", project_id="p", approved=True)
            with mock.patch("backend.architectos.adapters_cli.shutil.which", return_value="/bin/echo"):
                guard = _cli_guard({"command": ["echo", "ok"], "approval_required": False}, request, root)
            self.assertFalse(guard["error"])
            self.assertEqual(guard["command"][0], "/bin/echo")
            self.assertEqual(guard["workdir"], root)


class GeminiAuthStateTests(unittest.TestCase):
    def test_oauth_file_marks_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            gemini = home / ".gemini"
            gemini.mkdir()
            (gemini / "oauth_creds.json").write_text(
                json.dumps({"refresh_token": "r", "id_token": _jwt("me@x.com")}),
                encoding="utf-8",
            )
            with mock.patch("backend.architectos.adapters_cli.Path.home", return_value=home):
                with mock.patch.dict(os.environ, {}, clear=True):
                    state = _gemini_session_auth_state()
            self.assertTrue(state["ready"])
            self.assertEqual(state["method"], "google-account")
            self.assertEqual(state["email"], "me@x.com")

    def test_api_key_fallback_and_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch("backend.architectos.adapters_cli.Path.home", return_value=home):
                with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"}, clear=True):
                    keyed = _gemini_session_auth_state()
                with mock.patch.dict(os.environ, {}, clear=True):
                    empty = _gemini_session_auth_state()
            self.assertEqual(keyed["method"], "api-key")
            self.assertFalse(empty["ready"])

    def test_ensure_oauth_personal_setting_writes_selected_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch("backend.architectos.adapters_cli.Path.home", return_value=home):
                _ensure_gemini_oauth_personal_setting()
                path = home / ".gemini" / "settings.json"
                data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["security"]["auth"]["selectedType"], "oauth-personal")


class CodexAuthStateTests(unittest.TestCase):
    def test_auth_json_is_enough(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            codex = home / ".codex"
            codex.mkdir()
            (codex / "auth.json").write_text(
                json.dumps({"auth_mode": "chatgpt", "tokens": {"id_token": _jwt("a@b.c"), "access_token": "t"}}),
                encoding="utf-8",
            )
            with mock.patch("backend.architectos.adapters_cli.Path.home", return_value=home):
                state = _codex_session_auth_state("/bin/codex")
            self.assertTrue(state["ready"])
            self.assertEqual(state["method"], "chatgpt")
            self.assertEqual(state["email"], "a@b.c")

    def test_login_status_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            completed = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="Logged in using ChatGPT", stderr=""
            )
            with mock.patch("backend.architectos.adapters_cli.Path.home", return_value=home):
                with mock.patch("backend.architectos.adapters_cli.subprocess.run", return_value=completed):
                    state = _codex_session_auth_state("/bin/codex")
            self.assertTrue(state["ready"])
            self.assertEqual(state["method"], "chatgpt")

    def test_status_probe_failure_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch("backend.architectos.adapters_cli.Path.home", return_value=home):
                with mock.patch(
                    "backend.architectos.adapters_cli.subprocess.run",
                    side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=8),
                ):
                    state = _codex_session_auth_state("/bin/codex")
            self.assertFalse(state["ready"])


class LaunchLoginTerminalTests(unittest.TestCase):
    def test_darwin_uses_osascript(self) -> None:
        with mock.patch("backend.architectos.adapters_cli.platform.system", return_value="Darwin"):
            with mock.patch("backend.architectos.adapters_cli.subprocess.Popen") as popen:
                result = _launch_cli_login_terminal(shell_command="echo hi", title="Login")
        self.assertTrue(result["ok"])
        self.assertEqual(result["launcher"], "Terminal.app")
        self.assertEqual(popen.call_args.args[0][0], "osascript")

    def test_linux_without_terminal_reports_failure(self) -> None:
        with mock.patch("backend.architectos.adapters_cli.platform.system", return_value="Linux"):
            with mock.patch("backend.architectos.adapters_cli.shutil.which", return_value=None):
                result = _launch_cli_login_terminal(shell_command="echo hi", title="Login")
        self.assertFalse(result["ok"])
        self.assertIn("No graphical terminal", result["message"])

    def test_windows_uses_cmd_start(self) -> None:
        with mock.patch("backend.architectos.adapters_cli.platform.system", return_value="Windows"):
            with mock.patch("backend.architectos.adapters_cli.subprocess.Popen") as popen:
                result = _launch_cli_login_terminal(shell_command="echo hi", title="Login")
        self.assertTrue(result["ok"])
        self.assertEqual(result["launcher"], "cmd")
        self.assertEqual(popen.call_args.args[0][:3], ["cmd", "/c", "start"])


class CliAdapterBehaviorTests(unittest.TestCase):
    def test_check_reports_missing_cli(self) -> None:
        adapter = CliAdapter("agy", ["agy", "-p"])
        with mock.patch("backend.architectos.adapters_cli.shutil.which", return_value=None):
            result = adapter.check({"command": ["agy"]}, Path("."))
        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "missing_cli")

    def test_generic_check_ready_when_executable_exists(self) -> None:
        adapter = CliAdapter("custom-cli", ["echo"])
        with mock.patch("backend.architectos.adapters_cli.shutil.which", return_value="/bin/echo"):
            result = adapter.check({"command": ["echo"]}, Path("/tmp"))
        self.assertTrue(result["ready"])
        self.assertEqual(result["status"], "configured")

    def test_gemini_check_uses_auth_state(self) -> None:
        adapter = CliAdapter("gemini-cli", ["agy", "-p"])
        with mock.patch("backend.architectos.adapters_cli.shutil.which", return_value="/bin/agy"):
            with mock.patch(
                "backend.architectos.adapters_cli._gemini_session_auth_state",
                return_value={"ready": True, "method": "google-account", "email": "a@b.c"},
            ):
                ready = adapter.check({"command": ["agy", "-p"]}, Path("."))
            with mock.patch(
                "backend.architectos.adapters_cli._gemini_session_auth_state",
                return_value={"ready": False},
            ):
                missing = adapter.check({"command": ["agy", "-p"]}, Path("."))
        self.assertTrue(ready["ready"])
        self.assertIn("signed in", ready["message"])
        self.assertFalse(missing["ready"])
        self.assertEqual(missing["status"], "missing_credentials")

    def test_codex_check_and_login(self) -> None:
        adapter = CliAdapter("codex-cli", ["codex"])
        with mock.patch("backend.architectos.adapters_cli.shutil.which", return_value="/bin/codex"):
            with mock.patch(
                "backend.architectos.adapters_cli._codex_session_auth_state",
                return_value={"ready": False},
            ):
                check = adapter.check({"command": ["codex"]}, Path("."))
            with mock.patch(
                "backend.architectos.adapters_cli._launch_cli_login_terminal",
                return_value={"ok": True, "launcher": "Terminal.app", "message": "opened"},
            ):
                login = adapter.login({"command": ["codex"]}, Path("/tmp/project"))
        self.assertEqual(check["status"], "missing_credentials")
        self.assertTrue(login["ok"])
        self.assertEqual(login["status"], "login_started")

    def test_login_unsupported_for_generic_provider(self) -> None:
        adapter = CliAdapter("custom-cli", ["echo"])
        with mock.patch("backend.architectos.adapters_cli.shutil.which", return_value="/bin/echo"):
            result = adapter.login({"command": ["echo"]}, Path("."))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unsupported")

    def test_run_requires_approval_and_handles_timeout(self) -> None:
        adapter = CliAdapter("custom-cli", ["echo"])
        request = ProviderRequest(message="hi", context="", project_id="p", approved=False)
        blocked = adapter.run({"command": ["echo"]}, request, Path("."))
        self.assertEqual(blocked["status"], "approval_required")

        approved = ProviderRequest(message="hi", context="", project_id="p", approved=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("backend.architectos.adapters_cli.shutil.which", return_value="/bin/echo"):
                with mock.patch(
                    "backend.architectos.adapters_cli.subprocess.run",
                    side_effect=subprocess.TimeoutExpired(cmd="echo", timeout=1),
                ):
                    timed_out = adapter.run(
                        {"command": ["echo"], "approval_required": False, "timeout_seconds": 1},
                        approved,
                        root,
                    )
        self.assertEqual(timed_out["status"], "timeout")

    def test_run_ok_and_stream_for_prompt_arg_provider(self) -> None:
        adapter = CliAdapter("gemini-cli", ["agy", "-p"])
        request = ProviderRequest(message="hi", context="", project_id="p", approved=True)
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="answer", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("backend.architectos.adapters_cli.shutil.which", return_value="/bin/agy"):
                with mock.patch("backend.architectos.adapters_cli.subprocess.run", return_value=completed):
                    result = adapter.run({"command": ["agy", "-p"], "approval_required": False}, request, root)
                    events = list(
                        adapter.stream({"command": ["agy", "-p"], "approval_required": False}, request, root)
                    )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["text"], "answer")
        self.assertEqual(events[0]["type"], "delta")
        self.assertEqual(events[-1]["type"], "done")


    def test_gemini_login_skips_tui_when_already_signed_in(self) -> None:
        adapter = CliAdapter("gemini-cli", ["agy", "-p"])
        with mock.patch("backend.architectos.adapters_cli.shutil.which", return_value="/bin/agy"):
            with mock.patch(
                "backend.architectos.adapters_cli._gemini_session_auth_state",
                return_value={"ready": True, "method": "google-account", "email": "a@b.c"},
            ):
                with mock.patch("backend.architectos.adapters_cli._launch_cli_login_terminal") as launch:
                    login = adapter.login({"command": ["agy", "-p"]}, Path("/tmp"))
        self.assertTrue(login["ok"])
        self.assertEqual(login["status"], "already_signed_in")
        self.assertIn("do not need to launch", login["message"])
        launch.assert_not_called()

    def test_gemini_login_and_run_oserror(self) -> None:
        adapter = CliAdapter("gemini-cli", ["agy", "-p"])
        with mock.patch("backend.architectos.adapters_cli.shutil.which", return_value="/bin/agy"):
            with mock.patch(
                "backend.architectos.adapters_cli._gemini_session_auth_state",
                return_value={"ready": False},
            ):
                with mock.patch(
                    "backend.architectos.adapters_cli._launch_cli_login_terminal",
                    return_value={"ok": True, "launcher": "Terminal.app", "message": "opened"},
                ):
                    login = adapter.login({"command": ["agy", "-p"]}, Path("/tmp"))
            request = ProviderRequest(message="hi", context="", project_id="p", approved=True)
            with mock.patch(
                "backend.architectos.adapters_cli.subprocess.run",
                side_effect=OSError("exec format error"),
            ):
                failed = adapter.run({"command": ["agy", "-p"], "approval_required": False}, request, Path("/tmp"))
        self.assertTrue(login["ok"])
        self.assertEqual(failed["status"], "error")
        self.assertIn("exec format error", failed["text"])

    def test_codex_ready_message_includes_email(self) -> None:
        adapter = CliAdapter("codex-cli", ["codex"])
        with mock.patch("backend.architectos.adapters_cli.shutil.which", return_value="/bin/codex"):
            with mock.patch(
                "backend.architectos.adapters_cli._codex_session_auth_state",
                return_value={"ready": True, "method": "chatgpt", "email": "a@b.c"},
            ):
                ready = adapter.check({"command": ["codex"]}, Path("."))
        self.assertTrue(ready["ready"])
        self.assertIn("a@b.c", ready["message"])

    def test_stream_surfaces_guard_errors(self) -> None:
        adapter = CliAdapter("custom-cli", ["echo"])
        request = ProviderRequest(message="hi", context="", project_id="p", approved=False)
        events = list(adapter.stream({"command": ["echo"]}, request, Path(".")))
        self.assertEqual(events[0]["type"], "delta")
        self.assertEqual(events[-1]["result"]["status"], "approval_required")


if __name__ == "__main__":
    unittest.main()
