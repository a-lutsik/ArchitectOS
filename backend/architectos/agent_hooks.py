"""Agent-hook bridge so memory works without waiting for the agent to call a tool.

MCP only fires when the model decides to call something, which leaves a silent
agent with no memory at all. Cursor, Claude Code and Codex each expose lifecycle
hooks that fire on their own, so this module normalizes their event payloads into
one shape, forwards the turn to ``capture_memory_turn`` over HTTP when the app is
running (otherwise it injects from the app's briefing cache and spools the capture
for the app to replay) and renders the reply in the form each client accepts.

Capture works for every supported client. Injection only happens where the client
documents it: Claude Code and Codex add hook output back as model-visible context
on prompt submit and session start, while Cursor hooks have no such field and keep
relying on MCP for injection.

Every failure is silent by design: a broken memory hook must never break the agent.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Mapping, NamedTuple

try:
    import fcntl
except ImportError:  # Windows: msvcrt byte locks instead
    fcntl = None  # type: ignore[assignment]
msvcrt: Any = None
if fcntl is None:
    try:
        import msvcrt as _msvcrt
    except ImportError:
        pass
    else:
        msvcrt = _msvcrt

from .paths import is_frozen, resolve_project_root
from .usage import estimate_tokens_from_text

if TYPE_CHECKING:
    import argparse
    import logging

_LOGGER: logging.Logger | None = None
# Set by the hook command: this interpreter runs one agent event and exits.
_STANDALONE = False


class _HookLog:
    """Log lines of a standalone hook process.

    Nothing configures logging there, so through ``logging`` these lines would
    reach only a NullHandler while every agent turn paid its ~10 ms import. With
    ARCHITECTOS_HOOK_DEBUG they join data/hooks.log (counts only), otherwise they
    are dropped, as before.
    """

    def info(self, message: str, *args: object) -> None:
        _debug(f"INFO {message % args}")

    def warning(self, message: str, *args: object) -> None:
        _debug(f"WARNING {message % args}")

    def debug(self, message: str, *args: object) -> None:
        _debug(f"DEBUG {message % args}")


def _log() -> logging.Logger | _HookLog:
    """The hook's logger: the module logger inside the app, a plain hooks.log writer in the hook command."""
    global _LOGGER
    if _STANDALONE:
        return _HookLog()
    if _LOGGER is None:
        import logging

        _LOGGER = logging.getLogger(__name__)
        _LOGGER.addHandler(logging.NullHandler())
    return _LOGGER


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
# SessionStart sources that open a fresh context window: what was injected before is gone.
_RESET_SOURCES = frozenset({"clear", "compact"})

# Clients that document adding hook output back into the model's input.
_INJECTS: frozenset[tuple[str, str]] = frozenset(
    {("claude", EVENT_PROMPT), ("claude", EVENT_SESSION), ("codex", EVENT_PROMPT), ("codex", EVENT_SESSION)}
)
# Codex rejects plain text on Stop: valid JSON is required when the hook exits 0.
_JSON_REQUIRED: frozenset[tuple[str, str]] = frozenset({("codex", EVENT_RESPONSE)})

_INJECT_PREFIX = "ArchitectOS project memory (scoped project context, not user text):"
_INJECT_MAX_CHARS = 4000


def hook_pack_budget(injection_budget: int) -> int:
    """Pack budget that keeps prefix + pack within ``agent.hook_char_budget``.

    32 characters of slack cover what pack_memory_context may add past its
    budget (its "[context budget reached]" marker and section-header accounting).
    """
    return injection_budget - len(_INJECT_PREFIX) - 2 - 32
# A pack is either ready fast or not worth waiting for; capture may take longer.
_INJECT_TIMEOUT = 2.0
_CAPTURE_TIMEOUT = 5.0

# Per-session injection state: data/hook_sessions/<digest of session_id>.json.
_SESSION_DIR = "hook_sessions"
_SESSION_TTL_S = 3 * 24 * 3600.0
_STABLE_EVERY_TURNS = 20
# The question waits in the session file until turn end captures it with the answer.
_PENDING_PROMPT_MAX_CHARS = 20000

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


class HookTurn(NamedTuple):
    """One normalized agent event, independent of which client produced it.

    A NamedTuple rather than a dataclass: dataclasses pulls in inspect, ~20 ms
    of import on every hook run.
    """

    client: str
    event: str
    raw_event: str = ""
    user_text: str = ""
    assistant_text: str = ""
    session_id: str = ""
    cwd: str = ""
    source: str = ""

    @property
    def can_inject(self) -> bool:
        return (self.client, self.event) in _INJECTS


def _debug_enabled() -> bool:
    return os.environ.get("ARCHITECTOS_HOOK_DEBUG", "").strip() in {"1", "true", "yes", "on"}


