"""Agent-hook bridge so memory works without waiting for the agent to call a tool.

MCP only fires when the model decides to call something, which leaves a silent
agent with no memory at all. Cursor, Claude Code and Codex each expose lifecycle
hooks that fire on their own, so this module normalizes their event payloads into
one shape, forwards the turn to ``capture_memory_turn`` (over HTTP when the app is
running, straight through the service otherwise) and renders the reply in the form
each client accepts.

Capture works for every supported client. Injection only happens where the client
documents it: Claude Code and Codex add hook output back as model-visible context
on prompt submit and session start, while Cursor hooks have no such field and keep
relying on MCP for injection.

Every failure is silent by design: a broken memory hook must never break the agent.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .paths import is_frozen, resolve_project_root

CLIENTS: tuple[str, ...] = ("cursor", "claude", "codex")

EVENT_PROMPT = "prompt"
EVENT_RESPONSE = "response"
EVENT_SESSION = "session"
EVENTS: tuple[str, ...] = (EVENT_PROMPT, EVENT_RESPONSE, EVENT_SESSION)

# Marker that identifies our own registrations inside a shared hooks config.
MARKER = "architectos_hook"
_OURS_MARKERS: tuple[str, ...] = (
    MARKER,
    " architectos hook ",
    " hook capture --client",
    "-m architectos hook",
)

_RAW_EVENT_ALIASES: dict[str, str] = {
    # Cursor (.cursor/hooks.json)
    "beforesubmitprompt": EVENT_PROMPT,
    "afteragentresponse": EVENT_RESPONSE,
    # Claude Code and Codex share these names.
    "userpromptsubmit": EVENT_PROMPT,
    "stop": EVENT_RESPONSE,
    "stopfailure": EVENT_RESPONSE,
    "subagentstop": EVENT_RESPONSE,
    "sessionstart": EVENT_SESSION,
}

_USER_TEXT_FIELDS = ("prompt", "user_prompt", "user_message", "message", "text", "content")
_ASSISTANT_TEXT_FIELDS = (
    "last_assistant_message",
    "assistant_message",
    "agent_response",
    "response",
    "message",
    "text",
    "content",
)
_CWD_FIELDS = ("cwd", "workspace_root", "workspace_roots", "project_root", "root_path")
_SESSION_FIELDS = ("session_id", "sessionId", "conversation_id", "chat_id")

# Clients that document adding hook output back into the model's input.
_INJECTS: frozenset[tuple[str, str]] = frozenset(
    {("claude", EVENT_PROMPT), ("claude", EVENT_SESSION), ("codex", EVENT_PROMPT), ("codex", EVENT_SESSION)}
)
# Codex rejects plain text on Stop: valid JSON is required when the hook exits 0.
_JSON_REQUIRED: frozenset[tuple[str, str]] = frozenset({("codex", EVENT_RESPONSE)})

_INJECT_PREFIX = "ArchitectOS project memory (scoped project context, not user text):"
_INJECT_MAX_CHARS = 4000
_DEFAULT_TIMEOUT = 5.0

# Which client events we register, and the timeout each one gets.
_REGISTRATIONS: dict[str, tuple[tuple[str, str, int], ...]] = {
    "cursor": (
        ("beforeSubmitPrompt", EVENT_PROMPT, 10),
        ("afterAgentResponse", EVENT_RESPONSE, 10),
    ),
    "claude": (
        ("SessionStart", EVENT_SESSION, 10),
        ("UserPromptSubmit", EVENT_PROMPT, 10),
        ("Stop", EVENT_RESPONSE, 10),
    ),
    "codex": (
        ("SessionStart", EVENT_SESSION, 10),
        ("UserPromptSubmit", EVENT_PROMPT, 10),
        ("Stop", EVENT_RESPONSE, 10),
    ),
}


@dataclass(frozen=True)
class HookTurn:
    """One normalized agent event, independent of which client produced it."""

    client: str
    event: str
    raw_event: str = ""
    user_text: str = ""
    assistant_text: str = ""
    session_id: str = ""
    cwd: str = ""

    @property
    def can_inject(self) -> bool:
        return (self.client, self.event) in _INJECTS


def _debug(message: str) -> None:
    if os.environ.get("ARCHITECTOS_HOOK_DEBUG", "").strip() not in {"1", "true", "yes", "on"}:
        return
    try:
        path = resolve_project_root() / "data" / "hooks.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{message}\n")
    except OSError:
        pass


def _clean(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _clean(item)
            if text:
                return text
    return ""


def _first_field(payload: Mapping[str, Any], fields: tuple[str, ...]) -> str:
    for field in fields:
        text = _clean(payload.get(field))
        if text:
            return text
    return ""


def normalize_payload(payload: Mapping[str, Any], *, client: str, event: str | None = None) -> HookTurn | None:
    """Translate one client's hook JSON into a :class:`HookTurn`.

    ``event`` comes from the registered command line, which is more reliable than
    the payload because Cursor does not always name the event it fired. The
    payload's own event name is only a fallback.
    """
    resolved_client = str(client or "").strip().lower()
    if resolved_client not in CLIENTS:
        return None
    raw_event = _clean(payload.get("hook_event_name")) or _clean(payload.get("hook_event")) or _clean(payload.get("event"))
    resolved_event = str(event or "").strip().lower()
    if resolved_event not in EVENTS:
        resolved_event = _RAW_EVENT_ALIASES.get(raw_event.lower(), "")
    if resolved_event not in EVENTS:
        return None
    user_text = _first_field(payload, _USER_TEXT_FIELDS) if resolved_event == EVENT_PROMPT else ""
    assistant_text = _first_field(payload, _ASSISTANT_TEXT_FIELDS) if resolved_event == EVENT_RESPONSE else ""
    return HookTurn(
        client=resolved_client,
        event=resolved_event,
        raw_event=raw_event,
        user_text=user_text,
        assistant_text=assistant_text,
        session_id=_first_field(payload, _SESSION_FIELDS),
        cwd=_first_field(payload, _CWD_FIELDS) or os.getcwd(),
    )


# -- Transport ---------------------------------------------------------------
def runtime_endpoint(root: Path | None = None) -> tuple[str, str] | None:
    """Base URL and auth token of the running app, or None when it is not up."""
    base = root or resolve_project_root()
    path = base / "data" / "architectos.runtime.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    url = _clean(state.get("url"))
    if not url:
        host = _clean(state.get("host")) or "127.0.0.1"
        port = state.get("port")
        if not port:
            return None
        url = f"http://{host}:{port}"
    token = _clean(state.get("auth_token"))
    if not token:
        return None
    return url.rstrip("/"), token


def _request_json(url: str, token: str, *, payload: dict[str, Any] | None, timeout: float) -> dict[str, Any] | None:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    request.add_header("X-ArchitectOS-Token", token)
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - loopback only
            body = response.read().decode("utf-8")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _debug(f"http failed: {exc}")
        return None
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _offline_allowed() -> bool:
    return os.environ.get("ARCHITECTOS_HOOK_OFFLINE", "1").strip() not in {"0", "false", "no", "off"}


def _direct_service(root: Path | None = None):
    from .service import ArchitectOSService

    return ArchitectOSService(root or resolve_project_root())


def run_turn(turn: HookTurn, *, root: Path | None = None, timeout: float | None = None, limit: int = 8) -> dict[str, Any] | None:
    """Send the turn to ArchitectOS and return its memory pack, or None on failure."""
    wait = float(timeout if timeout is not None else os.environ.get("ARCHITECTOS_HOOK_TIMEOUT", _DEFAULT_TIMEOUT))
    endpoint = runtime_endpoint(root)
    if turn.event == EVENT_SESSION:
        if endpoint:
            url, token = endpoint
            query = urllib.parse.urlencode({"cwd": turn.cwd}) if turn.cwd else ""
            result = _request_json(f"{url}/api/memory/briefing?{query}", token, payload=None, timeout=wait)
            if result is not None:
                return result
        if not _offline_allowed():
            return None
        try:
            return _direct_service(root).memory_briefing(cwd=turn.cwd or None)
        except Exception as exc:  # noqa: BLE001 - hooks must stay silent
            _debug(f"briefing failed: {exc}")
            return None

    payload = {
        "user_text": turn.user_text,
        "assistant_text": turn.assistant_text,
        "cwd": turn.cwd,
        "client": turn.client,
        "limit": limit,
    }
    if endpoint:
        url, token = endpoint
        result = _request_json(f"{url}/api/memory/turn", token, payload=payload, timeout=wait)
        if result is not None:
            return result
    if not _offline_allowed():
        return None
    try:
        return _direct_service(root).capture_memory_turn(
            turn.user_text,
            turn.assistant_text,
            cwd=turn.cwd or None,
            client=turn.client,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001 - hooks must stay silent
        _debug(f"capture failed: {exc}")
        return None


# -- Rendering ---------------------------------------------------------------
def _has_memory(result: dict[str, Any] | None) -> bool:
    """True when the pack carries actual memory, not just its own header."""
    if not result:
        return False
    return bool(result.get("hits") or result.get("stable"))


def _silent(client: str, event: str | None) -> str:
    """Say nothing, in the form the client tolerates when the hook exits 0."""
    return "{}" if (client, str(event or "")) in _JSON_REQUIRED else ""


def render_stdout(turn: HookTurn, result: dict[str, Any] | None) -> str:
    """Client-specific stdout for one handled event ("" means stay silent)."""
    if not turn.can_inject:
        return _silent(turn.client, turn.event)
    context = str((result or {}).get("context") or "").strip()
    # An empty project would otherwise spend tokens on a pack that says "no hits".
    if not context or not _has_memory(result):
        return _silent(turn.client, turn.event)
    body = f"{_INJECT_PREFIX}\n\n{context}"
    if len(body) > _INJECT_MAX_CHARS:
        body = body[:_INJECT_MAX_CHARS].rstrip() + "\n[truncated]"
    hook_event_name = turn.raw_event or ("SessionStart" if turn.event == EVENT_SESSION else "UserPromptSubmit")
    return json.dumps({"hookSpecificOutput": {"hookEventName": hook_event_name, "additionalContext": body}})


def handle_capture(
    raw_stdin: str,
    *,
    client: str,
    event: str | None = None,
    root: Path | None = None,
) -> str:
    """Full capture path for one event: parse, forward, render. Never raises."""
    if os.environ.get("ARCHITECTOS_HOOK_DISABLED", "").strip() in {"1", "true", "yes", "on"}:
        return _silent(client, event)
    try:
        parsed = json.loads(raw_stdin) if raw_stdin.strip() else {}
    except ValueError:
        parsed = {}
    payload = parsed if isinstance(parsed, dict) else {}
    turn = normalize_payload(payload, client=client, event=event)
    if turn is None:
        _debug(f"ignored event client={client} event={event}")
        return _silent(client, event)
    if turn.event == EVENT_PROMPT and not turn.user_text:
        return _silent(turn.client, turn.event)
    if turn.event == EVENT_RESPONSE and not turn.assistant_text:
        return _silent(turn.client, turn.event)
    result = run_turn(turn, root=root)
    _debug(
        f"client={turn.client} event={turn.event} project={(result or {}).get('project_id')} "
        f"queued={len((result or {}).get('queued') or [])} auto={len((result or {}).get('auto_accepted') or [])}"
    )
    return render_stdout(turn, result)


# -- Installation ------------------------------------------------------------
def _is_ours_command(command: Any) -> bool:
    text = str(command or "")
    return any(marker in text for marker in _OURS_MARKERS)


def entry_script(root: Path | None = None) -> Path:
    """Absolute path of the repo-level hook entry point."""
    if root is not None:
        return (root / "architectos_hook.py").resolve()
    return (Path(__file__).resolve().parents[2] / "architectos_hook.py").resolve()


def hook_entry_available(*, script: Path | None = None) -> bool:
    """True when this process can write a hook command that will actually run."""
    if is_frozen():
        return Path(sys.executable).exists()
    if script is not None:
        return Path(script).exists()
    return True


def hook_command(client: str, event: str, *, python: str | None = None, script: Path | None = None) -> str:
    interpreter = python or sys.executable or "python3"
    if script is not None:
        return f'"{interpreter}" "{script}" capture --client {client} --event {event}'
    if is_frozen():
        return f'"{sys.executable}" hook capture --client {client} --event {event}'
    target = entry_script()
    if target.exists():
        return f'"{interpreter}" "{target}" capture --client {client} --event {event}'
    return f'"{interpreter}" -m architectos hook capture --client {client} --event {event}'


def config_path(client: str, *, project_root: Path | None = None) -> Path:
    """Where the client keeps its hook config (user scope unless project_root is set)."""
    base = project_root if project_root is not None else Path.home()
    if client == "cursor":
        return base / ".cursor" / "hooks.json"
    if client == "claude":
        return base / ".claude" / "settings.json"
    if client == "codex":
        return base / ".codex" / "hooks.json"
    raise ValueError(f"unknown client: {client}")


def _read_config(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _strip_ours_cursor(entries: Any) -> list[Any]:
    kept: list[Any] = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and _is_ours_command(entry.get("command")):
            continue
        kept.append(entry)
    return kept


def _strip_ours_nested(entries: Any) -> list[Any]:
    kept: list[Any] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            kept.append(entry)
            continue
        inner = [
            item
            for item in entry["hooks"]
            if not (isinstance(item, dict) and _is_ours_command(item.get("command")))
        ]
        if inner:
            updated = dict(entry)
            updated["hooks"] = inner
            kept.append(updated)
    return kept


def _apply(config: dict[str, Any], client: str, *, python: str | None, script: Path | None, remove: bool) -> dict[str, Any]:
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    if client == "cursor":
        config["version"] = config.get("version") or 1
    for event_name, event, timeout in _REGISTRATIONS[client]:
        if client == "cursor":
            entries = _strip_ours_cursor(hooks.get(event_name))
            if not remove:
                entries.append({"command": hook_command(client, event, python=python, script=script), "timeout": timeout})
        else:
            entries = _strip_ours_nested(hooks.get(event_name))
            if not remove:
                entries.append({
                    "hooks": [{
                        "type": "command",
                        "command": hook_command(client, event, python=python, script=script),
                        "timeout": timeout,
                    }],
                })
        if entries:
            hooks[event_name] = entries
        else:
            hooks.pop(event_name, None)
    if hooks:
        config["hooks"] = hooks
    else:
        config.pop("hooks", None)
    return config


def install(
    client: str,
    *,
    project_root: Path | None = None,
    python: str | None = None,
    script: Path | None = None,
    remove: bool = False,
) -> dict[str, Any]:
    """Register (or remove) our hooks in one client's config, preserving the rest."""
    if client not in CLIENTS:
        raise ValueError(f"unknown client: {client}")
    target = script if script is not None else entry_script()
    if not remove and script is not None and not Path(script).exists():
        raise FileNotFoundError(f"hook entry script not found: {target}")
    if not remove and script is None and not hook_entry_available():
        raise FileNotFoundError("hook entry is not available in this process")
    path = config_path(client, project_root=project_root)
    config = _apply(_read_config(path), client, python=python, script=script, remove=remove)
    _write_config(path, config)
    events = [event for _, event, _ in _REGISTRATIONS[client]]
    result: dict[str, Any] = {
        "client": client,
        "config": str(path),
        "events": [] if remove else events,
        "removed": bool(remove),
    }
    if client == "codex" and not remove:
        result["note"] = (
            "Codex only runs hooks you have trusted: open Codex and run /hooks to review and trust this entry. "
            "Also make sure [features].hooks is not set to false in ~/.codex/config.toml."
        )
    if client == "cursor" and not remove:
        result["note"] = (
            "Cursor hooks capture turns but cannot inject context back; keep the ArchitectOS MCP server "
            "registered so Cursor still receives memory in its prompt."
        )
    return result


