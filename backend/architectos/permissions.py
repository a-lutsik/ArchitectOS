"""Interactive permission grants for agent tools that leave the project sandbox.

The model still just calls fs_read / fs_write. The runtime pauses, the Ask UI
asks the user, and the same tool call is retried if they allow it.
"""

from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

PERMISSION_WAIT_SECONDS = 180


class PermissionRequired(Exception):
    """Raised by a tool handler when the user must approve an action first."""

    def __init__(self, kind: str, action: str, target: str, reason: str = "") -> None:
        self.kind = str(kind or "sandbox")
        self.action = str(action or "read")
        self.target = str(target or "")
        self.reason = str(reason or "").strip() or f"{self.action} {self.target} needs permission".strip()
        super().__init__(self.reason)

    def as_payload(self, name: str) -> dict[str, Any]:
        return {
            "ok": False,
            "name": name,
            "needs_permission": True,
            "permission": {
                "kind": self.kind,
                "action": self.action,
                "target": self.target,
                "reason": self.reason,
            },
            "error": self.reason,
            "summary": "",
        }


def _normalize_path(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or raw == "*":
        return raw or "*"
    path = Path(raw).expanduser()
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def path_covered(granted: str, target: str) -> bool:
    if granted in {"", "*"}:
        return True
    granted_path = Path(_normalize_path(granted))
    target_path = Path(_normalize_path(target))
    if granted_path == target_path:
        return True
    try:
        target_path.relative_to(granted_path)
        return True
    except ValueError:
        return False


class PermissionBroker:
    """In-memory pending prompts and run/session grants. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tls = threading.local()
        self._pending: dict[str, dict[str, Any]] = {}
        self._grants: list[dict[str, Any]] = []
        self._run_chat: dict[str, str] = {}

    def bind_run(self, run_id: str, chat_id: str = "") -> None:
        rid = str(run_id or "").strip()
        cid = str(chat_id or "").strip()
        self._tls.run_id = rid
        self._tls.chat_id = cid
        if rid:
            with self._lock:
                if cid:
                    self._run_chat[rid] = cid
                elif rid not in self._run_chat:
                    self._run_chat[rid] = ""

    def active_run_id(self) -> str:
        return str(getattr(self._tls, "run_id", "") or "")

    def active_chat_id(self) -> str:
        rid = self.active_run_id()
        tls_chat = str(getattr(self._tls, "chat_id", "") or "")
        if tls_chat:
            return tls_chat
        with self._lock:
            return str(self._run_chat.get(rid) or "")

    def is_allowed(self, kind: str, action: str, target: str, run_id: str | None = None) -> bool:
        rid = str(run_id or self.active_run_id() or "")
        cid = self.active_chat_id()
        if rid:
            with self._lock:
                cid = cid or str(self._run_chat.get(rid) or "")
        kind = str(kind or "")
        action = str(action or "")
        with self._lock:
            for grant in self._grants:
                if grant.get("kind") != kind:
                    continue
                granted_action = str(grant.get("action") or "*")
                if granted_action not in {action, "*"}:
                    continue
                scope = str(grant.get("scope") or "run")
                if scope == "session":
                    if not cid or str(grant.get("chat_id") or "") != cid:
                        continue
                elif str(grant.get("run_id") or "") != rid:
                    continue
                if kind == "sandbox" and not path_covered(str(grant.get("target") or "*"), target):
                    continue
                return True
        return False

    def grant(self, kind: str, action: str, target: str, *, run_id: str = "", chat_id: str = "", scope: str = "run") -> None:
        rid = str(run_id or self.active_run_id() or "")
        cid = str(chat_id or self.active_chat_id() or "")
        entry = {
            "kind": str(kind or "sandbox"),
            "action": str(action or "*"),
            "target": target if str(kind) != "sandbox" else _normalize_path(target),
            "run_id": rid,
            "chat_id": cid,
            "scope": "session" if scope == "session" else "run",
        }
        with self._lock:
            self._grants.append(entry)

    def create_request(self, kind: str, action: str, target: str, reason: str = "", *, run_id: str = "") -> dict[str, Any]:
        rid = str(run_id or self.active_run_id() or "")
        request_id = f"perm_{secrets.token_hex(8)}"
        payload = {
            "request_id": request_id,
            "run_id": rid,
            "kind": str(kind or "sandbox"),
            "action": str(action or "read"),
            "target": str(target or ""),
            "reason": str(reason or ""),
        }
        with self._lock:
            self._pending[request_id] = {
                "event": threading.Event(),
                "decision": None,
                "run_id": rid,
                "payload": payload,
            }
        return payload

    def wait(
        self,
        request_id: str,
        timeout: float = PERMISSION_WAIT_SECONDS,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            pending = self._pending.get(str(request_id or ""))
        if not pending:
            return {"allow": False, "scope": "once", "missing": True}
        deadline = time.monotonic() + max(1.0, float(timeout))
        while time.monotonic() < deadline:
            if cancel_requested and cancel_requested():
                return self.decide(request_id, allow=False, scope="once")
            if pending["event"].wait(timeout=0.25):
                return dict(pending.get("decision") or {"allow": False, "scope": "once"})
        return self.decide(request_id, allow=False, scope="once", timed_out=True)

    def decide(
        self,
        request_id: str,
        *,
        allow: bool,
        scope: str = "once",
        timed_out: bool = False,
    ) -> dict[str, Any]:
        request_id = str(request_id or "")
        with self._lock:
            pending = self._pending.get(request_id)
            if not pending:
                raise ValueError("permission request not found")
            if pending.get("decision") is not None:
                return dict(pending["decision"])
            normalized_scope = "session" if allow and scope == "session" else "once"
            decision = {"allow": bool(allow), "scope": normalized_scope, "timed_out": bool(timed_out)}
            pending["decision"] = decision
            payload = dict(pending.get("payload") or {})
            run_id = str(pending.get("run_id") or "")
            chat_id = str(self._run_chat.get(run_id) or "")
            event = pending["event"]
        if allow:
            self.grant(
                str(payload.get("kind") or "sandbox"),
                str(payload.get("action") or "*"),
                str(payload.get("target") or "*"),
                run_id=run_id,
                chat_id=chat_id,
                scope="session" if normalized_scope == "session" else "run",
            )
        event.set()
        return decision

    def deny_run(self, run_id: str) -> None:
        rid = str(run_id or "")
        with self._lock:
            pending_ids = [key for key, item in self._pending.items() if str(item.get("run_id") or "") == rid]
        for request_id in pending_ids:
            try:
                self.decide(request_id, allow=False, scope="once")
            except ValueError:
                continue