def _debug(message: str, root: Path | None = None) -> None:
    if not _debug_enabled():
        return
    try:
        path = (root or resolve_project_root()) / "data" / "hooks.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {message}\n")
    except OSError:
        pass


def write_private(path: Path, text: str, *, append: bool = False) -> None:
    """Write ``text`` to a file only its owner can read, like the database and runtime.json.

    Session files hold the pending question, the spool the question and answer, the
    briefing cache memory lines. A file left from an older version is tightened too.
    """
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    with open(os.open(path, flags, 0o600), "a" if append else "w", encoding="utf-8") as handle:
        handle.write(text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows may ignore or reject POSIX modes


# msvcrt has no blocking lock without a 10 s cap, so a Windows wait polls at this pace;
# a Windows replace refused by a reader is retried at the same pace.
_LOCK_POLL_S = 0.01
# Windows refuses os.replace while another process has the target open (a hook or the
# app reading the cache or a session file); such a reader lets go within milliseconds.
_RETRY_REPLACE = os.name == "nt"
_REPLACE_WAIT_S = 1.0
# Lock files this process already warned about: in the app every memory write takes the
# cache lock, and an unusable one would otherwise log a warning per write.
_LOCK_WARNED: set[str] = set()


def replace_file(source: Path, target: Path) -> None:
    """os.replace that waits up to a second for a Windows reader of ``target`` to let go."""
    deadline = time.monotonic() + _REPLACE_WAIT_S
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if not _RETRY_REPLACE or time.monotonic() >= deadline:
                raise
            time.sleep(_LOCK_POLL_S)


def _acquire(fd: int, wait: float | None) -> bool:
    """Take the exclusive lock on ``fd``: block (``wait`` None) or give up after ``wait`` seconds."""
    if fcntl is not None and wait is None:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return True
    # A held lock: flock says EWOULDBLOCK, msvcrt EACCES.
    busy: type[OSError] = BlockingIOError if fcntl is not None else PermissionError
    deadline = None if wait is None else time.monotonic() + wait
    while True:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except busy:
            pass
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(_LOCK_POLL_S if deadline is None else max(0.0, min(_LOCK_POLL_S, deadline - time.monotonic())))


def _release(fd: int) -> None:
    try:
        if fcntl is None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass  # closing the handle releases it too
    finally:
        os.close(fd)  # with flock, closing is the release


@contextmanager
def file_lock(path: Path, *, wait: float | None = None) -> Iterator[bool]:
    """Hold an exclusive lock on ``path`` across processes (hooks of one session, app and MCP servers).

    flock on POSIX, a one-byte msvcrt lock on Windows. Yields True while the lock
    is held. With ``wait`` (seconds) it gives up on a lock held elsewhere for
    longer and yields False: the block then runs without it and must say so. A
    lock file that cannot be made or locked (read-only folder, full disk, a file
    system without locks) must not cost the turn its pack or its capture: the
    block then runs unlocked (True), with a warning.
    """
    if fcntl is None and msvcrt is None:
        yield True
        return
    fd: int | None = None
    held = True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        held = _acquire(fd, wait)
    except OSError as exc:
        if str(path) in _LOCK_WARNED:
            _log().debug("lock file still unusable, going on without it: %s (%s)", path.name, exc)
        else:
            _LOCK_WARNED.add(str(path))
            _log().warning("lock file unusable, going on without it: %s (%s)", path.name, exc)
        if fd is not None:
            os.close(fd)
            fd = None
    if not held and fd is not None:
        os.close(fd)
        fd = None
    try:
        yield held
    finally:
        if fd is not None:
            _release(fd)


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
        source=_clean(payload.get("source")).lower() if resolved_event == EVENT_SESSION else "",
    )


