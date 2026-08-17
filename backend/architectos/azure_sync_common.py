"""Shared helpers for Azure DevOps / Teams sync mixins."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .mcp import MCPError
from .tool_gateway import mcp_tool_error_text

_LOG = logging.getLogger("architectos.service")


class AzureSyncCommonMixin:
    """Helpers shared across Boards/Git/Wiki sync mixins."""

    @staticmethod
    def _azure_mcp_require_success(result: Any, tool_name: str) -> Any:
        """Raise when Azure DevOps MCP returns isError (otherwise ingest silently shows 0 items)."""
        payload = result.get("result") if isinstance(result, dict) and "result" in result else result
        message = mcp_tool_error_text(result)
        if message:
            raise MCPError(message or f"{tool_name} failed.")
        return payload

    def _find_memory_node_by_metadata(self, project_id: str, key: str, value: str):
        needle = str(value or "").strip()
        if not needle:
            return None
        for node in self.repository.list_nodes():
            if node.status != "active":
                continue
            if node.project_id not in {project_id, None}:
                continue
            if str(dict(node.metadata or {}).get(key) or "").strip() == needle:
                return node
        return None

    def _azure_boards_project(self, payload: dict[str, Any]) -> str:
        for key in ("ado_project", "azure_project", "boards_project"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        for key in ("ado_mcp_project", "ADO_PROJECT", "AZURE_DEVOPS_PROJECT"):
            value = str(os.environ.get(key) or "").strip()
            if value:
                return value
        server = self.mcp_manager.get_server("azure-devops")
        if server:
            env_project = str((server.env or {}).get("ado_mcp_project") or "").strip()
            if env_project:
                return env_project
        return "E-AI"

    def _azure_boards_payloads(self, result: Any) -> list[Any]:
        payloads: list[Any] = []
        if isinstance(result, dict):
            if "structuredContent" in result:
                payloads.append(result["structuredContent"])
            content = result.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text = str(item.get("text") or "").strip()
                        if not text:
                            continue
                        try:
                            payloads.append(json.loads(text))
                        except json.JSONDecodeError:
                            payloads.append({"text": text})
                    elif isinstance(item, dict):
                        payloads.append(item)
            payloads.append(result)
        elif isinstance(result, list):
            payloads.extend(result)
        elif isinstance(result, str):
            text = result.strip()
            if text:
                try:
                    payloads.append(json.loads(text))
                except json.JSONDecodeError:
                    payloads.append({"text": text})
        return payloads

    @staticmethod
    def _azure_boards_identity(value: Any) -> str:
        if isinstance(value, dict):
            name = str(value.get("displayName") or value.get("name") or "").strip()
            email = str(value.get("uniqueName") or value.get("mailAddress") or "").strip()
            if name and email:
                return f"{name} <{email}>"
            return name or email
        return str(value or "").strip()