def status(*, project_root: Path | None = None) -> dict[str, Any]:
    """What is installed where, plus whether the running app is reachable."""
    endpoint = runtime_endpoint()
    clients: list[dict[str, Any]] = []
    for client in CLIENTS:
        for scope, base in (("user", None), ("project", project_root)):
            if scope == "project" and project_root is None:
                continue
            path = config_path(client, project_root=base)
            raw_hooks = _read_config(path).get("hooks")
            hooks: dict[str, Any] = raw_hooks if isinstance(raw_hooks, dict) else {}
            installed = [
                event_name
                for event_name, entries in hooks.items()
                if _is_ours_command(json.dumps(entries or []))
            ]
            registered = _REGISTRATIONS[client]
            clients.append({
                "client": client,
                "scope": scope,
                "config": str(path),
                "exists": path.exists(),
                "installed_events": sorted(installed),
                "installed": len(installed) == len(registered),
                "events": [event_name for event_name, _, _ in registered],
                "injects": any((client, event) in _INJECTS for _, event, _ in registered),
            })
    return {
        "entry_script": str(sys.executable) if is_frozen() else str(entry_script()),
        "entry_script_exists": hook_entry_available(),
        "frozen": is_frozen(),
        "command": hook_command("codex", "prompt"),
        "server": {"reachable": bool(endpoint), "url": (endpoint or ("", ""))[0]},
        "offline_fallback": _offline_allowed(),
        "clients": clients,
    }