# -- Session state -----------------------------------------------------------
def prune_hook_sessions(root: Path, *, max_age_s: float = _SESSION_TTL_S) -> int:
    """Delete session files untouched for ``max_age_s`` so data/ does not grow forever."""
    cutoff = time.time() - max_age_s
    removed = 0
    try:
        entries = list((root / "data" / _SESSION_DIR).iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        _log().info("hook sessions pruned: removed=%s max_age_h=%.0f", removed, max_age_s / 3600.0)
    return removed


class HookSession:
    """What one agent session has already been shown, per project.

    Each project the session touched keeps a prompt counter, the turn the stable
    layer was last injected and the ids injected so far (id -> turn). State is
    keyed by project so one project's ids never steer another's pack, even when a
    session id is reused across folders. ``cwd`` remembers which project the app
    resolved for a folder, so the next prompt knows whose ids to exclude.
    """

    def __init__(self, path: Path, state: dict[str, Any]) -> None:
        self.path = path
        self.state = state

    @staticmethod
    def _path(root: Path, session_id: str) -> Path:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        return root / "data" / _SESSION_DIR / f"{digest}.json"

    @classmethod
    @contextmanager
    def edit(cls, root: Path, session_id: str) -> Iterator["HookSession"]:
        """The session read, changed and saved under its lock file.

        Two hooks of one session can overlap (the next prompt arrives while turn end
        still waits for the app), so every change re-reads the file under the lock;
        keep the block short, never across an HTTP call.
        """
        with file_lock(cls._path(root, session_id).with_suffix(".lock")):
            session = cls.open(root, session_id)
            yield session
            session.save()

    @classmethod
    def open(cls, root: Path, session_id: str) -> "HookSession":
        path = cls._path(root, session_id)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            prune_hook_sessions(root)
            state = {}
        except (OSError, ValueError) as exc:
            _log().warning("hook session file unreadable, starting fresh: %s (%s)", path.name, exc)
            state = {}
        if not isinstance(state, dict):
            _log().warning("hook session file is not an object, starting fresh: %s", path.name)
            state = {}
        return cls(path, state)

    def project(self, project_id: str) -> dict[str, Any]:
        projects = self.state.setdefault("projects", {})
        entry = projects.get(project_id)
        if not isinstance(entry, dict) or not isinstance(entry.get("seen"), dict):
            entry = projects[project_id] = {"turn": 0, "stable_turn": None, "seen": {}}
        return entry

    def project_for(self, cwd: str) -> str:
        """The project for ``cwd``: as the app resolved it in this session, else as it would.

        A folder the session has not seen (the agent moved into a subfolder between
        prompt and turn end) takes the cached roots and bind files the offline hook
        uses, and failing those the nearest folder above it that the session saw.
        """
        known = self.state.get("cwd") or {}
        if project := known.get(cwd):
            return str(project)
        if not cwd:
            return ""
        if project := cached_project_for(cwd, load_briefing_cache(self.path.parents[2])):
            return project
        folder = os.path.dirname(cwd)
        while folder and folder != cwd:
            if project := known.get(folder):
                return str(project)
            cwd, folder = folder, os.path.dirname(folder)
        return ""

    def plan_prompt(self, cwd: str) -> tuple[bool, list[str]]:
        """(include the stable layer?, ids to exclude) for the next prompt in ``cwd``.

        The stable layer is due on the session's first prompt for a project (the
        start for clients without SessionStart) and again after it has been absent
        for ``stable_every`` prompts, which covers a client-side compaction.
        """
        project_id = self.project_for(cwd)
        entry = (self.state.get("projects") or {}).get(project_id) if project_id else None
        if not isinstance(entry, dict):
            return True, []
        every = int(self.state.get("stable_every", _STABLE_EVERY_TURNS))
        stable_turn = entry.get("stable_turn")
        due = stable_turn is None or (every > 0 and int(entry.get("turn") or 0) + 1 - int(stable_turn) >= every)
        return due, list(entry.get("seen") or {})

    def record(
        self, turn: HookTurn, result: dict[str, Any] | None, shown: list[tuple[str, str]], *,
        include_stable: bool, transport: str,
    ) -> None:
        """Remember what this event injected, under the project the app resolved.

        ``shown`` is (id, section) of each memory line that reached the agent (see
        shown_memories). The stable layer counts as shown only when one of its lines
        did, or when the app itself answered that the project has none; an offline
        or empty pack leaves it due for the next prompt.
        """
        if result is None:
            return
        project_id = str(result.get("project_id") or self.project_for(turn.cwd))
        if not project_id:
            return
        if turn.cwd:
            self.state.setdefault("cwd", {})[turn.cwd] = project_id
        if "stable_every_turns" in result:
            self.state["stable_every"] = int(result["stable_every_turns"])
        entry = self.project(project_id)
        stable_shown = any(section == "stable" for _, section in shown) or (transport == "http" and not result.get("stable"))
        if turn.event == EVENT_SESSION:
            if turn.source in _RESET_SOURCES:
                entry["seen"] = {}
            if stable_shown:
                entry["stable_turn"] = int(entry["turn"])
        elif turn.event == EVENT_PROMPT:
            entry["turn"] = int(entry["turn"]) + 1
            if include_stable and stable_shown:
                entry["stable_turn"] = entry["turn"]
        for node_id, _ in shown:
            entry["seen"][node_id] = entry["turn"]

    def keep_prompt(self, cwd: str, text: str) -> str:
        """Hold the question until turn end, under the project resolved for ``cwd``.

        Returns the question it replaces: that turn never ended (the agent was
        stopped before turn end fired), and the caller spools it instead of losing it.
        """
        pending = self.state.setdefault("pending", {})
        project_id = self.project_for(cwd)
        orphan = str(pending.get(project_id) or "")
        pending[project_id] = text[:_PENDING_PROMPT_MAX_CHARS]
        return orphan

    def take_prompt(self, cwd: str) -> str:
        """The held question for ``cwd``'s project, removed from the file once taken."""
        pending = self.state.get("pending")
        return str(pending.pop(self.project_for(cwd), "") or "") if isinstance(pending, dict) else ""

    def save(self) -> None:
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            scratch = self.path.with_suffix(".tmp")
            write_private(scratch, json.dumps(self.state, ensure_ascii=False))
            replace_file(scratch, self.path)
        except OSError as exc:
            _log().warning("hook session file not saved: %s (%s)", self.path.name, exc)


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


def _exchange(url: str, token: str, body: bytes | None, timeout: float) -> tuple[int, bytes]:
    """One HTTP/1.0 request to the app's loopback server: (status, response body).

    The app serves plain HTTP on 127.0.0.1 and closes the connection after each
    reply, so a socket is all the hook needs; urllib.request would add ~45 ms of
    imports (http.client, email, ssl) to every agent turn.
    """
    authority, _, path = url.split("://", 1)[-1].partition("/")
    host, _, port = authority.rpartition(":")
    lines = [
        f"{'GET' if body is None else 'POST'} /{path} HTTP/1.0",
        f"Host: {authority}",
        f"X-ArchitectOS-Token: {token}",
        "Connection: close",
    ]
    if body is not None:
        lines += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout
    # getaddrinfo takes a bytes host as is and runs a str one through the idna
    # codec (~4 ms of imports); typeshed only lists str.
    address = ((host or "127.0.0.1").encode("ascii"), int(port or 80))
    try:
        connected = socket.create_connection(address, timeout=timeout)  # type: ignore[arg-type]
    except TimeoutError as exc:
        raise ConnectionError(f"connect timed out: {exc}") from exc  # the app never got the request
    with connected as conn:
        try:
            conn.settimeout(max(0.001, deadline - time.monotonic()))
            conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + (body or b""))
        except TimeoutError as exc:
            raise ConnectionError(f"send timed out: {exc}") from exc  # an incomplete request is never served
        # One deadline for the whole reply, not per recv: a trickling reply must not
        # hold the agent past ``timeout``.
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no complete reply within {timeout:.1f}s")
            conn.settimeout(remaining)
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    head, _, reply = b"".join(chunks).partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), reply


