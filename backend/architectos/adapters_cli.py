"""CLI-backed provider adapter: runs local agent binaries as subprocesses.

Split out of ``adapters``, which now covers only the HTTP provider families and
the router. This module owns subprocess orchestration and the session/auth state
probing for the Gemini and Codex CLIs. Names are re-exported from ``adapters``
for backward-compatible imports and existing mock.patch targets.
"""

from __future__ import annotations

import base64
import json
import os
import platform
import queue
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .adapters_base import ProviderAdapter, ProviderRequest


class CliAdapter(ProviderAdapter):
    def __init__(self, provider_id: str, default_command: list[str]) -> None:
        self.provider_id = provider_id
        self.default_command = default_command

    def check(self, provider: dict[str, Any], project_root: Path) -> dict[str, Any]:
        command = _provider_command(provider, self.default_command)
        if not command:
            return {
                "provider_id": self.provider_id,
                "ready": False,
                "status": "missing_command",
                "message": "CLI command is not configured.",
                "hint": "Set the command field to the CLI invocation ArchitectOS should run.",
                "actions": ["Set command", "Run provider test again"],
                "details": {},
            }
        executable = shutil.which(command[0])
        if not executable:
            return {
                "provider_id": self.provider_id,
                "ready": False,
                "status": "missing_cli",
                "message": f"CLI executable not found: {command[0]}",
                "hint": f"Install {command[0]} or update the command to an executable on PATH.",
                "actions": [f"Install {command[0]}", "Update command", "Restart ArchitectOS if PATH changed"],
                "details": {"command": command},
            }
        if self.provider_id == "gemini-cli":
            return self._check_gemini_auth(executable, command, project_root)
        if self.provider_id == "codex-cli":
            return self._check_codex_auth(executable, command, project_root)
        return {
            "provider_id": self.provider_id,
            "ready": True,
            "status": "configured",
            "message": f"CLI executable is available: {executable}",
            "hint": "CLI is available. Use a short chat prompt to verify auth/session state.",
            "actions": ["Send a short chat prompt", "Review stderr if execution fails"],
            "details": {"command": [executable, *command[1:]], "workdir": str(project_root)},
        }

    def login(self, provider: dict[str, Any], project_root: Path) -> dict[str, Any]:
        command = _provider_command(provider, self.default_command)
        if not command:
            return {"provider_id": self.provider_id, "ok": False, "status": "missing_command", "message": "CLI command is not configured."}
        executable = shutil.which(command[0])
        if not executable:
            return {
                "provider_id": self.provider_id,
                "ok": False,
                "status": "missing_cli",
                "message": f"CLI executable not found: {command[0]}",
                "hint": f"Install {command[0]} first, then sign in again.",
            }
        if self.provider_id == "gemini-cli":
            return self._start_gemini_login(executable, project_root)
        if self.provider_id == "codex-cli":
            return self._start_codex_login(executable, project_root)
        return {
            "provider_id": self.provider_id,
            "ok": False,
            "status": "unsupported",
            "message": "This provider does not support in-app session login.",
            "hint": "Configure API credentials in the environment, or install and authenticate the CLI manually.",
        }

    def _check_gemini_auth(self, executable: str, command: list[str], project_root: Path) -> dict[str, Any]:
        auth = _gemini_session_auth_state()
        details = {
            "command": [executable, *command[1:]],
            "workdir": str(project_root),
            "auth_method": auth.get("method") or "",
            "auth_email": auth.get("email") or "",
            "supports_login": True,
            "login_label": "Sign in with Google",
            "cli": "antigravity",
        }
        if auth.get("ready"):
            method = str(auth.get("method") or "session")
            email = str(auth.get("email") or "")
            if method == "google-account":
                who = f" as {email}" if email else ""
                message = f"Antigravity CLI (agy) is signed in with Google{who}."
                hint = "Google account session is ready for ArchitectOS chats. Gemini CLI API keys are no longer required."
            else:
                message = "Antigravity CLI can use an environment API token, but Google sign-in is preferred."
                hint = "Prefer Sign in with Google. GEMINI_API_KEY is ignored by agy."
            return {
                "provider_id": self.provider_id,
                "ready": True,
                "status": "configured",
                "message": message,
                "hint": hint,
                "actions": ["Send a short chat prompt", "Sign in with Google again to refresh session"],
                "details": details,
            }
        return {
            "provider_id": self.provider_id,
            "ready": False,
            "status": "missing_credentials",
            "message": "Antigravity CLI (agy) is installed, but no Google account session was found.",
            "hint": "Click Sign in with Google, choose Google OAuth in the terminal, paste the browser code, then Test again.",
            "actions": ["Sign in with Google", "Run provider test again"],
            "details": details,
        }

    def _start_gemini_login(self, executable: str, project_root: Path) -> dict[str, Any]:
        launched = _launch_cli_login_terminal(
            shell_command=(
                f'cd {shlex.quote(str(project_root))} && '
                f'echo "Antigravity login: choose Google OAuth, then paste the browser code if asked." && '
                f'{shlex.quote(executable)}'
            ),
            title="Antigravity Sign in with Google",
        )
        return {
            "provider_id": self.provider_id,
            "ok": bool(launched.get("ok")),
            "status": "login_started" if launched.get("ok") else "login_failed",
            "message": launched.get("message") or "Started Antigravity Google sign-in.",
            "hint": "In the opened terminal choose Google OAuth, finish the browser flow (paste the code if prompted), then click Test in ArchitectOS.",
            "details": {
                "auth_method": "google-account",
                "supports_login": True,
                "login_label": "Sign in with Google",
                "launcher": launched.get("launcher") or "",
                "cli": "antigravity",
            },
        }

    def _check_codex_auth(self, executable: str, command: list[str], project_root: Path) -> dict[str, Any]:
        auth = _codex_session_auth_state(executable)
        details = {
            "command": [executable, *command[1:]],
            "workdir": str(project_root),
            "auth_method": auth.get("method") or "",
            "auth_email": auth.get("email") or "",
            "supports_login": True,
            "login_label": "Sign in with ChatGPT",
        }
        if auth.get("ready"):
            method = str(auth.get("method") or "session")
            email = str(auth.get("email") or "")
            who = f" ({email})" if email else ""
            label = "ChatGPT" if method == "chatgpt" else method.replace("-", " ")
            return {
                "provider_id": self.provider_id,
                "ready": True,
                "status": "configured",
                "message": f"Codex CLI is signed in via {label}{who}.",
                "hint": "Account session is ready for ArchitectOS chats.",
                "actions": ["Send a short chat prompt", "Sign in with ChatGPT again to refresh session"],
                "details": details,
            }
        return {
            "provider_id": self.provider_id,
            "ready": False,
            "status": "missing_credentials",
            "message": "Codex CLI is installed, but no ChatGPT/API session was found.",
            "hint": "Click Sign in with ChatGPT, complete browser login, then Test again.",
            "actions": ["Sign in with ChatGPT", "Run provider test again"],
            "details": details,
        }

    def _start_codex_login(self, executable: str, project_root: Path) -> dict[str, Any]:
        launched = _launch_cli_login_terminal(
            shell_command=f'cd {shlex.quote(str(project_root))} && {shlex.quote(executable)} login',
            title="Codex Sign in with ChatGPT",
        )
        return {
            "provider_id": self.provider_id,
            "ok": bool(launched.get("ok")),
            "status": "login_started" if launched.get("ok") else "login_failed",
            "message": launched.get("message") or "Started Codex ChatGPT sign-in.",
            "hint": "Complete the browser login in the opened terminal, then click Test in ArchitectOS.",
            "details": {
                "auth_method": "chatgpt",
                "supports_login": True,
                "login_label": "Sign in with ChatGPT",
                "launcher": launched.get("launcher") or "",
            },
        }

    def _uses_prompt_arg(self) -> bool:
        return self.provider_id == "gemini-cli"

    def run(self, provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> dict[str, Any]:
        guard = _cli_guard(provider, request, project_root)
        if guard.get("error"):
            return {"provider_id": self.provider_id, "status": guard["status"], "text": guard["text"], "raw": guard}
        prompt = self.build_prompt(request)
        command = list(guard["command"])
        stdin_text = None
        if self._uses_prompt_arg():
            command = _cli_command_with_prompt_arg(command, prompt, int(provider.get("timeout_seconds") or 180))
        else:
            stdin_text = prompt
        try:
            proc = subprocess.run(
                command,
                input=stdin_text,
                text=True,
                capture_output=True,
                cwd=str(guard["workdir"]),
                timeout=int(provider.get("timeout_seconds") or 180),
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            return {"provider_id": self.provider_id, "status": "timeout", "text": f"CLI timed out after {exc.timeout} seconds.", "raw": {"command": command, "workdir": str(guard["workdir"])}}
        except OSError as exc:
            return {"provider_id": self.provider_id, "status": "error", "text": f"CLI execution failed: {exc}", "raw": {"command": command, "workdir": str(guard["workdir"])}}
        output = (proc.stdout or "").strip()
        error = (proc.stderr or "").strip()
        status = "ok" if proc.returncode == 0 else "error"
        text = output if output else error
        return {"provider_id": self.provider_id, "status": status, "text": text, "raw": {"returncode": proc.returncode, "stderr": error, "command": command, "workdir": str(guard["workdir"])}}

    def stream(self, provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> Iterator[dict[str, Any]]:
        if self._uses_prompt_arg():
            result = self.run(provider, request, project_root)
            text = str(result.get("text") or "")
            if text:
                yield {"type": "delta", "text": text}
            yield {"type": "done", "result": result}
            return
        guard = _cli_guard(provider, request, project_root)
        if guard.get("error"):
            result = {"provider_id": self.provider_id, "status": guard["status"], "text": guard["text"], "raw": guard}
            yield {"type": "delta", "text": result["text"]}
            yield {"type": "done", "result": result}
            return
        command = list(guard["command"])
        chunks: list[str] = []
        stderr = ""
        returncode = 1
        status = "error"
        started = time.monotonic()
        timeout = int(provider.get("timeout_seconds") or 180)
        output_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(guard["workdir"]),
                shell=False,
            )
            assert proc.stdin is not None
            assert proc.stdout is not None
            assert proc.stderr is not None

            stdout_stream = proc.stdout

            def pump_stdout() -> None:
                while True:
                    char = stdout_stream.read(1)
                    if not char:
                        break
                    output_queue.put(("stdout", char))

            reader = threading.Thread(target=pump_stdout, daemon=True)
            reader.start()
            proc.stdin.write(self.build_prompt(request))
            proc.stdin.close()
            while proc.poll() is None or not output_queue.empty():
                if request.cancel_requested and request.cancel_requested():
                    proc.terminate()
                    status = "cancelled"
                    break
                if time.monotonic() - started > timeout:
                    proc.kill()
                    status = "timeout"
                    break
                try:
                    kind, chunk = output_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if kind == "stdout":
                    chunks.append(chunk)
                    yield {"type": "delta", "text": chunk}
            try:
                returncode = proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                returncode = proc.wait()
                status = "timeout"
            stderr = proc.stderr.read().strip()
        except OSError as exc:
            result = {"provider_id": self.provider_id, "status": "error", "text": f"CLI execution failed: {exc}", "raw": {"command": command, "workdir": str(guard["workdir"])}}
            yield {"type": "delta", "text": result["text"]}
            yield {"type": "done", "result": result}
            return
        if status not in {"cancelled", "timeout"}:
            status = "ok" if returncode == 0 else "error"
        text = "".join(chunks).strip() or stderr
        yield {"type": "done", "result": {"provider_id": self.provider_id, "status": status, "text": text, "raw": {"returncode": returncode, "stderr": stderr, "command": command, "workdir": str(guard["workdir"])}}}


def _cli_guard(provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> dict[str, Any]:
    if bool(provider.get("approval_required", True)) and not request.approved:
        return {"error": True, "status": "approval_required", "text": "CLI run approval is required before ArchitectOS can execute this provider.", "approval_required": True}
    command = _provider_command(provider, [])
    if not command:
        return {"error": True, "status": "error", "text": "CLI command is not configured."}
    executable = shutil.which(command[0])
    if not executable:
        return {"error": True, "status": "error", "text": f"CLI executable not found: {command[0]}", "command": command}
    command[0] = executable
    workdir, error = _provider_workdir(provider, project_root)
    if error:
        return {"error": True, "status": "workdir_rejected", "text": error, "command": command}
    return {"error": False, "command": command, "workdir": workdir}


def _jwt_email_claim(token: str | None) -> str:
    raw = str(token or "").strip()
    if not raw or raw.count(".") < 2:
        return ""
    try:
        payload = raw.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return ""
    email = str(data.get("email") or data.get("preferred_username") or "").strip()
    return email if "@" in email else ""


def _gemini_home() -> Path:
    return Path.home() / ".gemini"


def _gemini_oauth_path() -> Path:
    return _gemini_home() / "oauth_creds.json"


def _gemini_settings_path() -> Path:
    return _gemini_home() / "settings.json"


def _gemini_session_auth_state() -> dict[str, Any]:
    api_key = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or os.environ.get("ANTIGRAVITY_TOKEN"))
    oauth_path = _gemini_oauth_path()
    oauth_ready = False
    email = ""
    if oauth_path.is_file():
        try:
            data = json.loads(oauth_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if isinstance(data, dict) and (data.get("refresh_token") or data.get("access_token")):
            oauth_ready = True
            email = _jwt_email_claim(str(data.get("id_token") or ""))
    accounts_path = _gemini_home() / "google_accounts.json"
    if accounts_path.is_file() and not email:
        try:
            accounts = json.loads(accounts_path.read_text(encoding="utf-8"))
            active = accounts.get("active") if isinstance(accounts, dict) else None
            if isinstance(active, dict):
                email = str(active.get("email") or active.get("account") or "").strip()
            elif isinstance(active, str) and "@" in active:
                email = active.strip()
        except Exception:
            pass
    selected = ""
    settings_path = _gemini_settings_path()
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            selected = str((((settings or {}).get("security") or {}).get("auth") or {}).get("selectedType") or "")
        except Exception:
            selected = ""
    # Antigravity uses the same ~/.gemini oauth session.
    if oauth_ready:
        return {"ready": True, "method": "google-account", "email": email, "selected_type": selected or "oauth-personal"}
    if api_key:
        return {"ready": True, "method": "api-key", "email": "", "selected_type": selected or "api-key"}
    return {"ready": False, "method": "", "email": "", "selected_type": selected}


def _ensure_gemini_oauth_personal_setting() -> None:
    path = _gemini_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    settings: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                settings = loaded
        except Exception:
            settings = {}
    security = settings.get("security")
    if not isinstance(security, dict):
        security = {}
        settings["security"] = security
    auth = security.get("auth")
    if not isinstance(auth, dict):
        auth = {}
        security["auth"] = auth
    auth["selectedType"] = "oauth-personal"
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def _codex_session_auth_state(executable: str) -> dict[str, Any]:
    auth_path = Path.home() / ".codex" / "auth.json"
    email = ""
    method = ""
    if auth_path.is_file():
        try:
            data = json.loads(auth_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if isinstance(data, dict):
            method = str(data.get("auth_mode") or "").strip().lower()
            tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
            email = _jwt_email_claim(str((tokens or {}).get("id_token") or ""))
            if method or (tokens or {}).get("access_token") or (tokens or {}).get("refresh_token") or data.get("OPENAI_API_KEY"):
                if not method:
                    method = "api-key" if data.get("OPENAI_API_KEY") else "session"
                return {"ready": True, "method": method, "email": email}
    try:
        proc = subprocess.run(
            [executable, "login", "status"],
            text=True,
            capture_output=True,
            timeout=8,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"ready": False, "method": "", "email": ""}
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}".strip()
    lower = text.lower()
    if proc.returncode == 0 and ("logged in" in lower or "chatgpt" in lower or "authenticated" in lower):
        if "chatgpt" in lower:
            method = "chatgpt"
        elif "api" in lower:
            method = "api-key"
        else:
            method = method or "session"
        return {"ready": True, "method": method, "email": email}
    return {"ready": False, "method": "", "email": ""}


def _launch_cli_login_terminal(*, shell_command: str, title: str) -> dict[str, Any]:
    system = platform.system().lower()
    try:
        if system == "darwin":
            script = (
                f'tell application "Terminal"\n'
                f'activate\n'
                f'do script {json.dumps(shell_command)}\n'
                f'end tell'
            )
            subprocess.Popen(["osascript", "-e", script], shell=False)
            return {"ok": True, "launcher": "Terminal.app", "message": f"Opened Terminal for {title}."}
        if system == "linux":
            for candidate in ("x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal", "xterm"):
                exe = shutil.which(candidate)
                if not exe:
                    continue
                if candidate == "gnome-terminal":
                    subprocess.Popen([exe, "--", "bash", "-lc", shell_command], shell=False)
                else:
                    subprocess.Popen([exe, "-e", f"bash -lc {shlex.quote(shell_command)}"], shell=False)
                return {"ok": True, "launcher": candidate, "message": f"Opened {candidate} for {title}."}
            return {
                "ok": False,
                "launcher": "",
                "message": "No graphical terminal found. Run the CLI login command manually in your shell.",
            }
        if system == "windows":
            subprocess.Popen(["cmd", "/c", "start", "cmd", "/k", shell_command], shell=False)
            return {"ok": True, "launcher": "cmd", "message": f"Opened Command Prompt for {title}."}
    except OSError as exc:
        return {"ok": False, "launcher": "", "message": f"Could not open login terminal: {exc}"}
    return {
        "ok": False,
        "launcher": "",
        "message": f"Unsupported OS for automatic login terminal launch ({system}). Run the CLI login command manually.",
    }


def _provider_workdir(provider: dict[str, Any], project_root: Path) -> tuple[Path, str]:
    policy = str(provider.get("workdir_policy") or "project-root")
    if policy != "custom":
        return project_root, ""
    raw = str(provider.get("workdir") or "").strip()
    if not raw:
        return project_root, ""
    candidate = Path(raw).expanduser().resolve()
    root = project_root.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return root, f"CLI workdir rejected by policy: {candidate} is outside {root}."
    if not candidate.exists() or not candidate.is_dir():
        return root, f"CLI workdir does not exist: {candidate}."
    return candidate, ""


def _cli_command_with_prompt_arg(command: list[str], prompt: str, timeout_seconds: int) -> list[str]:
    """Build an agy/gemini-style invocation where the prompt is a -p/--print argument."""
    cleaned = [part for part in command if part != "-"]
    if not cleaned:
        cleaned = ["agy", "-p"]
    # Migrate legacy gemini executable to agy when present on PATH.
    if cleaned[0] in {"gemini", "gemini-cli"} and shutil.which("agy"):
        cleaned[0] = "agy"
    flags = {"-p", "--print", "--prompt"}
    out: list[str] = []
    prompt_attached = False
    index = 0
    while index < len(cleaned):
        token = cleaned[index]
        out.append(token)
        if token in flags:
            next_token = cleaned[index + 1] if index + 1 < len(cleaned) else None
            if next_token is not None and not str(next_token).startswith("-"):
                out.append(prompt)
                prompt_attached = True
                index += 2
                continue
            out.append(prompt)
            prompt_attached = True
            index += 1
            continue
        index += 1
    if not prompt_attached:
        out.extend(["-p", prompt])
    # Prefer a print timeout close to ArchitectOS provider timeout.
    if "--print-timeout" not in out:
        seconds = max(30, int(timeout_seconds or 180))
        out.extend(["--print-timeout", f"{seconds}s"])
    return out


def _provider_command(provider: dict[str, Any], default_command: list[str]) -> list[str]:
    if isinstance(provider.get("command"), list):
        command = [str(part) for part in provider["command"] if str(part)]
        if command:
            return _normalize_cli_command(command, default_command)
    if isinstance(provider.get("command"), str) and provider["command"].strip():
        return _normalize_cli_command(provider["command"].strip().split(), default_command)
    return list(default_command)


def _normalize_cli_command(command: list[str], default_command: list[str]) -> list[str]:
    if not command:
        return list(default_command)
    # Auto-migrate stored Gemini CLI commands to Antigravity (agy).
    if command[0] in {"gemini", "gemini-cli"}:
        if shutil.which("agy"):
            rest = [part for part in command[1:] if part != "-"]
            if not rest or rest == ["-p"] or rest == ["-p", "-"]:
                return ["agy", "-p"]
            return ["agy", *rest]
        return command
    return command
