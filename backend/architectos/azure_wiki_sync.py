"""Azure Wiki page import and memory upserts."""

from __future__ import annotations

import logging
from typing import Any

from .mcp import MCPError
from .models import stable_id, utc_now
from .tool_gateway import call_ado_tool

_LOG = logging.getLogger("architectos.service")


class AzureWikiSyncMixin:
    """Azure Wiki page ingest."""

    def _import_azure_wiki_to_memory(self, project_id: str, limit: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        if "_ingest_source_deadline" not in payload:
            payload = self._with_ingest_timeouts("azure-wiki", payload)
        server = self.mcp_manager.get_server("azure-devops")
        if not server or not server.enabled:
            raise ValueError("Enable Azure DevOps MCP server before ingesting Wiki pages.")
        ado_project = self._azure_boards_project(payload)
        item_timeout = self._ingest_item_timeout(payload, 20)
        pages = self._azure_wiki_collect_pages(ado_project, limit, payload)
        imported: list[dict[str, Any]] = []
        updated = 0
        for page in pages[:limit]:
            if self._ingest_deadline_expired(payload, "azure-wiki"):
                break
            item = self._azure_wiki_page_item(project_id, ado_project, page, item_timeout=item_timeout)
            if not item:
                continue
            node, was_update = self._upsert_azure_wiki_memory_node(project_id, item)
            if was_update:
                updated += 1
            imported.append(node.to_dict())
        return {"imported": imported, "updated": updated, "count": len(imported)}

    def _ingest_azure_wiki_candidates(self, project_id: str, limit: int, payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        payload = payload or {}
        if "_ingest_source_deadline" not in payload:
            payload = self._with_ingest_timeouts("azure-wiki", payload)
        server = self.mcp_manager.get_server("azure-devops")
        if not server or not server.enabled:
            raise ValueError("Enable Azure DevOps MCP server before ingesting Wiki pages.")
        ado_project = self._azure_boards_project(payload)
        item_timeout = self._ingest_item_timeout(payload, 20)
        candidates: list[dict[str, Any]] = []
        for page in self._azure_wiki_collect_pages(ado_project, limit, payload)[:limit]:
            if self._ingest_deadline_expired(payload, "azure-wiki"):
                break
            item = self._azure_wiki_page_item(project_id, ado_project, page, item_timeout=item_timeout)
            if item:
                candidates.append(item)
        return candidates

    def _azure_wiki_collect_pages(self, ado_project: str, limit: int, payload: dict[str, Any]) -> list[dict[str, Any]]:
        requested = payload.get("wiki_pages") or payload.get("pages")
        if isinstance(requested, list) and requested:
            pages: list[dict[str, Any]] = []
            for item in requested:
                if isinstance(item, dict) and (item.get("path") or item.get("url")):
                    pages.append(item)
                elif isinstance(item, str) and item.strip():
                    pages.append({"path": item.strip()})
            return pages[: max(limit * 2, limit)]

        wikis = self._azure_wiki_list_wikis(ado_project)
        if not wikis:
            return []
        preferred = str(payload.get("wiki_identifier") or payload.get("wiki_id") or "").strip()
        selected = []
        if preferred:
            selected = [wiki for wiki in wikis if str(wiki.get("id") or wiki.get("name") or "") == preferred]
        if not selected:
            selected = wikis
        pages = []
        per_wiki = max(limit, 10)
        for wiki in selected:
            wiki_id = str(wiki.get("id") or wiki.get("name") or "").strip()
            wiki_name = str(wiki.get("name") or wiki_id).strip()
            if not wiki_id:
                continue
            listed = self._azure_wiki_list_pages(ado_project, wiki_id, per_wiki)
            for page in listed:
                page = dict(page)
                page.setdefault("wikiIdentifier", wiki_id)
                page.setdefault("wiki_name", wiki_name)
                page.setdefault("project", ado_project)
                pages.append(page)
                if len(pages) >= limit * 2:
                    return pages
        return pages

    def _azure_wiki_list_wikis(self, ado_project: str) -> list[dict[str, Any]]:
        try:
            result = call_ado_tool(self.mcp_manager, "list_wikis", {"project": ado_project})
        except MCPError:
            return []
        wikis: list[dict[str, Any]] = []
        for payload in self._azure_boards_payloads(result.get("result") or result):
            if isinstance(payload, list):
                wikis.extend(item for item in payload if isinstance(item, dict))
            elif isinstance(payload, dict):
                for key in ("value", "wikis", "results", "data"):
                    value = payload.get(key)
                    if isinstance(value, list):
                        wikis.extend(item for item in value if isinstance(item, dict))
                        break
                else:
                    if payload.get("id") or payload.get("name"):
                        wikis.append(payload)
        return wikis

    def _azure_wiki_list_pages(self, ado_project: str, wiki_identifier: str, top: int) -> list[dict[str, Any]]:
        try:
            result = call_ado_tool(
                self.mcp_manager,
                "list_wiki_pages",
                {"wikiIdentifier": wiki_identifier, "project": ado_project, "top": top},
            )
        except MCPError:
            return []
        pages: list[dict[str, Any]] = []
        for payload in self._azure_boards_payloads(result.get("result") or result):
            if isinstance(payload, list):
                pages.extend(item for item in payload if isinstance(item, dict))
            elif isinstance(payload, dict):
                for key in ("value", "pages", "results", "data"):
                    value = payload.get(key)
                    if isinstance(value, list):
                        pages.extend(item for item in value if isinstance(item, dict))
                        break
                else:
                    if payload.get("path") or payload.get("id"):
                        pages.append(payload)
        return pages

    def _azure_wiki_page_item(
        self,
        project_id: str,
        ado_project: str,
        page: dict[str, Any],
        *,
        item_timeout: float | None = None,
    ) -> dict[str, Any] | None:
        wiki_id = str(page.get("wikiIdentifier") or page.get("wiki_id") or page.get("wikiId") or "").strip()
        path = str(page.get("path") or page.get("pagePath") or "/").strip() or "/"
        if not path.startswith("/"):
            path = f"/{path}"
        wiki_name = str(page.get("wiki_name") or page.get("wikiName") or wiki_id).strip()
        page_id = str(page.get("id") or page.get("pageId") or "").strip()
        url = str(page.get("url") or page.get("remoteUrl") or "").strip()
        arguments: dict[str, Any]
        if url:
            arguments = {"url": url}
        elif wiki_id:
            arguments = {"wikiIdentifier": wiki_id, "project": ado_project, "path": path}
        else:
            return None
        try:
            content_result = call_ado_tool(
                self.mcp_manager,
                "get_wiki_page_content",
                arguments,
                timeout=item_timeout or 20,
            )
        except MCPError:
            return None
        content = self._azure_wiki_content_from_payload(content_result.get("result") or content_result)
        if not content.strip():
            return None
        title = path.rstrip("/").split("/")[-1].replace("-", " ").replace("_", " ").strip() or "Wiki Home"
        page_key = f"{wiki_id}:{path}".lower()
        text = f"Azure Wiki page: {title}\n\nWiki: {wiki_name}\nPath: {path}\nProject: {ado_project}\n\n{content[:3500]}"
        return {
            "id": stable_id("wiki", project_id, page_key, title[:80]),
            "project_id": project_id,
            "source_type": "azure-wiki",
            "source_ref": page_key,
            "label": f"Wiki: {title}"[:180],
            "type": "Doc",
            "scope": "project",
            "text": text[:4500],
            "confidence": 0.78,
            "metadata": {
                "template": "azure_wiki_page",
                "source": "azure_wiki",
                "source_type": "azure-wiki",
                "wiki_page_key": page_key,
                "wiki_id": wiki_id,
                "wiki_name": wiki_name,
                "wiki_path": path,
                "wiki_page_id": page_id,
                "wiki_url": url,
                "ado_project": ado_project,
            },
        }

    def _azure_wiki_content_from_payload(self, payload: Any) -> str:
        chunks: list[str] = []
        for item in self._azure_boards_payloads(payload):
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                for key in ("content", "text", "markdown", "pageContent", "body"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        chunks.append(value)
                        break
                else:
                    content_list = item.get("content")
                    if isinstance(content_list, list):
                        for entry in content_list:
                            if isinstance(entry, dict) and entry.get("type") == "text":
                                text = str(entry.get("text") or "").strip()
                                if text:
                                    chunks.append(text)
        return "\n\n".join(chunk.strip() for chunk in chunks if chunk and chunk.strip()).strip()

    def _upsert_azure_wiki_memory_node(self, project_id: str, item: dict[str, Any]) -> tuple[Any, bool]:
        metadata = dict(item.get("metadata") or {})
        page_key = str(metadata.get("wiki_page_key") or item.get("source_ref") or "").strip()
        existing = self._find_memory_node_by_wiki_page_key(project_id, page_key) if page_key else None
        if existing:
            existing.type = str(item.get("type") or existing.type or "Doc")
            existing.label = str(item.get("label") or existing.label)
            existing.text = str(item.get("text") or existing.text)
            existing.confidence = float(item.get("confidence") or existing.confidence or 0.78)
            merged = dict(existing.metadata or {})
            merged.update(metadata)
            existing.metadata = merged
            existing.updated_at = utc_now()
            node = self.repository.upsert_node(existing)
            node = self.memory_lifecycle.initialize_node(node, "azure_wiki_refresh")
            self.graph_auto_linker.link_node(node, project_id)
            return node, True
        node = self.repository.add_node(
            str(item.get("type") or "Doc"),
            str(item.get("label") or "Azure Wiki page"),
            str(item.get("scope") or "project"),
            str(item.get("text") or ""),
            self._memory_project_id_for_scope(str(item.get("scope") or "project"), project_id),
            None,
            float(item.get("confidence") or 0.78),
            metadata,
        )
        node = self.memory_lifecycle.initialize_node(node, "azure_wiki")
        self.graph_auto_linker.link_node(node, project_id)
        return node, False

    def _find_memory_node_by_wiki_page_key(self, project_id: str, page_key: str):
        for node in self.repository.list_nodes():
            if node.status != "active":
                continue
            if node.project_id not in {project_id, None}:
                continue
            if str(dict(node.metadata or {}).get("wiki_page_key") or "").strip() == page_key:
                return node
        return None
