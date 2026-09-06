"""Small public API over the ArchitectOS memory engine.

The desktop app, MCP server and agent hooks all share one SQLite store. This
module is the same engine without the HTTP shell: point it at a data root and
call search / add / capture. The full ``ArchitectOSService`` stays available
as ``.service`` for anything the thin wrapper does not expose.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .paths import resolve_project_root


class ArchitectOS:
    """Local-first memory SDK. One root → one ``data/architectos.db``."""

    def __init__(self, root: str | Path | None = None) -> None:
        from .service import ArchitectOSService

        self.root = Path(root).expanduser().resolve() if root is not None else resolve_project_root()
        self.service = ArchitectOSService(self.root)

    def search(self, query: str, *, project_id: str | None = None, scope: str | None = None, limit: int = 8) -> dict[str, Any]:
        return self.service.search_memory(query, project_id=project_id, scope=scope, limit=limit)

    def context(self, query: str, *, project_id: str | None = None, scope: str | None = None, limit: int = 8) -> dict[str, Any]:
        return self.service.context(query, project_id=project_id, scope=scope, limit=limit)

    def briefing(self, *, project_id: str | None = None, cwd: str | None = None, limit: int = 6) -> dict[str, Any]:
        return self.service.memory_briefing(project_id=project_id, cwd=cwd, limit=limit)

    def add(
        self,
        text: str,
        *,
        label: str | None = None,
        type: str = "Lesson",
        project_id: str | None = None,
        scope: str = "project",
    ) -> dict[str, Any]:
        body = " ".join(str(text or "").split())
        return self.service.add_memory({
            "text": body,
            "label": str(label or body[:80] or "memory"),
            "type": type,
            "project_id": project_id or "architectos",
            "scope": scope,
        })

    def capture_turn(
        self,
        user_text: str,
        assistant_text: str = "",
        *,
        project_id: str | None = None,
        cwd: str | None = None,
        client: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        return self.service.capture_memory_turn(
            user_text,
            assistant_text,
            project_id=project_id,
            cwd=cwd,
            client=client,
            limit=limit,
        )

    def install_hooks(self, clients: str | list[str] = "all", *, scope: str = "user") -> dict[str, Any]:
        return self.service.configure_agent_hooks({"clients": clients, "scope": scope})

    def uninstall_hooks(self, clients: str | list[str] = "all", *, scope: str = "user") -> dict[str, Any]:
        return self.service.configure_agent_hooks({"clients": clients, "scope": scope}, remove=True)

    def hook_status(self) -> dict[str, Any]:
        return self.service.agent_hook_status()
