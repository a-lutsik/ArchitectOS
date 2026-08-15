from __future__ import annotations

import logging
import os
import queue
import secrets
import threading
from pathlib import Path
from typing import Any

from .adapters import ProviderRouter
from .council import CouncilOrchestrator
from .code_graph import CodeGraphIngestor
from .config import APP_ENV, APP_NAME, APP_VERSION, BACKUP_RETENTION
from .files import FileStore
from .lsp import CodeIntelligenceManager
from .mcp import MCPManager
from .embeddings import MemoryEmbeddingEngine, build_embedding_provider
from .graph_service import GraphAutoLinker, GraphServiceMixin
from .retrieval_service import RetrievalServiceMixin
from .chat_service import ChatServiceMixin
from .ingestion_service import IngestionServiceMixin, MemoryIngestionEngine
from .azure_sync_service import AzureSyncServiceMixin
from .project_scan_service import ProjectScanServiceMixin
from .chat_session_service import ChatSessionServiceMixin
from .tool_exec_service import ToolExecServiceMixin
from .integrations_service import IntegrationsServiceMixin
from .providers_council_service import ProvidersCouncilServiceMixin
from .settings_router_service import SettingsRouterServiceMixin
from .ai_runtime_service import AiRuntimeServiceMixin
from .lifecycle_service import LifecycleServiceMixin, MemoryLifecycleEngine
from .search import HybridSearchStrategy
from .release import release_manifest
from .security import SecurityPolicy
from .storage import SQLiteMemoryRepository
from .tool_gateway import (
    ToolGateway,
)

_LOG = logging.getLogger("architectos.service")


