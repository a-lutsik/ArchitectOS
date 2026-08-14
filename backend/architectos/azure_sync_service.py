"""Azure DevOps (Boards / Git / Wiki) and Microsoft Teams ingestion for the service.

Extracted from ``service.py``. All Azure/Teams external-source import + candidate
building lives here. Depends only on shared constants and leaf modules, never on
``service`` itself, so it introduces no import cycle. Repository access, MCP
managers and per-source ingest orchestration are reached through ``self`` via the
MRO on :class:`ArchitectOSService`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from pathlib import Path
from typing import Any

from .chunking import (
    work_item_comment_chunks,
    work_item_description_chunks,
    work_item_main_parts,
)
from .constants import (
    ADO_BOARD_MEMORY_TYPES,
    ADO_BOARD_WORK_ITEM_TYPES,
    ADO_FETCH_WORKERS,
    ADO_ITEM_TIMEOUT,
    ADO_MAX_RELATIONS,
)
from .mcp import MCPError
from .models import stable_id, utc_now
from .teams_graph import TeamsGraphClient
from .tool_gateway import call_ado_tool, mcp_tool_error_text

_LOG = logging.getLogger("architectos.service")


class AzureSyncServiceMixin:
    """Azure Boards/Git/Wiki + Teams import, candidate building and node upserts."""

    def _import_azure_boards_to_memory(self, project_id: str, limit: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        items = self._ingest_azure_boards_candidates(project_id, limit, payload)
        imported: list[dict[str, Any]] = []
        updated = 0
        nodes: list[Any] = []
        chunk_nodes = 0
        for item in items:
            node, was_update = self._upsert_azure_boards_memory_node(project_id, item)
            if was_update:
                updated += 1
            chunk_nodes += self._sync_azure_boards_work_item_chunks(node, item, project_id)
            nodes.append((node, item))
            imported.append(node.to_dict())
            self._set_ingest_partial(payload, {
                "imported": list(imported),
                "updated": updated,
                "count": len(imported),
                "chunk_nodes": chunk_nodes,
            })

        # Build work item lookup map once to optimize DB queries
        by_work_item: dict[str, Any] = {}
        for existing in self.repository.list_nodes():
            if existing.status != "active":
                continue
            if existing.project_id not in {project_id, None}:
                continue
            meta = dict(existing.metadata or {})
            if str(meta.get("chunk_role") or "").strip():
                continue
            work_item_id = str(meta.get("work_item_id") or "").strip()
            if work_item_id:
                by_work_item[work_item_id] = existing

        for node, item in nodes:
            self._link_azure_boards_relations(node, item, by_work_item)
        result = {
            "imported": imported,
            "updated": updated,
            "count": len(imported),
            "chunk_nodes": chunk_nodes,
        }
        self._set_ingest_partial(payload, result)
        return result

    def _upsert_azure_boards_memory_node(self, project_id: str, item: dict[str, Any]) -> tuple[Any, bool]:
        metadata = self._azure_boards_memory_metadata(item)
        work_item_id = str(metadata.get("work_item_id") or "").strip()
        existing = self._find_memory_node_by_work_item_id(project_id, work_item_id) if work_item_id else None
        if existing:
            existing.type = str(item.get("type") or existing.type)
            existing.label = str(item.get("label") or existing.label)
            existing.text = str(item.get("text") or existing.text)
            existing.confidence = float(item.get("confidence") or existing.confidence or 0.8)
            merged = dict(existing.metadata or {})
            merged.update(metadata)
            existing.metadata = merged
            existing.updated_at = utc_now()
            node = self.repository.upsert_node(existing)
            node = self.memory_lifecycle.initialize_node(node, "azure_boards_refresh")
            self.graph_auto_linker.link_node(node, project_id)
            return node, True
        node = self.repository.add_node(
            str(item.get("type") or "Artifact"),
            str(item.get("label") or f"ADO work item {work_item_id}"),
            str(item.get("scope") or "project"),
            str(item.get("text") or ""),
            self._memory_project_id_for_scope(str(item.get("scope") or "project"), project_id),
            None,
            float(item.get("confidence") or 0.8),
            metadata,
        )
        node = self.memory_lifecycle.initialize_node(node, "azure_boards")
        self.graph_auto_linker.link_node(node, project_id)
        return node, False

    def _azure_boards_memory_metadata(self, item: dict[str, Any]) -> dict[str, Any]:
        meta = dict(item.get("metadata") or {})
        payload = {
            "source": "azure_boards",
            "source_type": "azure-boards",
            "source_ref": item.get("source_ref") or meta.get("work_item_id"),
        }
        for key in (
            "work_item_id",
            "work_item_type",
            "work_item_state",
            "work_item_url",
            "assigned_to",
            "iteration_path",
            "area_path",
            "tags",
            "relations",
            "comments",
            "parent_id",
            "ado_project",
            "template",
            "chunk_role",
            "description_full",
            "comment_chunk_count",
            "description_chunk_count",
        ):
            if meta.get(key) not in (None, "", [], {}):
                payload[key] = meta[key]
        return payload

    def _find_memory_node_by_work_item_id(self, project_id: str, work_item_id: str):
        for node in self.repository.list_nodes():
            if node.status != "active":
                continue
            if node.project_id not in {project_id, None}:
                continue
            meta = dict(node.metadata or {})
            if str(meta.get("chunk_role") or "") in {"comment", "description"}:
                continue
            if str(meta.get("work_item_id") or "").strip() == work_item_id:
                return node
        return None

    def _azure_git_mcp_server_id(self) -> str:
        # Prefer the repositories-scoped server; the boards server may omit git tools.
        for server_id in ("azure-devops-git", "azure-devops"):
            server = self.mcp_manager.get_server(server_id)
            if server and server.enabled:
                return server_id
        raise ValueError("Enable Azure DevOps or Azure DevOps Git MCP server before ingesting repos/PRs.")

    def _import_azure_git_to_memory(self, project_id: str, limit: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        server_id = self._azure_git_mcp_server_id()
        ado_project = self._azure_boards_project(payload)
        items = self._ingest_azure_git_candidates(project_id, ado_project, server_id, limit, payload)
        imported: list[dict[str, Any]] = []
        updated = 0
        for item in items:
            node, was_update = self._upsert_azure_git_memory_node(project_id, item)
            if was_update:
                updated += 1
            imported.append(node.to_dict())
            self.graph_auto_linker.link_node(node, project_id)
        return {"imported": imported, "updated": updated, "count": len(imported)}

    def _ingest_azure_git_candidates(
        self,
        project_id: str,
        ado_project: str,
        server_id: str,
        limit: int,
        payload: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        payload = payload or {}
        candidates: list[dict[str, Any]] = []
        preferred = {name.lower() for name in self._azure_git_preferred_repo_names(project_id, payload)}
        item_timeout = self._ingest_item_timeout(payload, 30)
        repos = self._azure_git_list_repos(server_id, ado_project, timeout=item_timeout)
        if preferred:
            repos = sorted(
                repos,
                key=lambda item: (0 if str(item.get("name") or "").lower() in preferred else 1, str(item.get("name") or "").lower()),
            )
        repo_budget = max(1, min(limit, 12))
        for repo in repos[:repo_budget]:
            if self._ingest_deadline_expired(payload, "azure-git"):
                return candidates[: max(limit * 2, limit)]
            candidate = self._build_azure_git_repo_candidate(project_id, ado_project, repo)
            if candidate:
                candidates.append(candidate)
        if self._ingest_deadline_expired(payload, "azure-git"):
            return candidates[: max(limit * 2, limit)]
        pr_status = str(payload.get("pr_status") or payload.get("pull_request_status") or "All").strip() or "All"
        pull_requests = self._azure_git_list_pull_requests(
            server_id,
            ado_project,
            max(limit * 2, 20),
            pr_status,
            timeout=item_timeout,
        )
        if preferred:
            pull_requests = sorted(
                pull_requests,
                key=lambda item: (
                    0 if str(item.get("repository") or item.get("repositoryName") or "").lower() in preferred else 1,
                    str(item.get("creationDate") or ""),
                ),
                reverse=False,
            )
            # Keep preferred first, then newest creationDate among the rest
            preferred_prs = [item for item in pull_requests if str(item.get("repository") or "").lower() in preferred]
            other_prs = [item for item in pull_requests if str(item.get("repository") or "").lower() not in preferred]
            preferred_prs.sort(key=lambda item: str(item.get("creationDate") or ""), reverse=True)
            other_prs.sort(key=lambda item: str(item.get("creationDate") or ""), reverse=True)
            pull_requests = preferred_prs + other_prs
        else:
            pull_requests.sort(key=lambda item: str(item.get("creationDate") or ""), reverse=True)
        pr_budget = max(1, limit)
        for pull_request in pull_requests[:pr_budget]:
            candidate = self._build_azure_git_pr_candidate(project_id, ado_project, pull_request)
            if candidate:
                candidates.append(candidate)
        return candidates[: max(limit * 2, limit)]

    def _azure_git_preferred_repo_names(self, project_id: str, payload: dict[str, Any] | None = None) -> list[str]:
        names: list[str] = []
        payload = payload or {}
        for key in ("ado_repository", "repository", "repo", "repo_name"):
            value = str(payload.get(key) or "").strip()
            if value:
                names.append(value)
        project = self.repository.get_project(project_id)
        if project:
            if project.name:
                names.append(str(project.name))
            root = str(project.root_path or "").strip()
            if root:
                names.append(Path(root).expanduser().name)
        # Deduplicate while preserving order
        seen: set[str] = set()
        ordered: list[str] = []
        for name in names:
            key = name.strip()
            if not key or key.lower() in seen:
                continue
            seen.add(key.lower())
            ordered.append(key)
        return ordered

    def _azure_git_list_repos(
        self,
        server_id: str,
        ado_project: str,
        *,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        if not server_id:
            raise ValueError("Azure Git MCP server id is required.")
        self._log_ingest(
            f"Listing Azure Git repos in project '{ado_project}' via {server_id}...",
            source="azure-git",
            current="azure-git",
        )
        result = call_ado_tool(
            self.mcp_manager,
            "list_repos",
            {"project": ado_project},
            server_id=server_id,
            timeout=timeout or 30,
        )
        tool_result = self._azure_mcp_require_success(result, "repo list")
        repos: list[dict[str, Any]] = []
        for payload in self._azure_boards_payloads(tool_result):
            items = payload if isinstance(payload, list) else payload.get("value") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                if isinstance(payload, dict) and payload.get("id") and payload.get("name"):
                    items = [payload]
                else:
                    continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("isDisabled"):
                    continue
                name = str(item.get("name") or "").strip()
                repo_id = str(item.get("id") or "").strip()
                if not name or not repo_id:
                    continue
                repos.append(item)
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in repos:
            key = str(item.get("id") or item.get("name") or "")
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        self._log_ingest(
            f"Found {len(deduped)} Azure Git repo(s).",
            source="azure-git",
            current="azure-git",
        )
        return deduped

    def _azure_git_list_pull_requests(
        self,
        server_id: str,
        ado_project: str,
        top: int,
        status: str,
        *,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        if not server_id:
            raise ValueError("Azure Git MCP server id is required.")
        arguments: dict[str, Any] = {
            "project": ado_project,
            "top": min(max(top, 1), 50),
        }
        if status:
            arguments["status"] = status
        self._log_ingest(
            f"Listing Azure Git pull requests in '{ado_project}' (status={status or 'All'})...",
            source="azure-git",
            current="azure-git",
        )
        result = call_ado_tool(
            self.mcp_manager,
            "list_pull_requests",
            arguments,
            server_id=server_id,
            timeout=timeout or 30,
        )
        tool_result = self._azure_mcp_require_success(result, "pull request list")
        pull_requests: list[dict[str, Any]] = []
        for payload in self._azure_boards_payloads(tool_result):
            items = payload if isinstance(payload, list) else payload.get("value") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                if isinstance(payload, dict) and (payload.get("pullRequestId") or payload.get("pull_request_id")):
                    items = [payload]
                else:
                    continue
            for item in items:
                if isinstance(item, dict) and (item.get("pullRequestId") or item.get("pull_request_id")):
                    pull_requests.append(item)
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in pull_requests:
            key = str(item.get("pullRequestId") or item.get("pull_request_id") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        self._log_ingest(
            f"Found {len(deduped)} Azure Git pull request(s).",
            source="azure-git",
            current="azure-git",
        )
        return deduped

    @staticmethod
    def _azure_mcp_require_success(result: Any, tool_name: str) -> Any:
        """Raise when Azure DevOps MCP returns isError (otherwise ingest silently shows 0 items)."""
        payload = result.get("result") if isinstance(result, dict) and "result" in result else result
        message = mcp_tool_error_text(result)
        if message:
            raise MCPError(message or f"{tool_name} failed.")
        return payload

    def _build_azure_git_repo_candidate(self, project_id: str, ado_project: str, repo: dict[str, Any]) -> dict[str, Any] | None:
        name = str(repo.get("name") or "").strip()
        repo_id = str(repo.get("id") or "").strip()
        if not name or not repo_id:
            return None
        web_url = str(repo.get("webUrl") or repo.get("remoteUrl") or "").strip()
        size = repo.get("size")
        parts = [f"Azure Git repository: {name}", f"Project: {ado_project}"]
        if web_url:
            parts.append(f"URL: {web_url}")
        if size not in (None, ""):
            parts.append(f"Size bytes: {size}")
        return {
            "id": stable_id("candidate", project_id, "azure-git", "repo", repo_id),
            "project_id": project_id,
            "source_type": "azure-git",
            "source_ref": web_url or repo_id,
            "label": f"Azure Git: {name}",
            "type": "Artifact",
            "scope": "project",
            "text": "\n".join(parts),
            "confidence": 0.88,
            "metadata": {
                "template": "azure_git_repository",
                "source": "azure_git",
                "source_type": "azure-git",
                "ado_project": ado_project,
                "repository_id": repo_id,
                "repository_name": name,
                "repository_url": web_url,
                "kind": "repository",
            },
        }

    def _build_azure_git_pr_candidate(self, project_id: str, ado_project: str, pull_request: dict[str, Any]) -> dict[str, Any] | None:
        pr_id = pull_request.get("pullRequestId") or pull_request.get("pull_request_id")
        try:
            pr_id_int = int(pr_id)
        except (TypeError, ValueError):
            return None
        title = str(pull_request.get("title") or f"Pull request {pr_id_int}").strip()
        status = str(pull_request.get("statusName") or pull_request.get("status") or "").strip()
        repository = str(pull_request.get("repository") or pull_request.get("repositoryName") or "").strip()
        created_by = self._azure_boards_identity(pull_request.get("createdBy"))
        source_branch = str(pull_request.get("sourceRefName") or "").replace("refs/heads/", "")
        target_branch = str(pull_request.get("targetRefName") or "").replace("refs/heads/", "")
        created = str(pull_request.get("creationDate") or "").strip()
        closed = str(pull_request.get("closedDate") or "").strip()
        is_draft = bool(pull_request.get("isDraft"))
        web_url = str(pull_request.get("url") or pull_request.get("webUrl") or "").strip()
        if not web_url and repository:
            web_url = f"https://dev.azure.com/{os.environ.get('ADO_ORG', '').strip()}/{ado_project}/_git/{repository}/pullrequest/{pr_id_int}"
        parts = [f"Azure Git pull request #{pr_id_int}: {title}"]
        if repository:
            parts.append(f"Repository: {repository}")
        if status:
            parts.append(f"Status: {status}")
        if created_by:
            parts.append(f"Created by: {created_by}")
        if source_branch or target_branch:
            parts.append(f"Branches: {source_branch or '?'} → {target_branch or '?'}")
        if created:
            parts.append(f"Created: {created}")
        if closed:
            parts.append(f"Closed: {closed}")
        if is_draft:
            parts.append("Draft: yes")
        if web_url:
            parts.append(f"URL: {web_url}")
        return {
            "id": stable_id("candidate", project_id, "azure-git", "pr", str(pr_id_int), title[:80]),
            "project_id": project_id,
            "source_type": "azure-git",
            "source_ref": web_url or str(pr_id_int),
            "label": f"PR #{pr_id_int}: {title[:120]}",
            "type": "Decision",
            "scope": "project",
            "text": "\n".join(parts),
            "confidence": 0.86,
            "metadata": {
                "template": "azure_git_pull_request",
                "source": "azure_git",
                "source_type": "azure-git",
                "ado_project": ado_project,
                "pull_request_id": str(pr_id_int),
                "pull_request_title": title,
                "pull_request_status": status,
                "pull_request_url": web_url,
                "repository_name": repository,
                "created_by": created_by,
                "source_branch": source_branch,
                "target_branch": target_branch,
                "is_draft": is_draft,
                "kind": "pull_request",
            },
        }

    def _upsert_azure_git_memory_node(self, project_id: str, item: dict[str, Any]) -> tuple[Any, bool]:
        metadata = self._azure_git_memory_metadata(item)
        existing = None
        pr_id = str(metadata.get("pull_request_id") or "").strip()
        repo_id = str(metadata.get("repository_id") or "").strip()
        if pr_id:
            existing = self._find_memory_node_by_metadata(project_id, "pull_request_id", pr_id)
        elif repo_id:
            existing = self._find_memory_node_by_metadata(project_id, "repository_id", repo_id)
        if existing:
            existing.type = str(item.get("type") or existing.type)
            existing.label = str(item.get("label") or existing.label)
            existing.text = str(item.get("text") or existing.text)
            existing.confidence = float(item.get("confidence") or existing.confidence or 0.8)
            merged = dict(existing.metadata or {})
            merged.update(metadata)
            existing.metadata = merged
            existing.updated_at = utc_now()
            node = self.repository.upsert_node(existing)
            node = self.memory_lifecycle.initialize_node(node, "azure_git_refresh")
            return node, True
        node = self.repository.add_node(
            str(item.get("type") or "Artifact"),
            str(item.get("label") or "Azure Git item"),
            str(item.get("scope") or "project"),
            str(item.get("text") or ""),
            self._memory_project_id_for_scope(str(item.get("scope") or "project"), project_id),
            None,
            float(item.get("confidence") or 0.8),
            metadata,
        )
        node = self.memory_lifecycle.initialize_node(node, "azure_git")
        return node, False

    def _azure_git_memory_metadata(self, item: dict[str, Any]) -> dict[str, Any]:
        meta = dict(item.get("metadata") or {})
        payload = {
            "source": "azure_git",
            "source_type": "azure-git",
            "source_ref": item.get("source_ref") or meta.get("pull_request_url") or meta.get("repository_url"),
        }
        for key in (
            "kind",
            "ado_project",
            "repository_id",
            "repository_name",
            "repository_url",
            "pull_request_id",
            "pull_request_title",
            "pull_request_status",
            "pull_request_url",
            "created_by",
            "source_branch",
            "target_branch",
            "is_draft",
            "template",
        ):
            if meta.get(key) not in (None, "", [], {}):
                payload[key] = meta[key]
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
        pages: list[dict[str, Any]] = []
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

    def _import_teams_meetings_to_memory(self, project_id: str, limit: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        meetings = self._collect_teams_meetings(limit, payload)
        imported: list[dict[str, Any]] = []
        updated = 0
        for meeting in meetings[:limit]:
            item = self._teams_meeting_item(project_id, meeting)
            if not item:
                continue
            node, was_update = self._upsert_teams_meeting_memory_node(project_id, item)
            if was_update:
                updated += 1
            imported.append(node.to_dict())
        return {"imported": imported, "updated": updated, "count": len(imported)}

    def _ingest_teams_meeting_candidates(self, project_id: str, limit: int, payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for meeting in self._collect_teams_meetings(limit, payload or {})[:limit]:
            item = self._teams_meeting_item(project_id, meeting)
            if item:
                candidates.append(item)
        return candidates

    def _collect_teams_meetings(self, limit: int, payload: dict[str, Any]) -> list[dict[str, Any]]:
        client = payload.get("_teams_graph_client")
        if client is None:
            client = TeamsGraphClient()
        lookback = payload.get("lookback_days") or payload.get("teams_lookback_days")
        include_transcripts = payload.get("include_transcripts")
        include_ai_insights = payload.get("include_ai_insights")
        if include_transcripts is None and "teams_include_transcripts" in payload:
            include_transcripts = payload.get("teams_include_transcripts")
        if include_ai_insights is None and "teams_include_ai_insights" in payload:
            include_ai_insights = payload.get("teams_include_ai_insights")
        return client.collect_meetings(
            limit=limit,
            lookback_days=int(lookback) if lookback not in (None, "") else None,
            include_transcripts=None if include_transcripts is None else bool(include_transcripts),
            include_ai_insights=None if include_ai_insights is None else bool(include_ai_insights),
        )

    def _teams_meeting_item(self, project_id: str, meeting: dict[str, Any]) -> dict[str, Any] | None:
        meeting_id = str(meeting.get("meeting_id") or "").strip()
        if not meeting_id:
            return None
        subject = str(meeting.get("subject") or "Teams meeting").strip() or "Teams meeting"
        start = str(meeting.get("start") or "").strip()
        end = str(meeting.get("end") or "").strip()
        organizer = str(meeting.get("organizer") or "").strip()
        attendees = [str(item).strip() for item in list(meeting.get("attendees") or []) if str(item).strip()]
        insights = str(meeting.get("insights_text") or "").strip()
        transcript = str(meeting.get("transcript_text") or "").strip()
        actions = [str(item).strip() for item in list(meeting.get("action_items") or []) if str(item).strip()]
        if not insights and not transcript:
            return None

        header = [
            f"Teams meeting: {subject}",
            f"When: {start} — {end}" if start or end else "",
            f"Organizer: {organizer}" if organizer else "",
            f"Attendees: {', '.join(attendees[:20])}" if attendees else "",
            f"Join: {meeting.get('join_url')}" if meeting.get("join_url") else "",
        ]
        body_parts = [line for line in header if line]
        if insights:
            body_parts.append("AI Insights / Facilitator notes:\n" + insights[:3500])
        if actions and not insights:
            body_parts.append("Action items:\n" + "\n".join(f"- {item}" for item in actions[:30]))
        if transcript:
            body_parts.append("Transcript:\n" + transcript[:3500])
        text = "\n\n".join(body_parts)
        return {
            "id": stable_id("teams", project_id, meeting_id, subject[:80]),
            "project_id": project_id,
            "source_type": "teams-meetings",
            "source_ref": meeting_id,
            "label": f"Teams: {subject}"[:180],
            "type": "Meeting",
            "scope": "project",
            "text": text[:5000],
            "confidence": 0.82 if insights else 0.74,
            "metadata": {
                "template": "teams_meeting",
                "source": "teams_graph",
                "source_type": "teams-meetings",
                "teams_meeting_id": meeting_id,
                "teams_event_id": meeting.get("event_id") or "",
                "meeting_title": subject,
                "meeting_start": start,
                "meeting_end": end,
                "organizer": organizer,
                "attendees": attendees,
                "join_url": meeting.get("join_url") or "",
                "web_link": meeting.get("web_link") or "",
                "action_items": actions,
                "has_insights": bool(meeting.get("has_insights")),
                "has_transcript": bool(meeting.get("has_transcript")),
                "insight_ids": list(meeting.get("insight_ids") or []),
                "transcript_ids": list(meeting.get("transcript_ids") or []),
            },
        }

    def _upsert_teams_meeting_memory_node(self, project_id: str, item: dict[str, Any]) -> tuple[Any, bool]:
        metadata = dict(item.get("metadata") or {})
        meeting_id = str(metadata.get("teams_meeting_id") or item.get("source_ref") or "").strip()
        existing = self._find_memory_node_by_teams_meeting_id(project_id, meeting_id) if meeting_id else None
        if existing:
            existing.type = str(item.get("type") or existing.type or "Meeting")
            existing.label = str(item.get("label") or existing.label)
            existing.text = str(item.get("text") or existing.text)
            existing.confidence = float(item.get("confidence") or existing.confidence or 0.8)
            merged = dict(existing.metadata or {})
            merged.update(metadata)
            existing.metadata = merged
            existing.updated_at = utc_now()
            node = self.repository.upsert_node(existing)
            node = self.memory_lifecycle.initialize_node(node, "teams_meeting_refresh")
            self.graph_auto_linker.link_node(node, project_id)
            return node, True
        node = self.repository.add_node(
            str(item.get("type") or "Meeting"),
            str(item.get("label") or "Teams meeting"),
            str(item.get("scope") or "project"),
            str(item.get("text") or ""),
            self._memory_project_id_for_scope(str(item.get("scope") or "project"), project_id),
            None,
            float(item.get("confidence") or 0.8),
            metadata,
        )
        node = self.memory_lifecycle.initialize_node(node, "teams_meeting")
        self.graph_auto_linker.link_node(node, project_id)
        return node, False

    def _find_memory_node_by_teams_meeting_id(self, project_id: str, meeting_id: str):
        for node in self.repository.list_nodes():
            if node.status != "active":
                continue
            if node.project_id not in {project_id, None}:
                continue
            if str(dict(node.metadata or {}).get("teams_meeting_id") or "").strip() == meeting_id:
                return node
        return None

    def _ingest_azure_boards_candidates(self, project_id: str, limit: int, payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        payload = payload or {}
        if "_ingest_source_deadline" not in payload:
            payload = self._with_ingest_timeouts("azure-boards", payload)
        server = self.mcp_manager.get_server("azure-devops")
        if not server or not server.enabled:
            raise ValueError("Enable Azure DevOps MCP server before ingesting Boards work items.")
        ado_project = self._azure_boards_project(payload)
        item_timeout = self._ingest_item_timeout(payload, ADO_ITEM_TIMEOUT)
        self._log_ingest("Collecting work item IDs from Azure Boards...", source="azure-boards", current="azure-boards")
        ids = self._azure_boards_collect_ids(ado_project, limit, payload)
        if self._ingest_deadline_expired(payload, "azure-boards"):
            return []
        if not ids:
            self._log_ingest("No work items found in Azure Boards.", source="azure-boards", current="azure-boards")
            return []

        items_to_fetch = ids[:limit]
        total_items = len(items_to_fetch)
        # stdio MCP cannot usefully multiplex; keep workers at 1 unless explicitly raised.
        workers = min(ADO_FETCH_WORKERS, total_items) or 1
        self._log_ingest(
            f"Found {len(ids)} work item(s). Fetching details for up to {limit} items "
            f"({workers} at a time, item_timeout={item_timeout:g}s)...",
            source="azure-boards",
            current="azure-boards",
        )

        results: list[dict[str, Any] | None] = [None] * total_items
        mcp_lock = threading.Lock()

        def fetch_one(idx: int, work_item_id: int) -> str:
            # Serialize MCP calls: parallel requests into one stdio process stall each other.
            with mcp_lock:
                if self._ingest_deadline_expired(payload, "azure-boards"):
                    return f"timeout:{work_item_id}"
                self._log_ingest(
                    f"[{idx + 1}/{total_items}] Fetching work item #{work_item_id}...",
                    source="azure-boards",
                    current="azure-boards",
                )
                try:
                    candidate = self._azure_boards_work_item_candidate(
                        project_id,
                        ado_project,
                        work_item_id,
                        item_timeout=item_timeout,
                    )
                    results[idx] = candidate
                    if candidate:
                        rels = len(list((candidate.get("metadata") or {}).get("relations") or []))
                        # Publish mid-flight so a source timeout can salvage downloads.
                        self._set_ingest_partial(payload, [c for c in results if c is not None])
                        return f"ok:{work_item_id}:{rels}"
                    return f"skip:{work_item_id}"
                except Exception as exc:  # noqa: BLE001 - one bad item must not stop the run
                    self._log_ingest(
                        f"Failed to fetch work item #{work_item_id}: {exc}",
                        level="warn",
                        source="azure-boards",
                        current="azure-boards",
                    )
                    return f"error:{work_item_id}"

        completed = 0
        # Avoid ``with ThreadPoolExecutor``: on timeout/abandon its ``__exit__`` would
        # ``shutdown(wait=True)`` and block on wedged MCP workers past the source budget.
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {
                executor.submit(fetch_one, idx, work_item_id): work_item_id
                for idx, work_item_id in enumerate(items_to_fetch)
            }
            pending = set(futures)
            while pending:
                remaining = self._ingest_deadline_remaining(payload)
                if remaining is not None and remaining <= 0:
                    self._log_ingest(
                        f"Azure Boards source timeout — cancelling {len(pending)} remaining item(s).",
                        level="warn",
                        source="azure-boards",
                        current="azure-boards",
                    )
                    for future in pending:
                        future.cancel()
                    self._abort_ingest_source("azure-boards")
                    self._set_ingest_partial(payload, [c for c in results if c is not None])
                    break
                wait_for = item_timeout + 5.0
                if remaining is not None:
                    wait_for = max(0.1, min(wait_for, remaining))
                try:
                    for future in as_completed(pending, timeout=wait_for):
                        pending.discard(future)
                        completed += 1
                        work_item_id = futures[future]
                        status = "done"
                        try:
                            status = future.result(timeout=0) or "done"
                        except Exception:
                            status = "error"
                        if status.startswith("ok:"):
                            parts = status.split(":")
                            rels = parts[2] if len(parts) > 2 else "?"
                            self._log_ingest(
                                f"[{completed}/{total_items}] Fetched work item #{work_item_id} ({rels} relations).",
                                source="azure-boards",
                                current="azure-boards",
                            )
                        elif status.startswith("skip:"):
                            self._log_ingest(
                                f"[{completed}/{total_items}] Skipped work item #{work_item_id}.",
                                source="azure-boards",
                                current="azure-boards",
                            )
                        elif status.startswith("timeout:"):
                            self._log_ingest(
                                f"[{completed}/{total_items}] Timed out work item #{work_item_id}.",
                                level="warn",
                                source="azure-boards",
                                current="azure-boards",
                            )
                        else:
                            self._log_ingest(
                                f"[{completed}/{total_items}] Failed work item #{work_item_id}.",
                                level="warn",
                                source="azure-boards",
                                current="azure-boards",
                            )
                        break
                except FuturesTimeoutError:
                    # No future finished within the wait window — likely a wedged MCP item.
                    self._log_ingest(
                        f"Azure Boards item wait exceeded {wait_for:g}s — restarting MCP and continuing.",
                        level="warn",
                        source="azure-boards",
                        current="azure-boards",
                    )
                    self._abort_ingest_source("azure-boards")
                    # Drop one stuck future if possible so the loop can progress.
                    stuck = next(iter(pending), None)
                    if stuck is not None:
                        pending.discard(stuck)
                        stuck.cancel()
                        completed += 1
                        work_item_id = futures.get(stuck)
                        if work_item_id:
                            self._log_ingest(
                                f"[{completed}/{total_items}] Abandoned work item #{work_item_id} after timeout.",
                                level="warn",
                                source="azure-boards",
                                current="azure-boards",
                            )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        candidates = [c for c in results if c is not None]
        self._set_ingest_partial(payload, candidates)
        if candidates and self._ingest_deadline_remaining(payload) is not None:
            remaining = self._ingest_deadline_remaining(payload)
            if remaining is not None and remaining <= 0:
                self._log_ingest(
                    f"Azure Boards returning {len(candidates)} candidate(s) collected before source timeout.",
                    level="warn",
                    source="azure-boards",
                    current="azure-boards",
                )
        return candidates

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

    def _azure_boards_collect_ids(self, ado_project: str, limit: int, payload: dict[str, Any]) -> list[int]:
        requested = payload.get("work_item_ids") or payload.get("ids")
        if isinstance(requested, list) and requested:
            ids: list[int] = []
            for item in requested:
                try:
                    value = int(item)
                except (TypeError, ValueError):
                    continue
                if value > 0 and value not in ids:
                    ids.append(value)
            return ids[: max(limit * 2, limit)]

        collected: list[int] = []
        seen: set[int] = set()
        item_timeout = self._ingest_item_timeout(payload, ADO_ITEM_TIMEOUT)

        def add_ids(values: list[int]) -> None:
            for value in values:
                if value > 0 and value not in seen:
                    seen.add(value)
                    collected.append(value)

        # Boards ingest covers the whole project by default; "assigned to me" is an explicit opt-in.
        mine_only = bool(payload.get("mine_only") or payload.get("assigned_to_me"))
        raw_all_items = payload.get("all_items")
        all_items = (True if raw_all_items is None else bool(raw_all_items)) and not mine_only
        types = self._normalize_azure_boards_work_item_types(payload.get("work_item_types"))
        if not types:
            self._log_ingest("No Azure Boards work item types selected.", level="warn", source="azure-boards", current="azure-boards")
            return []
        if self._ingest_deadline_expired(payload, "azure-boards"):
            return []
        if all_items:
            add_ids(self._azure_boards_ids_from_wiql(ado_project, max(limit * 2, 50), types, timeout=item_timeout))
        else:
            add_ids(self._azure_boards_ids_from_my_work(ado_project, max(limit * 2, 20), timeout=item_timeout))

        if len(collected) < limit and not self._ingest_deadline_expired(payload, "azure-boards"):
            search_text = str(payload.get("search_text") or payload.get("query") or "a OR e OR i OR o OR u").strip() or "a OR e"
            add_ids(self._azure_boards_ids_from_search(ado_project, search_text, types, max(limit * 2, 20), timeout=item_timeout))
        return collected

    def _normalize_azure_boards_work_item_types(self, raw: Any) -> list[str]:
        if raw in (None, "", "all", "*"):
            return list(ADO_BOARD_WORK_ITEM_TYPES)
        values = raw if isinstance(raw, list) else [raw]
        known = {item.lower(): item for item in ADO_BOARD_WORK_ITEM_TYPES}
        selected: list[str] = []
        for value in values:
            key = str(value or "").strip().lower()
            if not key:
                continue
            canonical = known.get(key)
            if canonical and canonical not in selected:
                selected.append(canonical)
        return selected

    def _azure_boards_ids_from_wiql(
        self,
        ado_project: str,
        top: int,
        work_item_types: list[str],
        *,
        timeout: float | None = None,
    ) -> list[int]:
        self._log_ingest("Running WIQL query to fetch latest project work items...", source="azure-boards", current="azure-boards")
        escaped_types = ", ".join(f"'{item.replace("'", "''")}'" for item in work_item_types)
        type_clause = f" AND [System.WorkItemType] IN ({escaped_types})" if escaped_types else ""
        try:
            result = call_ado_tool(
                self.mcp_manager,
                "wiql",
                {
                    "wiql": f"SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = '{ado_project}'{type_clause} ORDER BY [System.ChangedDate] DESC",
                    "project": ado_project,
                    "top": max(1, int(top)),
                },
                timeout=timeout or ADO_ITEM_TIMEOUT,
            )
        except MCPError as exc:
            self._log_ingest(f"WIQL query failed: {exc}", level="warn", source="azure-boards", current="azure-boards")
            return []
        ids = self._azure_boards_ids_from_payload(result.get("result") or result)[:top]
        self._log_ingest(f"WIQL query returned {len(ids)} item ID(s).", source="azure-boards", current="azure-boards")
        return ids

    def _azure_boards_ids_from_my_work(self, ado_project: str, top: int, *, timeout: float | None = None) -> list[int]:
        self._log_ingest("Fetching work items assigned to me...", source="azure-boards", current="azure-boards")
        try:
            result = call_ado_tool(
                self.mcp_manager,
                "my_work",
                {"project": ado_project, "type": "assignedtome", "top": top, "includeCompleted": True},
                timeout=timeout or ADO_ITEM_TIMEOUT,
            )
        except MCPError as exc:
            self._log_ingest(f"my work items query failed: {exc}", level="warn", source="azure-boards", current="azure-boards")
            return []
        ids = self._azure_boards_ids_from_payload(result.get("result") or result)
        self._log_ingest(f"Found {len(ids)} item(s) assigned to me.", source="azure-boards", current="azure-boards")
        return ids

    def _azure_boards_ids_from_search(
        self,
        ado_project: str,
        search_text: str,
        work_item_types: list[str],
        top: int,
        *,
        timeout: float | None = None,
    ) -> list[int]:
        self._log_ingest(f"Searching work items matching '{search_text}'...", source="azure-boards", current="azure-boards")
        ids: list[int] = []
        call_timeout = timeout or ADO_ITEM_TIMEOUT
        for work_item_type in work_item_types:
            self._log_ingest(f"Searching type {work_item_type}...", source="azure-boards", current="azure-boards")
            try:
                result = call_ado_tool(
                    self.mcp_manager,
                    "search",
                    {
                        "searchText": search_text,
                        "project": ado_project,
                        "workItemType": [work_item_type],
                        "top": min(top, 25),
                        "skip": 0,
                    },
                    timeout=call_timeout,
                )
            except MCPError:
                continue
            ids.extend(self._azure_boards_ids_from_payload(result.get("result") or result))
            if len(ids) >= top:
                break
        deduped: list[int] = []
        seen: set[int] = set()
        for value in ids:
            if value not in seen:
                seen.add(value)
                deduped.append(value)
        self._log_ingest(f"Search returned {len(deduped[:top])} item ID(s).", source="azure-boards", current="azure-boards")
        return deduped[:top]

    def _azure_boards_work_item_candidate(
        self,
        project_id: str,
        ado_project: str,
        work_item_id: int,
        *,
        item_timeout: float | None = None,
    ) -> dict[str, Any] | None:
        # Prefer relations over expand=all: "all" pulls revisions/attachments and can
        # hang the Azure DevOps MCP stdio process on hub items.
        timeout = float(item_timeout if item_timeout is not None else ADO_ITEM_TIMEOUT)
        timeout = max(5.0, min(timeout, 600.0))
        try:
            detailed = call_ado_tool(
                self.mcp_manager,
                "get_item",
                {"id": work_item_id, "project": ado_project, "expand": "relations"},
                timeout=timeout,
            )
        except MCPError as exc:
            self._log_ingest(
                f"Skipped work item #{work_item_id} (details unavailable: {exc}).",
                level="warn",
                source="azure-boards",
                current="azure-boards",
            )
            # Timed-out/hung MCP calls leave the stdio process wedged; restart so the
            # next item is not blocked behind the dead request.
            if "timed out" in str(exc).lower():
                try:
                    self.mcp_manager.close_session("azure-devops")
                    self._log_ingest(
                        "Restarted Azure DevOps MCP session after timeout.",
                        level="warn",
                        source="azure-boards",
                        current="azure-boards",
                    )
                except Exception:
                    pass
            return None
        work_item = self._azure_boards_first_work_item(detailed.get("result") or detailed)
        if not work_item:
            return None
        comments: list[dict[str, Any]] = []
        fields = dict(work_item.get("fields") or {})
        comment_count = fields.get("System.CommentCount")
        if comment_count is None or int(comment_count) > 0:
            try:
                comments_result = call_ado_tool(
                    self.mcp_manager,
                    "list_comments",
                    {"project": ado_project, "workItemId": work_item_id, "top": 20},
                    timeout=min(timeout, 15.0),
                )
                comments = self._azure_boards_comments_from_payload(comments_result.get("result") or comments_result)
            except MCPError as exc:
                comments = []
                if "timed out" in str(exc).lower():
                    try:
                        self.mcp_manager.close_session("azure-devops")
                    except Exception:
                        pass
        return self._build_azure_boards_candidate(project_id, ado_project, work_item, comments)

    def _build_azure_boards_candidate(
        self,
        project_id: str,
        ado_project: str,
        work_item: dict[str, Any],
        comments: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        fields = dict(work_item.get("fields") or {})
        work_item_id = int(work_item.get("id") or fields.get("System.Id") or 0)
        if work_item_id <= 0:
            return None
        wi_type = str(fields.get("System.WorkItemType") or "Work Item").strip()
        title = str(fields.get("System.Title") or f"Work Item {work_item_id}").strip()
        state = str(fields.get("System.State") or "").strip()
        assigned = self._azure_boards_identity(fields.get("System.AssignedTo"))
        iteration = str(fields.get("System.IterationPath") or "").strip()
        area = str(fields.get("System.AreaPath") or "").strip()
        tags = str(fields.get("System.Tags") or "").strip()
        description = self._azure_boards_html_to_text(
            fields.get("System.Description")
            or fields.get("Microsoft.VSTS.TCM.ReproSteps")
            or fields.get("System.History")
            or ""
        )
        acceptance = self._azure_boards_html_to_text(fields.get("Microsoft.VSTS.Common.AcceptanceCriteria") or "")
        parent_id = fields.get("System.Parent")
        relations = self._azure_boards_relations(work_item)
        url = str(work_item.get("url") or fields.get("System.Href") or f"https://dev.azure.com/{ado_project}/_workitems/edit/{work_item_id}")
        desc_chunks = work_item_description_chunks(description)
        comment_chunks = work_item_comment_chunks(comments)
        text = work_item_main_parts(
            wi_type=wi_type,
            work_item_id=work_item_id,
            title=title,
            state=state,
            assigned=assigned,
            iteration=iteration,
            area=area,
            tags=tags,
            parent_id=parent_id,
            description=description,
            acceptance=acceptance,
            relations=relations,
            comment_count=len(comment_chunks),
            description_chunk_count=len(desc_chunks),
        )
        memory_type = ADO_BOARD_MEMORY_TYPES.get(wi_type.lower(), "Requirement" if "req" in wi_type.lower() else "Artifact")
        return {
            "id": stable_id("candidate", project_id, "azure-boards", str(work_item_id), title[:80]),
            "project_id": project_id,
            "source_type": "azure-boards",
            "source_ref": str(work_item_id),
            "label": f"ADO {wi_type} #{work_item_id}: {title}"[:180],
            "type": memory_type,
            "scope": "project",
            "text": text[:4500],
            "confidence": 0.8,
            "metadata": {
                "template": "azure_boards_work_item",
                "source": "azure_devops_mcp",
                "chunk_role": "main",
                "work_item_id": str(work_item_id),
                "work_item_type": wi_type,
                "work_item_state": state,
                "work_item_url": url,
                "assigned_to": assigned,
                "iteration_path": iteration,
                "area_path": area,
                "tags": tags,
                "parent_id": str(parent_id or ""),
                "ado_project": ado_project,
                "relations": relations,
                "comments": comments[:12],
                "description_full": description[:12000],
                "comment_chunk_count": len(comment_chunks),
                "description_chunk_count": len(desc_chunks),
            },
        }

    def _sync_azure_boards_work_item_chunks(self, parent_node, item: dict[str, Any], project_id: str) -> int:
        """Create/update linked comment + overflow-description nodes for a work item."""
        metadata = dict(item.get("metadata") or {})
        work_item_id = str(metadata.get("work_item_id") or "").strip()
        if not work_item_id:
            return 0
        comments = list(metadata.get("comments") or [])
        description = str(metadata.get("description_full") or "")
        created = 0
        keep_ids: set[str] = set()

        for chunk in work_item_comment_chunks(comments):
            node = self._upsert_azure_boards_chunk_node(
                project_id=project_id,
                parent_node=parent_node,
                work_item_id=work_item_id,
                chunk_role="comment",
                chunk_key=f"comment:{chunk['comment_id']}:{chunk['part_index']}",
                label=str(chunk["label"]),
                text=str(chunk["text"]),
                extra={
                    "comment_id": chunk["comment_id"],
                    "comment_author": chunk["author"],
                    "comment_created_date": chunk["created_date"],
                    "part_index": chunk["part_index"],
                    "part_count": chunk["part_count"],
                },
                edge_type="HAS_COMMENT",
            )
            keep_ids.add(node.id)
            created += 1

        for index, piece in enumerate(work_item_description_chunks(description)):
            node = self._upsert_azure_boards_chunk_node(
                project_id=project_id,
                parent_node=parent_node,
                work_item_id=work_item_id,
                chunk_role="description",
                chunk_key=f"description:{index}",
                label=f"ADO #{work_item_id} description part {index + 1}"[:180],
                text=piece,
                extra={"part_index": index},
                edge_type="HAS_CHUNK",
            )
            keep_ids.add(node.id)
            created += 1

        # Soft-archive stale chunk nodes from prior ingest of this work item.
        for existing in self.repository.list_nodes():
            if existing.status != "active":
                continue
            if existing.project_id not in {project_id, None}:
                continue
            meta = dict(existing.metadata or {})
            if str(meta.get("parent_work_item_id") or "") != work_item_id:
                continue
            if str(meta.get("chunk_role") or "") not in {"comment", "description"}:
                continue
            if existing.id in keep_ids:
                continue
            existing.status = "archived"
            existing.metadata["archived_reason"] = "stale_work_item_chunk"
            self.repository.upsert_node(existing)
        return created

    def _upsert_azure_boards_chunk_node(
        self,
        *,
        project_id: str,
        parent_node,
        work_item_id: str,
        chunk_role: str,
        chunk_key: str,
        label: str,
        text: str,
        extra: dict[str, Any],
        edge_type: str,
    ):
        existing = self._find_memory_node_by_chunk_key(project_id, work_item_id, chunk_key)
        metadata = {
            "source": "azure_boards",
            "source_type": "azure-boards-chunk",
            "chunk_role": chunk_role,
            "chunk_key": chunk_key,
            "work_item_id": work_item_id,
            "parent_work_item_id": work_item_id,
            "parent_node_id": parent_node.id,
            **extra,
        }
        if existing:
            existing.label = label
            existing.text = text
            existing.type = "Doc"
            existing.status = "active"
            merged = dict(existing.metadata or {})
            merged.update(metadata)
            existing.metadata = merged
            node = self.repository.upsert_node(existing)
        else:
            node = self.repository.add_node(
                "Doc",
                label,
                "project",
                text,
                self._memory_project_id_for_scope("project", project_id),
                None,
                0.72,
                metadata,
            )
            node = self.memory_lifecycle.initialize_node(node, "azure_boards_chunk")
        try:
            self.repository.add_edge(parent_node.id, node.id, edge_type, "project", 0.9)
        except Exception:  # noqa: BLE001
            pass
        return node

    def _find_memory_node_by_chunk_key(self, project_id: str, work_item_id: str, chunk_key: str):
        for node in self.repository.list_nodes():
            if node.project_id not in {project_id, None}:
                continue
            meta = dict(node.metadata or {})
            if str(meta.get("parent_work_item_id") or meta.get("work_item_id") or "") != work_item_id:
                continue
            if str(meta.get("chunk_key") or "") == chunk_key:
                return node
        return None

    def _azure_boards_relations(self, work_item: dict[str, Any]) -> list[dict[str, Any]]:
        relations: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for relation in list(work_item.get("relations") or []):
            if not isinstance(relation, dict):
                continue
            rel_type = str(relation.get("rel") or relation.get("type") or "related")
            url = str(relation.get("url") or "")
            related_id = self._azure_boards_id_from_url(url)
            if not related_id:
                continue
            related_id = str(related_id)
            if related_id in seen_ids:
                continue
            seen_ids.add(related_id)
            attributes = dict(relation.get("attributes") or {})
            name = str(attributes.get("name") or attributes.get("comment") or "")
            link_type = rel_type.split(".")[-1].replace("-Forward", "").replace("-Reverse", "")
            if "Hierarchy-Forward" in rel_type:
                link_type = "child"
            elif "Hierarchy-Reverse" in rel_type:
                link_type = "parent"
            elif "Related" in rel_type:
                link_type = "related"
            relations.append({
                "work_item_id": related_id,
                "link_type": link_type,
                "rel": rel_type,
                "name": name,
                "url": url,
            })
        if len(relations) > ADO_MAX_RELATIONS:
            # Keep the most meaningful links (parent/child) first so hub items
            # (Epics/Features with hundreds of relations) stay bounded.
            priority = {"parent": 0, "child": 1, "related": 2}
            relations.sort(key=lambda rel: priority.get(str(rel.get("link_type")), 3))
            relations = relations[:ADO_MAX_RELATIONS]
        return relations

    def _azure_boards_comments_from_payload(self, payload: Any) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        for item in self._azure_boards_payloads(payload):
            if isinstance(item, dict):
                raw_list = item.get("comments") or item.get("value") or item.get("items")
                if isinstance(raw_list, list):
                    for comment in raw_list:
                        parsed = self._azure_boards_normalize_comment(comment)
                        if parsed:
                            comments.append(parsed)
                else:
                    parsed = self._azure_boards_normalize_comment(item)
                    if parsed:
                        comments.append(parsed)
            elif isinstance(item, list):
                for comment in item:
                    parsed = self._azure_boards_normalize_comment(comment)
                    if parsed:
                        comments.append(parsed)
        return comments

    def _azure_boards_normalize_comment(self, comment: Any) -> dict[str, Any] | None:
        if not isinstance(comment, dict):
            return None
        text = self._azure_boards_html_to_text(comment.get("text") or comment.get("body") or comment.get("content") or "")
        if not text:
            return None
        author_raw = comment.get("createdBy") or comment.get("author") or comment.get("user") or {}
        author = self._azure_boards_identity(author_raw) if isinstance(author_raw, (dict, str)) else "unknown"
        return {
            "id": str(comment.get("id") or ""),
            "author": author,
            "created_date": str(comment.get("createdDate") or comment.get("created_date") or ""),
            "text": text[:800],
        }

    def _azure_boards_first_work_item(self, payload: Any) -> dict[str, Any] | None:
        for item in self._azure_boards_payloads(payload):
            if isinstance(item, dict) and (item.get("id") or item.get("fields")):
                return item
            if isinstance(item, list):
                for nested in item:
                    if isinstance(nested, dict) and (nested.get("id") or nested.get("fields")):
                        return nested
            if isinstance(item, dict):
                for key in ("value", "workItems", "results", "data"):
                    value = item.get(key)
                    if isinstance(value, list):
                        for nested in value:
                            if isinstance(nested, dict) and (nested.get("id") or nested.get("fields")):
                                return nested
        return None

    def _azure_boards_ids_from_payload(self, payload: Any) -> list[int]:
        ids: list[int] = []
        for item in self._azure_boards_payloads(payload):
            ids.extend(self._azure_boards_extract_ids(item))
        deduped: list[int] = []
        seen: set[int] = set()
        for value in ids:
            if value not in seen:
                seen.add(value)
                deduped.append(value)
        return deduped

    def _azure_boards_extract_ids(self, payload: Any) -> list[int]:
        found: list[int] = []
        if isinstance(payload, dict):
            for key in ("id", "workItemId", "System.Id"):
                try:
                    value = int(payload.get(key))  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    value = 0
                if value > 0:
                    found.append(value)
            for key in ("ids", "workItemIds", "targetIds"):
                value = payload.get(key)
                if isinstance(value, list):
                    for item in value:
                        try:
                            number = int(item)
                        except (TypeError, ValueError):
                            continue
                        if number > 0:
                            found.append(number)
            for key in ("value", "workItems", "results", "data", "fields"):
                nested = payload.get(key)
                if nested is not None:
                    found.extend(self._azure_boards_extract_ids(nested))
            url = payload.get("url")
            if isinstance(url, str):
                related = self._azure_boards_id_from_url(url)
                if related:
                    found.append(related)
        elif isinstance(payload, list):
            for item in payload:
                found.extend(self._azure_boards_extract_ids(item))
        elif isinstance(payload, (int, float)):
            number = int(payload)
            if number > 0:
                found.append(number)
        elif isinstance(payload, str):
            related = self._azure_boards_id_from_url(payload)
            if related:
                found.append(related)
            else:
                for match in re.findall(r"\b(\d{4,7})\b", payload):
                    found.append(int(match))
        return found

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
    def _azure_boards_id_from_url(url: str) -> int | None:
        match = re.search(r"/work[Ii]tems/(\d+)", url or "")
        if not match:
            return None
        return int(match.group(1))

    @staticmethod
    def _azure_boards_identity(value: Any) -> str:
        if isinstance(value, dict):
            name = str(value.get("displayName") or value.get("name") or "").strip()
            email = str(value.get("uniqueName") or value.get("mailAddress") or "").strip()
            if name and email:
                return f"{name} <{email}>"
            return name or email
        return str(value or "").strip()

    @staticmethod
    def _azure_boards_html_to_text(value: Any) -> str:
        text = str(value or "")
        if not text:
            return ""
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</p>", "\n", text)
        text = re.sub(r"(?i)<li[^>]*>", "- ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()