# -- CLI ---------------------------------------------------------------------
def expand_clients(raw: Any) -> list[str]:
    """Turn "all" / "codex,claude" / ["codex"] into a validated client list."""
    if isinstance(raw, (list, tuple, set)):
        requested = [str(item).strip().lower() for item in raw if str(item).strip()]
    else:
        value = str(raw or "").strip().lower()
        if value in {"", "all"}:
            return list(CLIENTS)
        requested = [item.strip() for item in value.split(",") if item.strip()]
    if not requested or "all" in requested:
        return list(CLIENTS)
    unknown = [item for item in requested if item not in CLIENTS]
    if unknown:
        raise ValueError(f"unknown client(s): {', '.join(unknown)}")
    return list(dict.fromkeys(requested))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="architectos_hook",
        description="Capture agent turns into ArchitectOS memory through client hooks.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture", help="Handle one hook event read as JSON on stdin.")
    capture.add_argument("--client", required=True, choices=list(CLIENTS))
    capture.add_argument("--event", choices=list(EVENTS), default=None)

    for name, help_text in (("install", "Register hooks."), ("uninstall", "Remove our hooks.")):
        action = sub.add_parser(name, help=help_text)
        action.add_argument("--client", default="all", help="cursor, claude, codex, a comma-separated list, or all.")
        action.add_argument("--project", action="store_true", help="Write to the repo config instead of the user config.")

    sub.add_parser("doctor", help="Show what is installed and whether the app is reachable.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "capture":
        try:
            raw = sys.stdin.read()
        except (OSError, ValueError):
            raw = ""
        try:
            out = handle_capture(raw, client=args.client, event=args.event)
        except Exception as exc:  # noqa: BLE001 - a hook must never break the agent
            _debug(f"capture crashed: {exc}")
            out = "{}" if args.client == "codex" and args.event == EVENT_RESPONSE else ""
        if out:
            sys.stdout.write(out)
        return 0

    if args.command == "doctor":
        print(json.dumps(status(project_root=resolve_project_root()), indent=2, sort_keys=True))
        return 0

    project_root = resolve_project_root() if args.project else None
    try:
        clients = expand_clients(args.client)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    failures = 0
    for client in clients:
        try:
            result = install(client, project_root=project_root, remove=args.command == "uninstall")
        except (OSError, ValueError) as exc:
            failures += 1
            print(f"{client}: {exc}", file=sys.stderr)
            continue
        action = "removed from" if result["removed"] else "installed into"
        events = ", ".join(result["events"]) or "-"
        print(f"{client}: {action} {result['config']} ({events})")
        note = result.get("note")
        if note:
            print(f"  note: {note}")
    return 1 if failures else 0