class ArchitectOSService(AiRuntimeServiceMixin, SettingsRouterServiceMixin, ProvidersCouncilServiceMixin, IntegrationsServiceMixin, ToolExecServiceMixin, ProjectScanServiceMixin, AzureSyncServiceMixin, IngestionServiceMixin, ChatSessionServiceMixin, ChatServiceMixin, RetrievalServiceMixin, GraphServiceMixin, LifecycleServiceMixin):
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        # Per-instance API token: the HTTP layer requires it for every /api/*
        # call so a malicious website cannot drive the local server (CSRF).
        self.auth_token = secrets.token_hex(32)
        self._load_project_env_files()
        self.repository = SQLiteMemoryRepository(project_root)
        retrieval_settings = (self.repository.get_setting("memory_retrieval") or {}) if hasattr(self.repository, "get_setting") else {}
        self.embedding_provider = build_embedding_provider(retrieval_settings)
        self.memory_embeddings = MemoryEmbeddingEngine(self.repository, self.embedding_provider)
        self.search_strategy = HybridSearchStrategy()
        # Embedding indexing goes through a queue: without a background worker
        # (tests, short-lived instances) it runs inline; the HTTP entry points
        # start the worker via start_background_maintenance().
        self._embedding_index_queue: queue.Queue[str] = queue.Queue()
        self._embedding_worker_started = False
        self.repository.register_node_upsert_listener(self._enqueue_embedding_index)
        self.provider_router = ProviderRouter(project_root)
        self.security_policy = SecurityPolicy()
        self.ingestion_engine = MemoryIngestionEngine(self.repository)
        self.memory_lifecycle = MemoryLifecycleEngine(self.repository)
        self._decay_stop = threading.Event()
        self._chat_session_stop = threading.Event()
        self._chat_finalize_lock = threading.Lock()
        self._chat_finalize_inflight: set[str] = set()
        self.graph_auto_linker = GraphAutoLinker(self.repository)
        self.code_graph = CodeGraphIngestor(
            self.repository,
            lambda: (self.repository.get_setting("code_graph") or {}) if hasattr(self.repository, "get_setting") else {},
        )
        self.mcp_manager = MCPManager(
            project_root,
            self._load_mcp_servers,
            self._save_mcp_servers,
            filesystem_root=self._filesystem_mcp_root,
        )
        self.tool_gateway = ToolGateway(
            self.mcp_manager,
            boards_project=lambda: self._azure_boards_project({}),
            native_handlers={
                "memory_search": self._tool_memory_search,
                "memory_get": self._tool_memory_get,
                "fs_read": self._tool_fs_read,
                "fs_list": self._tool_fs_list,
                "fs_search": self._tool_fs_search,
                "fs_write": self._tool_fs_write,
            },
        )
        self.code_intel = CodeIntelligenceManager(project_root, self._load_code_intel_servers, self._save_code_intel_servers)
        self.council = CouncilOrchestrator(self.run_ai, self._agent_selectable_providers)
        self.files = FileStore(self.repository.data_dir / "uploads")
        self.cancelled_runs: set[str] = set()
        # Guards cancelled_runs: request threads add, provider threads poll.
        self._cancelled_runs_lock = threading.Lock()
        self.repository.seed_if_empty()
        self._memory_rescan_lock = threading.Lock()
        self._memory_rescan_state: dict[str, Any] = {
            "running": False,
            "trigger": "",
            "started_at": "",
            "finished_at": "",
            "error": "",
            "result": None,
        }
        self._memory_ingest_lock = threading.Lock()
        self._memory_ingest_state: dict[str, Any] = {
            "running": False,
            "project_id": "",
            "sources": [],
            "current": "",
            "started_at": "",
            "finished_at": "",
            "error": "",
            "result": None,
            "logs": [],
        }

    def _load_project_env_files(self) -> None:
        for filename in (".env.local", ".env"):
            path = self.project_root / filename
            if not path.is_file():
                continue
            try:
                for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    item = line.strip()
                    if not item or item.startswith("#") or "=" not in item:
                        continue
                    key, value = item.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and value and key not in os.environ:
                        os.environ[key] = value
            except OSError:
                continue

    def health(self) -> dict[str, Any]:
        return {"ok": True, "app": APP_NAME, "version": APP_VERSION, "environment": APP_ENV, "root": str(self.project_root)}

    def version(self) -> dict[str, Any]:
        manifest = release_manifest(self.project_root)
        return {
            "app": APP_NAME,
            "version": APP_VERSION,
            "environment": APP_ENV,
            "root": str(self.project_root),
            "release": {"ready": manifest["ready"], "file_count": manifest["file_count"], "checks": manifest["checks"]},
        }

    def readiness(self) -> dict[str, Any]:
        frontend = {
            "status": "ok" if all((self.project_root / "frontend" / name).exists() for name in ("index.html", "app.js", "styles.css")) else "error",
            "files": ["index.html", "app.js", "styles.css"],
        }
        database = self.repository.health_check()
        security = {"status": "ok" if self.repository.get_setting("security") else "error", "policy": self.repository.get_setting("security") or {}}
        release = release_manifest(self.project_root)
        providers = {"status": "ok" if self.repository.list_providers() else "error", "count": len(self.repository.list_providers())}
        checks = {"database": database, "frontend": frontend, "security": security, "release": {"status": "ok" if release["ready"] else "error", "checks": release["checks"]}, "providers": providers}
        ok = all(item.get("status") == "ok" for item in checks.values())
        return {"ok": ok, "app": APP_NAME, "version": APP_VERSION, "environment": APP_ENV, "checks": checks}

    def create_backup(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        retention = max(1, int(payload.get("retention") or BACKUP_RETENTION))
        return self.repository.create_backup(str(payload.get("label") or ""), retention)

    def list_backups(self, limit: int = 20) -> dict[str, Any]:
        return {"backups": self.repository.list_backups(limit)}

    def security_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        inspected = self.security_policy.inspect_text(str(payload.get("text") or ""))
        return {
            "redacted": inspected["redacted"],
            "text": inspected["text"],
            "findings": inspected["findings"],
            "finding_count": inspected["finding_count"],
            "policy": self.repository.get_setting("security") or {},
        }

    def system_folder_picker(self, payload: dict[str, Any]) -> dict[str, Any]:
        initial_path = Path(str(payload.get("initial_path") or self.project_root)).expanduser()
        if not initial_path.exists() or not initial_path.is_dir():
            initial_path = self.project_root
        from .folder_picker import pick_folder_subprocess

        return pick_folder_subprocess(str(initial_path))

    def analytics(self, project_id: str | None = None) -> dict[str, Any]:
        payload = self.repository.analytics(project_id)
        payload["memory_lifecycle"] = self.memory_lifecycle.analytics(project_id)
        try:
            payload["embeddings"] = {
                **self.memory_embeddings.provider_info(),
                "coverage": self.repository.memory_embedding_coverage(project_id),
            }
        except Exception as exc:
            _LOG.warning("analytics embeddings coverage skipped: %s", exc)
            payload["embeddings"] = {"coverage": {"active_nodes": 0, "indexed": 0, "missing": 0, "coverage_pct": 0.0}}
        try:
            payload["provider_usage"] = self._provider_run_stats(limit=500, project_id=project_id).get("usage") or {
                "totals": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0, "runs_with_usage": 0},
                "by_model": [],
            }
        except Exception as exc:
            _LOG.warning("analytics provider usage skipped: %s", exc)
            payload["provider_usage"] = {
                "totals": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0, "runs_with_usage": 0},
                "by_model": [],
            }
        return payload
