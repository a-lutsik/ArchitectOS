"""MCP server hub CRUD and code-intelligence (LSP) endpoints.

Extracted from ``service.py``. Covers persistence and management of MCP servers
(list/upsert/test/auth/tools/call) and the code-intelligence endpoints backed by
the language-server manager (symbols/hover/diagnostics/references, language
discovery and server install). Depends only on leaf modules; repository access
and language/MCP managers are reached through ``self`` via the MRO on
:class:`ArchitectOSService`.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from .lsp import LSPError, find_executable, is_runnable_install_command, resolve_install_command
from .mcp import MCPError

_LOG = logging.getLogger("architectos.service")


class IntegrationsServiceMixin:
    """MCP server hub management and code-intelligence (LSP) endpoints."""

    def _load_mcp_servers(self) -> list[dict[str, Any]]:
        return list((self.repository.get_setting("mcp_servers") or {}).get("servers") or [])

    def _save_mcp_servers(self, servers: list[dict[str, Any]]) -> None:
        self.repository.set_setting("mcp_servers", {"servers": servers})

    def mcp_servers(self) -> dict[str, Any]:
        return {"servers": self.mcp_manager.list_servers()}

    def update_mcp_server(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"server": self.mcp_manager.upsert_server(payload)}
        except MCPError as exc:
            raise ValueError(str(exc)) from exc

    def test_mcp_server(self, server_id: str) -> dict[str, Any]:
        try:
            return self.mcp_manager.check(server_id)
        except MCPError as exc:
            raise ValueError(str(exc)) from exc

    def start_mcp_auth(self, server_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.mcp_manager.start_auth(server_id, str(payload.get("base_url") or "http://127.0.0.1:8765"))
        except MCPError as exc:
            raise ValueError(str(exc)) from exc

    def complete_mcp_auth(self, query: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.mcp_manager.complete_auth(query)
        except MCPError as exc:
            raise ValueError(str(exc)) from exc

    def mcp_tools(self, server_id: str) -> dict[str, Any]:
        try:
            return self.mcp_manager.list_tools(server_id)
        except MCPError as exc:
            raise ValueError(str(exc)) from exc

    def call_mcp_tool(self, server_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        arguments = dict(payload.get("arguments") or {})
        try:
            return self.mcp_manager.call_tool(server_id, str(payload.get("tool") or ""), arguments)
        except MCPError as exc:
            raise ValueError(str(exc)) from exc

    # --- Code Intelligence (LSP) --------------------------------------------

    def _load_code_intel_servers(self) -> list[dict[str, Any]]:
        return list((self.repository.get_setting("code_intel") or {}).get("servers") or [])

    def _save_code_intel_servers(self, servers: list[dict[str, Any]]) -> None:
        self.repository.set_setting("code_intel", {"servers": servers})

    def code_intel_servers(self) -> dict[str, Any]:
        return {"servers": self.code_intel.list_servers()}

    def code_languages(self, project_id: str | None = None) -> dict[str, Any]:
        project_id = project_id or "architectos"
        try:
            root = self._project_root(project_id)
        except ValueError as exc:
            return self._empty_system_project_response(project_id, "languages", str(exc))
        counts = Counter(path.suffix.lower() for path in self._iter_importable_files(root, 2000) if path.suffix)
        servers = self.code_intel.list_servers()
        by_extension: dict[str, dict[str, Any]] = {}
        for server in servers:
            for extension in server.get("extensions") or []:
                by_extension[str(extension).lower()] = server
        languages = []
        for extension, count in counts.most_common():
            server = by_extension.get(extension)
            if not server:
                continue
            command = server.get("command") or []
            executable = command[0] if command else ""
            ready = bool(executable and find_executable(str(executable)))
            install_command = resolve_install_command(str(server.get("id") or ""), str(server.get("install_command") or ""))
            languages.append({
                "extension": extension,
                "count": count,
                "language": server.get("language_id") or server.get("id") or extension.lstrip("."),
                "server_id": server.get("id") or "",
                "server_label": server.get("label") or server.get("id") or "",
                "ready": ready,
                "status": "available" if ready else "missing",
                "install_command": install_command,
                "install_runnable": is_runnable_install_command(install_command),
                "command": command,
                "fallback": extension in {".py", ".js", ".jsx", ".ts", ".tsx", ".java"},
            })
        return {"project_id": project_id, "root": str(root), "languages": languages, "count": len(languages)}

    def update_code_intel_server(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"server": self.code_intel.upsert_server(payload)}
        except LSPError as exc:
            raise ValueError(str(exc)) from exc

    def test_code_intel_server(self, server_id: str) -> dict[str, Any]:
        try:
            return self.code_intel.check(server_id)
        except LSPError as exc:
            raise ValueError(str(exc)) from exc

    def install_code_intel_server(self, server_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        timeout = int(payload.get("timeout_seconds") or 600)
        try:
            result = self.code_intel.install(server_id, timeout_seconds=timeout)
        except LSPError as exc:
            raise ValueError(str(exc)) from exc
        return {"project_id": str(payload.get("project_id") or "architectos"), **result}

    def code_symbols(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        root = self._project_root(project_id)
        relative_path = str(payload.get("path") or "").strip()
        if not relative_path:
            raise ValueError("file path is required")
        path = self._safe_project_path(root, relative_path)
        if not path.is_file():
            raise ValueError("file is not readable by ArchitectOS")
        preview = self._read_text_preview(path)
        if not preview["readable"]:
            raise ValueError(preview["message"])
        text = str(preview["text"])
        try:
            return {"project_id": project_id, **self.code_intel.symbols(root, str(path.relative_to(root)), text, path.suffix.lower())}
        except LSPError as exc:
            raise ValueError(str(exc)) from exc

    def code_hover(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        root = self._project_root(project_id)
        relative_path = str(payload.get("path") or "").strip()
        if not relative_path:
            raise ValueError("file path is required")
        path = self._safe_project_path(root, relative_path)
        if not path.is_file():
            raise ValueError("file is not readable by ArchitectOS")
        preview = self._read_text_preview(path)
        if not preview["readable"]:
            raise ValueError(preview["message"])
        text = str(preview["text"])
        try:
            return {"project_id": project_id, **self.code_intel.hover(root, str(path.relative_to(root)), text, path.suffix.lower(), int(payload.get("line") or 0), int(payload.get("character") or 0))}
        except LSPError as exc:
            raise ValueError(str(exc)) from exc

    def code_diagnostics(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        root = self._project_root(project_id)
        relative_path = str(payload.get("path") or "").strip()
        if not relative_path:
            raise ValueError("file path is required")
        path = self._safe_project_path(root, relative_path)
        if not path.is_file():
            raise ValueError("file is not readable by ArchitectOS")
        preview = self._read_text_preview(path)
        if not preview["readable"]:
            raise ValueError(preview["message"])
        text = str(preview["text"])
        return {"project_id": project_id, **self.code_intel.diagnostics(str(path.relative_to(root)), text, path.suffix.lower())}

    def code_references(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        root = self._project_root(project_id)
        relative_path = str(payload.get("path") or "").strip()
        if not relative_path:
            raise ValueError("file path is required")
        path = self._safe_project_path(root, relative_path)
        if not path.is_file():
            raise ValueError("file is not readable by ArchitectOS")
        preview = self._read_text_preview(path)
        if not preview["readable"]:
            raise ValueError(preview["message"])
        text = str(preview["text"])
        try:
            return {"project_id": project_id, **self.code_intel.references(root, str(path.relative_to(root)), text, path.suffix.lower(), int(payload.get("line") or 0), int(payload.get("character") or 0), str(payload.get("query") or ""))}
        except LSPError as exc:
            raise ValueError(str(exc)) from exc
