"""MCP server config model and shared protocol constants."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .constants import SECRET_MASK

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "ArchitectOS", "version": "1.0.0"}




@dataclass(slots=True)
class MCPServerConfig:
    id: str
    label: str
    command: list[str]
    url: str = ""
    enabled: bool = False
    approval_required: bool = True
    allow_local: bool = False
    transport: str = "stdio"
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    auth: dict[str, Any] = field(default_factory=dict)
    status: str = "planned"
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MCPServerConfig":
        command = data.get("command")
        if isinstance(command, str):
            command = command.split()
        transport = str(data.get("transport") or "stdio").strip().lower()
        if transport in {"remote http", "remote-http"}:
            transport = "http"
        return cls(
            id=str(data.get("id") or ""),
            label=str(data.get("label") or data.get("id") or ""),
            command=[str(part) for part in (command or []) if str(part)],
            url=str(data.get("url") or ""),
            enabled=bool(data.get("enabled")),
            approval_required=bool(data.get("approval_required", True)),
            allow_local=bool(data.get("allow_local")),
            transport=transport,
            headers={str(key): str(value) for key, value in dict(data.get("headers") or {}).items()},
            env={str(key): str(value) for key, value in dict(data.get("env") or {}).items()},
            auth=dict(data.get("auth") or {}),
            status=str(data.get("status") or "planned"),
            notes=str(data.get("notes") or ""),
        )

    def to_dict(self, include_secrets: bool = True) -> dict[str, Any]:
        if include_secrets:
            headers = dict(self.headers)
            env = dict(self.env)
        else:
            # Keys stay visible so editors can round-trip the config; values are masked.
            headers = {key: SECRET_MASK for key in self.headers}
            env = {key: SECRET_MASK for key in self.env}
        payload = {
            "id": self.id,
            "label": self.label,
            "command": list(self.command),
            "url": self.url,
            "enabled": self.enabled,
            "approval_required": self.approval_required,
            "allow_local": self.allow_local,
            "transport": self.transport,
            "headers": headers,
            "env": env,
            "status": self.status,
            "notes": self.notes,
        }
        if include_secrets:
            payload["auth"] = dict(self.auth)
        elif self.auth:
            auth = dict(self.auth)
            payload["auth"] = {
                "status": auth.get("status") or ("authorized" if auth.get("access_token") else ""),
                "expires_at": auth.get("expires_at") or 0,
                "has_access_token": bool(auth.get("access_token")),
                "has_refresh_token": bool(auth.get("refresh_token")),
            }
        else:
            payload["auth"] = {}
        return payload


class MCPError(RuntimeError):
    pass