def _request_json(
    url: str,
    token: str,
    *,
    payload: dict[str, Any] | None,
    timeout: float,
    root: Path | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Parsed reply plus why there is none: "" (ok), "timeout", "unreachable" or "error".

    A timeout means the app got the whole request and may still act on it;
    "unreachable" (refused, or a connect or send that timed out) means the event
    surely went nowhere.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    try:
        status, body = _exchange(url, token, data, timeout)
    except TimeoutError as exc:
        _debug(f"http failed: {exc}", root)
        _log().warning("hook request timed out after %.1fs: %s", timeout, url.split("?", 1)[0])
        return None, "timeout"
    except OSError as exc:
        _debug(f"http failed: {exc}", root)
        return None, "unreachable"
    except (ValueError, IndexError) as exc:
        _debug(f"http failed: malformed reply ({exc})", root)
        return None, "error"
    if status >= 400:
        _debug(f"http failed: HTTP {status}", root)
        return None, "error"
    try:
        parsed = json.loads(body.decode("utf-8"))
    except ValueError:
        return None, "error"
    return (parsed, "") if isinstance(parsed, dict) else (None, "error")


def _offline_allowed() -> bool:
    return os.environ.get("ARCHITECTOS_HOOK_OFFLINE", "1").strip() not in {"0", "false", "no", "off"}


def run_turn(turn: HookTurn, *, root: Path | None = None, timeout: float | None = None, limit: int = 8) -> dict[str, Any] | None:
    """Send the turn to ArchitectOS and return its memory pack, or None on failure."""
    return _send_turn(turn, root=root, timeout=timeout, limit=limit)[0]


def _timeout(turn: HookTurn) -> float:
    """HTTP timeout for this event: short when the agent waits for a pack, longer for capture."""
    if turn.event == EVENT_RESPONSE:
        name, default = "ARCHITECTOS_HOOK_TIMEOUT", _CAPTURE_TIMEOUT
    else:
        name, default = "ARCHITECTOS_HOOK_INJECT_TIMEOUT", _INJECT_TIMEOUT
    try:
        return max(0.1, float(os.environ.get(name, default)))
    except ValueError:
        return default


def _send_turn(
    turn: HookTurn,
    *,
    root: Path | None = None,
    timeout: float | None = None,
    limit: int = 8,
    include_stable: bool = True,
    exclude_ids: list[str] | None = None,
    capture: bool = True,
) -> tuple[dict[str, Any] | None, str]:
    """Memory pack plus the transport that produced it: http, offline or none."""
    wait = float(timeout) if timeout is not None else _timeout(turn)
    endpoint = runtime_endpoint(root)
    failure = "unreachable"
    if endpoint:
        url, token = endpoint
        if turn.event == EVENT_SESSION:
            from urllib.parse import urlencode  # only the session briefing needs it; keep prompt turns lean

            query = urlencode({"cwd": turn.cwd}) if turn.cwd else ""
            result, failure = _request_json(f"{url}/api/memory/briefing?{query}", token, payload=None, timeout=wait, root=root)
        else:
            payload = {
                "user_text": turn.user_text,
                "assistant_text": turn.assistant_text,
                "cwd": turn.cwd,
                "client": turn.client,
                "limit": limit,
                "hook": True,
                "include_stable": include_stable,
                "exclude_ids": exclude_ids or [],
                "capture": capture,
            }
            result, failure = _request_json(f"{url}/api/memory/turn", token, payload=payload, timeout=wait, root=root)
        if result is not None:
            return result, "http"
    if not _offline_allowed():
        return None, "none"
    # A timed-out capture already reached the app and is likely still running; spooling
    # it too would capture it twice. An app that answered with an error (500 from a
    # locked store, a cut reply) did not capture it, and the question has already left
    # the session file: spool it. A replay of a turn the app did store is deduplicated.
    spool = capture and turn.event != EVENT_SESSION and failure in ("unreachable", "error")
    return _offline_turn(turn, root or resolve_project_root(), include_stable=include_stable,
                         exclude_ids=exclude_ids or [], spool=spool), "offline"


# -- Offline: briefing cache and capture spool -------------------------------
# With the app closed the hook never opens the store: it injects what the app
# last cached in data/briefing_cache.json and appends captures to
# data/hook_spool.jsonl, which the app drains on start and every minute.
BRIEFING_CACHE = "briefing_cache.json"
# Left by a process that could not evict a changed memory from the cache (the file
# would not be replaced, or its lock stayed held): until the next locked write
# re-checks every cached line against the store, the offline hook injects no lines.
BRIEFING_CACHE_DIRTY = f"{BRIEFING_CACHE}.dirty"
# The marker a writer has claimed: renamed, not removed, until the re-checked cache is
# written, so the hook keeps off the lines meanwhile and a new mark is a new file.
BRIEFING_CACHE_DIRTY_CLAIMED = f"{BRIEFING_CACHE_DIRTY}.claimed"


def briefing_cache_dirty(root: Path) -> bool:
    """True while the cache may hold a changed memory (see BRIEFING_CACHE_DIRTY)."""
    data = root / "data"
    return (data / BRIEFING_CACHE_DIRTY).exists() or (data / BRIEFING_CACHE_DIRTY_CLAIMED).exists()
HOOK_SPOOL = "hook_spool.jsonl"
_SPOOL_MAX_BYTES = 5 * 1024 * 1024
_BRIEFING_CHAR_BUDGET = 2000
# Mirrors agent_hook_service.HOOK_CACHE_TTL_HOURS for a cache written before the setting existed.
_CACHE_TTL_HOURS = 24.0
_HOOK_CHAR_BUDGET = 1600
# Same bind file and walk as mcp_server.read_mcp_bind_project_id, without its imports.
_BIND_FILENAME = ".architectos.json"
# Section titles of search.pack_memory_context: the cache keeps a pack's lines per section.
STABLE_TITLE = "Stable memory (pinned / constraints / long-term):"
HITS_TITLE = "Retrieved for this query:"
def pack_lines_shown(context: str, lines: Iterable[tuple[str, str, str]]) -> list[list[Any]]:
    """[id, section, end] for each (id, section, line) of a rendered pack, in pack order.

    ``end`` is where the line stops in ``context``, so the hook can tell which lines
    a cut injection still carries whole. The ids come from the pack's own line list,
    never read back out of its text: a memory's text may hold a line that looks
    like another memory's.
    """
    shown: list[list[Any]] = []
    position = 0
    for node_id, section, line in lines:
        at = context.find(line, position)
        if at < 0:
            continue
        position = at + len(line)
        shown.append([node_id, section, position])
    return shown


def shown_memories(result: Mapping[str, Any] | None, injected: str) -> list[tuple[str, str]]:
    """(id, section) of each memory line that reached the agent whole in ``injected``."""
    if not (result and injected):
        return []
    context = str(result.get("context") or "")
    # render_stdout injects the stripped context after the prefix and cuts it at _INJECT_MAX_CHARS.
    kept = len(context) - len(context.lstrip()) + _INJECT_MAX_CHARS - len(_INJECT_PREFIX) - 2
    return [
        (str(item[0]), str(item[1]))
        for item in result.get("shown") or []
        if isinstance(item, list) and len(item) >= 3 and isinstance(item[2], int) and item[2] <= kept
    ]


def pack_header(context: str) -> str:
    """The lines of a rendered pack before its first memory section."""
    header: list[str] = []
    for line in context.splitlines():
        if line.strip() in (STABLE_TITLE, HITS_TITLE):
            break
        header.append(line)
    return "\n".join(header).strip()


def render_cached_pack(entry: Mapping[str, Any], *, include_stable: bool, exclude: set[str], budget: int) -> tuple[str, list[list[Any]]]:
    """Rebuild a pack from cached lines: (context, shown as pack_lines_shown); "" when nothing is new.

    Like the app's hook pack, a requested stable layer ignores ``exclude``.
    """
    header = str(entry.get("header") or "")
    lines, used, taken = [header], len(header), set()
    picked: list[tuple[str, str, str]] = []
    for key, title in (("stable", STABLE_TITLE), ("hits", HITS_TITLE)):
        if key == "stable" and not include_stable:
            continue
        section: list[str] = []
        cost = used + len(title) + 2
        for item in entry.get(key) or []:
            if not (isinstance(item, list) and len(item) >= 2):  # [id, line] or [id, line, signature]
                continue
            node_id, line = str(item[0]), str(item[1])
            if node_id in taken or (key == "hits" and node_id in exclude) or cost + len(line) + 1 > budget:
                continue
            section.append(line)
            picked.append((node_id, key, line))
            taken.add(node_id)
            cost += len(line) + 1
        if section:
            lines.extend(["", title, *section])
            used = cost
    if not taken:
        return "", []
    context = "\n".join(lines).strip()
    return context, pack_lines_shown(context, picked)


def _fresh_sections(entry: Mapping[str, Any], ttl_hours: float) -> dict[str, Any]:
    """``entry`` without the sections the app cached more than ``ttl_hours`` ago (0 keeps all).

    Each project's stable lines and hit lines carry their own write time, so a
    write for one project never renews another's.
    """
    fresh = dict(entry)
    if ttl_hours <= 0:
        return fresh
    now = datetime.now(timezone.utc)
    for key in ("stable", "hits"):
        written = str(entry.get(f"{key}_at") or entry.get("updated_at") or "")
        try:
            age_h = (now - datetime.fromisoformat(written)).total_seconds() / 3600.0
        except ValueError:
            age_h = float("inf")
        if age_h > ttl_hours:
            fresh[key] = []
    return fresh


def _bound_project_id(cwd: str) -> str:
    try:
        current = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return ""
    for folder in (current, *current.parents):
        candidate = folder / _BIND_FILENAME
        try:
            if not candidate.is_file():
                continue
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return ""
        return str(data.get("project_id") or "").strip() if isinstance(data, dict) else ""
    return ""


def cached_project_for(cwd: str, cache: Mapping[str, Any]) -> str:
    """The project the app would pick for ``cwd``, without the store.

    Bind file first, then the deepest cached root, then the app's default
    project (as ``capture_memory_turn`` falls back to it). "" only when the app
    never wrote a cache, and the offline hook then injects nothing.
    """
    bound = _bound_project_id(cwd)
    if bound:
        return bound
    try:
        target = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return ""
    roots = cache.get("roots")
    best, best_depth = "", -1
    for project_id, raw_root in (roots.items() if isinstance(roots, dict) else ()):
        candidate = Path(str(raw_root))
        if (candidate == target or candidate in target.parents) and len(candidate.parts) > best_depth:
            best, best_depth = str(project_id), len(candidate.parts)
    return best or str(cache.get("default_project") or "")


def load_briefing_cache(root: Path) -> dict[str, Any]:
    path = root / "data" / BRIEFING_CACHE
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        _log().warning("briefing cache unreadable, offline hook stays silent: %s", exc)
        return {}
    return cache if isinstance(cache, dict) else {}


def spool_turn(root: Path, record: dict[str, Any]) -> bool:
    """Append one capture for the app to replay; False when the spool is full or unwritable."""
    path = root / "data" / HOOK_SPOOL
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        size = path.stat().st_size if path.exists() else 0
        if size + len(line) > _SPOOL_MAX_BYTES:
            _log().warning("hook spool full, capture dropped: bytes=%s limit=%s", size, _SPOOL_MAX_BYTES)
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        write_private(path, line, append=True)
    except OSError as exc:
        _log().warning("hook spool not written: %s", exc)
        return False
    return True


def spool_status(root: Path) -> dict[str, Any]:
    """Captures waiting for the app: count, bytes and age of the oldest one (for doctor)."""
    data = root / "data"
    files = [data / HOOK_SPOOL, *sorted(data.glob(f"{HOOK_SPOOL}.*.draining"))]
    records, size, oldest = 0, 0, ""
    for path in files:
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    size += len(line.encode("utf-8"))
                    if not line.strip():
                        continue
                    records += 1
                    if not oldest:
                        try:
                            oldest = str(json.loads(line).get("ts") or "")
                        except (ValueError, AttributeError):
                            pass
        except OSError:
            continue
    age_s = None
    if oldest:
        try:
            age_s = round((datetime.now(timezone.utc) - datetime.fromisoformat(oldest)).total_seconds())
        except ValueError:
            age_s = None
    return {"path": str(data / HOOK_SPOOL), "records": records, "bytes": size, "oldest_age_s": age_s}


def _offline_turn(
    turn: HookTurn,
    root: Path,
    *,
    include_stable: bool,
    exclude_ids: list[str],
    spool: bool,
) -> dict[str, Any]:
    """The app is closed: inject from the briefing cache, spool the capture. Never opens the store."""
    cache = load_briefing_cache(root)
    project_id = cached_project_for(turn.cwd, cache) if turn.cwd else ""
    result: dict[str, Any] = {"project_id": project_id, "context": "", "hits": [], "stable": []}
    raw_settings = cache.get("settings")
    settings: dict[str, Any] = raw_settings if isinstance(raw_settings, dict) else {}
    if "hook_stable_every_turns" in settings:
        result["stable_every_turns"] = int(settings["hook_stable_every_turns"])
    if spool:
        result["spooled"] = spool_turn(root, {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "project_id": project_id,
            "cwd": turn.cwd,
            "client": turn.client,
            "user_text": turn.user_text,
            "assistant_text": turn.assistant_text,
        })
    entry = (cache.get("projects") or {}).get(project_id) if project_id else None
    if isinstance(entry, dict):
        entry = _fresh_sections(entry, float(settings.get("hook_cache_ttl_hours", _CACHE_TTL_HOURS)))
    dirty = briefing_cache_dirty(root)
    if turn.can_inject and isinstance(entry, dict) and not dirty:
        session = turn.event == EVENT_SESSION
        context, shown = render_cached_pack(
            {**entry, "hits": []} if session else entry,  # session start is the briefing: stable layer only
            include_stable=include_stable or session,
            exclude=set() if session else set(exclude_ids),
            budget=_BRIEFING_CHAR_BUDGET if session else hook_pack_budget(int(settings.get("hook_char_budget") or _HOOK_CHAR_BUDGET)),
        )
        result.update(
            context=context, shown=shown,
            stable=[item[0] for item in shown if item[1] == "stable"], hits=[item[0] for item in shown if item[1] == "hits"],
        )
    _log().info(
        "offline hook: kind=%s project=%s cached=%s dirty=%s injected_ids=%s spooled=%s",
        turn.event, project_id or "-", isinstance(entry, dict), dirty, len(result["hits"]) + len(result["stable"]),
        result.get("spooled", False),
    )
    return result


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
        _debug(f"ignored event client={client} event={event}", root)
        return _silent(client, event)
    if turn.event == EVENT_PROMPT and not turn.user_text:
        return _silent(turn.client, turn.event)
    if turn.event == EVENT_RESPONSE and not turn.assistant_text and not turn.session_id:
        return _silent(turn.client, turn.event)
    started = time.perf_counter()
    session_root = root or resolve_project_root()
    # Turn end never injects, so it skips the stable layer. With a session the
    # prompt only injects and turn end captures question + answer in one call;
    # without one, each event captures its own text as before.
    include_stable, capture = turn.event != EVENT_RESPONSE, True
    exclude_ids: list[str] = []
    if turn.session_id and turn.event == EVENT_PROMPT:
        include_stable, exclude_ids = HookSession.open(session_root, turn.session_id).plan_prompt(turn.cwd)
        capture = False
    elif turn.session_id and turn.event == EVENT_RESPONSE:
        # Taken and saved before the send: a prompt hook running meanwhile must not
        # find the question still held (and spool it a second time).
        with HookSession.edit(session_root, turn.session_id) as session:
            turn = turn._replace(user_text=session.take_prompt(turn.cwd))
        # Turn end with no answer text (the agent was interrupted) still captures the question.
        if not (turn.user_text or turn.assistant_text):
            return _silent(turn.client, turn.event)
    if turn.session_id and turn.event == EVENT_PROMPT and not turn.can_inject:
        result, transport = None, "deferred"  # nothing to inject; the question waits for turn end
    else:
        result, transport = _send_turn(
            turn, root=root, include_stable=include_stable, exclude_ids=exclude_ids, capture=capture
        )
    out = render_stdout(turn, result)
    injected = _injected_text(out)
    if turn.can_inject and not injected:
        _log().info("injection skipped: kind=%s transport=%s reason=%s", turn.event, transport,
                  "no reply" if result is None else "empty pack")
    if turn.session_id:
        # Re-read under the lock: another hook of this session may have saved meanwhile.
        with HookSession.edit(session_root, turn.session_id) as session:
            session.record(turn, result, shown_memories(result, injected), include_stable=include_stable, transport=transport)
            if turn.event == EVENT_PROMPT:
                orphan = session.keep_prompt(turn.cwd, turn.user_text)
                if orphan and _offline_allowed():
                    spool_turn(session_root, {
                        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "project_id": session.project_for(turn.cwd),
                        "cwd": turn.cwd,
                        "client": turn.client,
                        "user_text": orphan,
                        "assistant_text": "",
                    })
    if _debug_enabled():
        _debug(_timing_line(turn, transport, (time.perf_counter() - started) * 1000.0, out, result), root)
    return out


def _injected_text(out: str) -> str:
    """The additionalContext a rendered reply carries ("" when the hook stayed silent)."""
    if not out or out == "{}":
        return ""
    return str(json.loads(out).get("hookSpecificOutput", {}).get("additionalContext") or "")


def _timing_line(turn: HookTurn, transport: str, elapsed_ms: float, out: str, result: dict[str, Any] | None) -> str:
    """One hooks.log line per event: counts and timings only, never prompt text.

    ``tokens`` estimates what was actually injected (0 when the hook stayed silent).
    """
    data = result or {}
    injected = _injected_text(out)
    return (
        f"event={turn.event} client={turn.client} transport={transport} ms={elapsed_ms:.1f} "
        f"tokens={estimate_tokens_from_text(injected)} hits={len(data.get('hits') or data.get('stable') or [])} "
        f"project={data.get('project_id')} queued={len(data.get('queued') or [])} auto={len(data.get('auto_accepted') or [])}"
    )


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
    """What is installed where, whether the running app is reachable and what waits for it offline."""
    endpoint = runtime_endpoint()
    data_root = resolve_project_root()
    cache = load_briefing_cache(data_root)
    try:
        sessions = sum(1 for _ in (data_root / "data" / _SESSION_DIR).glob("*.json"))
    except OSError:
        sessions = 0
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
        "offline": {
            "spool": spool_status(data_root),
            "briefing_cache": {
                "path": str(data_root / "data" / BRIEFING_CACHE),
                "projects": len(cache.get("projects") or {}),
                "updated_at": cache.get("updated_at"),
                "dirty": briefing_cache_dirty(data_root),
            },
            "sessions": sessions,
        },
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


def build_parser(command: str | None = None) -> argparse.ArgumentParser:
    """The CLI parser; ``command="capture"`` builds only that subcommand.

    Every agent event runs ``capture``, and building the other subparsers costs
    it ~8 ms of argparse setup it never uses.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="architectos_hook",
        description="Capture agent turns into ArchitectOS memory through client hooks.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture", help="Handle one hook event read as JSON on stdin.")
    capture.add_argument("--client", required=True, choices=list(CLIENTS))
    capture.add_argument("--event", choices=list(EVENTS), default=None)
    if command == "capture":
        return parser

    for name, help_text in (("install", "Register hooks."), ("uninstall", "Remove our hooks.")):
        action = sub.add_parser(name, help=help_text)
        action.add_argument("--client", default="all", help="cursor, claude, codex, a comma-separated list, or all.")
        action.add_argument("--project", action="store_true", help="Write to the repo config instead of the user config.")

    sub.add_parser("doctor", help="Show what is installed and whether the app is reachable.")
    return parser


def _capture_argv(argv: list[str]) -> tuple[str, str | None] | None:
    """(client, event) when argv is exactly what ``hook_command`` registers, else None.

    Every agent event starts a new interpreter; argparse and the shutil, bz2 and
    lzma its help formatter imports cost each one ~15 ms. Any other form goes
    through argparse as before.
    """
    if len(argv) not in (3, 5) or argv[:2] != ["capture", "--client"] or argv[2] not in CLIENTS:
        return None
    if len(argv) == 3:
        return argv[2], None
    return (argv[2], argv[4]) if argv[3] == "--event" and argv[4] in EVENTS else None


def _capture(client: str, event: str | None) -> int:
    global _STANDALONE
    _STANDALONE = True
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        raw = ""
    try:
        out = handle_capture(raw, client=client, event=event)
    except Exception as exc:  # noqa: BLE001 - a hook must never break the agent
        _debug(f"capture crashed: {exc}")
        out = "{}" if client == "codex" and event == EVENT_RESPONSE else ""
    if out:
        sys.stdout.write(out)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    capture = _capture_argv(argv)
    if capture is not None:
        return _capture(*capture)
    args = build_parser(argv[0] if argv else None).parse_args(argv)

    if args.command == "capture":
        return _capture(args.client, args.event)

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
