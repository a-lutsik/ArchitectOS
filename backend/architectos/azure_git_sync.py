"""Azure Repos (git repositories / pull requests) import and memory upserts."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .models import stable_id, utc_now
from .tool_gateway import call_ado_tool

_LOG = logging.getLogger("architectos.service")


class AzureGitSyncMixin:
    """Azure Repos repository and pull-request ingest."""

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
        pr_id: Any = pull_request.get("pullRequestId") or pull_request.get("pull_request_id")
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
