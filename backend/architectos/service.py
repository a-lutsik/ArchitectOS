from __future__ import annotations

from collections import Counter, deque
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from datetime import datetime, timezone
import fnmatch
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .adapters import ProviderRequest, ProviderRouter
from .council import CouncilOrchestrator
from .chat_memory import (
    DEFAULT_CHAT_CANDIDATE_TTL_DAYS,
    DEFAULT_CHAT_MEMORY_MODE,
    evaluate_chat_turn,
    normalize_chat_memory_mode,
)
from .chunking import (
    work_item_comment_chunks,
    work_item_description_chunks,
    work_item_main_parts,
)
from .config import APP_ENV, APP_NAME, APP_VERSION, BACKUP_RETENTION
from .files import FileStore
from .lsp import CodeIntelligenceManager, LSPError, find_executable, is_runnable_install_command, resolve_install_command
from .mcp import MCPManager, MCPError
from .models import Project, stable_id, utc_now
from .routing import RouterPolicy, classify_role
from .embeddings import HashEmbeddingProvider, MemoryEmbeddingEngine, build_embedding_provider
from .search import HybridSearchStrategy, pack_memory_context
from .release import release_manifest
from .rich_response import RESPONSE_FORMAT_POLICY, split_rich_response
from .security import SecurityPolicy
from .storage import SQLiteMemoryRepository, sanitize_text
from .teams_graph import TeamsGraphClient, TeamsGraphError, teams_graph_configured
from .tool_gateway import (
    MAX_TOOL_ROUNDS,
    ToolGateway,
    compact_tool_trace_label,
    format_tool_results_for_prompt,
    parse_tool_calls,
    strip_tool_call_json,
)
from .usage import usage_from_result

TEXT_EXTENSIONS = {".md", ".txt", ".rst", ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".kt", ".kts", ".go", ".rs", ".c", ".h", ".cpp", ".cc", ".hpp", ".cs", ".rb", ".php", ".swift", ".scala", ".sql", ".css", ".scss", ".sass", ".less", ".html", ".htm", ".vue", ".svelte", ".xml", ".gradle", ".groovy", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".toml", ".ini", ".properties", ".json", ".yml", ".yaml", ".tf", ".proto", ".dart", ".kt"}
DOC_EXTENSIONS = {".md", ".txt", ".rst", ".adoc", ".org"}
CODE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".kt", ".kts", ".go", ".rs",
    ".c", ".h", ".cpp", ".cc", ".hpp", ".cs", ".rb", ".php", ".swift", ".scala", ".sql",
    ".css", ".scss", ".sass", ".less", ".html", ".htm", ".vue", ".svelte", ".xml",
    ".gradle", ".groovy", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".toml", ".ini", ".properties", ".json", ".yml", ".yaml", ".tf", ".proto", ".dart",
}
DEFAULT_EXCLUDES = {
    ".git", "__pycache__", "node_modules", ".pytest_cache", "data", "memory",
    "target", "dist", "build", "out", ".idea", ".vscode", "vendor", ".gradle",
    "coverage", ".next", ".turbo", "bin", "obj",
}
TEXT_PREVIEW_BYTES = 200_000
TEXT_SAMPLE_BYTES = 8_192
CODE_FILE_MAX_BYTES = 320_000
GENERIC_FILE_MAX_BYTES = 160_000
SYSTEM_PROJECT_ID = "architectos"
TERMINAL_SHELLS = {
    "powershell": ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"],
    "cmd": ["cmd.exe", "/d", "/c"],
    "bash": ["bash", "-lc"],
    "sh": ["sh", "-lc"],
}
TERMINAL_DANGEROUS_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"\b(remove-item|rm|del|erase|rmdir|rd)\b",
        r"\b(format|diskpart|shutdown|restart-computer|stop-computer|taskkill|stop-process)\b",
        r"\bgit\s+(reset\s+--hard|clean)\b",
        r"\bmkfs(?:\.\w+)?\b",
    ]
]
INGESTION_SOURCE_ALIASES = {
    "issue": "issues",
    "issues": "issues",
    "tracker": "issues",
    "trackers": "issues",
    "pr": "prs",
    "prs": "prs",
    "pull_request": "prs",
    "pull_requests": "prs",
    "meeting": "meetings",
    "meetings": "meetings",
    "granola": "granola",
    "granola_meeting": "granola",
    "granola_meetings": "granola",
    "commit": "git",
    "commits": "git",
    "azure-boards": "azure-boards",
    "azure_boards": "azure-boards",
    "azureboards": "azure-boards",
    "boards": "azure-boards",
    "ado-boards": "azure-boards",
    "ado_boards": "azure-boards",
    "work-items": "azure-boards",
    "work_items": "azure-boards",
    "workitems": "azure-boards",
    "azure-devops": "azure-boards",
    "azure_devops": "azure-boards",
    "azure-wiki": "azure-wiki",
    "azure_wiki": "azure-wiki",
    "azurewiki": "azure-wiki",
    "ado-wiki": "azure-wiki",
    "ado_wiki": "azure-wiki",
    "wiki": "azure-wiki",
    "azure-git": "azure-git",
    "azure_git": "azure-git",
    "azure-repos": "azure-git",
    "azure_repos": "azure-git",
    "ado-git": "azure-git",
    "ado_git": "azure-git",
    "ado-repos": "azure-git",
    "ado_repos": "azure-git",
    "azure-prs": "azure-git",
    "azure_prs": "azure-git",
    "teams": "teams-meetings",
    "teams-meetings": "teams-meetings",
    "teams_meetings": "teams-meetings",
    "teams-meeting": "teams-meetings",
    "ms-teams": "teams-meetings",
    "microsoft-teams": "teams-meetings",
    "graph-teams": "teams-meetings",
    "facilitator": "teams-meetings",
}
ALL_LOCAL_INGESTION_SOURCES = ["docs", "code", "chat", "git", "adr", "issues", "prs", "meetings"]
ALL_INGESTION_SOURCES = [*ALL_LOCAL_INGESTION_SOURCES, "granola", "azure-boards", "azure-wiki", "azure-git", "teams-meetings"]
ADO_BOARD_WORK_ITEM_TYPES = ["Requirement", "Feature", "User Story", "Task", "Bug", "Epic", "Product Backlog Item"]


def _ado_env_int(name: str, default: int, minimum: int = 1, maximum: int = 100000) -> int:
    try:
        value = int(str(os.environ.get(name) or "").strip() or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


# Hub work items (Epics/Features) can carry hundreds of relations. Fetching and
# linking every one blocks the import, so we bound how many we keep/link per item.
ADO_MAX_RELATIONS = _ado_env_int("ADO_MAX_RELATIONS", 120, minimum=1, maximum=5000)
# Azure DevOps MCP is a single stdio process. Parallel fetches pile up behind one
# hung hub item and look like a full freeze — keep default sequential.
ADO_FETCH_WORKERS = _ado_env_int("ADO_FETCH_WORKERS", 1, minimum=1, maximum=8)
# Per-work-item MCP timeout. Hub items can stall the Node MCP server; skip and
# restart the session so the rest of the import keeps moving.
ADO_ITEM_TIMEOUT = _ado_env_int("ADO_ITEM_TIMEOUT", 25, minimum=5, maximum=600)
# Whole Azure Boards source budget (collect IDs + fetch details).
ADO_SOURCE_TIMEOUT = _ado_env_int("ADO_SOURCE_TIMEOUT", 600, minimum=30, maximum=7200)

# Per-source ingest budgets. Payload can override via timeouts / item_timeout / source_timeout.
DEFAULT_INGEST_TIMEOUTS: dict[str, dict[str, int]] = {
    "files": {"item": 10, "source": 180},
    "chat": {"item": 10, "source": 120},
    "git": {"item": 10, "source": 60},
    "granola": {"item": 30, "source": 240},
    "azure-boards": {"item": ADO_ITEM_TIMEOUT, "source": ADO_SOURCE_TIMEOUT},
    "azure-git": {"item": 30, "source": 180},
    "azure-wiki": {"item": 20, "source": 300},
    "teams-meetings": {"item": 30, "source": 300},
}
ADO_BOARD_MEMORY_TYPES = {
    "requirement": "Requirement",
    "feature": "Feature",
    "epic": "Feature",
    "user story": "Requirement",
    "product backlog item": "Requirement",
    "task": "Artifact",
    "bug": "Constraint",
}
_LOG = logging.getLogger("architectos.service")
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_\-/#.]{2,}", re.IGNORECASE)
DUPLICATE_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "will", "shall",
    "should", "memory", "candidate", "architectos", "project", "file", "imported",
    # Granola / meeting template boilerplate — shared across unrelated meetings.
    "granola", "meeting", "meetings", "attendees", "attendee", "creator", "note",
    "notes", "date", "gmt", "utc", "gmail.com", "summary", "action", "items",
}
GRAPH_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "will", "shall",
    "should", "have", "has", "are", "was", "were", "been", "being",
}


class MemoryIngestionEngine:
    def __init__(self, repository: SQLiteMemoryRepository) -> None:
        self.repository = repository

    def prepare_candidates(self, project_id: str, candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        references = self._reference_items(project_id)
        prepared: list[dict[str, Any]] = []
        seen_fingerprints: set[str] = set()
        for candidate in candidates:
            enriched = self._enrich_candidate(candidate)
            fingerprint = str(enriched["metadata"]["fingerprint"])
            if fingerprint in seen_fingerprints:
                enriched["metadata"]["duplicate"] = True
                enriched["metadata"]["duplicate_reason"] = "same ingestion batch fingerprint"
            duplicate = self._find_duplicate(enriched, references)
            if duplicate:
                enriched["metadata"].update(duplicate)
                enriched["confidence"] = min(float(enriched.get("confidence") or 0.62), 0.45)
            prepared.append(enriched)
            seen_fingerprints.add(fingerprint)
            references.append(self._reference_from_candidate(enriched))
            if len(prepared) >= limit:
                break
        return prepared

    def _reference_items(self, project_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for node in self.repository.list_nodes():
            if node.status != "active" or node.project_id not in {project_id, None}:
                continue
            meta = dict(node.metadata or {})
            items.append({
                "id": node.id,
                "label": node.label,
                "kind": "memory",
                "tokens": self._tokens(f"{node.label} {node.text}"),
                "fingerprint": self._fingerprint(f"{node.label} {node.text}"),
                "meeting_id": str(meta.get("meeting_id") or ""),
                "work_item_id": str(meta.get("work_item_id") or ""),
                "source_type": str(meta.get("source") or meta.get("source_type") or ""),
                "source_ref": str(meta.get("source_ref") or meta.get("granola_url") or ""),
            })
        for candidate in self.repository.list_memory_candidates(project_id, status=None, limit=500):
            if candidate.get("status") not in {"candidate", "duplicate"}:
                continue
            items.append(self._reference_from_candidate(candidate))
        return items

    def _reference_from_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(candidate.get("metadata") or {})
        return {
            "id": str(candidate.get("id") or ""),
            "label": str(candidate.get("label") or ""),
            "kind": "candidate",
            "tokens": set(metadata.get("tokens") or self._tokens(f"{candidate.get('label', '')} {candidate.get('text', '')}")),
            "fingerprint": str(metadata.get("fingerprint") or self._fingerprint(f"{candidate.get('label', '')} {candidate.get('text', '')}")),
            "meeting_id": str(metadata.get("meeting_id") or ""),
            "work_item_id": str(metadata.get("work_item_id") or ""),
            "source_type": str(candidate.get("source_type") or metadata.get("source") or ""),
            "source_ref": str(candidate.get("source_ref") or metadata.get("granola_url") or ""),
        }

    def _enrich_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(candidate)
        metadata = dict(enriched.get("metadata") or {})
        text = f"{enriched.get('label', '')} {enriched.get('text', '')}"
        tokens = sorted(self._tokens(text))
        meeting_id = str(metadata.get("meeting_id") or "").strip()
        work_item_id = str(metadata.get("work_item_id") or "").strip()
        fingerprint = self._fingerprint(text)
        if meeting_id:
            # Meeting identity must dominate fingerprint so distinct Granola notes never collide.
            fingerprint = f"meeting:{meeting_id}|{fingerprint}"
        if work_item_id:
            fingerprint = f"ado:{work_item_id}|{fingerprint}"
        metadata.setdefault("fingerprint", fingerprint)
        metadata.setdefault("tokens", tokens[:24])
        metadata.setdefault("duplicate", False)
        enriched["metadata"] = metadata
        return enriched

    def _find_duplicate(self, candidate: dict[str, Any], references: list[dict[str, Any]]) -> dict[str, Any] | None:
        metadata = dict(candidate.get("metadata") or {})
        candidate_tokens = set(metadata.get("tokens") or [])
        candidate_fingerprint = str(metadata.get("fingerprint") or "")
        candidate_meeting_id = str(metadata.get("meeting_id") or "").strip()
        candidate_work_item_id = str(metadata.get("work_item_id") or "").strip()
        candidate_source = str(candidate.get("source_type") or metadata.get("source") or "")
        best: dict[str, Any] | None = None
        for reference in references:
            if reference.get("id") == candidate.get("id"):
                continue
            ref_meeting_id = str(reference.get("meeting_id") or "").strip()
            ref_work_item_id = str(reference.get("work_item_id") or "").strip()
            # Distinct Granola/meeting identities are never textual duplicates of each other.
            if candidate_meeting_id and ref_meeting_id and candidate_meeting_id != ref_meeting_id:
                continue
            if candidate_work_item_id and ref_work_item_id and candidate_work_item_id != ref_work_item_id:
                continue
            if candidate_work_item_id and ref_work_item_id and candidate_work_item_id == ref_work_item_id:
                return {
                    "duplicate": True,
                    "duplicate_score": 1.0,
                    "duplicate_of": reference.get("id"),
                    "duplicate_label": reference.get("label"),
                    "duplicate_kind": reference.get("kind"),
                    "duplicate_reason": "same Azure Boards work item id",
                }
            if reference.get("fingerprint") == candidate_fingerprint:
                score = 1.0
            else:
                score = self._similarity(candidate_tokens, set(reference.get("tokens") or []))
            # Short Granola stubs are mostly metadata; demand a stricter match.
            threshold = 0.82 if candidate_source == "granola" and len(candidate_tokens) < 16 else 0.68
            if score >= threshold and (not best or score > float(best["duplicate_score"])):
                best = {
                    "duplicate": True,
                    "duplicate_score": round(score, 3),
                    "duplicate_of": reference.get("id"),
                    "duplicate_label": reference.get("label"),
                    "duplicate_kind": reference.get("kind"),
                }
        return best
    def _tokens(self, text: str) -> set[str]:
        return {token.lower() for token in TOKEN_RE.findall(text) if token.lower() not in DUPLICATE_STOPWORDS}

    def _fingerprint(self, text: str) -> str:
        counts = Counter(self._tokens(text))
        return "|".join(token for token, _count in counts.most_common(18))

    def _similarity(self, left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        overlap = len(left & right)
        jaccard = overlap / len(left | right)
        containment = overlap / min(len(left), len(right))
        return max(jaccard, containment * 0.85)

DEFAULT_MEMORY_LIFECYCLE = {
    "enabled": True,
    "refresh_on_access": True,
    "short_term_ttl_days": 14,
    "archive_after_days": 30,
    "delete_after_days": 0,
    "promote_after_hits": 5,
    "auto_rescan_on_startup": True,
    "auto_rescan_limit": 24,
    "auto_rescan_all_projects": True,
    "chat_memory_mode": DEFAULT_CHAT_MEMORY_MODE,
    "chat_candidate_ttl_days": DEFAULT_CHAT_CANDIDATE_TTL_DAYS,
    "chat_store_facts_only": True,
    "long_term_types": ["Decision", "Constraint", "Requirement"],
    "architecture_keywords": ["architecture", "adr", "decision", "constraint", "security", "provider", "routing"],
}


class MemoryLifecycleEngine:
    def __init__(self, repository: SQLiteMemoryRepository) -> None:
        self.repository = repository

    def settings(self) -> dict[str, Any]:
        configured = self.repository.get_setting("memory_lifecycle") or {}
        settings = dict(DEFAULT_MEMORY_LIFECYCLE)
        settings.update({key: value for key, value in configured.items() if key in settings})
        settings["enabled"] = bool(settings.get("enabled"))
        settings["refresh_on_access"] = bool(settings.get("refresh_on_access"))
        settings["auto_rescan_on_startup"] = bool(settings.get("auto_rescan_on_startup", True))
        settings["auto_rescan_all_projects"] = bool(settings.get("auto_rescan_all_projects", True))
        settings["chat_store_facts_only"] = bool(settings.get("chat_store_facts_only", True))
        settings["chat_memory_mode"] = normalize_chat_memory_mode(settings.get("chat_memory_mode"))
        for key in ("short_term_ttl_days", "archive_after_days", "delete_after_days", "promote_after_hits", "auto_rescan_limit", "chat_candidate_ttl_days"):
            settings[key] = max(0, int(settings.get(key) or 0))
        settings["long_term_types"] = [str(item) for item in settings.get("long_term_types") or []]
        settings["architecture_keywords"] = [str(item).lower() for item in settings.get("architecture_keywords") or []]
        return settings

    def initialize_node(self, node: Any, source: str = "memory") -> Any:
        metadata = dict(getattr(node, "metadata", {}) or {})
        if metadata.get("memory_tier"):
            return node
        node.metadata = self.seed_metadata(
            node_type=str(getattr(node, "type", "") or ""),
            created_at=str(getattr(node, "created_at", "") or utc_now()),
            metadata=metadata,
            source=source,
        )
        return self.repository.upsert_node(node)

    def seed_metadata(
        self,
        *,
        node_type: str,
        created_at: str,
        metadata: dict[str, Any] | None = None,
        source: str = "memory",
    ) -> dict[str, Any]:
        """Attach lifecycle fields without writing — used to avoid a second upsert on promote."""
        seeded = dict(metadata or {})
        if seeded.get("memory_tier"):
            return seeded
        settings = self.settings()
        # Minimal stand-in so existing helpers can score/classify.
        class _Probe:
            pass

        probe = _Probe()
        probe.type = node_type
        probe.created_at = created_at or utc_now()
        probe.metadata = seeded
        long_term = self._should_be_long_term(probe, settings)
        seeded["memory_tier"] = "long_term" if long_term else "short_term"
        seeded["lifecycle_state"] = "stable" if long_term else "fresh"
        seeded["decay_enabled"] = not long_term
        seeded["retention_policy"] = "keep" if long_term else ("delete" if node_type == "Artifact" else "archive")
        seeded["access_count"] = int(seeded.get("access_count") or 0)
        seeded.setdefault("first_seen_at", created_at or utc_now())
        seeded.setdefault("last_accessed_at", created_at or utc_now())
        seeded["memory_score"] = self._score(probe, seeded, settings)
        seeded["lifecycle_source"] = source
        if long_term:
            seeded.setdefault("promoted_to_long_term_at", utc_now())
        return seeded

    def refresh_nodes(self, node_ids: list[str], reason: str = "access") -> dict[str, Any]:
        settings = self.settings()
        if not settings["enabled"] or not settings["refresh_on_access"]:
            return {"refreshed": 0, "promoted": 0}
        refreshed = 0
        promoted = 0
        for node_id in dict.fromkeys(node_ids):
            node = self.repository.get_node(node_id)
            if not node or node.status != "active":
                continue
            node = self.initialize_node(node, reason)
            metadata = dict(node.metadata or {})
            metadata["access_count"] = int(metadata.get("access_count") or 0) + 1
            metadata["last_accessed_at"] = utc_now()
            metadata["last_refresh_reason"] = reason
            if metadata.get("memory_tier") != "long_term":
                metadata["lifecycle_state"] = "fresh"
            if metadata.get("memory_tier") != "long_term" and self._should_promote(node, metadata, settings):
                metadata["memory_tier"] = "long_term"
                metadata["lifecycle_state"] = "stable"
                metadata["decay_enabled"] = False
                metadata["retention_policy"] = "keep"
                metadata["promoted_to_long_term_at"] = utc_now()
                promoted += 1
            metadata["memory_score"] = self._score(node, metadata, settings)
            node.metadata = metadata
            self.repository.upsert_node(node)
            refreshed += 1
        return {"refreshed": refreshed, "promoted": promoted}

    def promote_long_term(self, node_id: str, reason: str = "manual") -> dict[str, Any]:
        node = self.repository.get_node(node_id)
        if not node:
            raise ValueError("memory not found")
        node = self.initialize_node(node, reason)
        metadata = dict(node.metadata or {})
        metadata["memory_tier"] = "long_term"
        metadata["lifecycle_state"] = "stable"
        metadata["decay_enabled"] = False
        metadata["retention_policy"] = "keep"
        metadata["promoted_to_long_term_at"] = utc_now()
        metadata["promotion_reason"] = reason
        metadata["memory_score"] = self._score(node, metadata, self.settings())
        node.metadata = metadata
        return {"node": self.repository.upsert_node(node).to_dict()}

    def run_decay(self, project_id: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        settings = self.settings()
        if not settings["enabled"]:
            return {"enabled": False, "changed": 0, "items": []}
        now = self._parse_time(utc_now())
        items: list[dict[str, Any]] = []
        changed = 0
        for node in self.repository.list_nodes():
            if project_id and node.project_id not in {project_id, None}:
                continue
            if node.status not in {"active", "archived"}:
                continue
            node = self.initialize_node(node, "decay")
            metadata = dict(node.metadata or {})
            if self._is_protected(node, metadata, settings):
                continue
            age_days = self._age_days(metadata, node, now)
            old_status = node.status
            old_state = str(metadata.get("lifecycle_state") or "fresh")
            action = "keep"
            if settings["delete_after_days"] and age_days >= settings["delete_after_days"] and metadata.get("retention_policy") == "delete":
                node.status = "deleted"
                metadata["lifecycle_state"] = "deleted"
                metadata["deleted_at"] = utc_now()
                action = "deleted"
            elif age_days >= settings["archive_after_days"]:
                node.status = "archived"
                metadata["lifecycle_state"] = "archived"
                metadata["archived_at"] = metadata.get("archived_at") or utc_now()
                action = "archived"
            elif age_days >= settings["short_term_ttl_days"]:
                metadata["lifecycle_state"] = "stale"
                action = "stale"
            elif settings["short_term_ttl_days"] and age_days >= max(1, settings["short_term_ttl_days"] // 2):
                metadata["lifecycle_state"] = "fading"
                action = "fading"
            metadata["memory_score"] = self._score(node, metadata, settings)
            if action != "keep" or old_status != node.status or old_state != metadata.get("lifecycle_state"):
                changed += 1
                items.append({"id": node.id, "label": node.label, "action": action, "age_days": age_days, "status": node.status, "state": metadata.get("lifecycle_state")})
                if not dry_run:
                    node.metadata = metadata
                    self.repository.upsert_node(node)
        return {"enabled": True, "changed": changed, "items": items, "settings": settings, "dry_run": dry_run, **self.expire_stale_chat_candidates(project_id, dry_run=dry_run)}

    def expire_stale_chat_candidates(self, project_id: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        """Auto-reject unpromoted chat candidates past TTL so the review queue stays clean."""
        settings = self.settings()
        ttl_days = int(settings.get("chat_candidate_ttl_days") or 0)
        if ttl_days <= 0:
            return {"chat_candidates_expired": 0, "chat_candidate_ttl_days": ttl_days}
        now = datetime.now(timezone.utc)
        expired = 0
        for candidate in self.repository.list_memory_candidates(project_id, "candidate", 500):
            source_type = str(candidate.get("source_type") or "").lower()
            meta = dict(candidate.get("metadata") or {})
            template = str(meta.get("template") or "")
            if source_type not in {"chat", "chat_favorite", "rules_keeper"} and template not in {"chat_turn_keeper", "chat_fact_keeper", "rules_keeper", "assistant_favorite"}:
                continue
            raw = str(candidate.get("created_at") or candidate.get("updated_at") or "")
            then = self._parse_time(raw) if raw else now
            age_days = max(0, int((now - then).total_seconds() // 86400))
            if age_days < ttl_days:
                continue
            expired += 1
            if dry_run:
                continue
            candidate["status"] = "rejected"
            candidate["rejected_at"] = utc_now()
            candidate["reject_reason"] = f"Auto-expired chat candidate after {ttl_days} day(s)"
            meta["expired_by_ttl"] = True
            candidate["metadata"] = meta
            self.repository.update_memory_candidate(candidate)
        return {"chat_candidates_expired": expired, "chat_candidate_ttl_days": ttl_days}

    def annotate_nodes(self, project_id: str | None = None) -> dict[str, int]:
        count = 0
        for node in self.repository.list_nodes():
            if project_id and node.project_id not in {project_id, None}:
                continue
            self.initialize_node(node, "annotate")
            count += 1
        try:
            self.expire_stale_chat_candidates(project_id, dry_run=False)
        except Exception:
            pass
        return {"annotated": count}

    def analytics(self, project_id: str | None = None) -> dict[str, Any]:
        self.annotate_nodes(project_id)
        by_tier: dict[str, int] = {}
        by_state: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for node in self.repository.list_nodes():
            if project_id and node.project_id not in {project_id, None}:
                continue
            metadata = dict(node.metadata or {})
            by_tier[str(metadata.get("memory_tier") or "unknown")] = by_tier.get(str(metadata.get("memory_tier") or "unknown"), 0) + 1
            by_state[str(metadata.get("lifecycle_state") or "unknown")] = by_state.get(str(metadata.get("lifecycle_state") or "unknown"), 0) + 1
            by_status[node.status] = by_status.get(node.status, 0) + 1
        return {"by_tier": by_tier, "by_state": by_state, "by_status": by_status, "settings": self.settings()}

    def _should_promote(self, node: Any, metadata: dict[str, Any], settings: dict[str, Any]) -> bool:
        return int(metadata.get("access_count") or 0) >= settings["promote_after_hits"] or self._should_be_long_term(node, settings)

    def _should_be_long_term(self, node: Any, settings: dict[str, Any]) -> bool:
        metadata = dict(getattr(node, "metadata", {}) or {})
        if metadata.get("favorite") or metadata.get("pinned"):
            return True
        node_type = str(getattr(node, "type", "") or "")
        if node_type in set(settings["long_term_types"]) or node_type in {"Project", "Provider"}:
            return True
        haystack = f"{getattr(node, 'label', '')} {getattr(node, 'text', '')} {' '.join(str(value) for value in metadata.values() if isinstance(value, str))}".lower()
        return any(keyword and keyword in haystack for keyword in settings["architecture_keywords"])

    def _is_protected(self, node: Any, metadata: dict[str, Any], settings: dict[str, Any]) -> bool:
        return metadata.get("memory_tier") == "long_term" or metadata.get("favorite") or metadata.get("pinned") or self._should_be_long_term(node, settings)

    def _age_days(self, metadata: dict[str, Any], node: Any, now: datetime) -> int:
        raw = metadata.get("last_accessed_at") or getattr(node, "updated_at", "") or getattr(node, "created_at", "") or utc_now()
        then = self._parse_time(str(raw))
        return max(0, int((now - then).total_seconds() // 86400))

    def _score(self, node: Any, metadata: dict[str, Any], settings: dict[str, Any]) -> float:
        base = 50.0 + 20.0 * float(getattr(node, "confidence", 0.0) or 0.0)
        if metadata.get("memory_tier") == "long_term":
            base += 24.0
        if metadata.get("lifecycle_state") == "fresh":
            base += 10.0
        elif metadata.get("lifecycle_state") == "fading":
            base -= 8.0
        elif metadata.get("lifecycle_state") == "stale":
            base -= 20.0
        elif metadata.get("lifecycle_state") == "archived":
            base -= 34.0
        base += min(18.0, 2.5 * int(metadata.get("access_count") or 0))
        if metadata.get("favorite") or metadata.get("pinned"):
            base += 18.0
        if metadata.get("duplicate"):
            base -= 20.0
        return round(max(0.0, min(100.0, base)), 2)

    def _parse_time(self, value: str) -> datetime:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)


SOURCE_HUB_CATALOG: dict[str, dict[str, str]] = {
    "docs": {"label": "Repo docs", "text": "Local repository documentation and markdown memory for this scope."},
    "code": {"label": "Code", "text": "Source code and configuration artifacts for this scope."},
    "git": {"label": "Git", "text": "Commit history and git-derived memory for this scope."},
    "adr": {"label": "ADRs", "text": "Architecture decision records for this scope."},
    "issues": {"label": "Issue files", "text": "Local issue/bug markdown files for this scope (not Azure Boards)."},
    "prs": {"label": "PR notes", "text": "Local pull-request notes and templates for this scope (not remote PRs)."},
    "meetings": {"label": "Meeting notes", "text": "Local meeting note files for this scope (prefer Granola for live meetings)."},
    "chat": {"label": "App chat", "text": "ArchitectOS chat-derived memory for this scope."},
    "granola": {"label": "Granola", "text": "Granola meeting memory for this scope."},
    "azure-boards": {"label": "Azure Boards", "text": "Azure Boards work items for this scope."},
    "azure-wiki": {"label": "Azure Wiki", "text": "Azure Wiki pages for this scope."},
    "azure-git": {"label": "Azure Git", "text": "Azure Repos repositories and pull requests for this scope."},
    "teams-meetings": {"label": "Teams", "text": "Microsoft Teams transcripts and AI/Facilitator insights for this scope."},
    "project_scan": {"label": "Project scan", "text": "Files imported by project scan for this scope."},
    "manual": {"label": "Manual", "text": "Manually captured memory for this scope."},
    "other": {"label": "Other", "text": "Ungrouped memory for this scope."},
}
SOURCE_HUB_ALIASES = {
    "doc": "docs",
    "documentation": "docs",
    "project_scan": "project_scan",
    "ui": "manual",
    "memory_candidate": "manual",
    "promoted_candidate": "manual",
    "azure_boards": "azure-boards",
    "boards": "azure-boards",
    "azure_wiki": "azure-wiki",
    "wiki": "azure-wiki",
    "azure_git": "azure-git",
    "azure_repos": "azure-git",
    "ado_git": "azure-git",
    "ado_repos": "azure-git",
    "teams": "teams-meetings",
    "teams_meetings": "teams-meetings",
    "ms_teams": "teams-meetings",
    "facilitator": "teams-meetings",
    "pr": "prs",
    "pull_request": "prs",
    "pull_requests": "prs",
    "issue": "issues",
    "meeting": "meetings",
    "commit": "git",
    "commits": "git",
    "git_cluster": "git",
}


class GraphAutoLinker:
    def __init__(self, repository: SQLiteMemoryRepository) -> None:
        self.repository = repository

    def link_node(
        self,
        node: Any,
        project_id: str | None = None,
        *,
        hub_cache: dict[tuple[str, str | None, str], Any] | None = None,
        existing_edge_ids: set[str] | None = None,
        nodes_snapshot: list[Any] | None = None,
        project_root_cache: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._is_structural_node(node):
            return {"created": 0, "edges": [], "linked_nodes": 0}
        scope = str(getattr(node, "scope", None) or "project")
        resolved_project_id = project_id or getattr(node, "project_id", None)
        if scope == "project":
            resolved_project_id = resolved_project_id or "architectos"
        else:
            resolved_project_id = None
        source_key = self._source_key_for_node(node)
        hub = self._ensure_source_hub(
            scope,
            resolved_project_id,
            source_key,
            hub_cache=hub_cache,
            nodes_snapshot=nodes_snapshot,
        )
        planned: list[tuple[str, str, str, str, float, str]] = [
            (hub.id, node.id, self._hub_leaf_edge_type(node, source_key), scope, 0.8, "source_hub"),
        ]
        if scope == "project":
            root = self._project_root(
                resolved_project_id or "architectos",
                nodes_snapshot=nodes_snapshot,
                root_cache=project_root_cache,
            )
            if root and root.id != hub.id:
                planned.append((root.id, hub.id, "HAS_MEMORY", "project", 0.9, "project_source_hub"))
        else:
            scope_root = self._ensure_scope_root(scope, nodes_snapshot=nodes_snapshot, hub_cache=hub_cache)
            if scope_root.id != hub.id:
                planned.append((scope_root.id, hub.id, "HAS_MEMORY", scope, 0.9, "scope_source_hub"))
        return self._write_edges(planned, existing_edge_ids=existing_edge_ids)

    def rebuild(self, project_id: str | None = None, similarity_limit: int = 3) -> dict[str, Any]:
        nodes = [node for node in self.repository.list_nodes() if node.status == "active"]
        if project_id:
            nodes = [
                node for node in nodes
                if node.project_id in {project_id, None} or str(getattr(node, "scope", "") or "") in {"shared", "global"}
            ]
        hub_cache: dict[tuple[str, str | None, str], Any] = {}
        project_root_cache: dict[str, Any] = {}
        existing_edge_ids = self.repository.list_edge_ids()
        root = self._project_root(project_id or "architectos", nodes_snapshot=nodes, root_cache=project_root_cache)

        planned: list[tuple[str, str, str, str, float, str]] = []
        content_nodes = [node for node in nodes if not self._is_structural_node(node)]
        by_id = {node.id: node for node in content_nodes}
        for node in content_nodes:
            scope = str(getattr(node, "scope", None) or "project")
            node_project_id = getattr(node, "project_id", None)
            if scope == "project":
                node_project_id = node_project_id or project_id or "architectos"
            else:
                node_project_id = None
            source_key = self._source_key_for_node(node)
            hub = self._ensure_source_hub(
                scope,
                node_project_id,
                source_key,
                hub_cache=hub_cache,
                nodes_snapshot=nodes,
            )
            planned.append((hub.id, node.id, self._hub_leaf_edge_type(node, source_key), scope, 0.8, "source_hub"))
            if scope == "project" and root and root.id != hub.id:
                planned.append((root.id, hub.id, "HAS_MEMORY", "project", 0.9, "project_source_hub"))
            elif scope != "project":
                scope_root = self._ensure_scope_root(scope, nodes_snapshot=nodes, hub_cache=hub_cache)
                if scope_root.id != hub.id:
                    planned.append((scope_root.id, hub.id, "HAS_MEMORY", scope, 0.9, "scope_source_hub"))

        # Inverted-index similarity: avoid O(n²) pairwise Jaccard over the full corpus.
        token_map = {node.id: self._similarity_tokens(node) for node in content_nodes}
        inverted: dict[str, list[str]] = {}
        for node_id, tokens in token_map.items():
            for token in tokens:
                inverted.setdefault(token, []).append(node_id)
        # Drop ultra-common tokens so one frequent word cannot explode candidates back toward O(n²).
        max_df = max(48, len(content_nodes) // 40)
        inverted = {token: ids for token, ids in inverted.items() if 1 < len(ids) <= max_df}
        candidate_cap = max(24, similarity_limit * 16)
        max_expansions = 1800

        for node in content_nodes:
            left = token_map.get(node.id) or set()
            if not left:
                continue
            # Prefer rare tokens; bound posting-list walks so large corpora stay interactive.
            ranked_tokens = sorted(
                (token for token in left if token in inverted),
                key=lambda token: len(inverted[token]),
            )
            shares: Counter[str] = Counter()
            expansions = 0
            for token in ranked_tokens:
                postings = inverted[token]
                for other_id in postings:
                    if other_id != node.id:
                        shares[other_id] += 1
                expansions += len(postings)
                if expansions >= max_expansions and len(shares) >= candidate_cap:
                    break
            scored: list[tuple[float, Any]] = []
            for other_id, _inter in shares.most_common(candidate_cap):
                other = by_id.get(other_id)
                if other is None:
                    continue
                score = self._similarity(left, token_map.get(other_id) or set())
                if score >= 0.32:
                    scored.append((score, other))
            for score, other in sorted(scored, key=lambda item: (-item[0], item[1].label))[:similarity_limit]:
                source, target = sorted([node.id, other.id])
                planned.append(
                    (source, target, "RELATED_TO", node.scope or other.scope or "project", min(0.88, 0.48 + score), "similarity")
                )

        written = self._write_edges(planned, existing_edge_ids=existing_edge_ids)
        pruned = self.prune_empty_source_hubs(project_id)
        return {**written, "root": root.id if root else "", "candidate_edges": len(planned), "pruned_hubs": pruned}

    def prune_empty_source_hubs(self, project_id: str | None = None) -> dict[str, Any]:
        """Remove source hubs and scope roots that have no content children."""
        nodes = self._active_nodes_for_project(project_id)
        by_id = {node.id: node for node in nodes}
        content_ids = {node.id for node in nodes if not self._is_structural_node(node)}
        child_targets = self._outgoing_targets(by_id)

        pruned_hubs = 0
        for node in list(nodes):
            meta = dict(node.metadata or {})
            if not meta.get("hub"):
                continue
            children = child_targets.get(node.id) or set()
            if any(child_id in content_ids for child_id in children):
                continue
            node.status = "deleted"
            meta["deleted_at"] = utc_now()
            meta["delete_reason"] = "empty_source_hub"
            node.metadata = meta
            self.repository.upsert_node(node)
            pruned_hubs += 1

        nodes = self._active_nodes_for_project(project_id)
        by_id = {node.id: node for node in nodes}
        child_targets = self._outgoing_targets(by_id)
        active_hub_ids = {node.id for node in nodes if dict(node.metadata or {}).get("hub")}
        pruned_scope_roots = 0
        for node in nodes:
            meta = dict(node.metadata or {})
            if not meta.get("scope_root"):
                continue
            children = child_targets.get(node.id) or set()
            if any(child_id in active_hub_ids for child_id in children):
                continue
            node.status = "deleted"
            meta["deleted_at"] = utc_now()
            meta["delete_reason"] = "empty_scope_root"
            node.metadata = meta
            self.repository.upsert_node(node)
            pruned_scope_roots += 1
        return {"hubs": pruned_hubs, "scope_roots": pruned_scope_roots}

    def _active_nodes_for_project(self, project_id: str | None = None) -> list[Any]:
        nodes = [node for node in self.repository.list_nodes() if node.status == "active"]
        if not project_id:
            return nodes
        return [
            node for node in nodes
            if node.project_id in {project_id, None} or str(getattr(node, "scope", "") or "") in {"shared", "global", "interface"}
        ]

    def _outgoing_targets(self, by_id: dict[str, Any]) -> dict[str, set[str]]:
        child_targets: dict[str, set[str]] = {}
        for edge in self.repository.list_edges():
            if edge.status != "active":
                continue
            if edge.source not in by_id or edge.target not in by_id:
                continue
            child_targets.setdefault(edge.source, set()).add(edge.target)
        return child_targets

    def _ensure_source_hub(
        self,
        scope: str,
        project_id: str | None,
        source_key: str,
        *,
        hub_cache: dict[tuple[str, str | None, str], Any] | None = None,
        nodes_snapshot: list[Any] | None = None,
    ) -> Any:
        source_key = self._normalize_source_key(source_key)
        catalog = SOURCE_HUB_CATALOG.get(source_key) or SOURCE_HUB_CATALOG["other"]
        label = f"Source: {catalog['label']}"
        hub_project_id = project_id if scope == "project" else None
        cache_key = (scope, hub_project_id or None, source_key)
        if hub_cache is not None and cache_key in hub_cache:
            return hub_cache[cache_key]
        nodes = nodes_snapshot if nodes_snapshot is not None else self.repository.list_nodes()
        for node in nodes:
            if node.status != "active":
                continue
            meta = dict(node.metadata or {})
            if (
                meta.get("hub")
                and str(meta.get("source_key") or "") == source_key
                and str(node.scope or "") == scope
                and (node.project_id or None) == (hub_project_id or None)
            ):
                if hub_cache is not None:
                    hub_cache[cache_key] = node
                return node
        text = f"{catalog['text']} Scope: {scope}."
        if hub_project_id:
            text += f" Project: {hub_project_id}."
        # Stable label includes source_key so ids never collide across hubs.
        hub = self.repository.add_node(
            "Concept",
            label,
            scope,
            text,
            hub_project_id,
            confidence=0.95,
            metadata={
                "hub": True,
                "source_key": source_key,
                "source": "source_hub",
                "source_type": source_key,
                "structural": True,
            },
        )
        if hub_cache is not None:
            hub_cache[cache_key] = hub
        if nodes_snapshot is not None:
            nodes_snapshot.append(hub)
        return hub

    def _ensure_scope_root(
        self,
        scope: str,
        *,
        nodes_snapshot: list[Any] | None = None,
        hub_cache: dict[tuple[str, str | None, str], Any] | None = None,
    ) -> Any:
        cache_key = (scope, None, f"__scope_root__:{scope}")
        if hub_cache is not None and cache_key in hub_cache:
            return hub_cache[cache_key]
        label = f"Scope: {scope}"
        nodes = nodes_snapshot if nodes_snapshot is not None else self.repository.list_nodes()
        for node in nodes:
            meta = dict(node.metadata or {})
            if node.status == "active" and meta.get("scope_root") and str(node.scope or "") == scope and node.project_id is None:
                if hub_cache is not None:
                    hub_cache[cache_key] = node
                return node
        root = self.repository.add_node(
            "Concept",
            label,
            scope,
            f"Root container for {scope}-scoped memory source groups.",
            None,
            confidence=0.98,
            metadata={"scope_root": True, "structural": True, "source": "scope_root"},
        )
        if hub_cache is not None:
            hub_cache[cache_key] = root
        if nodes_snapshot is not None:
            nodes_snapshot.append(root)
        return root

    def _project_root(
        self,
        project_id: str,
        *,
        nodes_snapshot: list[Any] | None = None,
        root_cache: dict[str, Any] | None = None,
    ) -> Any | None:
        if root_cache is not None and project_id in root_cache:
            return root_cache[project_id]
        nodes = nodes_snapshot if nodes_snapshot is not None else self.repository.list_nodes()
        found = None
        for node in nodes:
            if node.type == "Project" and node.project_id == project_id:
                found = node
                break
        if found is None:
            for node in nodes:
                if node.type == "Project" and node.label.lower() == project_id.lower():
                    found = node
                    break
        if found is None:
            for node in nodes:
                if node.type == "Project" and node.label == "ArchitectOS":
                    found = node
                    break
        if root_cache is not None:
            root_cache[project_id] = found
        return found

    def _source_key_for_node(self, node: Any) -> str:
        metadata = dict(getattr(node, "metadata", {}) or {})
        raw = str(
            metadata.get("source_type")
            or metadata.get("source")
            or metadata.get("template")
            or ""
        ).strip().lower()
        if raw in {"source_hub", "scope_root", "project_profile"}:
            return "other"
        key = self._normalize_source_key(raw)
        if key in SOURCE_HUB_CATALOG and key not in {"other", "project_scan"}:
            return key
        if key == "project_scan" or raw == "project_scan":
            path = str(metadata.get("path") or getattr(node, "label", "") or "").lower()
            if path.endswith((".md", ".txt", ".rst", ".adoc")):
                return "docs"
            return "code"
        path = str(metadata.get("path") or metadata.get("extension") or getattr(node, "label", "") or "").lower()
        if any(path.endswith(ext) for ext in (".md", ".txt", ".rst", ".adoc")):
            return "docs"
        if any(ext in path for ext in (".py", ".java", ".js", ".ts", ".tsx", ".jsx", ".css", ".json", ".yml", ".yaml", ".xml")):
            return "code"
        node_type = str(getattr(node, "type", "") or "").lower()
        if node_type == "meeting":
            return "meetings"
        if node_type == "doc":
            return "docs"
        if node_type in {"artifact", "feature", "requirement", "constraint"} and "azure" in raw:
            if "git" in raw or "repo" in raw or "pull" in raw or "pr" in raw:
                return "azure-git"
            return "azure-boards" if "board" in raw or "work" in raw else key
        if not raw or raw in {"ui", "manual", "memory_candidate"}:
            return "manual"
        return key if key in SOURCE_HUB_CATALOG else "other"

    def _normalize_source_key(self, value: str) -> str:
        key = str(value or "").strip().lower().replace("_", "-")
        key = SOURCE_HUB_ALIASES.get(key, key)
        key = SOURCE_HUB_ALIASES.get(key.replace("-", "_"), key)
        if key in SOURCE_HUB_CATALOG:
            return key
        compact = key.replace("-", "")
        for candidate in SOURCE_HUB_CATALOG:
            if candidate.replace("-", "") == compact:
                return candidate
        return "other"

    def _hub_leaf_edge_type(self, node: Any, source_key: str) -> str:
        if source_key in {"docs", "adr", "meetings", "azure-wiki", "granola", "chat", "teams-meetings"}:
            return "DOCUMENTED_IN"
        if source_key in {"code", "project_scan"}:
            return "IMPLEMENTS"
        if source_key == "azure-boards":
            return "HAS_MEMORY"
        return self._root_edge_type(node)

    @staticmethod
    def _is_structural_node(node: Any) -> bool:
        if str(getattr(node, "type", "") or "") == "Project":
            return True
        metadata = dict(getattr(node, "metadata", {}) or {})
        return bool(metadata.get("hub") or metadata.get("scope_root") or metadata.get("structural"))

    def _root_edge_type(self, node: Any) -> str:
        metadata = dict(getattr(node, "metadata", {}) or {})
        source = " ".join(str(metadata.get(key) or "") for key in ("source", "source_type", "template", "path")).lower()
        label = str(getattr(node, "label", "") or "").lower()
        node_type = str(getattr(node, "type", "") or "").lower()
        if node_type in {"doc", "decision", "meeting"} or any(item in source for item in ("docs", "adr", "meeting", ".md", ".txt", ".rst")):
            return "DOCUMENTED_IN"
        if node_type == "artifact" or any(item in source for item in (".py", ".js", ".ts", ".tsx", ".json", ".yaml", ".yml")) or label.endswith((".py", ".js", ".ts", ".tsx", ".json")):
            return "IMPLEMENTS"
        return "HAS_MEMORY"

    def _metadata_text(self, node: Any) -> str:
        metadata = dict(getattr(node, "metadata", {}) or {})
        return " ".join(str(value) for value in metadata.values() if isinstance(value, (str, int, float)))

    def _tokens(self, text: str) -> set[str]:
        tokens: set[str] = set()
        for raw in TOKEN_RE.findall(text or ""):
            token = raw.lower().strip("._-/#")
            if len(token) < 3 or token in GRAPH_STOPWORDS:
                continue
            tokens.add(token)
        return tokens

    def _similarity(self, left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        overlap = len(left & right)
        jaccard = overlap / len(left | right)
        containment = overlap / min(len(left), len(right))
        return max(jaccard, containment * 0.72)

    def _similarity_tokens(self, node: Any) -> set[str]:
        # Keep structural/lifecycle metadata out of RELATED_TO scoring — it creates
        # huge false-positive cliques (timestamps, TTL, paths) on large corpora.
        return self._tokens(f"{getattr(node, 'label', '')} {getattr(node, 'text', '')} {getattr(node, 'type', '')}")

    def _write_edges(
        self,
        planned: list[tuple[str, str, str, str, float, str]],
        *,
        existing_edge_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        shared = existing_edge_ids is not None
        existing = existing_edge_ids if shared else {edge.id for edge in self.repository.list_edges()}
        created = 0
        linked_nodes: set[str] = set()
        edges: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for source, target, edge_type, scope, confidence, reason in planned:
            if not source or not target or source == target:
                continue
            key = (source, target, edge_type, scope or "project")
            if key in seen:
                continue
            seen.add(key)
            edge_id = stable_id("edge", edge_type, source, target, scope or "project")
            # Batch promote passes a shared set and can skip redundant writes.
            if shared and edge_id in existing:
                linked_nodes.update({source, target})
                continue
            edge = self.repository.add_edge(source, target, edge_type, scope or "project", confidence)
            if edge.id not in existing:
                created += 1
            existing.add(edge.id)
            linked_nodes.update({source, target})
            payload = edge.to_dict()
            payload["reason"] = reason
            edges.append(payload)
        return {"created": created, "edges": edges, "linked_nodes": len(linked_nodes)}


class ArchitectOSService:
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
        # Local hash re-rank stays offline/fast; persisted vectors use embedding_provider above.
        self.search_strategy = HybridSearchStrategy(embedding_provider=HashEmbeddingProvider())
        self.repository.register_node_upsert_listener(self.memory_embeddings.index_node)
        self.provider_router = ProviderRouter(project_root)
        self.security_policy = SecurityPolicy()
        self.ingestion_engine = MemoryIngestionEngine(self.repository)
        self.memory_lifecycle = MemoryLifecycleEngine(self.repository)
        self.graph_auto_linker = GraphAutoLinker(self.repository)
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

    def projects(self) -> list[dict[str, Any]]:
        return [project.to_dict() for project in self.repository.list_projects()]

    def create_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not str(payload.get("root_path") or "").strip():
            raise ValueError("project folder path is required")
        project, created = self._upsert_project_profile(payload)
        data = project.to_dict()
        action = "created" if created else "existing"
        message = (
            f"Project {project.name} was created successfully."
            if created
            else f"Project {project.name} already exists. Switched to the existing project."
        )
        return {**data, "project_id": project.id, "project": data, "created": created, "action": action, "message": message}

    def _upsert_project_profile(self, payload: dict[str, Any]) -> tuple[Project, bool]:
        root_raw = str(payload.get("root_path") or "").strip()
        name = str(payload.get("name") or "").strip()
        description = str(payload.get("description") or "").strip()
        incoming_config = dict(payload.get("config") or {})
        root_path = ""
        if root_raw:
            root = self._validate_project_root(root_raw)
            root_path = str(root)
            name = name or root.name or "Project"
        if not name:
            raise ValueError("project name is required")
        existing = self._find_project_by_root(root_path) if root_path else None
        if existing:
            existing.name = name
            existing.description = description or existing.description
            existing.root_path = root_path
            merged_config = dict(existing.config or {})
            merged_config.update(incoming_config)
            existing.config = merged_config
            project = self.repository.upsert_project(existing)
            created = False
        else:
            project_id = stable_id("project", root_path) if root_path else stable_id("project", name, root_path)
            project = self.repository.upsert_project(Project(id=project_id, name=name, root_path=root_path, description=description, config=incoming_config))
            created = True
        self._ensure_project_memory_root(project)
        return project, created

    def _find_project_by_root(self, root_path: str) -> Project | None:
        if not root_path:
            return None
        try:
            wanted = Path(root_path).resolve()
        except OSError:
            return None
        for project in self.repository.list_projects():
            if not project.root_path:
                continue
            try:
                if Path(project.root_path).resolve() == wanted:
                    return project
            except OSError:
                continue
        return None

    def _validate_project_root(self, root_path: str) -> Path:
        root = Path(root_path).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError(f"project path does not exist: {root}")
        return root

    def _ensure_project_memory_root(self, project: Project) -> None:
        if project.id == SYSTEM_PROJECT_ID and not project.root_path and self._is_architectos_source_root():
            return
        text = f"Project profile for {project.name}. Root: {project.root_path}."
        for node in self.repository.list_nodes():
            if node.type == "Project" and node.project_id == project.id:
                node.label = project.name
                node.text = text
                node.scope = "project"
                node.metadata["project_root"] = project.root_path
                node.metadata["project_config"] = dict(project.config or {})
                self.repository.upsert_node(node)
                return
        node = self.repository.add_node(
            "Project",
            project.name,
            "project",
            text,
            project.id,
            confidence=0.9,
            metadata={"source": "project_profile", "project_root": project.root_path, "project_config": dict(project.config or {})},
        )
        self.memory_lifecycle.initialize_node(node, "project_profile")

    def _is_architectos_source_root(self) -> bool:
        return (
            (self.project_root / "backend" / "architectos" / "service.py").is_file()
            and (self.project_root / "frontend" / "app.js").is_file()
        )

    def _empty_system_project_response(self, project_id: str, collection: str, message: str) -> dict[str, Any]:
        return {"project_id": project_id, "root": "", collection: [], "count": 0, "message": message}

    def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        label = str(payload.get("label") or "").strip()
        text = str(payload.get("text") or "").strip()
        scope = str(payload.get("scope") or "project")
        if not label:
            raise ValueError("memory label is required")
        if not text:
            raise ValueError("memory text is required")
        node = self.repository.add_node(
            str(payload.get("type") or "Lesson"),
            label,
            scope,
            text,
            self._memory_project_id_for_scope(scope, payload.get("project_id") or "architectos"),
            payload.get("interface_id"),
            float(payload.get("confidence") or 0.8),
            {"source": str(payload.get("source") or "ui"), "source_type": str(payload.get("source_type") or payload.get("source") or "manual")},
        )
        node = self.memory_lifecycle.initialize_node(node, "ui")
        self.graph_auto_linker.link_node(node, payload.get("project_id") or "architectos")
        return node.to_dict()

    def _memory_project_id_for_scope(self, scope: str, project_id: str | None) -> str | None:
        normalized = str(scope or "project")
        if normalized in {"shared", "global"}:
            return None
        return project_id or "architectos"

    def toggle_memory_favorite(self, node_id: str) -> dict[str, Any]:
        return self.repository.toggle_memory_favorite(node_id).to_dict()


    def list_memory_candidates(self, project_id: str | None = None, status: str | None = "candidate", limit: int = 50) -> dict[str, Any]:
        normalized = str(status or "").strip().lower()
        if normalized in {"", "all", "*"}:
            status = None
        elif normalized in {"pending"}:
            status = "candidate"
        counts = self.repository.count_memory_candidates_by_status(project_id)
        return {
            "candidates": self.repository.list_memory_candidates(project_id, status, limit),
            "counts": counts,
            "pending": counts.get("pending", 0),
            "accepted": counts.get("accepted", 0),
            "rejected": counts.get("rejected", 0),
        }

    def list_memory_items(
        self,
        project_id: str | None = None,
        lifecycle_state: str | None = None,
        tier: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        self.memory_lifecycle.annotate_nodes(project_id)
        state_filter = str(lifecycle_state or "all").strip().lower()
        tier_filter = str(tier or "all").strip().lower()
        status_filter = str(status or "all").strip().lower()
        nodes = []
        counts_by_state: dict[str, int] = {}
        counts_by_tier: dict[str, int] = {}
        counts_by_status: dict[str, int] = {}
        for node in self.repository.list_nodes():
            if project_id and node.project_id not in {project_id, None}:
                continue
            metadata = dict(node.metadata or {})
            node_state = str(metadata.get("lifecycle_state") or "unknown").lower()
            node_tier = str(metadata.get("memory_tier") or "unknown").lower()
            node_status = str(node.status or "active").lower()
            counts_by_state[node_state] = counts_by_state.get(node_state, 0) + 1
            counts_by_tier[node_tier] = counts_by_tier.get(node_tier, 0) + 1
            counts_by_status[node_status] = counts_by_status.get(node_status, 0) + 1
            if state_filter not in {"", "all", "*"} and node_state != state_filter:
                continue
            if tier_filter not in {"", "all", "*"} and node_tier != tier_filter:
                continue
            if status_filter not in {"", "all", "*"} and node_status != status_filter:
                continue
            nodes.append(node.to_dict())
        limited = nodes[: max(1, limit)]
        return {
            "project_id": project_id,
            "items": limited,
            "count": len(limited),
            "total": len(nodes),
            "counts": {
                "by_state": counts_by_state,
                "by_tier": counts_by_tier,
                "by_status": counts_by_status,
            },
        }

    def memory_ingest_status(self) -> dict[str, Any]:
        with self._memory_ingest_lock:
            state = dict(self._memory_ingest_state)
            state["logs"] = list(state.get("logs") or [])
            return state

    def _reset_ingest_progress(self, *, project_id: str, sources: list[str], keep_running: bool = True) -> None:
        with self._memory_ingest_lock:
            self._memory_ingest_state = {
                "running": keep_running,
                "project_id": project_id,
                "sources": list(sources),
                "current": "starting",
                "started_at": utc_now(),
                "finished_at": "",
                "error": "",
                "result": None,
                "logs": [],
            }

    def _log_ingest(self, message: str, *, level: str = "info", source: str = "", current: str | None = None) -> None:
        entry = {
            "at": utc_now(),
            "level": level,
            "source": source,
            "message": str(message),
        }
        with self._memory_ingest_lock:
            logs = list(self._memory_ingest_state.get("logs") or [])
            logs.append(entry)
            self._memory_ingest_state["logs"] = logs[-200:]
            if current is not None:
                self._memory_ingest_state["current"] = current
            elif source:
                self._memory_ingest_state["current"] = source

    def schedule_memory_ingest(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(payload or {})
        with self._memory_ingest_lock:
            if self._memory_ingest_state.get("running"):
                state = dict(self._memory_ingest_state)
                state["logs"] = list(state.get("logs") or [])
                return {"scheduled": False, "reason": "already_running", **state}
            sources = self._normalize_ingestion_sources(
                payload.get("sources") or ["docs", "code", "chat", "git", "adr", "issues", "prs", "meetings"]
            )
            project_id = str(payload.get("project_id") or "architectos")
            self._memory_ingest_state = {
                "running": True,
                "project_id": project_id,
                "sources": sources,
                "current": "queued",
                "started_at": utc_now(),
                "finished_at": "",
                "error": "",
                "result": None,
                "logs": [{
                    "at": utc_now(),
                    "level": "info",
                    "source": "",
                    "message": f"Queued ingest for {project_id}: {', '.join(sources)}",
                }],
            }

        def worker() -> None:
            try:
                result = self.ingest_memory(payload)
                with self._memory_ingest_lock:
                    self._memory_ingest_state.update({
                        "running": False,
                        "finished_at": utc_now(),
                        "error": "",
                        "result": result,
                        "current": "done",
                    })
            except Exception as exc:  # noqa: BLE001 - keep UI responsive on ingest failure
                self._log_ingest(str(exc), level="error", current="error")
                with self._memory_ingest_lock:
                    self._memory_ingest_state.update({
                        "running": False,
                        "finished_at": utc_now(),
                        "error": str(exc),
                        "result": None,
                        "current": "error",
                    })

        threading.Thread(target=worker, name="architectos-memory-ingest", daemon=True).start()
        return {"scheduled": True, **self.memory_ingest_status()}

    def ingest_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        sources = self._normalize_ingestion_sources(payload.get("sources") or ["docs", "code", "chat", "git", "adr", "issues", "prs", "meetings"])
        limit = max(1, int(payload.get("limit") or 20))
        ingest_mode = self._normalize_ingest_mode(payload.get("ingest_mode") or payload.get("mode"))
        direct_to_memory = ingest_mode == "memory"
        mcp_direct_to_memory = ingest_mode in {"memory", "mixed"}
        project = self.repository.get_project(project_id)
        with self._memory_ingest_lock:
            progress_owned = not bool(self._memory_ingest_state.get("running"))
        if progress_owned:
            self._reset_ingest_progress(project_id=project_id, sources=sources, keep_running=True)
        else:
            with self._memory_ingest_lock:
                self._memory_ingest_state["project_id"] = project_id
                self._memory_ingest_state["sources"] = list(sources)
                self._memory_ingest_state["current"] = "starting"
        self._log_ingest(
            f"Starting ingest · project={project_id} · limit={limit} · mode={ingest_mode} · sources={', '.join(sources)}",
            current="starting",
        )
        candidates: list[dict[str, Any]] = []
        warnings: list[str] = []
        file_sources = {"docs", "code", "adr", "issues", "prs", "meetings"}
        needs_project_root = any(source in sources for source in file_sources | {"git"})
        root: Path | None = None
        try:
            if needs_project_root:
                root_raw = str(payload.get("root_path") or (project.root_path if project and project.root_path else self.project_root))
                if project_id == SYSTEM_PROJECT_ID and not str(payload.get("root_path") or (project.root_path if project else "")).strip() and self._is_architectos_source_root():
                    raise ValueError("Choose or create a project folder before ingesting memory.")
                root = Path(root_raw).resolve()
                if not root.exists() or not root.is_dir():
                    raise ValueError(f"project path does not exist: {root}")
                self._log_ingest(f"Using project root {root}", source="files", current="files")
            if any(source in sources for source in file_sources):
                assert root is not None
                wanted = [item for item in sources if item in file_sources]
                self._log_ingest(f"Scanning project files for {', '.join(wanted)}…", source="files", current="files")
                file_candidates = self._ingest_file_candidates(project_id, root, sources, limit * 2)
                candidates.extend(file_candidates)
                by_file = Counter(str(item.get("source_type") or "file") for item in file_candidates)
                detail = ", ".join(f"{key}={value}" for key, value in sorted(by_file.items())) or "none"
                self._log_ingest(f"File scan done · {len(file_candidates)} candidate(s) ({detail})", source="files")
            if "chat" in sources:
                self._log_ingest("Reading chat history…", source="chat", current="chat")
                chat_candidates = self._ingest_chat_candidates(project_id, limit)
                candidates.extend(chat_candidates)
                self._log_ingest(f"Chat done · {len(chat_candidates)} candidate(s)", source="chat")
            if "git" in sources:
                assert root is not None
                self._log_ingest("Scanning git history…", source="git", current="git")
                git_candidates = self._ingest_git_candidates(project_id, root, limit)
                candidates.extend(git_candidates)
                self._log_ingest(f"Git done · {len(git_candidates)} candidate(s)", source="git")
            if "granola" in sources:
                self._log_ingest("Calling Granola MCP…", source="granola", current="granola")
                try:
                    granola_result = self._run_source_with_timeout(
                        "granola",
                        payload,
                        warnings,
                        lambda timed: self._ingest_granola_candidates(project_id, limit, timed),
                    )
                    if granola_result is not None:
                        granola_candidates = list(granola_result or [])
                        candidates.extend(granola_candidates)
                        self._log_ingest(f"Granola done · {len(granola_candidates)} candidate(s)", source="granola")
                except MCPError as exc:
                    warnings.append(f"Granola MCP skipped: {exc}")
                    self._log_ingest(f"Granola skipped: {exc}", level="warn", source="granola")
            boards_imported: list[dict[str, Any]] = []
            boards_updated = 0
            if "azure-boards" in sources:
                self._log_ingest("Importing Azure Boards work items…", source="azure-boards", current="azure-boards")
                try:
                    if mcp_direct_to_memory:
                        boards = self._run_source_with_timeout(
                            "azure-boards",
                            payload,
                            warnings,
                            lambda timed: self._import_azure_boards_to_memory(project_id, limit, timed),
                        )
                        if boards is not None:
                            boards_imported = list(boards.get("imported") or [])
                            boards_updated = int(boards.get("updated") or 0)
                            message = f"Azure Boards done · {len(boards_imported)} written ({boards_updated} updated)"
                            self._log_ingest(message, source="azure-boards")
                    else:
                        boards_candidates = self._run_source_with_timeout(
                            "azure-boards",
                            payload,
                            warnings,
                            lambda timed: self._ingest_azure_boards_candidates(project_id, limit, timed),
                        )
                        if boards_candidates is not None:
                            candidates.extend(list(boards_candidates or []))
                            message = f"Azure Boards done · {len(boards_candidates)} candidate(s)"
                            self._log_ingest(message, source="azure-boards")
                except MCPError as exc:
                    warnings.append(f"Azure Boards MCP skipped: {exc}")
                    self._log_ingest(f"Azure Boards skipped: {exc}", level="warn", source="azure-boards")
                except ValueError as exc:
                    warnings.append(f"Azure Boards skipped: {exc}")
                    self._log_ingest(f"Azure Boards skipped: {exc}", level="warn", source="azure-boards")
            azure_git_imported: list[dict[str, Any]] = []
            azure_git_updated = 0
            if "azure-git" in sources:
                self._log_ingest("Importing Azure Git repos and pull requests…", source="azure-git", current="azure-git")
                try:
                    if mcp_direct_to_memory:
                        azure_git = self._run_source_with_timeout(
                            "azure-git",
                            payload,
                            warnings,
                            lambda timed: self._import_azure_git_to_memory(project_id, limit, timed),
                        )
                        if azure_git is not None:
                            azure_git_imported = list(azure_git.get("imported") or [])
                            azure_git_updated = int(azure_git.get("updated") or 0)
                            message = f"Azure Git done · {len(azure_git_imported)} written ({azure_git_updated} updated)"
                            self._log_ingest(message, source="azure-git")
                    else:
                        def _git_candidates(timed: dict[str, Any]) -> list[dict[str, Any]]:
                            ado_project = self._azure_boards_project(timed)
                            server_id = str(timed.get("server_id") or "").strip() or self._azure_git_mcp_server_id()
                            return self._ingest_azure_git_candidates(project_id, ado_project, server_id, limit, timed)

                        azure_git_candidates = self._run_source_with_timeout("azure-git", payload, warnings, _git_candidates)
                        if azure_git_candidates is not None:
                            candidates.extend(list(azure_git_candidates or []))
                            message = f"Azure Git done · {len(azure_git_candidates)} candidate(s)"
                            self._log_ingest(message, source="azure-git")
                except MCPError as exc:
                    warnings.append(f"Azure Git MCP skipped: {exc}")
                    self._log_ingest(f"Azure Git skipped: {exc}", level="warn", source="azure-git")
                except ValueError as exc:
                    warnings.append(f"Azure Git skipped: {exc}")
                    self._log_ingest(f"Azure Git skipped: {exc}", level="warn", source="azure-git")
            wiki_imported: list[dict[str, Any]] = []
            wiki_updated = 0
            if "azure-wiki" in sources:
                self._log_ingest("Importing Azure Wiki pages…", source="azure-wiki", current="azure-wiki")
                try:
                    if mcp_direct_to_memory:
                        wiki = self._run_source_with_timeout(
                            "azure-wiki",
                            payload,
                            warnings,
                            lambda timed: self._import_azure_wiki_to_memory(project_id, limit, timed),
                        )
                        if wiki is not None:
                            wiki_imported = list(wiki.get("imported") or [])
                            wiki_updated = int(wiki.get("updated") or 0)
                            message = f"Azure Wiki done · {len(wiki_imported)} written ({wiki_updated} updated)"
                            self._log_ingest(message, source="azure-wiki")
                    else:
                        wiki_candidates = self._run_source_with_timeout(
                            "azure-wiki",
                            payload,
                            warnings,
                            lambda timed: self._ingest_azure_wiki_candidates(project_id, limit, timed),
                        )
                        if wiki_candidates is not None:
                            candidates.extend(list(wiki_candidates or []))
                            message = f"Azure Wiki done · {len(wiki_candidates)} candidate(s)"
                            self._log_ingest(message, source="azure-wiki")
                except MCPError as exc:
                    warnings.append(f"Azure Wiki MCP skipped: {exc}")
                    self._log_ingest(f"Azure Wiki skipped: {exc}", level="warn", source="azure-wiki")
                except ValueError as exc:
                    warnings.append(f"Azure Wiki skipped: {exc}")
                    self._log_ingest(f"Azure Wiki skipped: {exc}", level="warn", source="azure-wiki")
            teams_imported: list[dict[str, Any]] = []
            teams_updated = 0
            if "teams-meetings" in sources:
                self._log_ingest("Importing Teams meetings (Graph transcripts / AI Insights)…", source="teams-meetings", current="teams-meetings")
                try:
                    if mcp_direct_to_memory:
                        teams = self._run_source_with_timeout(
                            "teams-meetings",
                            payload,
                            warnings,
                            lambda timed: self._import_teams_meetings_to_memory(project_id, limit, timed),
                        )
                        if teams is not None:
                            teams_imported = list(teams.get("imported") or [])
                            teams_updated = int(teams.get("updated") or 0)
                            message = f"Teams done · {len(teams_imported)} written ({teams_updated} updated)"
                            self._log_ingest(message, source="teams-meetings")
                    else:
                        teams_candidates = self._run_source_with_timeout(
                            "teams-meetings",
                            payload,
                            warnings,
                            lambda timed: self._ingest_teams_meeting_candidates(project_id, limit, timed),
                        )
                        if teams_candidates is not None:
                            candidates.extend(list(teams_candidates or []))
                            message = f"Teams done · {len(teams_candidates)} candidate(s)"
                            self._log_ingest(message, source="teams-meetings")
                except TeamsGraphError as exc:
                    warnings.append(f"Teams Graph skipped: {exc}")
                    self._log_ingest(f"Teams skipped: {exc}", level="warn", source="teams-meetings")
                except ValueError as exc:
                    warnings.append(f"Teams Graph skipped: {exc}")
                    self._log_ingest(f"Teams skipped: {exc}", level="warn", source="teams-meetings")
            self._log_ingest(f"Preparing {'memory writes' if direct_to_memory else 'review queue'} from {len(candidates)} raw candidate(s)…", current="prepare")
            prepared = self.ingestion_engine.prepare_candidates(project_id, candidates, limit)
            if direct_to_memory:
                written_nodes = self._write_candidates_directly_to_memory(project_id, prepared)
                saved: list[dict[str, Any]] = []
            else:
                written_nodes = []
                saved = [self.repository.upsert_memory_candidate(candidate) for candidate in prepared]
            duplicates = sum(1 for candidate in saved if dict(candidate.get("metadata") or {}).get("duplicate"))
            by_source = Counter(str(candidate.get("source_type") or "manual") for candidate in saved)
            if boards_imported:
                by_source["azure-boards"] = by_source.get("azure-boards", 0) + len(boards_imported)
            if azure_git_imported:
                by_source["azure-git"] = by_source.get("azure-git", 0) + len(azure_git_imported)
            if wiki_imported:
                by_source["azure-wiki"] = by_source.get("azure-wiki", 0) + len(wiki_imported)
            if teams_imported:
                by_source["teams-meetings"] = by_source.get("teams-meetings", 0) + len(teams_imported)
            result = {
                "project_id": project_id,
                "sources": sources,
                "ingest_mode": ingest_mode,
                "candidates": saved,
                "count": len(saved),
                "duplicates": duplicates,
                "by_source": dict(sorted(by_source.items())),
                "pending": len(self.repository.list_memory_candidates(project_id, "candidate", 500)),
                "warnings": warnings,
                "boards_imported": boards_imported,
                "boards_count": len(boards_imported),
                "boards_updated": boards_updated,
                "azure_git_imported": azure_git_imported,
                "azure_git_count": len(azure_git_imported),
                "azure_git_updated": azure_git_updated,
                "wiki_imported": wiki_imported,
                "wiki_count": len(wiki_imported),
                "wiki_updated": wiki_updated,
                "teams_imported": teams_imported,
                "teams_count": len(teams_imported),
                "teams_updated": teams_updated,
                "direct_imported": [node.to_dict() for node in written_nodes],
                "direct_count": len(written_nodes),
                "memory_written": len(written_nodes) + len(boards_imported) + len(azure_git_imported) + len(wiki_imported) + len(teams_imported),
            }
            self._log_ingest(
                f"Ingest finished · {result['count']} candidate(s), {duplicates} duplicate hint(s), {result['memory_written']} memory writes",
                current="done",
            )
            with self._memory_ingest_lock:
                self._memory_ingest_state["result"] = result
                self._memory_ingest_state["current"] = "done"
                if progress_owned:
                    self._memory_ingest_state["running"] = False
                    self._memory_ingest_state["finished_at"] = utc_now()
                    self._memory_ingest_state["error"] = ""
            return result
        except Exception as exc:
            self._log_ingest(str(exc), level="error", current="error")
            with self._memory_ingest_lock:
                if progress_owned:
                    self._memory_ingest_state["running"] = False
                    self._memory_ingest_state["finished_at"] = utc_now()
                    self._memory_ingest_state["error"] = str(exc)
            raise

    def _normalize_ingest_mode(self, value: Any) -> str:
        raw = str(value or "").strip().lower().replace("-", "_")
        if raw in {"memory", "direct", "direct_memory", "direct_to_memory", "add_to_memory"}:
            return "memory"
        if raw in {"candidate", "candidates", "review", "review_queue"}:
            return "candidates"
        if raw in {"", "mixed", "legacy", "auto"}:
            return "mixed"
        raise ValueError("ingest_mode must be candidates, memory, or mixed")

    def _resolve_ingest_timeouts(self, source: str, payload: dict[str, Any] | None = None) -> tuple[float, float]:
        """Return (item_timeout_seconds, source_timeout_seconds) for an ingest source."""
        payload = payload or {}
        defaults = DEFAULT_INGEST_TIMEOUTS.get(source) or {"item": 30, "source": 300}
        item_default = float(defaults.get("item") or 30)
        source_default = float(defaults.get("source") or 300)

        per_source = {}
        raw_map = payload.get("timeouts")
        if isinstance(raw_map, dict):
            entry = raw_map.get(source) or raw_map.get(source.replace("-", "_"))
            if isinstance(entry, dict):
                per_source = entry
            elif isinstance(entry, (int, float, str)):
                per_source = {"source": entry}

        source_key = source.replace("-", "_")
        item_raw = (
            per_source.get("item")
            or per_source.get("item_timeout")
            or payload.get(f"{source_key}_item_timeout")
            or payload.get("item_timeout")
            or item_default
        )
        source_raw = (
            per_source.get("source")
            or per_source.get("source_timeout")
            or payload.get(f"{source_key}_source_timeout")
            or payload.get("source_timeout")
            or source_default
        )
        try:
            item_timeout = float(item_raw)
        except (TypeError, ValueError):
            item_timeout = item_default
        try:
            source_timeout = float(source_raw)
        except (TypeError, ValueError):
            source_timeout = source_default
        item_timeout = max(5.0, min(item_timeout, 600.0))
        source_timeout = max(item_timeout, min(source_timeout, 7200.0))
        return item_timeout, source_timeout

    def _with_ingest_timeouts(self, source: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        enriched = dict(payload or {})
        item_timeout, source_timeout = self._resolve_ingest_timeouts(source, enriched)
        enriched["_ingest_source"] = source
        enriched["_ingest_item_timeout"] = item_timeout
        enriched["_ingest_source_timeout"] = source_timeout
        enriched["_ingest_source_deadline"] = time.monotonic() + source_timeout
        return enriched

    @staticmethod
    def _ingest_item_timeout(payload: dict[str, Any] | None, default: float = 30.0) -> float:
        payload = payload or {}
        try:
            value = float(payload.get("_ingest_item_timeout") or default)
        except (TypeError, ValueError):
            value = default
        return max(5.0, min(value, 600.0))

    @staticmethod
    def _ingest_deadline_remaining(payload: dict[str, Any] | None) -> float | None:
        payload = payload or {}
        deadline = payload.get("_ingest_source_deadline")
        if deadline is None:
            return None
        try:
            return max(0.0, float(deadline) - time.monotonic())
        except (TypeError, ValueError):
            return None

    def _ingest_deadline_expired(self, payload: dict[str, Any] | None, source: str = "") -> bool:
        remaining = self._ingest_deadline_remaining(payload)
        if remaining is None:
            return False
        if remaining > 0:
            return False
        label = source or str((payload or {}).get("_ingest_source") or "source")
        self._log_ingest(
            f"{label} source timeout reached — skipping remaining work.",
            level="warn",
            source=label,
            current=label,
        )
        return True

    def _abort_ingest_source(self, source: str) -> None:
        """Best-effort: kill wedged MCP sessions so a hung item cannot block later sources."""
        session_ids = {
            "azure-boards": ("azure-devops",),
            "azure-git": ("azure-devops-git", "azure-devops"),
            "azure-wiki": ("azure-devops",),
            "granola": ("granola",),
        }.get(source, ())
        for server_id in session_ids:
            try:
                self.mcp_manager.close_session(server_id)
            except Exception:
                pass

    def _set_ingest_partial(self, payload: dict[str, Any] | None, result: Any) -> None:
        """Publish a salvageable mid-flight result for source-timeout recovery."""
        box = (payload or {}).get("_ingest_partial")
        if isinstance(box, dict):
            box["result"] = result

    def _run_source_with_timeout(
        self,
        source: str,
        payload: dict[str, Any] | None,
        warnings: list[str],
        fn: Callable[[dict[str, Any]], Any],
    ) -> Any:
        """Run one ingest source under a hard source-level timeout.

        Important: never use ``with ThreadPoolExecutor(...)`` here. Its ``__exit__``
        calls ``shutdown(wait=True)``, which would block past the timeout while a
        wedged MCP worker keeps running.

        Sources should call ``_set_ingest_partial`` as items complete so a timeout
        can still return already-fetched candidates instead of discarding them.
        """
        timed = self._with_ingest_timeouts(source, payload)
        item_timeout = float(timed["_ingest_item_timeout"])
        source_timeout = float(timed["_ingest_source_timeout"])
        partial_box: dict[str, Any] = {"result": None}
        timed["_ingest_partial"] = partial_box
        self._log_ingest(
            f"Timeouts · item={item_timeout:g}s · source={source_timeout:g}s",
            source=source,
            current=source,
        )
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(fn, timed)
            try:
                return future.result(timeout=source_timeout)
            except FuturesTimeoutError:
                message = f"{source} source timeout after {source_timeout:g}s"
                warnings.append(message)
                self._log_ingest(message, level="warn", source=source, current=source)
                self._abort_ingest_source(source)
                # Brief grace: inner path may be returning partial candidates right now.
                try:
                    result = future.result(timeout=2.0)
                    if result is not None:
                        self._log_ingest(
                            f"{source} kept partial result after timeout.",
                            level="warn",
                            source=source,
                            current=source,
                        )
                        return result
                except FuturesTimeoutError:
                    pass
                except Exception:
                    pass
                salvaged = partial_box.get("result")
                if salvaged is not None:
                    count = len(salvaged) if isinstance(salvaged, list) else (
                        len(salvaged.get("imported") or []) if isinstance(salvaged, dict) else 1
                    )
                    self._log_ingest(
                        f"{source} salvaged {count} partial item(s) after timeout.",
                        level="warn",
                        source=source,
                        current=source,
                    )
                    return salvaged
                future.cancel()
                return None
        finally:
            # Detach immediately — do not wait for hung MCP/stdio workers.
            executor.shutdown(wait=False, cancel_futures=True)

    def _write_candidates_directly_to_memory(self, project_id: str, candidates: list[dict[str, Any]]) -> list[Any]:
        written = []
        for candidate in candidates:
            scope = str(candidate.get("scope") or "project")
            metadata = self._promoted_candidate_metadata(candidate)
            metadata["source"] = "autoscan_direct"
            metadata["ingest_mode"] = "memory"
            metadata["candidate_source_id"] = candidate.get("id")
            node = self.repository.add_node(
                str(candidate.get("type") or "Lesson"),
                str(candidate.get("label") or "AutoScan memory"),
                scope,
                str(candidate.get("text") or ""),
                self._memory_project_id_for_scope(scope, project_id),
                None,
                float(candidate.get("confidence") or 0.72),
                metadata,
            )
            node = self.memory_lifecycle.initialize_node(node, "autoscan_direct")
            self.graph_auto_linker.link_node(node, project_id)
            if str(candidate.get("source_type") or "") == "azure-boards":
                self._link_azure_boards_relations(node, candidate)
            written.append(node)
        return written

    def memory_rescan_status(self) -> dict[str, Any]:
        with self._memory_rescan_lock:
            return dict(self._memory_rescan_state)

    def _default_rescan_sources(self, include_mcp: bool = True) -> list[str]:
        sources = list(ALL_LOCAL_INGESTION_SOURCES)
        if not include_mcp:
            return sources
        granola = self.mcp_manager.get_server("granola")
        if granola and granola.enabled:
            sources.append("granola")
        ado = self.mcp_manager.get_server("azure-devops")
        if ado and ado.enabled:
            sources.append("azure-boards")
            sources.append("azure-wiki")
        ado_git = self.mcp_manager.get_server("azure-devops-git")
        if (ado and ado.enabled) or (ado_git and ado_git.enabled):
            sources.append("azure-git")
        if teams_graph_configured():
            sources.append("teams-meetings")
        return sources

    def _memory_rescan_settings(self) -> dict[str, Any]:
        life = dict(self.repository.get_setting("memory_lifecycle") or {})
        return {
            "auto_rescan_on_startup": bool(life.get("auto_rescan_on_startup", True)),
            "auto_rescan_limit": max(1, int(life.get("auto_rescan_limit") or 24)),
            "auto_rescan_all_projects": bool(life.get("auto_rescan_all_projects", True)),
        }

    def rescan_memory_sources(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        settings = self._memory_rescan_settings()
        include_mcp = payload.get("include_mcp")
        if include_mcp is None:
            include_mcp = True
        sources = payload.get("sources")
        if not sources or sources == "all" or sources == ["all"]:
            sources = self._default_rescan_sources(include_mcp=bool(include_mcp))
        else:
            sources = self._normalize_ingestion_sources(sources)
        limit = max(1, int(payload.get("limit") or settings["auto_rescan_limit"]))
        all_projects = bool(payload.get("all_projects", settings["auto_rescan_all_projects"]))
        project_id = str(payload.get("project_id") or "").strip()
        projects = self._projects_for_memory_rescan(project_id or None, all_projects=all_projects)
        results: list[dict[str, Any]] = []
        warnings: list[str] = []
        total = 0
        duplicates = 0
        memory_written = 0
        boards_count = 0
        azure_git_count = 0
        wiki_count = 0
        teams_count = 0
        for project in projects:
            try:
                self._log_ingest(
                    f"Rescan project {project.name or project.id} · sources={', '.join(sources)}",
                    current=f"project:{project.id}",
                )
                ingest = self.ingest_memory({
                    "project_id": project.id,
                    "sources": sources,
                    "limit": limit,
                    "root_path": project.root_path,
                    "all_items": payload.get("all_items"),
                    "ingest_mode": payload.get("ingest_mode") or payload.get("mode"),
                })
            except ValueError as exc:
                warnings.append(f"{project.name or project.id}: {exc}")
                continue
            warnings.extend(str(item) for item in (ingest.get("warnings") or []))
            total += int(ingest.get("count") or 0)
            duplicates += int(ingest.get("duplicates") or 0)
            memory_written += int(ingest.get("memory_written") or 0)
            boards_count += int(ingest.get("boards_count") or 0)
            azure_git_count += int(ingest.get("azure_git_count") or 0)
            wiki_count += int(ingest.get("wiki_count") or 0)
            teams_count += int(ingest.get("teams_count") or 0)
            results.append({
                "project_id": project.id,
                "name": project.name,
                "count": ingest.get("count") or 0,
                "duplicates": ingest.get("duplicates") or 0,
                "by_source": ingest.get("by_source") or {},
                "pending": ingest.get("pending") or 0,
                "memory_written": ingest.get("memory_written") or 0,
                "boards_count": ingest.get("boards_count") or 0,
                "azure_git_count": ingest.get("azure_git_count") or 0,
                "wiki_count": ingest.get("wiki_count") or 0,
                "teams_count": ingest.get("teams_count") or 0,
            })
        return {
            "ok": True,
            "sources": sources,
            "limit": limit,
            "projects": results,
            "count": total,
            "duplicates": duplicates,
            "memory_written": memory_written,
            "boards_count": boards_count,
            "azure_git_count": azure_git_count,
            "wiki_count": wiki_count,
            "teams_count": teams_count,
            "warnings": warnings,
            "pending": sum(int(item.get("pending") or 0) for item in results),
        }

    def _projects_for_memory_rescan(self, project_id: str | None, *, all_projects: bool) -> list[Project]:
        if not all_projects:
            target_id = project_id
            if not target_id:
                workspace = dict(self.repository.get_setting("workspace") or {})
                target_id = str(workspace.get("current_project_id") or "")
            project = self.repository.get_project(target_id) if target_id else None
            if not project:
                raise ValueError(f"project not found: {target_id or '(none)'}")
            return [project]
        projects: list[Project] = []
        for project in self.repository.list_projects():
            root = str(project.root_path or "").strip()
            if project.id == SYSTEM_PROJECT_ID and not root:
                continue
            if not root:
                continue
            projects.append(project)
        return projects

    def start_background_maintenance(self) -> None:
        """Spawn embedding warmup/backfill threads. Called by the HTTP entry
        points, not __init__, so tests and short-lived service instances never
        race daemon threads against tempdir cleanup."""
        if self.memory_embeddings.enabled() and bool((self.repository.get_setting("memory_retrieval") or {}).get("reindex_on_startup", True)):
            # Never block HTTP startup on embedding backfill — local Ollama/bge-m3 can
            # take minutes across thousands of nodes.
            threading.Thread(target=self._backfill_embeddings_safe, name="embed-backfill", daemon=True).start()
        if self.memory_embeddings.enabled():
            threading.Thread(target=self._warmup_embeddings_safe, name="embed-warmup", daemon=True).start()

    def schedule_startup_memory_rescan(self) -> dict[str, Any]:
        settings = self._memory_rescan_settings()
        if not settings["auto_rescan_on_startup"]:
            return {"scheduled": False, "reason": "disabled"}
        return self.schedule_memory_rescan({
            "trigger": "startup",
            "all_projects": settings["auto_rescan_all_projects"],
            "limit": settings["auto_rescan_limit"],
            "include_mcp": True,
        })

    def schedule_memory_rescan(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(payload or {})
        trigger = str(payload.get("trigger") or "manual")
        with self._memory_rescan_lock:
            if self._memory_rescan_state.get("running"):
                return {"scheduled": False, "reason": "already_running", **dict(self._memory_rescan_state)}
            self._memory_rescan_state = {
                "running": True,
                "trigger": trigger,
                "started_at": utc_now(),
                "finished_at": "",
                "error": "",
                "result": None,
            }

        def worker() -> None:
            try:
                result = self.rescan_memory_sources(payload)
                with self._memory_rescan_lock:
                    self._memory_rescan_state.update({
                        "running": False,
                        "finished_at": utc_now(),
                        "error": "",
                        "result": result,
                    })
                _LOG.info(
                    "memory rescan (%s) finished: %s candidate(s) across %s project(s)",
                    trigger,
                    result.get("count"),
                    len(result.get("projects") or []),
                )
            except Exception as exc:  # noqa: BLE001 - background worker must not crash the app
                _LOG.exception("memory rescan (%s) failed", trigger)
                with self._memory_rescan_lock:
                    self._memory_rescan_state.update({
                        "running": False,
                        "finished_at": utc_now(),
                        "error": str(exc),
                        "result": None,
                    })

        threading.Thread(target=worker, name=f"architectos-memory-rescan-{trigger}", daemon=True).start()
        return {"scheduled": True, "trigger": trigger, **self.memory_rescan_status()}

    def promote_memory_candidate(
        self,
        candidate_id: str,
        payload: dict[str, Any] | None = None,
        *,
        by_work_item: dict[str, Any] | None = None,
        hub_cache: dict[tuple[str, str | None, str], Any] | None = None,
        existing_edge_ids: set[str] | None = None,
        nodes_snapshot: list[Any] | None = None,
        project_root_cache: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        candidate = self.repository.get_memory_candidate(candidate_id)
        if not candidate:
            raise ValueError("memory candidate not found")
        payload = payload or {}
        if candidate.get("status") == "promoted" and candidate.get("promoted_node_id"):
            node = self.repository.get_node(str(candidate["promoted_node_id"]))
            return {"candidate": candidate, "memory": node.to_dict() if node else None}
        node_type = str(payload.get("type") or candidate.get("type") or "Lesson")
        created_at = utc_now()
        metadata = self.memory_lifecycle.seed_metadata(
            node_type=node_type,
            created_at=created_at,
            metadata=self._promoted_candidate_metadata(candidate),
            source="promoted_candidate",
        )
        node = self.repository.add_node(
            node_type,
            str(payload.get("label") or candidate.get("label") or "Memory candidate"),
            str(payload.get("scope") or candidate.get("scope") or "project"),
            str(payload.get("text") or candidate.get("text") or ""),
            self._memory_project_id_for_scope(
                str(payload.get("scope") or candidate.get("scope") or "project"),
                str(candidate.get("project_id") or "architectos"),
            ),
            payload.get("interface_id"),
            float(payload.get("confidence") or candidate.get("confidence") or 0.72),
            metadata,
        )
        # Lifecycle already seeded — initialize_node is a no-op write skip.
        node = self.memory_lifecycle.initialize_node(node, "promoted_candidate")
        if nodes_snapshot is not None:
            nodes_snapshot.append(node)
        self.graph_auto_linker.link_node(
            node,
            str(candidate.get("project_id") or "architectos"),
            hub_cache=hub_cache,
            existing_edge_ids=existing_edge_ids,
            nodes_snapshot=nodes_snapshot,
            project_root_cache=project_root_cache,
        )
        self._link_azure_boards_relations(node, candidate, by_work_item=by_work_item)
        candidate["status"] = "promoted"
        candidate["promoted_node_id"] = node.id
        candidate["promoted_at"] = utc_now()
        candidate = self.repository.update_memory_candidate(candidate)
        return {"candidate": candidate, "memory": node.to_dict()}

    def _promoted_candidate_metadata(self, candidate: dict[str, Any]) -> dict[str, Any]:
        meta = dict(candidate.get("metadata") or {})
        payload = {
            "source": "memory_candidate",
            "candidate_id": candidate["id"],
            "source_type": candidate.get("source_type"),
            "source_ref": candidate.get("source_ref"),
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
        ):
            if meta.get(key) not in (None, "", [], {}):
                payload[key] = meta[key]
        return payload

    def _link_azure_boards_relations(self, node, candidate: dict[str, Any], by_work_item: dict[str, Any] | None = None) -> None:
        metadata = dict(candidate.get("metadata") or {})
        relations = list(metadata.get("relations") or [])
        if not relations:
            return
        project_id = str(candidate.get("project_id") or "architectos")
        if by_work_item is None:
            by_work_item = {}
            for existing in self.repository.list_nodes():
                if existing.status != "active":
                    continue
                if existing.project_id not in {project_id, None}:
                    continue
                work_item_id = str(dict(existing.metadata or {}).get("work_item_id") or "").strip()
                if work_item_id:
                    by_work_item[work_item_id] = existing
        for relation in relations:
            if not isinstance(relation, dict):
                continue
            related_id = str(relation.get("work_item_id") or "").strip()
            target = by_work_item.get(related_id)
            if not target or target.id == node.id:
                continue
            edge_type = str(relation.get("link_type") or "related").replace(" ", "_")[:48] or "related"
            try:
                self.repository.add_edge(node.id, target.id, edge_type, "project", 0.86)
            except Exception:  # noqa: BLE001 - linking is best-effort
                continue

    def reject_memory_candidate(self, candidate_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        candidate = self.repository.get_memory_candidate(candidate_id)
        if not candidate:
            raise ValueError("memory candidate not found")
        candidate["status"] = "rejected"
        candidate["rejected_at"] = utc_now()
        candidate["reject_reason"] = str((payload or {}).get("reason") or "")
        return {"candidate": self.repository.update_memory_candidate(candidate)}

    def batch_update_memory_candidates(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        project_id = str(payload.get("project_id") or "architectos")
        action = str(payload.get("action") or "").strip().lower()
        if action in {"accept", "approve", "promote"}:
            action = "promote"
        elif action in {"reject", "decline"}:
            action = "reject"
        else:
            raise ValueError("action must be promote/accept or reject")

        status_filter = self._normalize_candidate_batch_status(payload.get("status") or payload.get("filter"))
        duplicate_only = bool(payload.get("duplicate_only") or payload.get("duplicates_only") or status_filter == "duplicate")
        exclude_duplicates = bool(payload.get("exclude_duplicates") or False)
        if status_filter == "duplicate":
            status_filter = "candidate"
            duplicate_only = True
        process_all = bool(payload.get("all") or payload.get("process_all") or payload.get("entire_queue"))
        # Default page-sized batches stay modest; Accept/Reject all processes the full matching set.
        if process_all:
            limit = None
        else:
            limit = max(1, min(int(payload.get("limit") or 500), 5000))
        reason = str(payload.get("reason") or ("Batch rejected in Memory UI" if action == "reject" else ""))

        listed_status = None if status_filter in {None, "all"} else status_filter
        candidates = self.repository.list_memory_candidates(project_id, listed_status, limit)
        selected: list[dict[str, Any]] = []
        for candidate in candidates:
            meta = dict(candidate.get("metadata") or {})
            is_duplicate = bool(meta.get("duplicate"))
            if duplicate_only and not is_duplicate:
                continue
            if exclude_duplicates and is_duplicate:
                continue
            selected.append(candidate)

        promoted = 0
        rejected = 0
        skipped = 0
        errors: list[str] = []
        results: list[dict[str, Any]] = []

        # Accept-all used to re-scan all memory nodes/edges per candidate (O(n²)).
        # Share indexes for the batch and pause embedding listeners until the end.
        by_work_item: dict[str, Any] | None = None
        hub_cache: dict[tuple[str, str | None, str], Any] | None = None
        existing_edge_ids: set[str] | None = None
        nodes_snapshot: list[Any] | None = None
        project_root_cache: dict[str, Any] | None = None
        paused_listeners: list[Any] = []
        if action == "promote" and selected:
            nodes_snapshot = list(self.repository.list_nodes())
            by_work_item = {}
            for existing in nodes_snapshot:
                if existing.status != "active":
                    continue
                if existing.project_id not in {project_id, None}:
                    continue
                work_item_id = str(dict(existing.metadata or {}).get("work_item_id") or "").strip()
                if work_item_id:
                    by_work_item[work_item_id] = existing
            hub_cache = {}
            project_root_cache = {}
            existing_edge_ids = {edge.id for edge in self.repository.list_edges()}
            paused_listeners = list(self.repository._node_upsert_listeners)
            self.repository._node_upsert_listeners = []

        try:
            for candidate in selected:
                candidate_id = str(candidate.get("id") or "")
                status = str(candidate.get("status") or "")
                try:
                    if action == "promote":
                        if status == "promoted" and candidate.get("promoted_node_id"):
                            skipped += 1
                            continue
                        result = self.promote_memory_candidate(
                            candidate_id,
                            {},
                            by_work_item=by_work_item,
                            hub_cache=hub_cache,
                            existing_edge_ids=existing_edge_ids,
                            nodes_snapshot=nodes_snapshot,
                            project_root_cache=project_root_cache,
                        )
                        memory = result.get("memory") or {}
                        memory_id = memory.get("id")
                        if memory_id and by_work_item is not None:
                            work_item_id = str((memory.get("metadata") or {}).get("work_item_id") or "").strip()
                            if work_item_id and nodes_snapshot is not None:
                                # Prefer the node already appended to the snapshot.
                                node = next((item for item in reversed(nodes_snapshot) if item.id == memory_id), None)
                                if node is None:
                                    node = self.repository.get_node(str(memory_id))
                                if node:
                                    by_work_item[work_item_id] = node
                        promoted += 1
                        results.append({"id": candidate_id, "status": "promoted", "memory_id": memory_id})
                    else:
                        if status == "rejected":
                            skipped += 1
                            continue
                        if status == "promoted":
                            # Do not delete durable memory on batch reject; only skip already promoted.
                            skipped += 1
                            continue
                        self.reject_memory_candidate(candidate_id, {"reason": reason})
                        rejected += 1
                        results.append({"id": candidate_id, "status": "rejected"})
                except Exception as exc:  # noqa: BLE001 - continue batch on single failures
                    errors.append(f"{candidate_id}: {exc}")
        finally:
            if paused_listeners:
                self.repository._node_upsert_listeners = paused_listeners
                # Catch up embeddings asynchronously so Accept All stays responsive.
                if promoted and self.memory_embeddings.enabled():
                    threading.Thread(
                        target=self._backfill_embeddings_safe,
                        name="embed-after-batch-promote",
                        daemon=True,
                    ).start()

        counts = self.repository.count_memory_candidates_by_status(project_id)
        return {
            "project_id": project_id,
            "action": action,
            "status": listed_status or "all",
            "duplicate_only": duplicate_only,
            "exclude_duplicates": exclude_duplicates,
            "process_all": process_all,
            "matched": len(selected),
            "promoted": promoted,
            "rejected": rejected,
            "skipped": skipped,
            "errors": errors[:50],
            "error_count": len(errors),
            "results": results[:100],
            "pending": counts.get("pending", 0),
            "accepted": counts.get("accepted", 0),
            "counts": counts,
        }

    def _normalize_candidate_batch_status(self, value: Any) -> str | None:
        raw = str(value or "").strip().lower()
        if not raw or raw in {"pending", "candidate", "all_pending"}:
            return "candidate"
        if raw in {"rejected", "reject"}:
            return "rejected"
        if raw in {"promoted", "promote", "accepted"}:
            return "promoted"
        if raw in {"duplicate", "duplicates"}:
            return "duplicate"
        if raw in {"all", "*"}:
            return "all"
        raise ValueError("status/filter must be pending, duplicate, rejected, promoted, or all")

    def search_memory(self, query: str, project_id: str | None = None, scope: str | None = None, limit: int = 8, refresh: bool = False) -> dict[str, Any]:
        # FTS + vector in parallel; vector is best-effort with a hard timeout.
        # Lifecycle bump is opt-in (refresh=True) so interactive search never waits on writes.
        limit = max(1, min(int(limit or 8), 40))
        pool = max(limit * 4, 32)
        score_cap = max(pool, 80)
        retrieval_settings = self.memory_embeddings.settings()
        vector_pool = max(8, min(int(retrieval_settings.get("vector_pool") or 64), 200))
        try:
            timeout_s = max(0.05, min(int(retrieval_settings.get("vector_query_timeout_ms") or 2500), 5000) / 1000.0)
        except (TypeError, ValueError):
            timeout_s = 2.5
        try:
            self.search_strategy.set_feedback_scores(self.repository.feedback_scores_for_nodes(project_id))
        except Exception:
            self.search_strategy.set_feedback_scores({})

        # Kick vector early so Cloudflare latency overlaps with FTS.
        vector_future = None
        vector_executor = None
        if self.memory_embeddings.enabled():
            vector_executor = ThreadPoolExecutor(max_workers=1)
            vector_future = vector_executor.submit(
                self.memory_embeddings.search_scored,
                query,
                project_id=project_id,
                scope=scope,
                limit=vector_pool,
            )

        fts_ids = self.repository.search_memory_fts(query, project_id=project_id, scope=scope, limit=pool)
        vector_scores, vector_ms, vector_timed_out = self._vector_search_memory_timed(
            query,
            project_id=project_id,
            scope=scope,
            limit=vector_pool,
            timeout_s=timeout_s,
            future=vector_future,
            executor=vector_executor,
        )
        vector_ids = list(vector_scores.keys())

        mode = "full-scan"
        keep: set[str] = set(fts_ids) | set(vector_ids)
        if fts_ids and vector_ids:
            mode = "hybrid"
        elif fts_ids:
            mode = "fts-prefilter"
        elif vector_ids:
            mode = "vector-prefilter"

        if keep:
            # Prefer exact retrieval hits; avoid loading the whole graph/edge table.
            ordered = list(dict.fromkeys([*fts_ids, *vector_ids]))
            keep = set(ordered[:score_cap])
            nodes = self.repository.list_nodes_by_ids(list(keep))
        else:
            # No lexical or vector hits — return empty quickly.
            return {
                "query": query,
                "hits": [],
                "retrieval": {
                    "mode": "empty",
                    "fts_hits": 0,
                    "vector_hits": 0,
                    "vector_ms": vector_ms,
                    "vector_timed_out": vector_timed_out,
                    "scored_nodes": 0,
                    "embeddings": self.memory_embeddings.provider_info(),
                },
            }

        hits = self.search_strategy.search(
            query,
            nodes,
            [],  # skip edge expansion for interactive search latency
            project_id,
            scope,
            limit,
            diversify=True,
            semantic_rerank=False,  # persisted vectors already scored; no live re-embed
            vector_scores=vector_scores,
            fts_ids=set(fts_ids),
        )
        if refresh and hits:
            hit_ids = [hit.node.id for hit in hits]
            try:
                self.memory_lifecycle.refresh_nodes(hit_ids, "search")
                refreshed = {node.id: node for node in self.repository.list_nodes_by_ids(hit_ids)}
                for hit in hits:
                    hit.node = refreshed.get(hit.node.id, hit.node)
            except Exception as exc:
                _LOG.warning("search refresh skipped: %s", exc)
        return {
            "query": query,
            "hits": [self._compact_search_hit(hit.to_dict()) for hit in hits],
            "retrieval": {
                "mode": mode,
                "fts_hits": len(fts_ids),
                "vector_hits": len(vector_ids),
                "vector_ms": vector_ms,
                "vector_timed_out": vector_timed_out,
                "scored_nodes": len(nodes),
                "embeddings": self.memory_embeddings.provider_info(),
            },
        }

    def _vector_search_memory_timed(
        self,
        query: str,
        *,
        project_id: str | None,
        scope: str | None,
        limit: int,
        timeout_s: float,
        future=None,
        executor=None,
    ) -> tuple[dict[str, float], float, bool]:
        """Best-effort semantic prefilter; never blocks Search longer than timeout_s."""
        if not self.memory_embeddings.enabled():
            return {}, 0.0, False
        started = time.perf_counter()
        owns_executor = executor is None
        if future is None:
            executor = ThreadPoolExecutor(max_workers=1)
            owns_executor = True
            future = executor.submit(
                self.memory_embeddings.search_scored,
                query,
                project_id=project_id,
                scope=scope,
                limit=limit,
            )
        timed_out = False
        scored: list[tuple[str, float]] = []
        try:
            # If the future was started before FTS, only wait for the remaining budget.
            wait_for = max(0.05, timeout_s - (time.perf_counter() - started))
            scored = future.result(timeout=wait_for)
        except FuturesTimeoutError:
            timed_out = True
            future.cancel()
            _LOG.debug("memory vector search timed out after %.0fms", timeout_s * 1000)
        except Exception as exc:
            _LOG.warning("memory vector search failed: %s", exc)
        finally:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        return {node_id: float(score) for node_id, score in scored}, elapsed_ms, timed_out

    @staticmethod
    def _compact_search_hit(hit: dict[str, Any]) -> dict[str, Any]:
        """Drop bulky metadata so Search palette responses stay small/fast."""
        node = dict(hit.get("node") or {})
        metadata = dict(node.get("metadata") or {})
        for key in (
            "comments",
            "description_full",
            "relations",
            "transcript",
            "raw",
            "payload",
            "diff",
            "files",
        ):
            metadata.pop(key, None)
        text = str(node.get("text") or "")
        if len(text) > 1200:
            node["text"] = text[:1200]
        node["metadata"] = metadata
        hit["node"] = node
        return hit

    def context(self, query: str, project_id: str | None = None, scope: str | None = None, limit: int = 8) -> dict[str, Any]:
        search = self.search_memory(query, project_id=project_id, scope=scope, limit=max(limit, 10))
        tasks = [task for task in self.repository.list_tasks(project_id) if task["status"] != "done"][:5]
        providers = [provider for provider in self.repository.list_providers() if provider["enabled"]][:5]
        boards_ids = self._boards_ids_from_memory_hits(search["hits"])
        packed = pack_memory_context(
            search["hits"],
            project_id=project_id,
            scope=scope,
            tasks=tasks,
            providers=providers,
            boards_ids=boards_ids,
        )
        return {
            "query": query,
            "context": packed,
            "hits": search["hits"],
            "retrieval": search.get("retrieval") or {},
        }

    def _select_graph_nodes_by_source_quota(
        self,
        nodes: list[Any],
        edges: list[Any],
        limit: int,
        must_ids: set[str] | None = None,
    ) -> list[Any]:
        """Pick up to ``limit`` nodes with a fair per-source quota.

        Each source gets an equal share of the remaining budget. If a source has
        fewer nodes than its share, leftover slots go round-robin to the next
        sources that still have candidates (so sparse sources don't waste quota
        while dense ones like Azure Boards eat the whole graph).
        """
        if limit <= 0:
            return []
        if len(nodes) <= limit:
            return list(nodes)

        must_ids = must_ids or set()
        degree: dict[str, int] = {}
        for edge in edges:
            source = getattr(edge, "source", None) or (edge.get("source") if isinstance(edge, dict) else None)
            target = getattr(edge, "target", None) or (edge.get("target") if isinstance(edge, dict) else None)
            if source:
                degree[str(source)] = degree.get(str(source), 0) + 1
            if target:
                degree[str(target)] = degree.get(str(target), 0) + 1

        must: list[Any] = []
        pools: dict[str, list[Any]] = {}
        seen: set[str] = set()
        for node in nodes:
            node_id = str(getattr(node, "id", None) or (node.get("id") if isinstance(node, dict) else "") or "")
            if not node_id or node_id in seen:
                continue
            metadata = dict(getattr(node, "metadata", None) or (node.get("metadata") if isinstance(node, dict) else {}) or {})
            node_type = str(getattr(node, "type", None) or (node.get("type") if isinstance(node, dict) else "") or "")
            is_must = (
                node_id in must_ids
                or node_type == "Project"
                or bool(metadata.get("favorite") or metadata.get("pinned"))
            )
            if is_must:
                must.append(node)
                seen.add(node_id)
                continue
            source_key = self.graph_auto_linker._source_key_for_node(node)
            pools.setdefault(source_key, []).append(node)
            seen.add(node_id)

        def _rank_key(item: Any) -> tuple[int, str]:
            item_id = str(getattr(item, "id", None) or (item.get("id") if isinstance(item, dict) else "") or "")
            label = str(getattr(item, "label", None) or (item.get("label") if isinstance(item, dict) else "") or "")
            return (-degree.get(item_id, 0), label.lower())

        for key in pools:
            pools[key].sort(key=_rank_key)

        selected: list[Any] = list(must)
        selected_ids = {
            str(getattr(node, "id", None) or (node.get("id") if isinstance(node, dict) else "") or "")
            for node in selected
        }
        remaining_budget = max(0, limit - len(selected))
        source_keys = sorted(pools.keys())
        if not source_keys or remaining_budget <= 0:
            return selected[:limit]

        base = remaining_budget // len(source_keys)
        bonus = remaining_budget % len(source_keys)
        leftovers: dict[str, list[Any]] = {}
        for index, key in enumerate(source_keys):
            quota = base + (1 if index < bonus else 0)
            bucket = pools[key]
            take = min(quota, len(bucket))
            for node in bucket[:take]:
                node_id = str(getattr(node, "id", None) or "")
                selected.append(node)
                selected_ids.add(node_id)
            leftovers[key] = bucket[take:]

        # Unused quota → next sources that still have nodes (round-robin).
        slots_left = limit - len(selected)
        while slots_left > 0:
            progressed = False
            for key in source_keys:
                queue = leftovers.get(key) or []
                if not queue:
                    continue
                node = queue.pop(0)
                node_id = str(getattr(node, "id", None) or "")
                if node_id and node_id not in selected_ids:
                    selected.append(node)
                    selected_ids.add(node_id)
                    slots_left -= 1
                    progressed = True
                    if slots_left <= 0:
                        break
            if not progressed:
                break
        return selected[:limit]

    def graph(
        self,
        project_id: str | None = None,
        task_id: str | None = None,
        provider_id: str | None = None,
        pinned: bool = False,
        node_type: str | None = None,
        source: str | None = None,
        scope: str | None = None,
        limit: int | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        self.memory_lifecycle.annotate_nodes(project_id)
        all_nodes = [node for node in self.repository.list_nodes() if node.status == "active"]
        if project_id:
            all_nodes = [node for node in all_nodes if node.project_id in {project_id, None} or node.scope in {"shared", "global"}]
        
        # Unique types/scopes/sources for filter dropdowns (before filtering)
        types = sorted(list({node.type for node in all_nodes if node.type}))
        scopes = sorted(list({node.scope for node in all_nodes if node.scope}))
        source_keys = sorted({self.graph_auto_linker._source_key_for_node(node) for node in all_nodes})
        sources = [
            {
                "id": key,
                "label": (SOURCE_HUB_CATALOG.get(key) or {}).get("label") or key,
            }
            for key in source_keys
        ]
        
        if pinned:
            all_nodes = [node for node in all_nodes if bool(node.metadata.get("favorite") or node.metadata.get("pinned"))]
            
        task = self.repository.get_task(task_id) if task_id else None
        task_node: dict[str, Any] | None = None
        if task:
            linked_ids = set(task.get("linked_memory_ids") or [])
            all_nodes = [node for node in all_nodes if node.id in linked_ids]
            task_node = {"id": f"task:{task['id']}", "type": "Task", "label": task["title"], "scope": "project", "text": task.get("detail") or "", "project_id": task.get("project_id"), "metadata": {"synthetic": True, "task_id": task["id"]}}
            
        provider_node: dict[str, Any] | None = None
        if provider_id:
            provider = next((item for item in self.repository.list_providers() if item["id"] == provider_id), None)
            needle = " ".join(str(item or "").lower() for item in [provider_id, provider.get("label") if provider else ""])
            all_nodes = [node for node in all_nodes if provider_id.lower() in (node.label + " " + node.text).lower() or (provider and str(provider.get("label") or "").lower() in (node.label + " " + node.text).lower())]
            provider_node = {"id": f"provider:{provider_id}", "type": "Provider", "label": (provider or {}).get("label") or provider_id, "scope": "project", "text": (provider or {}).get("notes") or f"Provider filter for {provider_id}.", "project_id": project_id, "metadata": {"synthetic": True, "provider_id": provider_id, "needle": needle.strip()}}
            
        # Apply source/type/scope filters
        filtered_nodes = all_nodes
        source_key = self.graph_auto_linker._normalize_source_key(source) if source else ""
        if source_key:
            # Keep Project roots visible so Groups → Projects and the graph spine still work.
            filtered_nodes = [
                node
                for node in filtered_nodes
                if node.type == "Project"
                or self.graph_auto_linker._source_key_for_node(node) == source_key
            ]
        if node_type:
            filtered_nodes = [node for node in filtered_nodes if node.type == node_type]
        if scope:
            filtered_nodes = [node for node in filtered_nodes if node.scope == scope]

        query = str(search or "").strip().lower()
        exact_id_hits: set[str] = set()
        if query:
            matched: list[Any] = []
            for node in filtered_nodes:
                meta = dict(node.metadata or {})
                meta_bits = " ".join(
                    str(meta.get(key) or "")
                    for key in (
                        "source",
                        "work_item_id",
                        "wiki_page_key",
                        "meeting_id",
                        "repo_id",
                        "pull_request_id",
                    )
                )
                haystack = " ".join(
                    [
                        str(node.id or ""),
                        str(node.label or ""),
                        str(node.text or ""),
                        str(node.type or ""),
                        str(node.scope or ""),
                        meta_bits,
                    ]
                ).lower()
                node_id = str(node.id or "").lower()
                if node_id == query or node_id.startswith(query) or query in haystack:
                    matched.append(node)
                    if node_id == query or (query.startswith("node_") and node_id.startswith(query)):
                        exact_id_hits.add(node.id)
            filtered_nodes = matched
            
        allowed = {node.id for node in filtered_nodes}
        all_edges = [edge for edge in self.repository.list_edges() if edge.source in allowed and edge.target in allowed]

        # Exact ID search: pull in 1-hop neighbors so the hit isn't an isolated dot.
        if exact_id_hits:
            neighbor_ids: set[str] = set()
            for edge in self.repository.list_edges():
                if edge.source in exact_id_hits:
                    neighbor_ids.add(edge.target)
                if edge.target in exact_id_hits:
                    neighbor_ids.add(edge.source)
            if neighbor_ids:
                by_id = {node.id: node for node in all_nodes}
                for node_id in neighbor_ids:
                    if node_id in allowed:
                        continue
                    node = by_id.get(node_id)
                    if node is None:
                        continue
                    filtered_nodes.append(node)
                    allowed.add(node_id)
                all_edges = [
                    edge
                    for edge in self.repository.list_edges()
                    if edge.source in allowed and edge.target in allowed
                ]
        
        total_count = len(filtered_nodes)
        
        # Apply density budget on the backend to keep it fast
        if limit is None:
            if query:
                limit = 400
            elif not node_type and not source_key and not scope and total_count > 200:
                limit = 200
            else:
                limit = 600
                
        allowed_selected = allowed
        if limit and len(filtered_nodes) > limit:
            must_ids = set(exact_id_hits)
            filtered_nodes = self._select_graph_nodes_by_source_quota(
                filtered_nodes,
                all_edges,
                limit,
                must_ids=must_ids,
            )
            allowed_selected = {node.id for node in filtered_nodes}
            all_edges = [edge for edge in all_edges if edge.source in allowed_selected and edge.target in allowed_selected]
            
        graph_nodes = [node.to_dict() for node in filtered_nodes]
        graph_edges = [edge.to_dict() for edge in all_edges]
        
        if task_node:
            graph_nodes.append(task_node)
            for node_id in allowed:
                if node_id in allowed_selected:
                    graph_edges.append({"id": stable_id("edge", "TASK_LINK", task_node["id"], node_id, "project"), "source": task_node["id"], "target": node_id, "type": "TASK_LINK", "scope": "project", "status": "active", "confidence": 0.8, "metadata": {"synthetic": True}})
        if provider_node:
            graph_nodes.append(provider_node)
            for node_id in allowed:
                if node_id in allowed_selected:
                    graph_edges.append({"id": stable_id("edge", "PROVIDER_RELATED", provider_node["id"], node_id, "project"), "source": provider_node["id"], "target": node_id, "type": "PROVIDER_RELATED", "scope": "project", "status": "active", "confidence": 0.62, "metadata": {"synthetic": True}})
                    
        return {
            "nodes": graph_nodes,
            "edges": graph_edges,
            "types": types,
            "scopes": scopes,
            "sources": sources,
            "total_nodes": total_count,
            "filters": {
                "task_id": task_id or "",
                "provider_id": provider_id or "",
                "pinned": pinned,
                "type": node_type or "",
                "source": source_key or "",
                "scope": scope or "",
                "search": query,
            }
        }

    def pin_graph_node(self, node_id: str) -> dict[str, Any]:
        node = self.repository.get_node(node_id)
        if not node:
            raise ValueError("graph node not found")
        node.metadata["pinned"] = not bool(node.metadata.get("pinned") or node.metadata.get("favorite"))
        node.metadata["favorite"] = node.metadata["pinned"]
        node = self.repository.upsert_node(node)
        if node.metadata.get("pinned"):
            return self.memory_lifecycle.promote_long_term(node.id, "pinned")
        return {"node": node.to_dict()}

    def rebuild_graph_links(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        project_id = str(payload.get("project_id") or "architectos")
        return {"project_id": project_id, **self.graph_auto_linker.rebuild(project_id)}

    def run_memory_decay(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        return self.memory_lifecycle.run_decay(str(payload.get("project_id") or "architectos"), bool(payload.get("dry_run") or False))

    def promote_memory_long_term(self, node_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        reason = str((payload or {}).get("reason") or "manual")
        return self.memory_lifecycle.promote_long_term(node_id, reason)

    def create_graph_edge(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = str(payload.get("source") or "").strip()
        target = str(payload.get("target") or "").strip()
        if not source or not target or source == target:
            raise ValueError("source and target graph nodes are required")
        source_node = self.repository.get_node(source)
        target_node = self.repository.get_node(target)
        if not source_node or not target_node:
            raise ValueError("source and target must be durable memory nodes")
        edge_type = str(payload.get("type") or "RELATED_TO")
        scope = str(payload.get("scope") or source_node.scope or target_node.scope or "project")
        edge = self.repository.add_edge(source, target, edge_type, scope, float(payload.get("confidence") or 0.72))
        return {"edge": edge.to_dict()}

    def merge_graph_nodes(self, source_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        target_id = str(payload.get("target_id") or "").strip()
        if not target_id or target_id == source_id:
            raise ValueError("target_id is required")
        source = self.repository.get_node(source_id)
        target = self.repository.get_node(target_id)
        if not source or not target:
            raise ValueError("source and target graph nodes are required")
        merged_text = f"{target.text}\n\nMerged from {source.label}:\n{source.text}"
        target.text = merged_text[:6000]
        target.metadata["merged_from"] = list(dict.fromkeys([*(target.metadata.get("merged_from") or []), source.id]))
        target.metadata["pinned"] = bool(target.metadata.get("pinned") or source.metadata.get("pinned") or source.metadata.get("favorite"))
        for edge in self.repository.list_edges():
            if edge.source == source.id and edge.target != target.id:
                self.repository.add_edge(target.id, edge.target, edge.type, edge.scope, edge.confidence)
            elif edge.target == source.id and edge.source != target.id:
                self.repository.add_edge(edge.source, target.id, edge.type, edge.scope, edge.confidence)
        source.status = "merged"
        source.metadata["merged_into"] = target.id
        source.metadata["merged_at"] = utc_now()
        target = self.repository.upsert_node(target)
        source = self.repository.upsert_node(source)
        return {"target": target.to_dict(), "source": source.to_dict()}

    def apply_graph_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action") or payload.get("command") or "").strip().lower()
        if action == "merge":
            source_id = str(payload.get("source_id") or "").strip()
            if not source_id:
                raise ValueError("source_id is required")
            return {"action": "merge", **self.merge_graph_nodes(source_id, payload)}
        if action == "rename":
            node_id = str(payload.get("node_id") or "").strip()
            label = str(payload.get("label") or "").strip()
            if not node_id or not label:
                raise ValueError("node_id and label are required")
            node = self.repository.get_node(node_id)
            if not node:
                raise ValueError("graph node not found")
            old_label = node.label
            node.label = label[:180]
            node.metadata["renamed_from"] = list(dict.fromkeys([*(node.metadata.get("renamed_from") or []), old_label]))
            node.metadata["renamed_at"] = utc_now()
            return {"action": "rename", "node": self.repository.upsert_node(node).to_dict()}
        if action == "relink":
            edge_id = str(payload.get("edge_id") or "").strip()
            source_id = str(payload.get("source_id") or payload.get("source") or "").strip()
            target_id = str(payload.get("target_id") or payload.get("target") or "").strip()
            if not source_id or not target_id:
                raise ValueError("source_id and target_id are required")
            source = self.repository.get_node(source_id)
            target = self.repository.get_node(target_id)
            if not source or not target:
                raise ValueError("source and target graph nodes are required")
            edge_type = str(payload.get("type") or "RELATED_TO")
            scope = str(payload.get("scope") or source.scope or target.scope or "project")
            if edge_id:
                self.repository.delete_edge(edge_id)
            edge = self.repository.add_edge(source_id, target_id, edge_type, scope, float(payload.get("confidence") or 0.72))
            return {"action": "relink", "edge": edge.to_dict(), "deleted_edge_id": edge_id}
        if action == "delete":
            node_id = str(payload.get("node_id") or "").strip()
            edge_id = str(payload.get("edge_id") or "").strip()
            hard = bool(payload.get("hard"))
            if edge_id:
                return {"action": "delete", "edge_id": edge_id, "deleted": self.repository.delete_edge(edge_id)}
            if not node_id:
                raise ValueError("node_id or edge_id is required")
            node = self.repository.get_node(node_id)
            if not node:
                raise ValueError("graph node not found")
            if hard:
                edge_count = self.repository.delete_node_edges(node_id)
                node.status = "deleted"
                node.metadata["deleted_at"] = utc_now()
                self.repository.upsert_node(node)
                return {"action": "delete", "node": node.to_dict(), "deleted_edges": edge_count}
            node.status = "archived"
            node.metadata["archived_at"] = utc_now()
            node.metadata["archived_reason"] = str(payload.get("reason") or "graph_command_delete")
            return {"action": "delete", "node": self.repository.upsert_node(node).to_dict(), "deleted_edges": 0}
        raise ValueError("action must be merge, rename, relink, or delete")

    def explain_graph_path(self, source_id: str, target_id: str) -> dict[str, Any]:
        if not source_id or not target_id:
            raise ValueError("source and target are required")
        nodes = {node.id: node for node in self.repository.list_nodes() if node.status == "active"}
        if source_id not in nodes or target_id not in nodes:
            raise ValueError("source and target graph nodes are required")
        adjacency: dict[str, list[tuple[str, str]]] = {node_id: [] for node_id in nodes}
        for edge in self.repository.list_edges():
            if edge.source in nodes and edge.target in nodes:
                adjacency.setdefault(edge.source, []).append((edge.target, edge.type))
                adjacency.setdefault(edge.target, []).append((edge.source, edge.type))
        queue: list[tuple[str, list[str], list[str]]] = [(source_id, [source_id], [])]
        seen = {source_id}
        while queue:
            current, path, edge_types = queue.pop(0)
            if current == target_id:
                labels = [nodes[node_id].label for node_id in path]
                explanation = " -> ".join(f"{label}{f' ({edge_types[index - 1]})' if index else ''}" for index, label in enumerate(labels))
                return {"found": True, "path": path, "labels": labels, "edge_types": edge_types, "explanation": explanation}
            for next_id, edge_type in adjacency.get(current, []):
                if next_id in seen:
                    continue
                seen.add(next_id)
                queue.append((next_id, [*path, next_id], [*edge_types, edge_type]))
        return {"found": False, "path": [], "labels": [], "edge_types": [], "explanation": "No connected path found between selected nodes."}

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

    def list_tasks(self, project_id: str | None = None) -> dict[str, Any]:
        return {"tasks": self.repository.list_tasks(project_id)}

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = self.security_policy.redact_text(str(payload.get("title") or "").strip())[0]
        detail = self.security_policy.redact_text(str(payload.get("detail") or ""))[0]
        if not title:
            raise ValueError("task title is required")
        return self.repository.upsert_task({"id": stable_id("task", payload.get("project_id") or "architectos", title, utc_now()), "project_id": str(payload.get("project_id") or "architectos"), "title": title, "status": str(payload.get("status") or "todo"), "priority": str(payload.get("priority") or "medium"), "detail": detail, "linked_memory_ids": list(payload.get("linked_memory_ids") or []), "created_at": utc_now()})

    def update_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        task = self.repository.get_task(task_id)
        if not task:
            raise ValueError("task not found")
        for key in ("title", "status", "priority", "detail"):
            if key in payload:
                value = str(payload[key])
                task[key] = self.security_policy.redact_text(value)[0] if key in {"title", "detail"} else value
        if "linked_memory_ids" in payload:
            task["linked_memory_ids"] = list(payload["linked_memory_ids"] or [])
        return self.repository.upsert_task(task)

    def list_chats(self, project_id: str | None = None, limit: int = 80) -> dict[str, Any]:
        chats = []
        for chat in self.repository.list_chats(project_id)[: max(1, min(int(limit or 80), 500))]:
            chats.append(self._chat_summary(chat, project_id))
        return {"chats": chats, "lightweight": True}

    def get_chat(self, chat_id: str) -> dict[str, Any]:
        chat = self.repository.get_chat(chat_id)
        if not chat:
            raise ValueError("chat not found")
        return self._enrich_chat(chat)

    def _enrich_chat(self, chat: dict[str, Any]) -> dict[str, Any]:
        chat = dict(chat)
        project_id = str(chat.get("project_id") or "architectos")
        chat_id = str(chat.get("id") or "")
        events = self.repository.list_keeper_events(project_id, chat_id, 1)
        if events:
            chat["keeper_status"] = events[0]
        summaries = self.repository.list_chat_context_summaries(chat_id)
        if summaries:
            chat["context_summaries"] = summaries
        return chat

    def _chat_summary(self, chat: dict[str, Any], project_id: str | None = None) -> dict[str, Any]:
        chat = dict(chat)
        messages = list(chat.get("messages") or [])
        last_message = next((message for message in reversed(messages) if str(message.get("text") or "").strip()), {})
        summary = {
            "id": chat.get("id"),
            "project_id": chat.get("project_id") or project_id or "architectos",
            "title": chat.get("title") or "New dialog",
            "favorite": bool(chat.get("favorite")),
            "created_at": chat.get("created_at") or "",
            "updated_at": chat.get("updated_at") or "",
            "message_count": len(messages),
            "last_message_at": last_message.get("created_at") or chat.get("updated_at") or "",
            "preview": str(last_message.get("text") or "")[:240],
        }
        enriched = self._enrich_chat({**chat, "messages": []})
        for key in ("keeper_status", "context_summaries"):
            if key in enriched:
                summary[key] = enriched[key]
        return summary

    def create_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title") or "New dialog").strip()
        return self.repository.upsert_chat({"id": stable_id("chat", payload.get("project_id") or "architectos", title, utc_now()), "project_id": str(payload.get("project_id") or "architectos"), "title": title, "messages": [], "favorite": False, "created_at": utc_now()})

    def post_chat_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        raw_message = str(payload.get("message") or "").strip()
        message_security = self.security_policy.inspect_text(raw_message)
        message = str(message_security["text"])
        if not message:
            raise ValueError("message is required")
        chat = self.repository.get_chat(str(payload.get("chat_id") or "")) if payload.get("chat_id") else None
        if not chat:
            chat = {"id": stable_id("chat", project_id, message[:60], utc_now()), "project_id": project_id, "title": message[:60] or "New dialog", "messages": [], "favorite": False, "created_at": utc_now()}
        user_message = {"role": "user", "text": message, "created_at": utc_now()}
        if message_security["redacted"]:
            user_message["security"] = {"redacted": True, "findings": message_security["findings"]}
        chat["messages"].append(user_message)
        route = self.run_ai({"project_id": project_id, "message": message, "provider_id": payload.get("provider_id") or "auto", "allow_cli": payload.get("allow_cli"), "attachments": payload.get("attachments")})
        response = self.security_policy.redact_text(str(route["text"]))[0]
        display_text, structured = split_rich_response(response)
        chat["messages"].append({
            "role": "assistant",
            "text": display_text or response,
            "raw_text": response,
            "structured": structured,
            "created_at": utc_now(),
            "context": route["context"],
            "provider": route["provider"],
            "usage": route.get("usage") or {},
            "security": route.get("security") or {},
        })
        chat = self.repository.upsert_chat(chat)
        if bool(payload.get("remember", False)):
            self.add_memory({"project_id": project_id, "type": "Lesson", "label": f"Dialog note: {message[:48]}", "scope": "project", "text": f"User asked: {message}. Local response: {response[:300]}", "source": "chat"})
        else:
            self._capture_chat_turn_memory_candidate(
                project_id,
                chat["id"],
                message,
                display_text or response,
                source="chat_keeper",
                metadata={"provider": route.get("provider") or {}, "mode": "chat"},
            )
        self._after_chat_turn(project_id, chat["id"], message, display_text or response, source="chat_keeper")
        return {"chat": chat, "response": display_text or response, "structured": structured, "context": route["context"], "usage": route.get("usage") or {}, "security": route.get("security") or {}}

    def run_ai(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id, message, context, request = self._provider_request(payload)
        self._sync_router_settings()
        provider_id = self._resolve_ai_provider_id(payload)
        include_writes = bool(payload.get("allow_cli") or payload.get("cli_approved") or payload.get("approved") or payload.get("allow_fs_writes"))
        memory_only = str(payload.get("ask_mode") or "").strip().lower() == "memory"
        tools_on = self._tools_enabled_for_payload(payload, include_writes=include_writes)
        if tools_on:
            result = {}
            tool_trace = []
            for event in self._run_ai_with_tools(
                project_id, message, context, request, provider_id,
                include_writes=include_writes, memory_only=memory_only,
            ):
                if event.get("type") == "result":
                    result = event["result"]
                    tool_trace = event["tool_trace"]
        else:
            result = self.provider_router.route(self.repository.list_providers(), request, provider_id)
            tool_trace = []
        response = self._route_response(project_id, message, context["context"], result)
        run = self._start_provider_run(request.run_id, project_id, message, result)
        self._finish_provider_run(run["id"], str(result.get("status") or "unknown"), result)
        response["run_id"] = run["id"]
        response["memory_hit_ids"] = self._hit_ids_from_context(context)
        response["retrieval"] = context.get("retrieval") or {}
        if tool_trace:
            response["tool_trace"] = tool_trace
            response["tool_trace_label"] = compact_tool_trace_label(tool_trace)
        return response

    def stream_ai(self, payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
        project_id = str(payload.get("project_id") or "architectos")
        raw_message = str(payload.get("message") or payload.get("query") or "").strip()
        run_id = str(payload.get("run_id") or stable_id("run", project_id, raw_message[:80], utc_now()))
        # Emit start immediately so Ask can render a live activity trace while context builds.
        yield {
            "type": "start",
            "run_id": run_id,
            "project_id": project_id,
            "message": raw_message,
            "provider": {
                "id": payload.get("provider_id") or "auto",
                "requested_id": payload.get("provider_id") or "auto",
                "selected": {},
                "status": "preparing",
            },
        }
        yield {
            "type": "progress",
            "phase": "context",
            "status": "Searching project memory…",
            "step_id": "prep:context",
            "kind": "status",
        }
        prepared = dict(payload)
        prepared["run_id"] = run_id
        project_id, message, context, request = self._provider_request(prepared)
        hit_ids = self._hit_ids_from_context(context)
        hit_count = len(hit_ids)
        yield {
            "type": "progress",
            "phase": "context_done",
            "status": f"Loaded {hit_count} memory hit{'s' if hit_count != 1 else ''}" if hit_count else "No memory hits",
            "step_id": "prep:context",
            "ok": True,
            "kind": "status",
            "memory_hit_ids": hit_ids,
        }
        self._sync_router_settings()
        provider_id = self._resolve_ai_provider_id(prepared)
        planned_provider = self._lookup_provider(provider_id)
        if planned_provider:
            provider_payload = self._provider_stream_payload(planned_provider, provider_id, status="streaming")
            yield {
                "type": "progress",
                "phase": "provider",
                "status": f"Using {provider_payload['selected']['label']}",
                "step_id": "prep:provider",
                "kind": "status",
                "ok": True,
                "provider": provider_payload,
            }
            yield {
                "type": "start",
                "run_id": run_id,
                "project_id": project_id,
                "message": message,
                "provider": provider_payload,
            }
        elif str(provider_id or "auto") == "auto":
            auto_payload = self._provider_stream_payload(
                {"id": "auto", "label": "Auto", "model": ""},
                "auto",
                status="routing",
            )
            yield {
                "type": "progress",
                "phase": "provider",
                "status": "Routing to best available model…",
                "step_id": "prep:provider",
                "kind": "status",
                "ok": True,
                "provider": auto_payload,
            }
            yield {
                "type": "start",
                "run_id": run_id,
                "project_id": project_id,
                "message": message,
                "provider": auto_payload,
            }
        include_writes = bool(prepared.get("allow_cli") or prepared.get("cli_approved") or prepared.get("approved") or prepared.get("allow_fs_writes"))
        memory_only = str(prepared.get("ask_mode") or "").strip().lower() == "memory"
        tools_on = self._tools_enabled_for_payload(prepared, include_writes=include_writes)
        # When tools are enabled, resolve TOOL_CALL rounds non-stream first, then stream only the final prose pass.
        if tools_on:
            result: dict[str, Any] = {}
            tool_trace: list[dict[str, Any]] = []
            for event in self._run_ai_with_tools(
                project_id, message, context, request, provider_id,
                include_writes=include_writes, memory_only=memory_only,
            ):
                if event.get("type") == "progress":
                    yield event
                elif event.get("type") == "result":
                    result = event["result"]
                    tool_trace = event["tool_trace"]

            selected = dict(result.get("selected_provider") or {})
            self._start_provider_run(run_id, project_id, message, {"provider_id": result.get("provider_id") or provider_id, "selected_provider": selected, "status": "running", "raw": {"approved": request.approved}})

            final_text = str(result.get("text") or "")
            if final_text:
                yield {"type": "delta", "text": final_text}
            response = self._route_response(project_id, message, context["context"], result)
            response["run_id"] = run_id
            response["memory_hit_ids"] = hit_ids
            response["retrieval"] = context.get("retrieval") or {}
            if tool_trace:
                response["tool_trace"] = tool_trace
                response["tool_trace_label"] = compact_tool_trace_label(tool_trace)
            self._finish_provider_run(run_id, str(result.get("status") or "unknown"), result)
            yield {"type": "done", "result": response}
            return

        audit_started = False
        for event in self.provider_router.stream(self.repository.list_providers(), request, provider_id):
            if event.get("type") == "start":
                selected = dict(event.get("selected_provider") or {})
                self._start_provider_run(run_id, project_id, message, {"provider_id": event.get("provider_id"), "selected_provider": selected, "status": "running", "raw": {"approved": request.approved}})
                audit_started = True
                yield {
                    "type": "progress",
                    "phase": "thinking",
                    "status": f"Calling {selected.get('label') or event.get('provider_id') or 'model'}…",
                    "step_id": "prep:model",
                    "kind": "status",
                    "provider": self._provider_stream_payload(selected, str(event.get("provider_id") or provider_id or "")),
                }
                yield {
                    "type": "start",
                    "run_id": run_id,
                    "project_id": project_id,
                    "message": message,
                    "context": context["context"],
                    "provider": self._provider_stream_payload(
                        selected,
                        str(event.get("provider_id") or provider_id or ""),
                        status="streaming",
                    ),
                }
            elif event.get("type") == "done":
                result = dict(event.get("result") or {})
                response = self._route_response(project_id, message, context["context"], result)
                response["run_id"] = run_id
                response["memory_hit_ids"] = hit_ids
                response["retrieval"] = context.get("retrieval") or {}
                if audit_started:
                    self._finish_provider_run(run_id, str(result.get("status") or "unknown"), result)
                yield {
                    "type": "progress",
                    "phase": "thinking_done",
                    "status": "Answer ready",
                    "step_id": "prep:model",
                    "ok": True,
                    "kind": "status",
                }
                yield {"type": "done", "result": response}
            else:
                yield event

    def stream_chat_message(self, payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
        project_id = str(payload.get("project_id") or "architectos")
        message = str(payload.get("message") or "").strip()
        if not message:
            raise ValueError("message is required")
        chat = self.repository.get_chat(str(payload.get("chat_id") or "")) if payload.get("chat_id") else None
        if not chat:
            chat = {"id": stable_id("chat", project_id, message[:60], utc_now()), "project_id": project_id, "title": message[:60] or "New dialog", "messages": [], "favorite": False, "created_at": utc_now()}
        chat["messages"].append({"role": "user", "text": message, "created_at": utc_now()})
        response_chunks: list[str] = []
        final_result: dict[str, Any] | None = None
        stream_payload = {
            "project_id": project_id,
            "chat_id": chat["id"],
            "message": message,
            "provider_id": payload.get("provider_id") or "auto",
            "limit": payload.get("limit") or 8,
            "allow_cli": payload.get("allow_cli"),
            "allow_fs_writes": payload.get("allow_fs_writes"),
            "attachments": payload.get("attachments"),
            "ask_mode": payload.get("ask_mode"),
            "enable_tools": payload.get("enable_tools"),
        }
        for event in self.stream_ai(stream_payload):
            if event.get("type") == "delta":
                response_chunks.append(str(event.get("text") or ""))
                yield event
            elif event.get("type") == "start":
                yield event
            elif event.get("type") == "progress":
                yield event
            elif event.get("type") == "done":
                final_result = dict(event.get("result") or {})
        if final_result is None:
            final_result = {"text": "".join(response_chunks), "context": "", "provider": {"id": "unknown", "status": "error"}}
        response = str(final_result.get("text") or "".join(response_chunks))
        display_text, structured = split_rich_response(response)
        chat["messages"].append({
            "role": "assistant",
            "text": display_text or response,
            "raw_text": response,
            "structured": structured,
            "created_at": utc_now(),
            "context": final_result.get("context") or "",
            "provider": final_result.get("provider") or {},
            "usage": final_result.get("usage") or {},
            "tool_trace": final_result.get("tool_trace") or [],
            "tool_trace_label": final_result.get("tool_trace_label") or "",
            "memory_hit_ids": list(final_result.get("memory_hit_ids") or []),
            "run_id": final_result.get("run_id") or "",
            "query": message,
        })
        chat = self.repository.upsert_chat(chat)
        if bool(payload.get("remember", False)):
            self.add_memory({"project_id": project_id, "type": "Lesson", "label": f"Dialog note: {message[:48]}", "scope": "project", "text": f"User asked: {message}. Local response: {response[:300]}", "source": "chat"})
        else:
            self._capture_chat_turn_memory_candidate(
                project_id,
                chat["id"],
                message,
                display_text or response,
                source="chat_keeper",
                metadata={"provider": final_result.get("provider") or {}, "mode": str(payload.get("ask_mode") or "chat")},
            )
        self._after_chat_turn(project_id, chat["id"], message, display_text or response, source="chat_keeper")
        yield {
            "type": "done",
            "run_id": final_result.get("run_id") or "",
            "chat": chat,
            "response": display_text or response,
            "raw_response": response,
            "structured": structured,
            "context": final_result.get("context") or "",
            "provider": final_result.get("provider") or {},
            "usage": final_result.get("usage") or {},
            "raw": final_result.get("raw"),
            "tool_trace": final_result.get("tool_trace") or [],
            "tool_trace_label": final_result.get("tool_trace_label") or "",
            "memory_hit_ids": list(final_result.get("memory_hit_ids") or []),
        }

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        if not run_id:
            raise ValueError("run id is required")
        self.cancelled_runs.add(run_id)
        existing = next((run for run in self.repository.list_provider_runs(limit=100) if run["id"] == run_id), None)
        if existing:
            existing["status"] = "cancel_requested"
            existing["cancel_requested_at"] = utc_now()
            self.repository.upsert_provider_run(existing)
        return {"run_id": run_id, "cancel_requested": True}

    def provider_runs(self, project_id: str | None = None, limit: int = 25) -> dict[str, Any]:
        return {"runs": self.repository.list_provider_runs(project_id, limit)}

    def _summarize_older_messages(self, messages: list[dict[str, Any]]) -> str:
        log_lines = []
        for msg in messages:
            role = "User" if msg.get("role") == "user" else "Assistant"
            text = str(msg.get("text") or "").strip()
            log_lines.append(f"{role}: {text[:1000]}")
        log_text = "\n".join(log_lines)
        
        prompt = (
            "Write a very brief, single-sentence or single-paragraph summary of the key topics, decisions, "
            "and user requests discussed in this conversation segment. Be concise and dense:\n\n"
            f"{log_text}"
        )
        try:
            res = self.run_ai({
                "message": prompt,
                "provider_id": "auto",
                "role": "review",
                "limit": 1,
            })
            summary = str(res.get("text") or "").strip()
            if summary:
                return f"[Summary of older conversation: {summary}]"
        except Exception:
            pass
        return "[Older conversation history omitted]"

    def _format_and_compress_chat_history(self, messages: list[dict[str, Any]], max_chars: int = 4000) -> str:
        if not messages:
            return ""
        formatted = []
        for msg in messages:
            role = "User" if msg.get("role") == "user" else "Assistant"
            text = str(msg.get("text") or "").strip()
            formatted.append((role, text))
            
        total_len = sum(len(text) for _, text in formatted)
        if total_len <= max_chars:
            lines = [f"{role}: {text}" for role, text in formatted]
            return "Conversation history:\n" + "\n".join(lines)
            
        keep_full_count = 4
        older_messages = formatted[:-keep_full_count]
        recent_messages = formatted[-keep_full_count:]
        
        compressed_lines = []
        if older_messages:
            older_struct = [{"role": "user" if r == "User" else "assistant", "text": t} for r, t in older_messages]
            summary = self._summarize_older_messages(older_struct)
            compressed_lines.append(summary)
                
        for role, text in recent_messages:
            compressed_lines.append(f"{role}: {text}")
            
        return "Conversation history:\n" + "\n".join(compressed_lines)

    def _provider_request(self, payload: dict[str, Any]) -> tuple[str, str, dict[str, Any], ProviderRequest]:
        project_id = str(payload.get("project_id") or "architectos")
        raw_message = str(payload.get("message") or payload.get("query") or "").strip()
        inspected_message = self.security_policy.inspect_text(raw_message)
        message = str(inspected_message["text"])
        if not message:
            raise ValueError("message is required")
        context = self.context(message, project_id=project_id, limit=int(payload.get("limit") or 8))
        context_text, context_redacted = self.security_policy.redact_text(context["context"])
        context = dict(context)
        context["context"] = context_text

        chat_id = str(payload.get("chat_id") or "").strip()
        # Prevent infinite recursion when calling run_ai inside _summarize_older_messages
        if payload.get("role") == "review":
            chat_id = ""

        history_text = ""
        if chat_id:
            chat = self.repository.get_chat(chat_id)
            if chat and chat.get("messages"):
                messages = list(chat.get("messages") or [])
                if messages and messages[-1]["role"] == "user" and messages[-1]["text"].strip() == message:
                    past_messages = messages[:-1]
                else:
                    past_messages = messages
                if past_messages:
                    history_text = self._format_and_compress_chat_history(past_messages)
        if history_text:
            context["context"] = f"{history_text}\n\n{context['context']}".strip()
        attachments = [str(item) for item in (payload.get("attachments") or []) if str(item).strip()]
        images: list[dict[str, Any]] = []
        if attachments:
            index = {str(item.get("id")): item for item in self.files.list_files(project_id)}
            image_ids = [fid for fid in attachments if self.files.is_image(index.get(fid, {}))]
            text_ids = [fid for fid in attachments if fid not in image_ids]
            files_text = self.files.context_for(project_id, text_ids)
            if files_text:
                clean_files, files_redacted = self.security_policy.redact_text(files_text)
                context["context"] = f"{context['context']}\n\nAttached Files:\n{clean_files}".strip()
                context_redacted = context_redacted or files_redacted
            images = self.files.image_payload(project_id, image_ids)
            if images:
                names = ", ".join(str(img.get("name") or "image") for img in images)
                context["context"] = f"{context['context']}\n\nAttached Images (sent to vision-capable models): {names}".strip()
        include_writes = bool(payload.get("allow_cli") or payload.get("cli_approved") or payload.get("approved") or payload.get("allow_fs_writes"))
        memory_only = str(payload.get("ask_mode") or "").strip().lower() == "memory"
        if self._tools_enabled_for_payload(payload, include_writes=include_writes):
            tool_section = self.tool_gateway.tool_prompt_section(include_writes=include_writes, memory_only=memory_only)
            if tool_section:
                context["context"] = f"{context['context']}\n\n{tool_section}".strip()
        elif str(payload.get("ask_mode") or "").strip().lower() != "review":
            # Even without tools, ask the model for Markdown / actions / mermaid.
            context["context"] = f"{context['context']}\n\n{RESPONSE_FORMAT_POLICY}".strip()
        if inspected_message["redacted"] or context_redacted:
            context["security"] = {"redacted": True, "message_findings": inspected_message["findings"], "context_redacted": context_redacted}
        run_id = str(payload.get("run_id") or stable_id("run", project_id, message[:80], utc_now()))
        approved = bool(payload.get("allow_cli") or payload.get("cli_approved") or payload.get("approved"))
        role = str(payload.get("role") or "") or classify_role(message)
        request = ProviderRequest(message=message, context=context["context"], project_id=project_id, run_id=run_id, approved=approved, role=role, images=images, cancel_requested=lambda: run_id in self.cancelled_runs)
        return project_id, message, context, request

    def _tools_enabled_for_payload(self, payload: dict[str, Any], *, include_writes: bool = False) -> bool:
        if payload.get("enable_tools") is False:
            return False
        ask_mode = str(payload.get("ask_mode") or "").strip().lower()
        # Pure Memory mode: only memory_get (no MCP boards/fs).
        if ask_mode == "memory":
            return self.tool_gateway.tools_available(include_writes=False, memory_only=True)
        if payload.get("enable_tools") is True or ask_mode in {"", "quick", "multi", "memory-mcp", "memory_mcp"}:
            return self.tool_gateway.tools_available(include_writes=include_writes)
        return self.tool_gateway.tools_available(include_writes=include_writes)

    def _resolve_ai_provider_id(self, payload: dict[str, Any]) -> str:
        provider_id = str(payload.get("provider_id") or "auto").strip() or "auto"
        ask_mode = str(payload.get("ask_mode") or "").strip().lower()
        include_writes = bool(payload.get("allow_cli") or payload.get("cli_approved") or payload.get("approved") or payload.get("allow_fs_writes"))
        tools_on = self._tools_enabled_for_payload(payload, include_writes=include_writes)
        if ask_mode == "memory":
            # memory_get needs a model that can emit tool_calls; use auto when tools are on.
            if tools_on:
                return "auto" if provider_id in {"", "local-memory", "auto"} else provider_id
            return "local-memory"
        if ask_mode in {"memory-mcp", "memory_mcp"}:
            # local-memory cannot emit TOOL_CALL JSON — use auto when tools are live.
            if tools_on:
                return "auto" if provider_id in {"", "local-memory", "auto"} else provider_id
            return "local-memory"
        if tools_on and provider_id == "local-memory":
            return "auto"
        return provider_id

    def _run_ai_with_tools(
        self,
        project_id: str,
        message: str,
        context: dict[str, Any],
        request: ProviderRequest,
        provider_id: str,
        *,
        include_writes: bool = False,
        memory_only: bool = False,
    ) -> Iterator[dict[str, Any]]:
        tool_trace: list[dict[str, Any]] = []
        run_step_seq = 0
        working = ProviderRequest(
            message=request.message,
            context=request.context,
            project_id=request.project_id,
            run_id=request.run_id,
            approved=request.approved,
            role=request.role,
            images=list(request.images or []),
            cancel_requested=request.cancel_requested,
        )
        result: dict[str, Any] = {}
        for round_idx in range(MAX_TOOL_ROUNDS + 1):
            if working.cancel_requested and working.cancel_requested():
                break
            force_final = round_idx >= MAX_TOOL_ROUNDS
            think_id = f"think:{round_idx}"
            planned = self._lookup_provider(provider_id)
            planned_label = ""
            if planned:
                planned_label = self._provider_stream_payload(planned, provider_id)["selected"]["label"]
            thinking_status = "Composing final answer…" if force_final else ("Thinking…" if round_idx == 0 else "Continuing with tool results…")
            if planned_label:
                thinking_status = f"{thinking_status} · {planned_label}"
            yield {
                "type": "progress",
                "phase": "thinking",
                "status": thinking_status,
                "step_id": think_id,
                "kind": "status",
                "round": round_idx + 1,
                "provider": self._provider_stream_payload(planned, provider_id) if planned else None,
            }
            if force_final:
                working = ProviderRequest(
                    message=(
                        f"{message}\n\n"
                        "Tool budget exhausted. Answer the user now using memory and TOOL_RESULT blocks already provided. "
                        "Do not emit tool_calls."
                    ),
                    context=working.context,
                    project_id=working.project_id,
                    run_id=working.run_id,
                    approved=working.approved,
                    role=working.role,
                    images=list(working.images or []),
                    cancel_requested=working.cancel_requested,
                )
            result = self.provider_router.route(self.repository.list_providers(), working, provider_id)
            selected_provider = dict(result.get("selected_provider") or {})
            provider_payload = self._provider_stream_payload(
                selected_provider,
                str(result.get("provider_id") or provider_id or ""),
                status="streaming",
            )
            yield {
                "type": "progress",
                "phase": "provider",
                "status": f"Using {provider_payload['selected']['label']}",
                "step_id": "prep:provider",
                "kind": "status",
                "ok": True,
                "provider": provider_payload,
                "round": round_idx + 1,
            }
            text = str(result.get("text") or "")
            calls = [] if force_final else parse_tool_calls(text)
            if not calls:
                cleaned = strip_tool_call_json(text)
                if cleaned != text.strip():
                    result = dict(result)
                    result["text"] = cleaned or text
                yield {
                    "type": "progress",
                    "phase": "thinking_done",
                    "status": f"Composing answer · {provider_payload['selected']['label']}",
                    "step_id": think_id,
                    "ok": True,
                    "kind": "status",
                    "round": round_idx + 1,
                    "provider": provider_payload,
                }
                break
            yield {
                "type": "progress",
                "phase": "thinking_done",
                "status": f"Planned {len(calls[:5])} tool call{'s' if len(calls[:5]) != 1 else ''} · {provider_payload['selected']['label']}",
                "step_id": think_id,
                "ok": True,
                "kind": "status",
                "round": round_idx + 1,
                "provider": provider_payload,
            }
            round_trace: list[dict[str, Any]] = []
            for call in calls[:5]:
                tool_name = str(call.get("name") or "")
                call_args = dict(call.get("arguments") or {})
                step_id = f"{run_step_seq}"
                run_step_seq += 1
                yield {
                    "type": "progress",
                    "phase": "tool_start",
                    "status": f"Running tool: {tool_name}...",
                    "tool_name": tool_name,
                    "arguments": call_args,
                    "step_id": step_id,
                    "round": round_idx + 1,
                    "tool_trace": tool_trace + round_trace + [{
                        "step_id": step_id,
                        "name": tool_name,
                        "arguments": call_args,
                        "ok": True,
                        "pending": True,
                        "summary": "Executing...",
                        "error": "",
                        "round": round_idx + 1,
                    }]
                }
                executed = self.tool_gateway.execute(
                    tool_name,
                    call_args,
                    include_writes=include_writes,
                    memory_only=memory_only,
                    project_id=project_id,
                )
                entry = {
                    "step_id": step_id,
                    "name": executed.get("name") or tool_name,
                    "arguments": call_args,
                    "ok": bool(executed.get("ok")),
                    "pending": False,
                    "summary": executed.get("summary") or "",
                    "error": executed.get("error") or "",
                    "round": round_idx + 1,
                }
                round_trace.append(entry)
                tool_trace.append(entry)
                yield {
                    "type": "progress",
                    "phase": "tool_finish",
                    "status": f"Finished tool: {tool_name}",
                    "tool_name": tool_name,
                    "arguments": call_args,
                    "step_id": step_id,
                    "ok": entry["ok"],
                    "round": round_idx + 1,
                    "tool_trace": list(tool_trace)
                }
            working = ProviderRequest(
                message=(
                    f"{message}\n\n"
                    f"{format_tool_results_for_prompt(round_trace)}\n\n"
                    "Continue. If you still need tools, emit tool_calls JSON; otherwise answer the user."
                ),
                context=working.context,
                project_id=working.project_id,
                run_id=working.run_id,
                approved=working.approved,
                role=working.role,
                images=list(working.images or []),
                cancel_requested=working.cancel_requested,
            )
        clean = dict(result)
        clean["text"] = strip_tool_call_json(str(clean.get("text") or "")) or str(clean.get("text") or "")
        yield {"type": "result", "result": clean, "tool_trace": tool_trace}

    @staticmethod
    def _hit_ids_from_context(context: dict[str, Any] | None) -> list[str]:
        ids: list[str] = []
        for hit in (context or {}).get("hits") or []:
            if not isinstance(hit, dict):
                continue
            node = hit.get("node") if isinstance(hit.get("node"), dict) else hit
            node_id = str((node or {}).get("id") or "").strip()
            if node_id and node_id not in ids:
                ids.append(node_id)
        return ids

    @staticmethod
    def _provider_stream_payload(provider: dict[str, Any] | None, provider_id: str = "", *, status: str = "streaming") -> dict[str, Any]:
        selected = dict(provider or {})
        pid = str(selected.get("id") or provider_id or "auto")
        label = str(selected.get("label") or pid)
        model = str(selected.get("model") or "").strip()
        display = label
        if model and model.lower() not in label.lower():
            display = f"{label} · {model}"
        return {
            "id": pid,
            "label": label,
            "model": model,
            "status": status,
            "selected": {
                "id": pid,
                "label": display,
                "model": model,
            },
        }

    def _lookup_provider(self, provider_id: str) -> dict[str, Any] | None:
        needle = str(provider_id or "").strip()
        if not needle or needle == "auto":
            return None
        return next((item for item in self.repository.list_providers() if str(item.get("id") or "") == needle), None)

    def record_retrieval_feedback(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(payload or {})
        project_id = str(payload.get("project_id") or "architectos")
        chat_id = str(payload.get("chat_id") or "").strip()
        message_index = payload.get("message_index")
        hit_ids = [str(item).strip() for item in (payload.get("hit_ids") or []) if str(item).strip()]
        query = str(payload.get("query") or "").strip()
        run_id = str(payload.get("run_id") or "").strip()

        if chat_id and message_index is not None:
            chat = self.repository.get_chat(chat_id)
            if chat:
                messages = list(chat.get("messages") or [])
                try:
                    idx = int(message_index)
                except (TypeError, ValueError):
                    idx = -1
                if 0 <= idx < len(messages):
                    msg = dict(messages[idx])
                    if not hit_ids:
                        hit_ids = [str(item).strip() for item in (msg.get("memory_hit_ids") or []) if str(item).strip()]
                    if not query:
                        # Prefer previous user turn as the retrieval query.
                        for prior in reversed(messages[:idx]):
                            if prior.get("role") == "user":
                                query = str(prior.get("text") or "").strip()
                                break
                    if not run_id:
                        run_id = str(msg.get("run_id") or "").strip()
                    msg["retrieval_rating"] = int(payload.get("rating") or 0)
                    messages[idx] = msg
                    chat["messages"] = messages
                    self.repository.upsert_chat(chat)

        record = self.repository.add_retrieval_feedback({
            "project_id": project_id,
            "chat_id": chat_id,
            "run_id": run_id,
            "message_index": message_index,
            "query": query,
            "rating": payload.get("rating"),
            "hit_ids": hit_ids,
            "note": payload.get("note") or "",
        })
        return {"feedback": record, "hit_count": len(hit_ids)}

    def list_retrieval_feedback(self, project_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        items = self.repository.list_retrieval_feedback(project_id, limit)
        return {"feedback": items, "count": len(items)}

    @staticmethod
    def _boards_ids_from_memory_hits(hits: list[dict[str, Any]]) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        for hit in hits or []:
            node = hit.get("node") if isinstance(hit, dict) else None
            if not isinstance(node, dict):
                continue
            meta = dict(node.get("metadata") or {})
            candidates = [
                meta.get("work_item_id"),
                meta.get("workItemId"),
                meta.get("ado_work_item_id"),
            ]
            source = str(meta.get("source") or node.get("source") or "").lower()
            for value in candidates:
                text = str(value or "").strip()
                if not text:
                    continue
                if text not in seen:
                    seen.add(text)
                    ids.append(text)
            if "azure" in source or "boards" in source:
                label = str(node.get("label") or "")
                match = re.search(r"#?(\d{3,})", label)
                if match and match.group(1) not in seen:
                    seen.add(match.group(1))
                    ids.append(match.group(1))
        return ids[:12]

    def _filesystem_mcp_root(self) -> Path:
        workspace = self.repository.get_setting("workspace") or {}
        project_id = str(workspace.get("current_project_id") or "").strip()
        if not project_id:
            projects = self.repository.list_projects()
            for project in projects:
                if project.id != SYSTEM_PROJECT_ID and project.root_path:
                    project_id = project.id
                    break
        if project_id:
            try:
                return self._project_root(project_id)
            except Exception:
                pass
        return self.project_root

    def _tool_fs_read(self, args: dict[str, Any]) -> dict[str, Any]:
        project_id = str(args.get("project_id") or "architectos")
        path = str(args.get("path") or "").strip()
        if not path:
            raise ValueError("fs_read requires path")
        result = self.project_file(project_id, path)
        text = str(result.get("text") or "")
        if len(text) > 8000:
            text = text[:8000] + "\n…[truncated]"
        return {"path": result.get("path"), "text": text, "size": result.get("size"), "readable": result.get("readable")}

    def _tool_memory_get(self, args: dict[str, Any]) -> dict[str, Any]:
        node_id = str(args.get("id") or args.get("node_id") or "").strip()
        if not node_id:
            raise ValueError("memory_get requires id")
        node = self.repository.get_node(node_id)
        if not node or node.status not in {"active", "archived"}:
            raise ValueError(f"memory node not found: {node_id}")
        include_neighbors = bool(args.get("include_neighbors", True))
        payload = {
            "id": node.id,
            "type": node.type,
            "label": node.label,
            "scope": node.scope,
            "project_id": node.project_id,
            "status": node.status,
            "confidence": node.confidence,
            "text": node.text,
            "metadata": dict(node.metadata or {}),
            "evidence": list(node.evidence or []),
        }
        if include_neighbors:
            neighbors: list[dict[str, Any]] = []
            for edge in self.repository.list_edges():
                other = ""
                if edge.source == node.id:
                    other = edge.target
                elif edge.target == node.id:
                    other = edge.source
                if not other:
                    continue
                related = self.repository.get_node(other)
                if not related or related.status != "active":
                    continue
                neighbors.append({
                    "edge_type": edge.type,
                    "id": related.id,
                    "type": related.type,
                    "label": related.label,
                    "chunk_role": dict(related.metadata or {}).get("chunk_role"),
                    "text_preview": (related.text or "")[:400],
                })
                if len(neighbors) >= 12:
                    break
            payload["neighbors"] = neighbors
        try:
            self.memory_lifecycle.refresh_nodes([node.id], "memory_get")
        except Exception:
            pass
        return payload

    def _tool_fs_list(self, args: dict[str, Any]) -> dict[str, Any]:
        project_id = str(args.get("project_id") or "architectos")
        limit = max(1, min(int(args.get("limit") or 80), 200))
        listed = self.project_files(project_id, limit=limit)
        relative = str(args.get("path") or "").strip().strip("./")
        files = list(listed.get("files") or [])
        if relative:
            prefix = relative.replace("\\", "/").rstrip("/") + "/"
            files = [
                item for item in files
                if str(item.get("path") or "").replace("\\", "/") == relative
                or str(item.get("path") or "").replace("\\", "/").startswith(prefix)
            ]
        return {"project_id": project_id, "root": listed.get("root"), "path": relative or ".", "files": files[:limit], "count": min(len(files), limit)}

    def _tool_fs_search(self, args: dict[str, Any]) -> dict[str, Any]:
        project_id = str(args.get("project_id") or "architectos")
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("fs_search requires query")
        mode = str(args.get("mode") or "name")
        limit = max(1, min(int(args.get("limit") or 40), 100))
        return self.project_search(project_id=project_id, query=query, mode=mode, limit=limit)

    def _tool_fs_write(self, args: dict[str, Any]) -> dict[str, Any]:
        project_id = str(args.get("project_id") or "architectos")
        path = str(args.get("path") or "").strip()
        if not path:
            raise ValueError("fs_write requires path")
        text = args.get("text")
        if text is None:
            text = args.get("content")
        return self.save_project_file({"project_id": project_id, "path": path, "text": str(text if text is not None else "")})

    def _start_provider_run(self, run_id: str, project_id: str, message: str, result: dict[str, Any]) -> dict[str, Any]:
        provider = dict(result.get("selected_provider") or {})
        raw = dict(result.get("raw") or {}) if isinstance(result.get("raw"), dict) else {}
        clean_message = self.security_policy.redact_text(message)[0]
        command, _command_redacted = self.security_policy.redact_payload(raw.get("command") or provider.get("command") or [])
        workdir, _workdir_redacted = self.security_policy.redact_text(str(raw.get("workdir") or provider.get("workdir") or self.project_root))
        return self.repository.upsert_provider_run({
            "id": run_id,
            "project_id": project_id,
            "provider_id": str(result.get("provider_id") or provider.get("id") or ""),
            "provider_label": str(provider.get("label") or provider.get("id") or ""),
            "status": str(result.get("status") or "running"),
            "message_preview": clean_message[:180],
            "command": command,
            "workdir": workdir,
            "approval_required": bool(provider.get("approval_required", provider.get("provider_type") == "cli")),
            "approved": bool(raw.get("approved", False)),
            "started_at": utc_now(),
        })

    def _finish_provider_run(self, run_id: str, status: str, result: dict[str, Any]) -> dict[str, Any] | None:
        runs = self.repository.list_provider_runs(limit=100)
        run = next((item for item in runs if item["id"] == run_id), None)
        if not run:
            return None
        raw = dict(result.get("raw") or {}) if isinstance(result.get("raw"), dict) else {}
        selected = result.get("selected_provider") if isinstance(result.get("selected_provider"), dict) else {}
        usage = usage_from_result(result) or (result.get("usage") if isinstance(result.get("usage"), dict) else None)
        run["status"] = status
        run["finished_at"] = utc_now()
        run["returncode"] = raw.get("returncode")
        run["stderr_preview"] = self.security_policy.redact_text(str(raw.get("stderr") or ""))[0][:500]
        run["model"] = str(selected.get("model") or (usage or {}).get("model") or run.get("model") or "")
        if usage:
            run["usage"] = usage
            if usage.get("cost_usd") is not None:
                run["cost_usd"] = usage.get("cost_usd")
        run["selected_provider"] = selected or run.get("selected_provider") or {}
        run["routing"] = result.get("routing") or run.get("routing") or {}
        return self.repository.upsert_provider_run(run)

    def _route_response(self, project_id: str, message: str, context: str, result: dict[str, Any]) -> dict[str, Any]:
        clean_message, message_redacted = self.security_policy.redact_text(message)
        clean_context, context_redacted = self.security_policy.redact_text(context)
        clean_text, text_redacted = self.security_policy.redact_text(str(result.get("text") or ""))
        clean_raw, raw_redacted = self.security_policy.redact_payload(result.get("raw"))
        clean_selected, provider_redacted = self.security_policy.redact_payload(result.get("selected_provider"))
        usage = usage_from_result(result)
        return {
            "project_id": project_id,
            "message": clean_message,
            "context": clean_context,
            "provider": {
                "id": result.get("provider_id"),
                "requested_id": result.get("requested_provider_id"),
                "selected": clean_selected,
                "status": result.get("status"),
                "routing": result.get("routing") or {},
            },
            "routing": result.get("routing") or {},
            "usage": usage or {},
            "text": clean_text,
            "raw": None if clean_raw is None else clean_raw,
            "security": {
                "redacted": bool(message_redacted or context_redacted or text_redacted or raw_redacted or provider_redacted),
                "message_redacted": message_redacted,
                "context_redacted": context_redacted,
                "result_redacted": text_redacted,
                "raw_redacted": raw_redacted,
            },
        }

    def workflows(self) -> dict[str, Any]:
        return {"workflows": list(self._workflow_definitions().values())}

    def run_workflow(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        definitions = self._workflow_definitions()
        workflow = definitions.get(workflow_id)
        if not workflow:
            raise ValueError("workflow not found")
        project_id = str(payload.get("project_id") or "architectos")
        query = self.security_policy.redact_text(str(payload.get("query") or workflow["name"]).strip())[0]
        if not query:
            raise ValueError("workflow query is required")
        provider_id = str(payload.get("provider_id") or "auto")
        scan_limit = max(0, int(payload.get("scan_limit") or workflow.get("scan_limit") or 8))
        steps: list[dict[str, Any]] = []
        artifacts: dict[str, Any] = {"query": query, "workflow": workflow}

        scan_result = {"count": 0, "imported": [], "root": str(self.project_root)}
        if scan_limit:
            try:
                scan_result = self.scan_project({"project_id": project_id, "limit": scan_limit})
                steps.append(self._workflow_step("scan", "Scan Project", "done", f"Imported {scan_result['count']} file summary node(s).", {"count": scan_result["count"], "root": scan_result["root"]}))
            except ValueError as exc:
                steps.append(self._workflow_step("scan", "Scan Project", "skipped", str(exc), {"root": str(self.project_root)}))
        else:
            steps.append(self._workflow_step("scan", "Scan Project", "skipped", "Project scan disabled for this run.", {"count": 0}))
        artifacts["scan"] = scan_result

        context = self.context(query, project_id=project_id, limit=int(payload.get("limit") or 8))
        steps.append(self._workflow_step("context", "Build Context", "done", f"Built context pack with {len(context['hits'])} memory hit(s).", {"hits": len(context["hits"])}))
        artifacts["context"] = context["context"]

        task = self.create_task({
            "project_id": project_id,
            "title": f"Workflow {workflow['name']}: {query[:64]}",
            "priority": str(payload.get("priority") or "medium"),
            "status": "doing",
            "detail": f"Workflow run started for {workflow['name']}. Provider: {provider_id}.",
        })
        steps.append(self._workflow_step("task", "Write Task", "done", f"Created workflow task: {task['title']}", {"task_id": task["id"]}))

        provider_message = self._workflow_prompt(workflow, query, context["context"])
        provider_result = self.run_ai({
            "project_id": project_id,
            "message": provider_message,
            "provider_id": provider_id,
            "limit": int(payload.get("limit") or 8),
            "allow_cli": payload.get("allow_cli"),
        })
        provider_status = str(provider_result.get("provider", {}).get("status") or "unknown")
        steps.append(self._workflow_step("provider", "Ask Provider", "done" if provider_result.get("text") else "empty", f"Provider {provider_result.get('provider', {}).get('id')} finished with {provider_status}.", {"run_id": provider_result.get("run_id"), "provider": provider_result.get("provider")}))
        artifacts["provider_result"] = provider_result

        memory_text = (
            f"Workflow: {workflow['name']}\n"
            f"Goal: {query}\n"
            f"Provider: {provider_result.get('provider', {}).get('id')} / {provider_status}\n\n"
            f"{provider_result.get('text') or 'No provider response text.'}"
        )
        memory = self.add_memory({
            "project_id": project_id,
            "type": "Lesson",
            "label": f"Workflow result: {query[:56]}",
            "scope": "project",
            "text": memory_text[:3200],
            "source": "workflow",
        })
        steps.append(self._workflow_step("memory", "Write Memory", "done", f"Persisted workflow memory: {memory['label']}", {"memory_id": memory["id"]}))

        task = self.update_task(task["id"], {"status": "done", "detail": f"Workflow completed. Memory: {memory['id']}. Provider run: {provider_result.get('run_id') or 'n/a'}."})
        review_task = self.create_task({
            "project_id": project_id,
            "title": f"Review workflow result: {query[:56]}",
            "priority": "medium",
            "status": "todo",
            "detail": f"Review memory {memory['id']} and promote follow-up work from workflow {workflow['name']}.",
            "linked_memory_ids": [memory["id"]],
        })
        steps.append(self._workflow_step("review", "Review", "done", f"Created review task: {review_task['title']}", {"task_id": review_task["id"], "memory_id": memory["id"]}))

        return {
            "workflow_id": workflow_id,
            "workflow": workflow,
            "status": "ok",
            "project_id": project_id,
            "query": query,
            "context": context["context"],
            "steps": steps,
            "task": task,
            "review_task": review_task,
            "memory": memory,
            "provider": provider_result.get("provider"),
            "response": provider_result.get("text") or "",
            "artifacts": artifacts,
        }

    def _workflow_definitions(self) -> dict[str, dict[str, Any]]:
        base_steps = [
            {"id": "scan", "name": "Scan Project"},
            {"id": "context", "name": "Build Context"},
            {"id": "task", "name": "Write Task"},
            {"id": "provider", "name": "Ask Provider"},
            {"id": "memory", "name": "Write Memory"},
            {"id": "review", "name": "Review"},
        ]
        return {
            "intro": {"id": "intro", "name": "Intro", "description": "Build onboarding context for a project or feature.", "prompt": "Produce a concise onboarding brief with known decisions, risks, and next actions.", "scan_limit": 8, "steps": base_steps},
            "access": {"id": "access", "name": "Access", "description": "Check provider, secret, and environment readiness.", "prompt": "Review provider setup and identify concrete access checks without exposing secrets.", "scan_limit": 0, "steps": base_steps},
            "rules": {"id": "rules", "name": "Rules", "description": "Extract project rules, decisions, and constraints into memory.", "prompt": "Extract durable engineering rules, constraints, and decisions from the context.", "scan_limit": 10, "steps": base_steps},
            "help": {"id": "help", "name": "Help", "description": "Create a context pack and implementation brief for a coding task.", "prompt": "Create an implementation brief with assumptions, plan, risks, and verification steps.", "scan_limit": 8, "steps": base_steps},
            "review": {"id": "review", "name": "Review", "description": "Prepare architecture/code review notes.", "prompt": "Review the context for correctness risks, missing tests, and follow-up tasks.", "scan_limit": 8, "steps": base_steps},
        }

    def _workflow_prompt(self, workflow: dict[str, Any], query: str, context: str) -> str:
        return (
            f"Run ArchitectOS workflow: {workflow['name']}\n"
            f"Goal: {query}\n"
            f"Instruction: {workflow.get('prompt') or workflow['description']}\n\n"
            "Use this context and return: summary, decisions, risks, next actions, and verification.\n\n"
            f"{context}"
        )

    def _workflow_step(self, step_id: str, name: str, status: str, message: str, artifact: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"id": step_id, "name": name, "status": status, "message": message, "artifact": artifact or {}, "finished_at": utc_now()}

    def providers(self) -> dict[str, Any]:
        return {"mode": "local-memory-first", "providers": self.repository.list_providers()}

    def test_provider(self, provider_id: str) -> dict[str, Any]:
        result = self.provider_router.check(self.repository.list_providers(), provider_id)
        selected = dict(result.get("selected_provider") or {})
        last_check = {
            "ready": bool(result.get("ready")),
            "status": str(result.get("status") or "unknown"),
            "message": str(result.get("message") or ""),
            "checked_at": utc_now(),
        }
        if selected.get("id") and selected.get("id") != "local-memory":
            selected["status"] = "configured" if result.get("ready") else str(result.get("status") or "error")
            selected["last_check"] = last_check
            self.repository.upsert_provider(selected)
        return {
            "provider": {
                "id": result.get("provider_id"),
                "requested_id": result.get("requested_provider_id"),
                "selected": selected,
                "ready": bool(result.get("ready")),
                "status": result.get("status"),
            },
            "message": result.get("message") or "",
            "hint": result.get("hint") or "",
            "actions": list(result.get("actions") or []),
            "details": result.get("details") or {},
            "checked_at": last_check["checked_at"],
        }

    def start_provider_login(self, provider_id: str) -> dict[str, Any]:
        result = self.provider_router.login(self.repository.list_providers(), provider_id)
        return {
            "provider": {
                "id": result.get("provider_id"),
                "requested_id": result.get("requested_provider_id"),
                "selected": result.get("selected_provider") or {},
                "ok": bool(result.get("ok")),
                "status": result.get("status"),
            },
            "message": result.get("message") or "",
            "hint": result.get("hint") or "",
            "details": result.get("details") or {},
        }

    def test_providers(self) -> dict[str, Any]:
        providers = self.repository.list_providers()
        return {"checks": [self.test_provider(provider["id"]) for provider in providers]}

    def connect_env_providers(self) -> dict[str, Any]:
        self._load_project_env_files()
        specs = {
            "openai": {"label": "OpenAI", "api_key_env": "OPENAI_API_KEY", "model": "gpt-4.1-mini"},
            "azure-openai": {"label": "Azure OpenAI", "api_key_env": "AZURE_OPENAI_API_KEY", "endpoint_env": "AZURE_OPENAI_ENDPOINT", "model": "gpt-4.1-mini", "model_env": "AZURE_OPENAI_DEPLOYMENT"},
            "anthropic": {"label": "Anthropic", "api_key_env": "ANTHROPIC_API_KEY", "model": "claude-sonnet-4-20250514"},
            "gemini-cli": {"label": "Antigravity CLI", "api_key_env": "GEMINI_API_KEY", "model": "", "provider_type": "cli"},
        }
        connected: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        for provider_id, spec in specs.items():
            env_name = spec["api_key_env"]
            endpoint_env = spec.get("endpoint_env")
            endpoint = os.environ.get(endpoint_env or "") if endpoint_env else ""
            model = os.environ.get(str(spec.get("model_env") or "")) or spec["model"]
            available = bool(os.environ.get(env_name)) and (not endpoint_env or bool(endpoint))
            if provider_id == "gemini-cli" and not available:
                from .adapters import _gemini_session_auth_state
                gemini_auth = _gemini_session_auth_state()
                available = bool(gemini_auth.get("ready"))
            payload = {
                "label": spec["label"],
                "provider_type": spec.get("provider_type") or "api",
                "api_key_env": env_name if provider_id != "gemini-cli" else "",
                "model": model,
                "enabled": available,
                "status": "configured" if available else "missing_credentials",
                "notes": (
                    "Uses Antigravity CLI (agy) with Google account sign-in. Gemini CLI is deprecated."
                    if provider_id == "gemini-cli"
                    else f"Uses {env_name} from process environment or .env.local."
                ),
                "command": "agy -p" if provider_id == "gemini-cli" else None,
            }
            payload = {key: value for key, value in payload.items() if value is not None}
            if endpoint:
                payload["base_url"] = endpoint
            provider = self.update_provider(provider_id, payload)
            check = self.test_provider(provider_id)
            item = {
                "id": provider_id,
                "label": spec["label"],
                "api_key_env": env_name,
                "ready": bool(check["provider"]["ready"]),
                "status": check["provider"]["status"],
                "message": check["message"],
            }
            if available:
                connected.append(item)
            else:
                missing.append(item)
        return {
            "connected": connected,
            "missing": missing,
            "message": f"Connected {len(connected)} API provider(s). Missing: {', '.join(item['api_key_env'] for item in missing) or 'none'}.",
        }

    def provider_models(self, provider_id: str) -> dict[str, Any]:
        result = self.provider_router.list_models(self.repository.list_providers(), provider_id)
        selected = dict(result.get("selected_provider") or {})
        if selected.get("id") == provider_id:
            selected["available_models"] = list(result.get("models") or [])
            selected["models_checked_at"] = utc_now()
            if result.get("ready") and result.get("models") and not selected.get("model"):
                selected["model"] = str(result["models"][0]["name"])
            if result.get("ready"):
                selected["status"] = "configured" if selected.get("enabled") else "available"
            else:
                selected["status"] = str(result.get("status") or "unreachable")
            self.repository.upsert_provider(selected)
        return {
            "provider": {
                "id": result.get("provider_id"),
                "requested_id": result.get("requested_provider_id"),
                "selected": selected,
                "ready": bool(result.get("ready")),
                "status": result.get("status"),
            },
            "models": list(result.get("models") or []),
            "message": result.get("message") or "",
            "hint": result.get("hint") or "",
            "base_url": result.get("base_url") or selected.get("base_url") or "",
        }

    def update_provider(self, provider_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        provider = next((item for item in self.repository.list_providers() if item["id"] == provider_id), {"id": provider_id, "label": provider_id, "provider_type": "custom", "status": "planned", "enabled": False, "model": "", "notes": ""})
        for key in ("label", "provider_type", "status", "model", "notes", "command", "base_url", "api_key_env", "workdir_policy", "workdir"):
            if key in payload:
                provider[key] = str(payload[key])
        if "timeout_seconds" in payload:
            provider["timeout_seconds"] = max(1, int(payload["timeout_seconds"] or 120))
        if "approval_required" in payload:
            provider["approval_required"] = bool(payload["approval_required"])
        if "enabled" in payload:
            provider["enabled"] = bool(payload["enabled"])
            provider["status"] = "configured" if provider["enabled"] else provider["status"]
        return self.repository.upsert_provider(provider)

    def settings(self) -> dict[str, Any]:
        settings = self.repository.all_settings()
        settings.setdefault("memory_lifecycle", self.memory_lifecycle.settings())
        settings.setdefault("memory_retrieval", self.memory_embeddings.settings())
        settings["memory_embeddings"] = self.memory_embeddings_status()
        return self._public_settings(settings)

    def memory_embeddings_status(self, project_id: str | None = None) -> dict[str, Any]:
        return self.memory_embeddings.status(project_id)

    def rebuild_memory_embeddings(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        if payload.get("provider") or payload.get("model") or payload.get("embedding_provider") or payload.get("embedding_model"):
            current = dict(self.repository.get_setting("memory_retrieval") or {})
            if payload.get("provider") or payload.get("embedding_provider"):
                current["embedding_provider"] = str(payload.get("provider") or payload.get("embedding_provider") or current.get("embedding_provider") or "auto")
            if payload.get("model") or payload.get("embedding_model"):
                current["embedding_model"] = str(payload.get("model") or payload.get("embedding_model") or "")
            if payload.get("dimensions") or payload.get("embedding_dimensions"):
                current["embedding_dimensions"] = int(payload.get("dimensions") or payload.get("embedding_dimensions") or 768)
            self.repository.set_setting("memory_retrieval", current)
            self._reload_embedding_engine(rebuild=False)
        if payload.get("clear"):
            self.repository.rebuild_memory_embeddings()
        if payload.get("async", True):
            threading.Thread(target=self._backfill_embeddings_safe, name="embed-rebuild-api", daemon=True).start()
            return {"started": True, "async": True, **self.memory_embeddings.provider_info()}
        result = self.memory_embeddings.rebuild_all() if payload.get("full") else self.memory_embeddings.ensure_indexed()
        return {"started": False, "async": False, **result}

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        prev_retrieval = dict(self.repository.get_setting("memory_retrieval") or {})
        for key, value in payload.items():
            if not isinstance(value, dict):
                raise ValueError("settings values must be objects")
            if key in {"memory_lifecycle", "ui", "security", "workspace", "router", "memory_retrieval"}:
                current = dict(self.repository.get_setting(key) or {})
                current.update(value)
                self.repository.set_setting(key, current)
            else:
                self.repository.set_setting(key, value)
        if "memory_retrieval" in payload:
            # Rebuild only when the embedding identity changes — timeout/pool/min_score are live.
            identity_keys = ("embeddings_enabled", "embedding_provider", "embedding_model", "embedding_dimensions")
            next_retrieval = dict(self.repository.get_setting("memory_retrieval") or {})
            needs_rebuild = any(
                str(prev_retrieval.get(key) or "") != str(next_retrieval.get(key) or "")
                for key in identity_keys
            )
            self._reload_embedding_engine(rebuild=needs_rebuild)
        self._sync_router_settings()
        return self.settings()

    def _reload_embedding_engine(self, *, rebuild: bool = False) -> None:
        retrieval_settings = dict(self.repository.get_setting("memory_retrieval") or {})
        self.embedding_provider = build_embedding_provider(retrieval_settings)
        self.memory_embeddings = MemoryEmbeddingEngine(self.repository, self.embedding_provider)
        # Drop previous embedding index listeners so provider switches don't stack.
        listeners = getattr(self.repository, "_node_upsert_listeners", None)
        if isinstance(listeners, list):
            self.repository._node_upsert_listeners = [
                item for item in listeners
                if getattr(getattr(item, "__self__", None), "__class__", type(None)).__name__ != "MemoryEmbeddingEngine"
            ]
        self.repository.register_node_upsert_listener(self.memory_embeddings.index_node)
        if rebuild and self.memory_embeddings.enabled():
            threading.Thread(target=self._backfill_embeddings_safe, name="embed-rebuild", daemon=True).start()

    def _warmup_embeddings_safe(self) -> None:
        """Prime the embedding provider so the first Search does not burn the timeout."""
        try:
            self.memory_embeddings.provider.embed("warmup", purpose="query")
            # Load unpacked vectors into RAM once (7k×1024 is fine locally).
            cache_size = len(self.memory_embeddings._load_vector_cache())  # noqa: SLF001
            _LOG.info(
                "embedding warmup ok · provider=%s model=%s dims=%s cache=%s",
                getattr(self.memory_embeddings.provider, "provider_id", ""),
                getattr(self.memory_embeddings.provider, "model", ""),
                getattr(self.memory_embeddings.provider, "dimensions", 0),
                cache_size,
            )
        except Exception as exc:
            _LOG.warning("embedding warmup skipped: %s", exc)

    def _backfill_embeddings_safe(self) -> None:
        try:
            # Drop vectors from a previous provider/model so dim mismatches don't linger.
            info = self.memory_embeddings.provider_info()
            provider = str(info.get("provider") or "")
            model = str(info.get("model") or "")
            with self.repository._connect() as conn:  # noqa: SLF001 - intentional maintenance
                if provider and model:
                    conn.execute(
                        "DELETE FROM memory_embeddings WHERE provider != ? OR model != ?",
                        (provider, model),
                    )
            # Index in chunks so a single restart can eventually cover the full corpus.
            total = 0
            empty_rounds = 0
            for _ in range(40):
                result = self.memory_embeddings.ensure_indexed()
                indexed = int(result.get("indexed") or 0)
                pending = int(result.get("pending") or 0)
                total += indexed
                if indexed <= 0:
                    empty_rounds += 1
                    # Provider down / all attempts failing — stop instead of spinning 40×.
                    if pending > 0 or empty_rounds >= 2:
                        break
                else:
                    empty_rounds = 0
            _LOG.info("embedding backfill done · indexed=%s provider=%s model=%s", total, provider, model)
        except Exception as exc:
            _LOG.warning("embedding backfill failed: %s", exc)

    def _public_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        public = dict(settings)
        mcp_settings = dict(public.get("mcp_servers") or {})
        servers = []
        for server in mcp_settings.get("servers") or []:
            item = dict(server)
            auth = dict(item.get("auth") or {})
            item["auth"] = {
                "status": auth.get("status") or ("authorized" if auth.get("access_token") else ""),
                "expires_at": auth.get("expires_at") or 0,
                "has_access_token": bool(auth.get("access_token")),
                "has_refresh_token": bool(auth.get("refresh_token")),
            } if auth else {}
            servers.append(item)
        if mcp_settings:
            public["mcp_servers"] = {**mcp_settings, "servers": servers}
        return public

    def _sync_router_settings(self) -> None:
        self.provider_router.router_settings = self.repository.get_setting("router") or {}

    # --- AI Router -----------------------------------------------------------

    def router_settings(self) -> dict[str, Any]:
        return {"router": self.repository.get_setting("router") or {}, "profiles": self._router_profiles()}

    def _router_profiles(self) -> dict[str, Any]:
        from .routing import PROVIDER_PROFILES
        return {key: {"cost": value["cost"], "latency": value["latency"], "quality": value["quality"], "strengths": sorted(value.get("strengths") or [])} for key, value in PROVIDER_PROFILES.items()}

    def update_router_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = dict(self.repository.get_setting("router") or {})
        if "strategy" in payload:
            current["strategy"] = str(payload["strategy"])
        if isinstance(payload.get("weights"), dict):
            current["weights"] = {key: float(value) for key, value in payload["weights"].items()}
        if "role_aware" in payload:
            current["role_aware"] = bool(payload["role_aware"])
        self.repository.set_setting("router", current)
        self._sync_router_settings()
        return self.router_settings()

    def routing_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._sync_router_settings()
        query = str(payload.get("message") or payload.get("query") or "").strip()
        role = str(payload.get("role") or "") or (classify_role(query) if query else None)
        provider_id = str(payload.get("provider_id") or "auto")
        policy = RouterPolicy(self.repository.get_setting("router") or {})
        plan = policy.select(self.repository.list_providers(), provider_id, role)
        return {"query": query, "role": role, "decision": plan["decision"], "selected_provider": {"id": plan["provider"].get("id"), "label": plan["provider"].get("label")}}

    # --- Multi-Agent ---------------------------------------------------------

    def council_config(self) -> dict[str, Any]:
        return self.council.config()

    def _agent_selectable_providers(self, providers: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        items = providers if providers is not None else self.repository.list_providers()
        selectable: list[dict[str, Any]] = [{
            "id": "auto",
            "label": "Auto (router)",
            "ready": True,
            "enabled": True,
            "status": "configured",
        }]
        for provider in items:
            provider_id = str(provider.get("id") or "")
            if not provider_id or provider_id == "local-memory":
                continue
            ready = self._provider_is_selectable(provider)
            selectable.append({
                "id": provider_id,
                "label": str(provider.get("label") or provider_id),
                "ready": ready,
                "enabled": bool(provider.get("enabled")),
                "status": str(provider.get("status") or ""),
            })
        return selectable

    def _provider_is_selectable(self, provider: dict[str, Any]) -> bool:
        if not provider or not provider.get("enabled"):
            return False
        last = provider.get("last_check") or {}
        if last.get("ready"):
            return True
        status = str(provider.get("status") or "").lower()
        return status in {"configured", "ok", "available", "ready"}

    def _provider_run_stats(self, limit: int = 240, project_id: str | None = None) -> dict[str, Any]:
        runs = self.repository.list_provider_runs(project_id, limit=max(20, min(int(limit or 240), 500)))
        by_provider: dict[str, dict[str, Any]] = {}
        by_role: dict[str, dict[str, dict[str, Any]]] = {}
        by_model: dict[str, dict[str, Any]] = {}
        totals = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "runs_with_usage": 0,
            "runs_with_cost": 0,
        }
        for run in runs:
            provider_id = str(run.get("provider_id") or "").strip()
            if not provider_id:
                continue
            status = str(run.get("status") or "").lower()
            ok = status in {"ok", "success", "completed", "done"}
            bucket = by_provider.setdefault(provider_id, {"total": 0, "ok": 0, "fail": 0})
            bucket["total"] += 1
            if ok:
                bucket["ok"] += 1
            else:
                bucket["fail"] += 1
            role = ""
            provider_meta = run.get("provider") if isinstance(run.get("provider"), dict) else {}
            selected = run.get("selected_provider") if isinstance(run.get("selected_provider"), dict) else {}
            routing = run.get("routing") if isinstance(run.get("routing"), dict) else {}
            if not routing and isinstance(provider_meta.get("routing"), dict):
                routing = provider_meta.get("routing") or {}
            if not routing and isinstance(selected.get("routing"), dict):
                routing = selected.get("routing") or {}
            role = str(routing.get("role") or run.get("role") or "").strip().lower()
            if role:
                role_bucket = by_role.setdefault(role, {})
                stats = role_bucket.setdefault(provider_id, {"total": 0, "ok": 0, "fail": 0})
                stats["total"] += 1
                if ok:
                    stats["ok"] += 1
                else:
                    stats["fail"] += 1

            usage = run.get("usage") if isinstance(run.get("usage"), dict) else {}
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or (prompt + completion))
            cost_raw = usage.get("cost_usd")
            if cost_raw is None:
                cost_raw = run.get("cost_usd")
            try:
                cost = float(cost_raw) if cost_raw is not None else None
            except (TypeError, ValueError):
                cost = None
            if prompt or completion or total_tokens:
                totals["prompt_tokens"] += prompt
                totals["completion_tokens"] += completion
                totals["total_tokens"] += total_tokens
                totals["runs_with_usage"] += 1
                if cost is not None:
                    totals["cost_usd"] += cost
                    totals["runs_with_cost"] += 1
                model = str(run.get("model") or selected.get("model") or usage.get("model") or "").strip() or "unknown"
                key = f"{provider_id}::{model}"
                model_bucket = by_model.setdefault(key, {
                    "provider_id": provider_id,
                    "model": model,
                    "runs": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                    "has_cost": False,
                })
                model_bucket["runs"] += 1
                model_bucket["prompt_tokens"] += prompt
                model_bucket["completion_tokens"] += completion
                model_bucket["total_tokens"] += total_tokens
                if cost is not None:
                    model_bucket["cost_usd"] += cost
                    model_bucket["has_cost"] = True

        for bucket in by_provider.values():
            total = max(1, int(bucket["total"]))
            bucket["success_rate"] = round(int(bucket["ok"]) / total, 3)
        for role_map in by_role.values():
            for bucket in role_map.values():
                total = max(1, int(bucket["total"]))
                bucket["success_rate"] = round(int(bucket["ok"]) / total, 3)
        model_rows = sorted(by_model.values(), key=lambda item: (-int(item["total_tokens"]), -int(item["runs"])))
        for row in model_rows:
            row["cost_usd"] = round(float(row["cost_usd"]), 6) if row.get("has_cost") else None
            row.pop("has_cost", None)
        totals["cost_usd"] = round(float(totals["cost_usd"]), 6)
        return {
            "runs_sampled": len(runs),
            "by_provider": by_provider,
            "by_role": by_role,
            "usage": {
                "totals": totals,
                "by_model": model_rows,
            },
        }

    def run_council(self, payload: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for event in self.stream_council(payload):
            if event.get("type") == "done":
                result = event.get("result") or {}
        return result

    def stream_council(self, payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
        project_id = str(payload.get("project_id") or "architectos")
        message = str(payload.get("message") or payload.get("query") or "").strip()
        chat_id = str(payload.get("chat_id") or "").strip()

        chat = self._chat_for_agent_turn(project_id, chat_id, message)
        chat["messages"].append({"role": "user", "text": message, "created_at": utc_now()})
        chat = self.repository.upsert_chat(chat)

        activity_trace_by_role: dict[str, dict[str, Any]] = {}
        for event in self.council.stream(payload):
            if event.get("type") == "progress":
                agent = event.get("agent") if isinstance(event.get("agent"), dict) else {}
                role = str(agent.get("role") or "")
                if role:
                    done = str(agent.get("status") or "").lower() == "done"
                    activity_trace_by_role[role] = {
                        "kind": "agent",
                        "step_id": f"council:{role}",
                        "name": role,
                        "role": role,
                        "role_name": str(agent.get("role_name") or role),
                        "ok": done,
                        "pending": not done,
                        "summary": "Finished" if done else "Running",
                        "provider": agent.get("provider") or {},
                    }
            if event.get("type") == "done":
                result = event.get("result") or {}
                assistant_text = self._council_assistant_text(result)
                display_text, structured = split_rich_response(assistant_text)
                chat["messages"].append({
                    "role": "assistant",
                    "text": display_text or assistant_text,
                    "raw_text": assistant_text,
                    "structured": structured,
                    "created_at": utc_now(),
                    "context": "",
                    "provider": {"id": "council", "status": "ok"},
                    "tool_trace": list(activity_trace_by_role.values()),
                    "tool_trace_label": "Council",
                })
                chat = self.repository.upsert_chat(chat)
                event["result"]["chat"] = chat
                event["result"]["structured"] = structured
                event["result"]["response"] = display_text or assistant_text
                self._capture_chat_turn_memory_candidate(
                    project_id,
                    chat["id"],
                    message,
                    display_text or assistant_text,
                    source="council_keeper",
                    metadata={"mode": "council", "models": result.get("models") or []},
                )
                self._after_chat_turn(project_id, chat["id"], message, display_text or assistant_text, source="council_keeper")
            yield event

    def chat_context_pack(self, chat_id: str, query: str = "", project_id: str | None = None) -> dict[str, Any]:
        chat = self.repository.get_chat(chat_id)
        if not chat:
            raise ValueError("chat not found")
        actual_project_id = str(chat.get("project_id") or project_id or "architectos")
        q = str(query or "").strip()
        if not q:
            messages = [m for m in chat.get("messages") or [] if str(m.get("text") or "").strip()]
            q = str(messages[-1].get("text") or "") if messages else str(chat.get("title") or "")
        summaries = self.repository.list_chat_context_summaries(chat_id)
        candidates = self.repository.list_memory_candidates(actual_project_id, "candidate", 20)
        relevant = self.search_memory(q or "memory", project_id=actual_project_id, limit=8, refresh=False)["hits"] if q else []
        events = self.repository.list_keeper_events(actual_project_id, chat_id, 20)
        return {
            "chat": chat,
            "summaries": summaries,
            "rolling_summary": next((s for s in summaries if s.get("summary_type") == "rolling"), None),
            "decision_log": next((s for s in summaries if s.get("summary_type") == "decision_log"), None),
            "relevant_memory": relevant,
            "pending_candidates": [c for c in candidates if str(c.get("source_ref") or "") == chat_id or str((c.get("metadata") or {}).get("chat_id") or "") == chat_id],
            "keeper_events": events,
        }

    def favorite_chat_message(self, chat_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        chat = self.repository.get_chat(chat_id)
        if not chat:
            raise ValueError("chat not found")
        index = int(payload.get("message_index"))
        messages = list(chat.get("messages") or [])
        if index < 0 or index >= len(messages):
            raise ValueError("message_index out of range")
        message = dict(messages[index])
        if message.get("role") != "assistant":
            raise ValueError("only assistant messages can be favorited into memory")
        favorite = bool(payload.get("favorite", True))
        message["favorite"] = favorite
        messages[index] = message
        chat["messages"] = messages
        chat = self.repository.upsert_chat(chat)
        candidate = None
        if favorite:
            user_text = self._nearest_user_message_before(messages, index)
            candidate = self._capture_favorite_message_candidate(str(chat.get("project_id") or "architectos"), chat_id, index, user_text, str(message.get("text") or ""))
        self._record_keeper_event(str(chat.get("project_id") or "architectos"), chat_id, "favorite", "candidate_created" if candidate else "updated", "Assistant favorite updated.", {"message_index": index, "candidate_id": (candidate or {}).get("id")})
        return {"chat": chat, "candidate": candidate}

    def retry_chat_keeper(self, chat_id: str) -> dict[str, Any]:
        chat = self.repository.get_chat(chat_id)
        if not chat:
            raise ValueError("chat not found")
        messages = list(chat.get("messages") or [])
        for index in range(len(messages) - 1, 0, -1):
            if messages[index].get("role") == "assistant":
                user_text = self._nearest_user_message_before(messages, index)
                assistant_text = str(messages[index].get("text") or "")
                candidate = self._capture_chat_turn_memory_candidate(str(chat.get("project_id") or "architectos"), chat_id, user_text, assistant_text, source="manual_retry", metadata={"message_index": index})
                self._after_chat_turn(str(chat.get("project_id") or "architectos"), chat_id, user_text, assistant_text, source="manual_retry")
                return {"chat": chat, "candidate": candidate}
        raise ValueError("chat has no assistant message to retry")

    def _chat_for_agent_turn(self, project_id: str, chat_id: str, message: str) -> dict[str, Any]:
        chat = self.repository.get_chat(chat_id) if chat_id else None
        if chat:
            return chat
        return {
            "id": chat_id or stable_id("chat", project_id, message[:60], utc_now()),
            "project_id": project_id,
            "title": message[:60] or "New council dialog",
            "messages": [],
            "favorite": False,
            "created_at": utc_now(),
        }

    def _council_assistant_text(self, result: dict[str, Any]) -> str:
        synthesis = str(result.get("synthesis") or "").strip()
        answers = result.get("answers") or []
        if synthesis:
            details = []
            for answer in answers:
                if not str(answer.get("text") or "").strip():
                    continue
                label = answer.get("label") or answer.get("provider_id") or "Model"
                details.append(f"<summary>{label}</summary>\n\n{answer.get('text')}")
            if len(details) > 1:
                blocks = "\n\n".join(f"<details>\n{d}\n</details>" for d in details)
                return f"{synthesis}\n\n---\n**Individual model answers**\n\n{blocks}".strip()
            return synthesis
        parts = []
        for answer in answers:
            label = answer.get("label") or answer.get("provider_id") or "Model"
            parts.append(f"\n\n— {label} —\n{answer.get('text') or '(no answer)'}")
        return "".join(parts).strip()

    def _capture_chat_turn_memory_candidate(
        self,
        project_id: str,
        chat_id: str,
        user_text: str,
        assistant_text: str,
        *,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        user_text = str(user_text or "").strip()
        assistant_text = str(assistant_text or "").strip()
        life = self.memory_lifecycle.settings()
        mode = normalize_chat_memory_mode(life.get("chat_memory_mode"))
        verdict = evaluate_chat_turn(user_text, assistant_text, mode=mode)
        if not verdict.keep:
            self._record_keeper_event(
                project_id,
                chat_id,
                source,
                "skipped",
                f"Skipped: {verdict.reason} (mode={mode}, score={verdict.score:.2f}).",
                {**(metadata or {}), "chat_memory_mode": mode, "salience": verdict.score},
            )
            return None

        facts_only = bool(life.get("chat_store_facts_only", True))
        facts = list(verdict.facts or [])
        if facts_only and facts:
            saved_last = None
            for index, fact in enumerate(facts[:3]):
                fact_text = str(fact.get("text") or "").strip()
                if not fact_text:
                    continue
                label_seed = fact_text[:72]
                candidate = {
                    "id": stable_id("candidate", project_id, "chat_fact", chat_id, fact_text[:240], str(index)),
                    "project_id": project_id,
                    "source_type": "chat",
                    "source_ref": chat_id,
                    "label": f"Chat fact: {label_seed}",
                    "type": str(fact.get("type") or self._chat_turn_memory_type(user_text, assistant_text)),
                    "scope": "project",
                    "text": (
                        "Durable fact extracted from chat (not a full transcript).\n\n"
                        f"{fact_text}\n\n"
                        f"Context user prompt: {user_text[:400]}"
                    ).strip(),
                    "confidence": max(0.55, min(0.85, 0.55 + verdict.score * 0.3)),
                    "metadata": {
                        "chat_id": chat_id,
                        "template": "chat_fact_keeper",
                        "source": source,
                        "source_type": "chat",
                        "fact_source": fact.get("source"),
                        "salience": verdict.score,
                        "chat_memory_mode": mode,
                        **dict(metadata or {}),
                    },
                }
                prepared = self.ingestion_engine.prepare_candidates(project_id, [candidate], 1)
                if not prepared:
                    continue
                saved_last = self.repository.upsert_memory_candidate(prepared[0])
                self._record_keeper_event(
                    project_id,
                    chat_id,
                    source,
                    "candidate_created",
                    f"Created chat fact candidate: {saved_last.get('label')}",
                    {"candidate_id": saved_last.get("id"), "salience": verdict.score, **dict(metadata or {})},
                )
            if not saved_last:
                self._record_keeper_event(project_id, chat_id, source, "skipped", "Skipped: duplicate or low-value fact candidates.", dict(metadata or {}))
            return saved_last

        # Fallback / aggressive raw-turn path (still gated).
        title = self._chat_turn_memory_title(user_text)
        body = (
            "Post-turn chat memory candidate.\n\n"
            f"User:\n{user_text[:1800]}\n\n"
            f"Assistant:\n{assistant_text[:2200]}"
        ).strip()
        candidate = {
            "id": stable_id("candidate", project_id, "chat_turn", chat_id, user_text[:240], assistant_text[:240]),
            "project_id": project_id,
            "source_type": "chat",
            "source_ref": chat_id,
            "label": title,
            "type": self._chat_turn_memory_type(user_text, assistant_text),
            "scope": "project",
            "text": body,
            "confidence": max(0.55, min(0.8, 0.5 + verdict.score * 0.3)),
            "metadata": {
                "chat_id": chat_id,
                "template": "chat_turn_keeper",
                "source": source,
                "source_type": "chat",
                "salience": verdict.score,
                "chat_memory_mode": mode,
                **dict(metadata or {}),
            },
        }
        prepared = self.ingestion_engine.prepare_candidates(project_id, [candidate], 1)
        if not prepared:
            self._record_keeper_event(project_id, chat_id, source, "skipped", "Skipped: duplicate or low-value candidate.", dict(metadata or {}))
            return None
        saved = self.repository.upsert_memory_candidate(prepared[0])
        self._record_keeper_event(project_id, chat_id, source, "candidate_created", f"Created memory candidate: {saved.get('label')}", {"candidate_id": saved.get("id"), **dict(metadata or {})})
        return saved

    def _capture_favorite_message_candidate(self, project_id: str, chat_id: str, message_index: int, user_text: str, assistant_text: str) -> dict[str, Any] | None:
        # Explicit ★ save always creates a candidate (opt-in), but still prefer facts when possible.
        verdict = evaluate_chat_turn(user_text, assistant_text, mode="aggressive")
        facts = list(verdict.facts or [])
        if facts:
            fact = facts[0]
            text = (
                "Favorited durable fact from assistant reply.\n\n"
                f"{fact.get('text')}\n\n"
                f"User:\n{user_text[:800]}\n\nAssistant excerpt:\n{assistant_text[:1200]}"
            ).strip()
            label = f"Favorite: {str(fact.get('text') or '')[:64]}"
            fact_type = str(fact.get("type") or self._chat_turn_memory_type(user_text, assistant_text))
        else:
            text = f"Favorited assistant reply.\n\nUser:\n{user_text[:1200]}\n\nAssistant:\n{assistant_text[:2600]}".strip()
            label = f"Favorite reply: {(user_text or assistant_text)[:72]}"
            fact_type = self._chat_turn_memory_type(user_text, assistant_text)
        candidate = {
            "id": stable_id("candidate", project_id, "assistant_favorite", chat_id, str(message_index), assistant_text[:240]),
            "project_id": project_id,
            "source_type": "chat_favorite",
            "source_ref": chat_id,
            "label": label,
            "type": fact_type,
            "scope": "project",
            "text": text,
            "confidence": 0.86,
            "metadata": {
                "chat_id": chat_id,
                "message_index": message_index,
                "template": "assistant_favorite",
                "explicit_save": True,
            },
        }
        prepared = self.ingestion_engine.prepare_candidates(project_id, [candidate], 1)
        return self.repository.upsert_memory_candidate(prepared[0]) if prepared else None

    def _after_chat_turn(self, project_id: str, chat_id: str, user_text: str, assistant_text: str, *, source: str) -> None:
        self._update_chat_summaries(project_id, chat_id, user_text, assistant_text)
        mode = normalize_chat_memory_mode(self.memory_lifecycle.settings().get("chat_memory_mode"))
        if mode == "off":
            return
        # Rules keeper only when the turn already looks durable (avoid chitchat → rule spam).
        verdict = evaluate_chat_turn(user_text, assistant_text, mode=mode)
        if not verdict.keep:
            return
        rule_candidate = self._capture_rules_candidate(project_id, chat_id, user_text, assistant_text, source=source)
        if rule_candidate:
            self._record_keeper_event(project_id, chat_id, "rules_keeper", "candidate_created", f"Created rule candidate: {rule_candidate.get('label')}", {"candidate_id": rule_candidate.get("id")})

    def _chat_turn_worth_memory(self, user_text: str, assistant_text: str) -> bool:
        mode = normalize_chat_memory_mode(self.memory_lifecycle.settings().get("chat_memory_mode"))
        return evaluate_chat_turn(user_text, assistant_text, mode=mode).keep

    def _chat_turn_memory_title(self, user_text: str) -> str:
        cleaned = " ".join(str(user_text or "").split())
        return f"Chat turn: {cleaned[:72] or 'memory candidate'}"

    def _chat_turn_memory_type(self, user_text: str, assistant_text: str) -> str:
        haystack = f"{user_text} {assistant_text}".lower()
        if any(word in haystack for word in ("decision", "решен", "adr", "architecture", "архитект")):
            return "Decision"
        if any(word in haystack for word in ("requirement", "требован", "must", "долж")):
            return "Requirement"
        if any(word in haystack for word in ("constraint", "огранич", "rule", "правил")):
            return "Constraint"
        return "Lesson"

    def _update_chat_summaries(self, project_id: str, chat_id: str, user_text: str, assistant_text: str) -> None:
        old = self.repository.get_chat_context_summary(chat_id, "rolling") or {}
        old_text = str(old.get("summary_text") or "")
        bullet = self._compact_turn_line(user_text, assistant_text)
        parts = [line for line in old_text.splitlines() if line.strip()]
        parts.append(f"- {bullet}")
        summary_text = "\n".join(parts[-12:])
        self.repository.upsert_chat_context_summary({
            "chat_id": chat_id,
            "project_id": project_id,
            "summary_type": "rolling",
            "summary_text": summary_text,
            "covered_until": utc_now(),
        })
        decision = self._decision_log_line(user_text, assistant_text)
        if decision:
            existing = self.repository.get_chat_context_summary(chat_id, "decision_log") or {}
            lines = [line for line in str(existing.get("summary_text") or "").splitlines() if line.strip()]
            if decision not in lines:
                lines.append(decision)
            self.repository.upsert_chat_context_summary({
                "chat_id": chat_id,
                "project_id": project_id,
                "summary_type": "decision_log",
                "summary_text": "\n".join(lines[-30:]),
                "covered_until": utc_now(),
            })

    def _capture_rules_candidate(self, project_id: str, chat_id: str, user_text: str, assistant_text: str, *, source: str) -> dict[str, Any] | None:
        haystack = f"{user_text}\n{assistant_text}".lower()
        if not any(word in haystack for word in ("rule", "rules", "constraint", "forbidden", "must", "never", "always", "правил", "огранич", "нельзя", "всегда", "долж")):
            return None
        if len(user_text.strip()) < 20:
            return None
        candidate = {
            "id": stable_id("candidate", project_id, "rules_keeper", chat_id, user_text[:240]),
            "project_id": project_id,
            "source_type": "rules_keeper",
            "source_ref": chat_id,
            "label": f"Rule: {self._chat_turn_memory_title(user_text).replace('Chat turn: ', '')}",
            "type": "Rule",
            "scope": "project",
            "text": f"Rule candidate from chat.\n\nUser:\n{user_text[:1800]}\n\nContext:\n{assistant_text[:1200]}",
            "confidence": 0.7,
            "metadata": {"chat_id": chat_id, "template": "rules_keeper", "source": source},
        }
        prepared = self.ingestion_engine.prepare_candidates(project_id, [candidate], 1)
        return self.repository.upsert_memory_candidate(prepared[0]) if prepared else None

    def _record_keeper_event(self, project_id: str, chat_id: str, kind: str, status: str, message: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.repository.add_keeper_event({
            "project_id": project_id,
            "chat_id": chat_id,
            "kind": kind,
            "status": status,
            "message": message,
            "metadata": dict(metadata or {}),
            "created_at": utc_now(),
        })

    def _compact_turn_line(self, user_text: str, assistant_text: str) -> str:
        user = " ".join(str(user_text or "").split())[:180]
        assistant = " ".join(str(assistant_text or "").split())[:180]
        return f"User asked: {user}. Assistant answered: {assistant}"

    def _decision_log_line(self, user_text: str, assistant_text: str) -> str:
        text = f"{user_text} {assistant_text}".lower()
        if not any(word in text for word in ("decision", "decided", "решен", "choose", "выбира", "долж", "must", "rule", "constraint", "огранич")):
            return ""
        source = " ".join(str(user_text or assistant_text or "").split())[:220]
        return f"- {utc_now()}: {source}"

    @staticmethod
    def _nearest_user_message_before(messages: list[dict[str, Any]], index: int) -> str:
        for item in reversed(messages[:index]):
            if item.get("role") == "user":
                return str(item.get("text") or "")
        return ""

    def list_files(self, project_id: str | None = None) -> dict[str, Any]:
        return {"files": self.files.list_files(str(project_id or "architectos"))}

    def upload_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        meta = self.files.upload(project_id, str(payload.get("name") or "file"), str(payload.get("content") or ""))
        return {"file": meta, "files": self.files.list_files(project_id)}

    def add_files_to_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        file_ids = [str(item) for item in (payload.get("file_ids") or []) if str(item).strip()]
        if not file_ids:
            raise ValueError("at least one file is required")
        scope = str(payload.get("scope") or "project")
        node_type = str(payload.get("type") or "Doc")
        imported = []
        skipped = []
        for file_id in file_ids[:20]:
            meta, text = self.files.get_text(project_id, file_id)
            if not meta:
                skipped.append({"id": file_id, "reason": "file not found"})
                continue
            if not meta.get("text_extracted") or not text.strip():
                skipped.append({"id": file_id, "name": meta.get("name"), "reason": "text could not be extracted"})
                continue
            clean_name = str(meta.get("name") or "file")
            body = (
                f"Uploaded file: {clean_name}\n"
                f"MIME: {meta.get('mime')}\n"
                f"Size: {meta.get('size')} bytes\n\n"
                f"{text.strip()[:12000]}"
            )
            node = self.repository.add_node(
                node_type,
                f"File: {clean_name}",
                scope,
                body,
                self._memory_project_id_for_scope(scope, project_id),
                confidence=float(payload.get("confidence") or 0.7),
                metadata={
                    "source": "memory_file_upload",
                    "file_id": file_id,
                    "file_name": clean_name,
                    "mime": meta.get("mime"),
                    "size": meta.get("size"),
                },
            )
            node = self.memory_lifecycle.initialize_node(node, "memory_file_upload")
            self.graph_auto_linker.link_node(node, project_id)
            imported.append(node.to_dict())
        return {
            "project_id": project_id,
            "imported": imported,
            "skipped": skipped,
            "count": len(imported),
            "files": self.files.list_files(project_id),
        }

    def delete_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        deleted = self.files.delete(project_id, str(payload.get("id") or ""))
        return {"deleted": deleted, "files": self.files.list_files(project_id)}

    # --- MCP Hub -------------------------------------------------------------

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


    def project_files(self, project_id: str | None = None, limit: int = 80) -> dict[str, Any]:
        project_id = project_id or "architectos"
        try:
            root = self._project_root(project_id)
        except ValueError as exc:
            project = self.repository.get_project(project_id)
            configured_root = str(project.root_path).strip() if project and project.root_path else ""
            status = "missing_root" if configured_root else "no_root"
            if status == "missing_root":
                message = "This project's folder isn't available on this computer. Update its path or connect another folder."
            else:
                message = "No project folder connected yet. Connect a folder to index files and memory."
            payload = self._empty_system_project_response(project_id, "files", message)
            payload["status"] = status
            payload["configured_root"] = configured_root
            payload["detail"] = str(exc)
            return payload
        files = []
        for path in self._iter_project_tree_entries(root, limit):
            stat = path.stat()
            relative = path.relative_to(root)
            files.append({
                "path": str(relative),
                "name": path.name,
                "type": "folder" if path.is_dir() else "file",
                "extension": path.suffix.lower(),
                "size": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            })
        return {"project_id": project_id, "root": str(root), "files": files, "count": len(files), "status": "ok"}

    def project_search(
        self,
        project_id: str | None = None,
        query: str = "",
        mode: str = "name",
        limit: int = 80,
        mask: str = "",
    ) -> dict[str, Any]:
        project_id = project_id or "architectos"
        query = str(query or "").strip()
        mode = "content" if str(mode or "").strip().lower() in {"content", "text", "grep", "in_files"} else "name"
        limit = max(1, min(int(limit or 80), 200))
        masks = self._parse_file_search_masks(mask)
        if not query and not (mode == "name" and masks):
            return {
                "project_id": project_id,
                "query": "",
                "mode": mode,
                "mask": self._format_file_search_masks(masks),
                "hits": [],
                "count": 0,
                "status": "empty_query",
            }
        root = self._project_root(project_id)
        if mode == "name":
            hits = self._search_project_files_by_name(root, query, limit, masks=masks)
        else:
            hits = self._search_project_files_by_content(root, query, limit, masks=masks)
        return {
            "project_id": project_id,
            "root": str(root),
            "query": query,
            "mode": mode,
            "mask": self._format_file_search_masks(masks),
            "hits": hits,
            "count": len(hits),
            "status": "ok",
        }

    def _parse_file_search_masks(self, mask: str | None) -> list[str]:
        raw = str(mask or "").strip()
        if not raw or raw in {"*", "*.*", "**/*"}:
            return []
        parts = [part.strip() for part in re.split(r"[,;]+", raw) if part.strip()]
        cleaned: list[str] = []
        for part in parts:
            if part in {"*", "*.*", "**/*"}:
                continue
            cleaned.append(part.replace("\\", "/"))
        return cleaned

    def _format_file_search_masks(self, masks: list[str]) -> str:
        return ", ".join(masks)

    def _path_matches_file_masks(self, relative: str, masks: list[str]) -> bool:
        if not masks:
            return True
        relative = relative.replace("\\", "/")
        name = Path(relative).name
        for pattern in masks:
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relative, pattern):
                return True
            if "/" not in pattern and not pattern.startswith("*"):
                if fnmatch.fnmatch(name, f"*{pattern}*") or fnmatch.fnmatch(relative, f"*{pattern}*"):
                    return True
        return False

    def _search_project_files_by_name(
        self,
        root: Path,
        query: str,
        limit: int,
        masks: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        needle = query.lower()
        masks = masks or []
        hits: list[dict[str, Any]] = []
        for path in self._iter_project_files_for_search(root, max_files=25_000):
            rel = str(path.relative_to(root)).replace("\\", "/")
            if not self._path_matches_file_masks(rel, masks):
                continue
            name = path.name
            if needle and needle not in name.lower() and needle not in rel.lower():
                continue
            hits.append({
                "path": rel,
                "name": name,
                "match": "name",
                "score": (2 if needle and needle in name.lower() else 1) if needle else 1,
                "line": 0,
                "snippet": rel,
            })
            if len(hits) >= limit:
                break
        hits.sort(key=lambda item: (-int(item.get("score") or 0), str(item.get("path") or "")))
        return hits

    def _search_project_files_by_content(
        self,
        root: Path,
        query: str,
        limit: int,
        masks: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        masks = masks or []
        rg_hits = self._search_project_files_by_content_rg(root, query, limit, masks=masks)
        if rg_hits is not None:
            return rg_hits
        return self._search_project_files_by_content_walk(root, query, limit, masks=masks)

    def _search_project_files_by_content_rg(
        self,
        root: Path,
        query: str,
        limit: int,
        masks: list[str] | None = None,
    ) -> list[dict[str, Any]] | None:
        masks = masks or []
        command = [
            "rg",
            "--json",
            "--max-count", "3",
            "--max-filesize", "400K",
            "-i",
            "-F",
            "--glob", "!**/.git/**",
            "--glob", "!**/node_modules/**",
            "--glob", "!**/target/**",
            "--glob", "!**/dist/**",
            "--glob", "!**/build/**",
        ]
        for pattern in masks:
            command.extend(["--glob", pattern])
        command.extend([query, str(root)])
        try:
            proc = subprocess.run(command, text=True, capture_output=True, timeout=12, shell=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode not in {0, 1}:
            return None
        hits: list[dict[str, Any]] = []
        seen: set[str] = set()
        for line in (proc.stdout or "").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            abs_path = Path(str((data.get("path") or {}).get("text") or ""))
            try:
                rel = str(abs_path.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            if any(part in DEFAULT_EXCLUDES for part in Path(rel).parts):
                continue
            if not self._path_matches_file_masks(rel, masks):
                continue
            if rel in seen:
                continue
            lines = data.get("lines") or {}
            text_line = str(lines.get("text") or "").rstrip("\n")
            line_no = int(((data.get("line_number") or 0) or 0))
            snippet = text_line.strip()
            if len(snippet) > 180:
                snippet = snippet[:177] + "…"
            hits.append({
                "path": rel,
                "name": Path(rel).name,
                "match": "content",
                "score": 1,
                "line": line_no,
                "snippet": snippet,
            })
            seen.add(rel)
            if len(hits) >= limit:
                break
        return hits

    def _search_project_files_by_content_walk(
        self,
        root: Path,
        query: str,
        limit: int,
        masks: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        needle = query.lower()
        masks = masks or []
        hits: list[dict[str, Any]] = []
        scanned = 0
        for path in self._iter_project_files_for_search(root, max_files=8_000):
            scanned += 1
            rel = str(path.relative_to(root)).replace("\\", "/")
            if not self._path_matches_file_masks(rel, masks):
                continue
            suffix = path.suffix.lower()
            if suffix and suffix not in TEXT_EXTENSIONS and suffix not in CODE_EXTENSIONS:
                if not self._path_looks_text(path):
                    continue
            try:
                if path.stat().st_size > CODE_FILE_MAX_BYTES:
                    continue
            except OSError:
                continue
            preview = self._read_text_preview(path, max_bytes=120_000)
            if not preview["readable"]:
                continue
            text = str(preview["text"] or "")
            lower = text.lower()
            pos = lower.find(needle)
            if pos < 0:
                continue
            line_no = lower.count("\n", 0, pos) + 1
            line_start = text.rfind("\n", 0, pos) + 1
            line_end = text.find("\n", pos)
            if line_end < 0:
                line_end = min(len(text), pos + 160)
            snippet = text[line_start:line_end].strip()
            if len(snippet) > 180:
                snippet = snippet[:177] + "…"
            hits.append({
                "path": rel,
                "name": path.name,
                "match": "content",
                "score": 1,
                "line": line_no,
                "snippet": snippet,
            })
            if len(hits) >= limit:
                break
        return hits

    def _iter_project_files_for_search(self, root: Path, max_files: int = 10_000):
        count = 0
        for current_root, dir_names, file_names in os.walk(root):
            dir_names[:] = sorted(name for name in dir_names if name not in DEFAULT_EXCLUDES)
            current = Path(current_root)
            for name in sorted(file_names):
                if name in DEFAULT_EXCLUDES:
                    continue
                yield current / name
                count += 1
                if count >= max_files:
                    return

    def project_file(self, project_id: str | None, relative_path: str) -> dict[str, Any]:
        project_id = project_id or "architectos"
        root = self._project_root(project_id)
        path = self._safe_project_path(root, relative_path)
        if not path.is_file():
            raise ValueError("file is not readable by ArchitectOS")
        preview = self._read_text_preview(path)
        clean = str(preview["text"])
        return {
            "project_id": project_id,
            "root": str(root),
            "path": str(path.relative_to(root)),
            "text": clean,
            "readable": bool(preview["readable"]),
            "binary": bool(preview["binary"]),
            "encoding": preview["encoding"],
            "truncated": bool(preview["truncated"]),
            "message": preview["message"],
            "redacted": bool(preview["redacted"]),
            "size": path.stat().st_size,
            "lines": clean.count("\n") + (1 if clean else 0),
            "summary": self._summarize_clean_file(path, root, clean) if preview["readable"] else str(preview["message"]),
        }

    def project_git_diff(self, project_id: str | None = None, relative_path: str | None = None) -> dict[str, Any]:
        project_id = project_id or "architectos"
        root = self._project_root(project_id)
        command = ["git", "-C", str(root), "diff", "--"]
        checked_path = ""
        if relative_path:
            path = self._safe_project_path(root, relative_path)
            checked_path = str(path.relative_to(root))
            command.append(checked_path)
        try:
            proc = subprocess.run(command, text=True, capture_output=True, timeout=8, shell=False)
        except OSError as exc:
            return {"project_id": project_id, "root": str(root), "path": checked_path, "status": "unavailable", "diff": "", "message": f"git diff failed: {exc}"}
        except subprocess.TimeoutExpired:
            return {"project_id": project_id, "root": str(root), "path": checked_path, "status": "timeout", "diff": "", "message": "git diff timed out."}
        diff, diff_redacted = self.security_policy.redact_text((proc.stdout or "")[:80_000])
        stderr, stderr_redacted = self.security_policy.redact_text((proc.stderr or "").strip()[:600])
        return {"project_id": project_id, "root": str(root), "path": checked_path, "status": "ok" if proc.returncode == 0 else "unavailable", "diff": diff, "message": stderr, "redacted": diff_redacted or stderr_redacted}

    # --- Terminal ----------------------------------------------------------

    def terminal_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        root = self._project_root(project_id)
        command = str(payload.get("command") or "").strip()
        if not command:
            raise ValueError("terminal command is required")
        risk = self._terminal_command_risk(command)
        allow_destructive = bool(payload.get("allow_destructive") or payload.get("approved"))
        if risk and not allow_destructive:
            return {
                "project_id": project_id,
                "root": str(root),
                "command": command,
                "status": "blocked",
                "returncode": None,
                "stdout": "",
                "stderr": f"Blocked risky command: {risk}. Enable destructive commands to run it.",
                "duration_ms": 0,
                "redacted": False,
            }
        shell_id = self._terminal_shell_id(str(payload.get("shell") or "auto"))
        shell = self._terminal_shell_command(shell_id)
        timeout = min(max(int(payload.get("timeout_seconds") or 20), 1), 120)
        started = datetime.now(timezone.utc)
        try:
            proc = subprocess.run([*shell, command], cwd=str(root), text=True, capture_output=True, timeout=timeout, shell=False)
            duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        except subprocess.TimeoutExpired as exc:
            stdout, stdout_redacted = self.security_policy.redact_text((exc.stdout or "")[:80_000])
            stderr, stderr_redacted = self.security_policy.redact_text((exc.stderr or "")[:20_000])
            return {
                "project_id": project_id,
                "root": str(root),
                "command": command,
                "shell": shell_id,
                "status": "timeout",
                "returncode": None,
                "stdout": stdout,
                "stderr": stderr or f"Command timed out after {timeout}s.",
                "duration_ms": timeout * 1000,
                "redacted": stdout_redacted or stderr_redacted,
            }
        except OSError as exc:
            return {
                "project_id": project_id,
                "root": str(root),
                "command": command,
                "shell": shell_id,
                "status": "unavailable",
                "returncode": None,
                "stdout": "",
                "stderr": f"Terminal shell failed: {exc}",
                "duration_ms": 0,
                "redacted": False,
            }
        stdout, stdout_redacted = self.security_policy.redact_text((proc.stdout or "")[:80_000])
        stderr, stderr_redacted = self.security_policy.redact_text((proc.stderr or "")[:20_000])
        return {
            "project_id": project_id,
            "root": str(root),
            "command": command,
            "shell": shell_id,
            "status": "ok" if proc.returncode == 0 else "error",
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": duration_ms,
            "redacted": stdout_redacted or stderr_redacted,
        }

    def terminal_open(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        root = self._project_root(project_id)
        commands = self._terminal_open_commands(root)
        errors = []
        for command in commands:
            try:
                subprocess.Popen(command, cwd=str(root), shell=False)
                return {"project_id": project_id, "root": str(root), "status": "opened", "command": command}
            except OSError as exc:
                errors.append(str(exc))
        return {"project_id": project_id, "root": str(root), "status": "unavailable", "message": "; ".join(errors[-3:]) or "No terminal launcher found."}

    def system_folder_picker(self, payload: dict[str, Any]) -> dict[str, Any]:
        initial_path = Path(str(payload.get("initial_path") or self.project_root)).expanduser()
        if not initial_path.exists() or not initial_path.is_dir():
            initial_path = self.project_root
        from .folder_picker import pick_folder_subprocess

        return pick_folder_subprocess(str(initial_path))

    def _terminal_shell_id(self, requested: str) -> str:
        requested = requested.lower().strip()
        if requested in TERMINAL_SHELLS:
            return requested
        if os.name == "nt":
            return "cmd" if shutil.which("cmd.exe") else "powershell"
        return "bash" if shutil.which("bash") else "sh"

    def _terminal_shell_command(self, shell_id: str) -> list[str]:
        command = TERMINAL_SHELLS.get(shell_id) or TERMINAL_SHELLS[self._terminal_shell_id("auto")]
        executable = shutil.which(command[0])
        if not executable:
            raise OSError(f"shell executable not found: {command[0]}")
        return [executable, *command[1:]]

    def _terminal_command_risk(self, command: str) -> str:
        normalized = command.strip()
        for pattern in TERMINAL_DANGEROUS_PATTERNS:
            match = pattern.search(normalized)
            if match:
                return match.group(0)
        return ""

    def _terminal_open_commands(self, root: Path) -> list[list[str]]:
        if os.name == "nt":
            commands = []
            wt = shutil.which("wt.exe") or shutil.which("wt")
            if wt:
                commands.append([wt, "-d", str(root)])
            powershell = shutil.which("powershell.exe")
            if powershell:
                literal_root = str(root).replace("'", "''")
                commands.append([powershell, "-NoLogo", "-NoExit", "-Command", f"Set-Location -LiteralPath '{literal_root}'"])
            cmd = shutil.which("cmd.exe")
            if cmd:
                commands.append([cmd, "/k", f"cd /d {root}"])
            return commands
        candidates = [
            ("x-terminal-emulator", ["-e", "sh", "-lc", f"cd {shlex.quote(str(root))}; exec $SHELL"]),
            ("gnome-terminal", ["--working-directory", str(root)]),
            ("konsole", ["--workdir", str(root)]),
            ("open", ["-a", "Terminal", str(root)]),
        ]
        return [[path, *args] for executable, args in candidates if (path := shutil.which(executable))]

    def selected_file_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        query = str(payload.get("query") or "selected file context").strip()
        paths = [str(item) for item in (payload.get("paths") or []) if str(item).strip()]
        if not paths and payload.get("path"):
            paths = [str(payload["path"])]
        if not paths:
            raise ValueError("at least one selected file path is required")
        files = [self.project_file(project_id, item) for item in paths[:6]]
        memory_context = self.context(query, project_id=project_id, limit=int(payload.get("limit") or 6))
        diff = self.project_git_diff(project_id)
        lines = [
            "ArchitectOS Selected File Context",
            f"Project: {project_id}",
            f"Query: {query}",
            "",
            "Selected Files:",
        ]
        for item in files:
            lines.append(f"- {item['path']} ({item['lines']} lines, {item['size']} bytes)")
        lines.extend(["", memory_context["context"], "", "File Contents:"])
        for item in files:
            content = item["text"][:12000] if item.get("readable", True) else item.get("message", "Preview unavailable.")
            lines.append(f"\n--- {item['path']} ---\n{content}")
        if diff.get("diff"):
            lines.append(f"\nGit Diff:\n{diff['diff'][:20000]}")
        return {"project_id": project_id, "query": query, "files": files, "git_diff": diff, "context": "\n".join(lines).strip()}

    def _project_root(self, project_id: str) -> Path:
        project = self.repository.get_project(project_id)
        if project and project.root_path:
            root = self._validate_project_root(str(project.root_path))
        elif project and self._is_architectos_source_root():
            raise ValueError(f"{project.name} has no project folder. Select a folder before indexing files.")
        else:
            root = self._validate_project_root(str(self.project_root))
        if project:
            self._ensure_project_memory_root(project)
        return root

    def _safe_project_path(self, root: Path, relative_path: str) -> Path:
        raw = str(relative_path or "").strip()
        if not raw:
            raise ValueError("file path is required")
        candidate = (root / raw).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            raise ValueError("file path is outside project root") from None
        return candidate

    def scan_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        reindex = bool(payload.get("reindex") or payload.get("reset") or payload.get("rebuild"))
        project = self.repository.get_project(project_id)
        if str(payload.get("root_path") or "").strip():
            project, _created = self._upsert_project_profile({
                "name": payload.get("name") or (project.name if project else ""),
                "root_path": payload.get("root_path"),
                "description": payload.get("description") or (project.description if project else ""),
            })
            project_id = project.id
        elif project_id == SYSTEM_PROJECT_ID and self._is_architectos_source_root():
            raise ValueError("Choose or create a project folder before scanning memory.")
        root = self._project_root(project_id)
        project = self.repository.get_project(project_id)
        exclude_patterns = list((project.config or {}).get("ignore_patterns") or []) if project else []
        imported = []
        imported_ids: set[str] = set()
        for path in self._iter_importable_files(root, int(payload.get("limit") or 25), exclude_patterns):
            relative = str(path.relative_to(root))
            node = self.repository.add_node("Doc" if path.suffix.lower() in {".md", ".txt", ".rst"} else "Artifact", relative, "project", self._summarize_file(path, root), project_id, confidence=0.72, metadata={"source": "project_scan", "path": relative, "project_root": str(root)})
            node = self.memory_lifecycle.initialize_node(node, "project_scan")
            self.graph_auto_linker.link_node(node, project_id)
            imported.append(node.to_dict())
            imported_ids.add(node.id)
        archived = 0
        if reindex:
            archived = self._archive_stale_scan_nodes(project_id, imported_ids)
        links = self.graph_auto_linker.rebuild(project_id) if (reindex or bool(payload.get("rebuild_links"))) else {"created": 0, "linked_nodes": 0, "edges": []}
        return {
            "project_id": project_id,
            "project": self.repository.get_project(project_id).to_dict(),
            "root": str(root),
            "imported": imported,
            "count": len(imported),
            "archived": archived,
            "links": links,
            "mode": "reindex" if reindex else "scan",
        }

    def _archive_stale_scan_nodes(self, project_id: str, imported_ids: set[str]) -> int:
        archived = 0
        for node in self.repository.list_nodes():
            metadata = dict(node.metadata or {})
            if node.project_id != project_id or metadata.get("source") != "project_scan" or node.id in imported_ids or node.status != "active":
                continue
            node.status = "archived"
            metadata["archived_at"] = utc_now()
            metadata["archive_reason"] = "project_reindex_missing_source"
            node.metadata = metadata
            self.repository.upsert_node(node)
            archived += 1
        return archived

    def export_bundle(self, project_id: str) -> dict[str, Any]:
        return self.repository.export_bundle(project_id or "architectos")

    def import_bundle(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.repository.import_bundle(payload)

    def _local_assistant_response(self, message: str, hits: list[dict[str, Any]]) -> str:
        if not hits:
            return "Local context is ready, but I found no matching memory yet. Add memory or scan the project, then ask again."
        labels = ", ".join(hit["node"]["label"] for hit in hits[:3])
        return f"Local context is ready for: {message}. Strongest memory matches: {labels}. Use the Context tab for the full pack."


    def _normalize_ingestion_sources(self, sources: Any) -> list[str]:
        normalized: list[str] = []
        for source in sources:
            item = INGESTION_SOURCE_ALIASES.get(str(source).strip().lower(), str(source).strip().lower())
            if item and item not in normalized:
                normalized.append(item)
        return normalized or ["docs", "code", "chat", "git"]

    def _ingest_file_candidates(self, project_id: str, root: Path, sources: list[str], limit: int) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        walk_limit = max(limit * 12, 120) if "code" in sources else max(limit * 3, 24)
        for path in self._iter_importable_files(root, walk_limit):
            preview = self._read_text_preview(path)
            if not preview["readable"]:
                continue
            clean = str(preview["text"])
            relative = str(path.relative_to(root))
            specialized = self._specialized_file_candidate(project_id, root, path, clean, sources)
            if specialized:
                candidates.append(specialized)
            else:
                source_type = self._generic_file_source(path)
                if source_type not in sources:
                    continue
                candidates.append({
                    "id": stable_id("candidate", project_id, source_type, relative, str(path.stat().st_mtime_ns)),
                    "project_id": project_id,
                    "source_type": source_type,
                    "source_ref": relative,
                    "label": f"{'Doc' if source_type == 'docs' else 'Code'}: {relative}",
                    "type": "Doc" if source_type == "docs" else "Artifact",
                    "scope": "project",
                    "text": self._summarize_clean_file(path, root, clean),
                    "confidence": 0.68 if source_type == "docs" else 0.6,
                    "metadata": {
                        "path": str(path),
                        "size": path.stat().st_size,
                        "extension": path.suffix.lower(),
                        "template": "generic",
                    },
                })
            if len(candidates) >= limit:
                break
        return candidates

    def _specialized_file_candidate(self, project_id: str, root: Path, path: Path, clean: str, sources: list[str]) -> dict[str, Any] | None:
        relative = str(path.relative_to(root))
        detector_order = [
            ("adr", self._looks_like_adr, self._build_adr_candidate),
            ("issues", self._looks_like_issue, self._build_issue_candidate),
            ("prs", self._looks_like_pr, self._build_pr_candidate),
            ("meetings", self._looks_like_meeting, self._build_meeting_candidate),
        ]
        for source, detector, builder in detector_order:
            if source in sources and detector(path, relative, clean):
                return builder(project_id, root, path, clean)
        return None

    def _generic_file_source(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in DOC_EXTENSIONS:
            return "docs"
        if suffix in CODE_EXTENSIONS:
            return "code"
        # Unknown but readable text still counts as code when scanning code sources.
        return "code"

    def _build_adr_candidate(self, project_id: str, root: Path, path: Path, clean: str) -> dict[str, Any]:
        relative = str(path.relative_to(root))
        title = self._markdown_title(clean, path)
        status = self._section_excerpt(clean, ["status"], 260)
        context = self._section_excerpt(clean, ["context", "problem"], 520)
        decision = self._section_excerpt(clean, ["decision", "decisions"], 700)
        consequences = self._section_excerpt(clean, ["consequences", "tradeoffs", "trade-offs"], 520)
        parts = [f"ADR: {title}"]
        if status:
            parts.append(f"Status: {status}")
        if context:
            parts.append(f"Context: {context}")
        if decision:
            parts.append(f"Decision: {decision}")
        if consequences:
            parts.append(f"Consequences: {consequences}")
        return self._candidate(project_id, "adr", relative, f"ADR: {title}", "Decision", "\n\n".join(parts), 0.82, path, {"template": "adr", "sections": ["status", "context", "decision", "consequences"]})

    def _build_issue_candidate(self, project_id: str, root: Path, path: Path, clean: str) -> dict[str, Any]:
        relative = str(path.relative_to(root))
        title = self._markdown_title(clean, path)
        summary = self._section_excerpt(clean, ["summary", "description", "problem"], 650) or self._first_lines(clean, 18)
        acceptance = self._section_excerpt(clean, ["acceptance criteria", "expected behavior", "expected"], 520)
        text = f"Issue tracker item: {title}\n\nSummary: {summary}"
        if acceptance:
            text += f"\n\nAcceptance/Expected: {acceptance}"
        return self._candidate(project_id, "issue", relative, f"Issue: {title}", "Requirement", text, 0.72, path, {"template": "issue"})

    def _build_pr_candidate(self, project_id: str, root: Path, path: Path, clean: str) -> dict[str, Any]:
        relative = str(path.relative_to(root))
        title = self._markdown_title(clean, path)
        summary = self._section_excerpt(clean, ["summary", "what changed", "changes"], 680) or self._first_lines(clean, 18)
        testing = self._section_excerpt(clean, ["testing", "test plan", "validation"], 460)
        text = f"Pull request memory candidate: {title}\n\nSummary: {summary}"
        if testing:
            text += f"\n\nTesting: {testing}"
        return self._candidate(project_id, "pr", relative, f"PR: {title}", "Artifact", text, 0.68, path, {"template": "pr"})

    def _build_meeting_candidate(self, project_id: str, root: Path, path: Path, clean: str) -> dict[str, Any]:
        relative = str(path.relative_to(root))
        title = self._markdown_title(clean, path)
        decisions = self._section_excerpt(clean, ["decisions", "decision"], 520)
        actions = self._section_excerpt(clean, ["action items", "actions", "next steps"], 640)
        notes = self._section_excerpt(clean, ["notes", "discussion"], 640) or self._first_lines(clean, 20)
        text = f"Meeting notes: {title}\n\nNotes: {notes}"
        if decisions:
            text += f"\n\nDecisions: {decisions}"
        if actions:
            text += f"\n\nAction Items: {actions}"
        return self._candidate(project_id, "meeting", relative, f"Meeting: {title}", "Meeting", text, 0.7, path, {"template": "meeting"})

    def _candidate(self, project_id: str, source_type: str, source_ref: str, label: str, node_type: str, text: str, confidence: float, path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "id": stable_id("candidate", project_id, source_type, source_ref, text[:500]),
            "project_id": project_id,
            "source_type": source_type,
            "source_ref": source_ref,
            "label": label,
            "type": node_type,
            "scope": "project",
            "text": text[:3000],
            "confidence": confidence,
            "metadata": {"path": str(path), "size": path.stat().st_size, **metadata},
        }
        return payload

    def _looks_like_adr(self, path: Path, relative: str, clean: str) -> bool:
        needle = relative.replace("\\", "/").lower()
        return (
            "/adr/" in f"/{needle}"
            or "/adrs/" in f"/{needle}"
            or "architecture/decision" in needle
            or path.name.lower().startswith(("adr-", "adr_"))
            or ("## status" in clean.lower() and "## decision" in clean.lower() and "## context" in clean.lower())
        )

    def _looks_like_issue(self, path: Path, relative: str, clean: str) -> bool:
        needle = relative.replace("\\", "/").lower()
        body = clean.lower()
        return (
            "/issues/" in f"/{needle}"
            or ".github/issue_template" in needle
            or path.name.lower().startswith(("issue-", "bug-", "ticket-"))
            or "acceptance criteria" in body
            or ("expected behavior" in body and "actual behavior" in body)
        )

    def _looks_like_pr(self, path: Path, relative: str, clean: str) -> bool:
        needle = relative.replace("\\", "/").lower()
        body = clean.lower()
        return (
            "pull_request_template" in needle
            or "/prs/" in f"/{needle}"
            or "/pull-requests/" in f"/{needle}"
            or path.name.lower().startswith(("pr-", "pull-request-"))
            or ("## summary" in body and ("## testing" in body or "test plan" in body))
        )

    def _looks_like_meeting(self, path: Path, relative: str, clean: str) -> bool:
        needle = relative.replace("\\", "/").lower()
        body = clean.lower()
        return (
            "/meetings/" in f"/{needle}"
            or "/minutes/" in f"/{needle}"
            or path.stem.lower().startswith(("meeting-", "minutes-", "standup-", "retro-"))
            or ("attendees" in body and ("action items" in body or "next steps" in body))
        )

    def _markdown_title(self, clean: str, path: Path) -> str:
        for line in clean.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip()[:140] or path.stem
        return path.stem.replace("_", " ").replace("-", " ").strip().title()

    def _section_excerpt(self, clean: str, names: list[str], limit: int) -> str:
        lines = clean.splitlines()
        collected: list[str] = []
        in_section = False
        wanted = {name.lower() for name in names}
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                heading = stripped.lstrip("#").strip().lower().rstrip(":")
                if in_section and heading not in wanted:
                    break
                in_section = heading in wanted
                continue
            if in_section and stripped:
                collected.append(stripped)
        return " ".join(collected)[:limit]

    def _first_lines(self, clean: str, limit: int) -> str:
        return "\n".join(line.strip() for line in clean.splitlines() if line.strip())[:limit * 120]

    def _ingest_chat_candidates(self, project_id: str, limit: int) -> list[dict[str, Any]]:
        candidates = []
        for chat in self.repository.list_chats(project_id)[:limit]:
            messages = [message for message in chat.get("messages", []) if str(message.get("text") or "").strip()]
            if not messages:
                continue
            text = "\n".join(f"{message.get('role', 'message')}: {str(message.get('text') or '').strip()}" for message in messages[-6:])
            candidates.append({
                "id": stable_id("candidate", project_id, "chat", chat["id"], text[:500]),
                "project_id": project_id,
                "source_type": "chat",
                "source_ref": chat["id"],
                "label": f"Chat: {chat.get('title') or chat['id']}",
                "type": "Lesson",
                "scope": "project",
                "text": f"Chat memory candidate from {chat.get('title') or chat['id']}.\n\n{text}",
                "confidence": 0.58,
                "metadata": {"chat_id": chat["id"], "messages": len(messages), "template": "chat"},
            })
        return candidates

    def _ingest_granola_candidates(self, project_id: str, limit: int, payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        payload = payload or {}
        if "_ingest_source_deadline" not in payload:
            payload = self._with_ingest_timeouts("granola", payload)
        item_timeout = self._ingest_item_timeout(payload, 30)
        listed = self.mcp_manager.call_tool("granola", "list_meetings", {}, timeout=item_timeout)
        meetings = self._granola_meetings_from_tool_result(listed.get("result") or listed)
        if not meetings:
            fallback = self._granola_text_from_tool_result(listed.get("result") or listed)
            if not fallback:
                return []
            return [self._granola_text_candidate(project_id, "recent-meetings", "Recent Granola meetings", fallback)]
        meetings = meetings[:limit]
        if self._ingest_deadline_expired(payload, "granola"):
            return []
        detailed = self._granola_meeting_details(meetings, item_timeout=item_timeout, payload=payload)
        if detailed:
            by_id = {self._granola_meeting_id(item): item for item in detailed if self._granola_meeting_id(item)}
            meetings = [self._merge_granola_meeting(item, by_id.get(self._granola_meeting_id(item))) for item in meetings]
        if not self._ingest_deadline_expired(payload, "granola"):
            self._granola_attach_transcripts(meetings, item_timeout=item_timeout, payload=payload)
        candidates = []
        thin = 0
        for index, meeting in enumerate(meetings):
            if self._ingest_deadline_expired(payload, "granola"):
                break
            candidate = self._granola_meeting_candidate(project_id, meeting, index)
            if candidate:
                if len(str(candidate.get("text") or "")) < 220:
                    thin += 1
                candidates.append(candidate)
        if thin and candidates:
            self._log_ingest(
                f"Granola: {thin}/{len(candidates)} meeting(s) still thin after get_meetings — notes/summary may be empty in Granola MCP",
                level="warn",
                source="granola",
            )
        return candidates

    def _merge_granola_meeting(self, base: dict[str, Any], detailed: dict[str, Any] | None) -> dict[str, Any]:
        if not detailed:
            return dict(base)
        merged = dict(base)
        for key, value in detailed.items():
            if value in (None, "", [], {}):
                continue
            if key in merged and merged.get(key) not in (None, "", [], {}) and key in {"id", "title", "date", "start_time"}:
                continue
            merged[key] = value
        return merged

    def _granola_meeting_details(
        self,
        meetings: list[dict[str, Any]],
        *,
        item_timeout: float = 30.0,
        payload: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        ids = [meeting_id for meeting_id in (self._granola_meeting_id(item) for item in meetings) if meeting_id]
        if not ids:
            return []
        # Official Granola MCP: get_meetings.meeting_ids maxItems = 10.
        detailed: list[dict[str, Any]] = []
        chunk_size = 10
        for offset in range(0, len(ids), chunk_size):
            if self._ingest_deadline_expired(payload, "granola"):
                break
            chunk = ids[offset : offset + chunk_size]
            page = self._granola_fetch_meeting_details_chunk(chunk, item_timeout=item_timeout)
            if page:
                detailed.extend(page)
            else:
                # Fall back to one-by-one when batch chunk fails.
                for meeting_id in chunk:
                    if self._ingest_deadline_expired(payload, "granola"):
                        break
                    page = self._granola_fetch_meeting_details_chunk([meeting_id], item_timeout=item_timeout)
                    if page:
                        detailed.extend(page)
        return detailed

    def _granola_fetch_meeting_details_chunk(
        self,
        meeting_ids: list[str],
        *,
        item_timeout: float = 30.0,
    ) -> list[dict[str, Any]]:
        for arguments in (
            {"meeting_ids": meeting_ids},
            {"ids": meeting_ids},
            {"meeting_id": meeting_ids[0]} if len(meeting_ids) == 1 else None,
            {"id": meeting_ids[0]} if len(meeting_ids) == 1 else None,
        ):
            if not arguments:
                continue
            try:
                result = self.mcp_manager.call_tool("granola", "get_meetings", arguments, timeout=item_timeout)
            except MCPError:
                continue
            detailed = self._granola_meetings_from_tool_result(result.get("result") or result)
            if detailed:
                return detailed
        return []

    def _granola_attach_transcripts(
        self,
        meetings: list[dict[str, Any]],
        *,
        item_timeout: float = 30.0,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Paid-plan enrichment: pull transcripts when notes/summary are still thin."""
        for meeting in meetings:
            if self._ingest_deadline_expired(payload, "granola"):
                return
            meeting_id = self._granola_meeting_id(meeting)
            if not meeting_id:
                continue
            if self._granola_meeting_body_richness(meeting) >= 280:
                continue
            if str(meeting.get("transcript") or "").strip():
                continue
            for arguments in ({"meeting_id": meeting_id}, {"id": meeting_id}, {"meeting_ids": [meeting_id]}):
                try:
                    result = self.mcp_manager.call_tool(
                        "granola",
                        "get_meeting_transcript",
                        arguments,
                        timeout=item_timeout,
                    )
                except MCPError:
                    continue
                text = self._granola_text_from_tool_result(result.get("result") or result)
                transcript = self._first_granola_value(
                    {"transcript": text, **(result.get("result") if isinstance(result.get("result"), dict) else {})},
                    ["transcript", "text", "content"],
                ) or text
                if transcript.strip():
                    meeting["transcript"] = transcript.strip()
                    break

    def _granola_meeting_body_richness(self, meeting: dict[str, Any]) -> int:
        chunks = [
            self._first_granola_value(meeting, ["summary", "ai_summary", "overview", "description", "summarized_notes", "enhanced_notes", "notes_markdown"]),
            self._first_granola_value(meeting, ["private_notes", "notes", "note", "content", "text"]),
            self._granola_joined_value(meeting.get("decisions") or meeting.get("decision")),
            self._granola_joined_value(meeting.get("action_items") or meeting.get("actions") or meeting.get("next_steps") or meeting.get("todos")),
        ]
        return sum(len(chunk) for chunk in chunks if chunk)

    def _granola_meeting_candidate(self, project_id: str, meeting: dict[str, Any], index: int) -> dict[str, Any] | None:
        meeting_id = self._granola_meeting_id(meeting) or f"meeting-{index}"
        title = self._first_granola_value(meeting, ["title", "name", "summary_title"], max_len=300) or f"Granola meeting {index + 1}"
        started_at = self._first_granola_value(meeting, ["start_time", "started_at", "created_at", "date", "updated_at"], max_len=120)
        attendees = self._granola_joined_value(meeting.get("attendees") or meeting.get("participants") or meeting.get("people") or meeting.get("known_participants"))
        summary = self._first_granola_value(
            meeting,
            ["summary", "ai_summary", "overview", "description", "summarized_notes", "enhanced_notes", "meeting_summary", "notes_markdown"],
            max_len=10000,
        )
        private_notes = self._first_granola_value(meeting, ["private_notes", "notes", "note", "content", "text", "body"], max_len=8000)
        decisions = self._granola_joined_value(meeting.get("decisions") or meeting.get("decision"))
        actions = self._granola_joined_value(meeting.get("action_items") or meeting.get("actions") or meeting.get("next_steps") or meeting.get("todos"))
        transcript = self._first_granola_value(meeting, ["transcript", "raw_transcript"], max_len=12000)
        parts = [f"Granola meeting: {title}"]
        if started_at:
            parts.append(f"Date: {started_at}")
        if attendees:
            parts.append(f"Attendees: {attendees}")
        if summary:
            parts.append(f"Summary:\n{summary}")
        if private_notes and private_notes.strip() != summary.strip():
            parts.append(f"Notes:\n{private_notes}")
        if decisions:
            parts.append(f"Decisions:\n{decisions}")
        if actions:
            parts.append(f"Action Items:\n{actions}")
        if transcript:
            # Keep a useful excerpt even when summary exists — often more concrete than the AI stub.
            excerpt_limit = 3500 if self._granola_meeting_body_richness(meeting) < 280 else 1600
            parts.append(f"Transcript excerpt:\n{transcript[:excerpt_limit]}")
        text = "\n\n".join(parts).strip()
        if len(text) < 4:
            return None
        source_ref = str(meeting.get("url") or meeting.get("href") or meeting_id)
        richness = self._granola_meeting_body_richness(meeting) + min(len(transcript), 800)
        confidence = 0.86 if richness >= 400 else 0.74 if richness >= 180 else 0.58
        return {
            "id": stable_id("candidate", project_id, "granola", meeting_id, text[:500]),
            "project_id": project_id,
            "source_type": "granola",
            "source_ref": source_ref,
            "label": f"Granola: {title}"[:180],
            "type": "Meeting",
            "scope": "project",
            "text": text[:12000],
            "confidence": confidence,
            "metadata": {
                "template": "granola_meeting",
                "meeting_id": meeting_id,
                "meeting_title": title,
                "meeting_date": started_at,
                "granola_url": str(meeting.get("url") or meeting.get("href") or ""),
                "attendees": attendees,
                "has_summary": bool(summary),
                "has_notes": bool(private_notes),
                "has_transcript": bool(transcript),
                "richness": richness,
                "source": "granola_mcp",
            },
        }

    def _granola_text_candidate(self, project_id: str, source_ref: str, title: str, text: str) -> dict[str, Any]:
        body = f"Granola meeting import: {title}\n\n{text}"
        return {
            "id": stable_id("candidate", project_id, "granola", source_ref, body[:500]),
            "project_id": project_id,
            "source_type": "granola",
            "source_ref": source_ref,
            "label": f"Granola: {title}"[:180],
            "type": "Meeting",
            "scope": "project",
            "text": body[:3000],
            "confidence": 0.58,
            "metadata": {"template": "granola_text", "source": "granola_mcp"},
        }

    def _granola_meetings_from_tool_result(self, result: Any) -> list[dict[str, Any]]:
        meetings: list[dict[str, Any]] = []
        for payload in self._granola_payloads(result):
            meetings.extend(self._granola_meetings_from_payload(payload))
        return meetings

    def _granola_meetings_from_payload(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [dict(item) for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("meetings", "items", "results", "data", "notes"):
            value = payload.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = self._granola_meetings_from_payload(value)
                if nested:
                    return nested
        if any(key in payload for key in ("id", "meeting_id", "title", "name", "summary", "notes")):
            return [payload]
        return []

    def _granola_payloads(self, result: Any) -> list[Any]:
        payloads: list[Any] = []
        if isinstance(result, dict):
            if "structuredContent" in result:
                payloads.append(result["structuredContent"])
            content = result.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        payloads.extend(self._granola_payloads_from_text(str(item.get("text") or "")))
                    elif isinstance(item, dict):
                        payloads.append(item)
            payloads.append(result)
        elif isinstance(result, list):
            payloads.extend(result)
        elif isinstance(result, str):
            payloads.extend(self._granola_payloads_from_text(result))
        return payloads

    def _granola_payloads_from_text(self, text: str) -> list[Any]:
        stripped = text.strip()
        if not stripped:
            return []
        xml_meetings = self._granola_meetings_from_xml(stripped)
        if xml_meetings:
            return [{"meetings": xml_meetings}]
        try:
            return [json.loads(stripped)]
        except json.JSONDecodeError:
            return [{"text": stripped}]

    def _granola_meetings_from_xml(self, text: str) -> list[dict[str, Any]]:
        """Parse Granola MCP list/get_meetings XML payload into meeting dicts."""
        if "<meeting" not in text:
            return []
        meetings: list[dict[str, Any]] = []
        pattern = re.compile(
            r'<meeting\b([^>]*)>(.*?)</meeting>',
            re.IGNORECASE | re.DOTALL,
        )
        attr_pattern = re.compile(r'([A-Za-z_:][\w:.-]*)\s*=\s*"([^"]*)"')
        for match in pattern.finditer(text):
            attrs = {key.lower(): value for key, value in attr_pattern.findall(match.group(1) or "")}
            body = match.group(2) or ""
            meeting: dict[str, Any] = {
                "id": attrs.get("id") or "",
                "title": attrs.get("title") or attrs.get("name") or "",
                "date": attrs.get("date") or attrs.get("start_time") or "",
                "start_time": attrs.get("date") or attrs.get("start_time") or "",
                "url": attrs.get("url") or attrs.get("href") or "",
            }
            participants = self._granola_xml_tag_text(body, "known_participants") or self._granola_xml_tag_text(body, "participants")
            if participants:
                meeting["attendees"] = [line.strip(" -•\t") for line in participants.splitlines() if line.strip()]
            summary = self._granola_xml_tag_text(body, "summary") or self._granola_xml_tag_text(body, "summarized_notes") or self._granola_xml_tag_text(body, "enhanced_notes")
            if summary:
                meeting["summary"] = summary
            notes = (
                self._granola_xml_tag_text(body, "private_notes")
                or self._granola_xml_tag_text(body, "notes")
                or self._granola_xml_tag_text(body, "notes_markdown")
            )
            if notes:
                meeting["private_notes"] = notes
            decisions = self._granola_xml_tag_text(body, "decisions")
            if decisions:
                meeting["decisions"] = [line.strip(" -•\t") for line in decisions.splitlines() if line.strip()]
            actions = self._granola_xml_tag_text(body, "action_items") or self._granola_xml_tag_text(body, "actions")
            if actions:
                meeting["action_items"] = [line.strip(" -•\t") for line in actions.splitlines() if line.strip()]
            transcript = self._granola_xml_tag_text(body, "transcript") or self._granola_xml_tag_text(body, "raw_transcript")
            if transcript:
                meeting["transcript"] = transcript
            if meeting["id"] or meeting["title"]:
                meetings.append(meeting)
        return meetings

    def _granola_xml_tag_text(self, body: str, tag: str) -> str:
        match = re.search(
            rf"<{tag}\b[^>]*>(.*?)</{tag}>",
            body,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return ""
        return re.sub(r"\s+\n", "\n", match.group(1)).strip()

    def _granola_text_from_tool_result(self, result: Any) -> str:
        chunks: list[str] = []
        if isinstance(result, dict):
            for item in result.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "text" and str(item.get("text") or "").strip():
                    chunks.append(str(item.get("text")).strip())
            for key in ("text", "summary", "notes"):
                if str(result.get(key) or "").strip():
                    chunks.append(str(result.get(key)).strip())
        elif isinstance(result, str):
            chunks.append(result.strip())
        return "\n\n".join(dict.fromkeys(chunk for chunk in chunks if chunk))

    def _granola_meeting_id(self, meeting: dict[str, Any]) -> str:
        return str(meeting.get("id") or meeting.get("meeting_id") or meeting.get("note_id") or meeting.get("document_id") or "").strip()

    def _first_granola_value(self, payload: dict[str, Any], keys: list[str], *, max_len: int = 12000) -> str:
        # Keep body fields (summary/notes/transcript) usable for memory; callers may pass a smaller max_len for titles.
        for key in keys:
            value = payload.get(key)
            if isinstance(value, (dict, list)):
                value = self._granola_joined_value(value)
            if str(value or "").strip():
                text = str(value).strip()
                return text[:max_len] if max_len > 0 else text
        return ""

    def _granola_joined_value(self, value: Any) -> str:
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, dict):
                    parts.append(str(item.get("name") or item.get("email") or item.get("text") or item.get("title") or item))
                else:
                    parts.append(str(item))
            return "; ".join(part.strip() for part in parts if part.strip())[:1400]
        if isinstance(value, dict):
            return "; ".join(f"{key}: {item}" for key, item in value.items() if str(item).strip())[:1400]
        return str(value or "").strip()[:1400]

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
        result = self.mcp_manager.call_tool(
            server_id,
            "repo_list_repos_by_project",
            {"project": ado_project},
            timeout=timeout or 30,
        )
        tool_result = self._azure_mcp_require_success(result, "repo_list_repos_by_project")
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
        result = self.mcp_manager.call_tool(
            server_id,
            "repo_list_pull_requests_by_repo_or_project",
            arguments,
            timeout=timeout or 30,
        )
        tool_result = self._azure_mcp_require_success(result, "repo_list_pull_requests_by_repo_or_project")
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
        if isinstance(payload, dict) and payload.get("isError"):
            message = ""
            content = payload.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        message = str(item.get("text") or "").strip()
                        if message:
                            break
            if not message:
                message = str(payload.get("message") or payload.get("error") or "").strip()
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
            result = self.mcp_manager.call_tool("azure-devops", "wiki_list_wikis", {"project": ado_project})
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
            result = self.mcp_manager.call_tool(
                "azure-devops",
                "wiki_list_pages",
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
            content_result = self.mcp_manager.call_tool(
                "azure-devops",
                "wiki_get_page_content",
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

        all_items = bool(payload.get("all_items", False))
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
            result = self.mcp_manager.call_tool(
                "azure-devops",
                "wit_query_by_wiql",
                {
                    "wiql": f"SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = '{ado_project}'{type_clause} ORDER BY [System.ChangedDate] DESC",
                    "project": ado_project
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
            result = self.mcp_manager.call_tool(
                "azure-devops",
                "wit_my_work_items",
                {"project": ado_project, "type": "assignedtome", "top": top, "includeCompleted": True},
                timeout=timeout or ADO_ITEM_TIMEOUT,
            )
        except MCPError as exc:
            self._log_ingest(f"wit_my_work_items failed: {exc}", level="warn", source="azure-boards", current="azure-boards")
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
                result = self.mcp_manager.call_tool(
                    "azure-devops",
                    "search_workitem",
                    {
                        "searchText": search_text,
                        "project": [ado_project],
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
            detailed = self.mcp_manager.call_tool(
                "azure-devops",
                "wit_get_work_item",
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
                comments_result = self.mcp_manager.call_tool(
                    "azure-devops",
                    "wit_list_work_item_comments",
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

    def _ingest_git_candidates(self, project_id: str, root: Path, limit: int) -> list[dict[str, Any]]:
        try:
            proc = subprocess.run(["git", "-C", str(root), "log", "--pretty=format:%h%x09%s", "-n", str(min(max(limit * 4, 20), 80))], text=True, capture_output=True, timeout=5, shell=False)
        except OSError:
            return []
        except subprocess.TimeoutExpired:
            return []
        output = (proc.stdout or "").strip()
        if proc.returncode != 0 or not output:
            return []
        commits = []
        for line in output.splitlines():
            if "\t" in line:
                commit_hash, subject = line.split("\t", 1)
            else:
                parts = line.split(" ", 1)
                commit_hash, subject = parts[0], parts[1] if len(parts) > 1 else line
            commits.append((commit_hash.strip(), subject.strip()))
        candidates = [{
            "id": stable_id("candidate", project_id, "git", root.as_posix(), output[:1000]),
            "project_id": project_id,
            "source_type": "git",
            "source_ref": str(root),
            "label": "Git: recent commit history",
            "type": "Decision",
            "scope": "project",
            "text": "Recent git history worth reviewing for durable memory:\n\n" + "\n".join(f"{item[0]} {item[1]}" for item in commits)[:2500],
            "confidence": 0.55,
            "metadata": {"root": str(root), "commits": len(commits), "template": "git_history"},
        }]
        candidates.extend(self._build_git_cluster_candidates(project_id, root, commits, limit))
        return candidates[:limit]

    def _build_git_cluster_candidates(self, project_id: str, root: Path, commits: list[tuple[str, str]], limit: int) -> list[dict[str, Any]]:
        clusters: dict[str, list[tuple[str, str]]] = {}
        for commit_hash, subject in commits:
            cluster = self._commit_cluster(subject)
            clusters.setdefault(cluster, []).append((commit_hash, subject))
        candidates: list[dict[str, Any]] = []
        for cluster, items in sorted(clusters.items(), key=lambda item: (-len(item[1]), item[0])):
            if len(items) < 2 and len(clusters) > 1:
                continue
            lines = [f"- {commit_hash} {subject}" for commit_hash, subject in items[:12]]
            candidates.append({
                "id": stable_id("candidate", project_id, "git_cluster", root.as_posix(), cluster, "|".join(subject for _commit_hash, subject in items[:12])),
                "project_id": project_id,
                "source_type": "git_cluster",
                "source_ref": f"{root}#{cluster}",
                "label": f"Git cluster: {cluster}",
                "type": "Decision",
                "scope": "project",
                "text": f"Semantic commit cluster '{cluster}' suggests durable project memory.\n\n" + "\n".join(lines),
                "confidence": min(0.78, 0.54 + 0.04 * len(items)),
                "metadata": {"root": str(root), "cluster": cluster, "commits": len(items), "template": "commit_cluster"},
            })
            if len(candidates) >= limit:
                break
        return candidates

    def _commit_cluster(self, subject: str) -> str:
        lower = subject.lower()
        prefix_match = re.match(r"^([a-z]+)(\([^)]+\))?!?:", lower)
        if prefix_match:
            prefix = prefix_match.group(1)
            if prefix in {"feat", "feature"}:
                return "feature"
            if prefix in {"fix", "bugfix", "hotfix"}:
                return "fix"
            if prefix in {"docs", "doc"}:
                return "docs"
            if prefix in {"test", "tests"}:
                return "tests"
            if prefix in {"refactor", "perf", "chore", "build", "ci"}:
                return prefix
        keyword_clusters = [
            ("security", ("security", "secret", "token", "auth", "approval")),
            ("memory", ("memory", "candidate", "ingest", "context")),
            ("provider", ("provider", "openai", "ollama", "claude", "codex", "router")),
            ("graph", ("graph", "node", "edge", "canvas")),
            ("workflow", ("workflow", "task", "review")),
            ("release", ("release", "version", "backup", "readiness", "package")),
        ]
        for cluster, keywords in keyword_clusters:
            if any(keyword in lower for keyword in keywords):
                return cluster
        return "general"

    def _path_matches_exclude(self, relative: str, pattern: str) -> bool:
        normalized = str(pattern or "").strip().replace("\\", "/")
        if not normalized:
            return False
        rel = relative.replace("\\", "/").strip("/")
        if normalized.endswith("/"):
            prefix = normalized.strip("/")
            return rel == prefix or rel.startswith(f"{prefix}/") or f"/{prefix}/" in f"/{rel}/"
        return rel == normalized or rel.endswith(f"/{normalized}") or normalized in rel.split("/")

    def _iter_importable_files(self, root: Path, limit: int, exclude_patterns: list[str] | None = None) -> list[Path]:
        patterns = [str(item).strip() for item in (exclude_patterns or []) if str(item).strip()]
        files: list[Path] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in DEFAULT_EXCLUDES for part in path.parts):
                continue
            relative = str(path.relative_to(root)).replace("\\", "/")
            if any(self._path_matches_exclude(relative, pattern) for pattern in patterns):
                continue
            suffix = path.suffix.lower()
            max_size = CODE_FILE_MAX_BYTES if suffix in CODE_EXTENSIONS else GENERIC_FILE_MAX_BYTES
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > max_size:
                continue
            if suffix not in TEXT_EXTENSIONS and suffix not in CODE_EXTENSIONS and suffix not in DOC_EXTENSIONS and not self._path_looks_text(path):
                continue
            files.append(path)
        files.sort(
            key=lambda item: (
                0 if item.suffix.lower() in CODE_EXTENSIONS else 1 if item.suffix.lower() in DOC_EXTENSIONS else 2,
                str(item).lower(),
            )
        )
        return files[:limit]

    def _iter_project_tree_entries(self, root: Path, limit: int) -> list[Path]:
        entries: list[Path] = []
        queue: deque[Path] = deque([root])
        while queue:
            current = queue.popleft()
            try:
                child_dirs = sorted(path for path in current.iterdir() if path.is_dir() and path.name not in DEFAULT_EXCLUDES)
            except OSError:
                continue
            for child in child_dirs:
                entries.append(child)
                queue.append(child)
                if len(entries) >= limit:
                    return entries
        for current_root, dir_names, file_names in os.walk(root):
            dir_names[:] = sorted(name for name in dir_names if name not in DEFAULT_EXCLUDES)
            file_names = sorted(file_names)
            current = Path(current_root)
            for name in file_names:
                entries.append(current / name)
                if len(entries) >= limit:
                    return entries
        return entries

    def _summarize_file(self, path: Path, root: Path) -> str:
        preview = self._read_text_preview(path)
        if not preview["readable"]:
            return str(preview["message"])
        clean = str(preview["text"])
        return self._summarize_clean_file(path, root, clean)

    def _summarize_clean_file(self, path: Path, root: Path, clean: str) -> str:
        suffix = path.suffix.lower()
        if suffix in CODE_EXTENSIONS:
            structure = self._extract_code_structure(clean, suffix)
            return f"Code file {path.relative_to(root)} structure:\n\n{structure}"
        lines = [line.strip() for line in clean.splitlines() if line.strip()]
        return f"File {path.relative_to(root)} imported into ArchitectOS memory.\n\n" + "\n".join(lines[:30])[:1800]

    def _extract_code_structure(self, clean: str, extension: str) -> str:
        lines = clean.splitlines()
        structural_lines = []
        in_multiline_comment = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("/*") or stripped.startswith('"""') or stripped.startswith("'''"):
                in_multiline_comment = True
            if in_multiline_comment:
                if stripped.endswith("*/") or stripped.endswith('"""') or stripped.endswith("'''"):
                    in_multiline_comment = False
                continue
            if stripped.startswith("//") or stripped.startswith("#") or stripped.startswith("*"):
                continue
            if extension == ".py":
                if stripped.startswith(("def ", "class ", "import ", "from ")):
                    structural_lines.append(line)
            elif extension in {".js", ".jsx", ".ts", ".tsx"}:
                if (
                    stripped.startswith(("import ", "export ", "class ", "interface ", "type ", "enum ")) or
                    "function " in stripped or
                    "constructor" in stripped or
                    (stripped.endswith("{") and any(k in stripped for k in ["public", "private", "protected", "static", "readonly", "async"]))
                ):
                    structural_lines.append(stripped)
            elif extension in {".java", ".kt", ".kts", ".scala"}:
                if (
                    stripped.startswith(("package ", "import ", "public class ", "class ", "interface ", "enum ", "public interface ", "public enum ")) or
                    ("class " in stripped and "{" in stripped) or
                    any(k in stripped for k in ["public ", "private ", "protected ", "static ", "final ", "abstract "])
                ):
                    if "=" not in stripped and (";" in stripped or "{" in stripped):
                        structural_lines.append(stripped)
            else:
                if any(k in stripped for k in ["class ", "def ", "function ", "import "]):
                    structural_lines.append(stripped)
        if not structural_lines:
            return "No high-level code structure detected."
        return "\n".join(structural_lines[:50])[:1700]

    def _read_text_preview(self, path: Path, max_bytes: int = TEXT_PREVIEW_BYTES) -> dict[str, Any]:
        size = path.stat().st_size
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        data = raw[:max_bytes]
        if self._looks_binary_bytes(data[:TEXT_SAMPLE_BYTES]):
            return {
                "readable": False,
                "binary": True,
                "encoding": None,
                "text": "",
                "redacted": False,
                "truncated": truncated,
                "message": f"Preview unavailable: {path.name} appears to be a binary file ({size} bytes).",
            }
        text, encoding = self._decode_text_bytes(data)
        clean, redacted = sanitize_text(text)
        return {
            "readable": True,
            "binary": False,
            "encoding": encoding,
            "text": clean,
            "redacted": redacted,
            "truncated": truncated,
            "message": "File preview truncated." if truncated else "",
        }

    def _path_looks_text(self, path: Path) -> bool:
        try:
            with path.open("rb") as handle:
                return not self._looks_binary_bytes(handle.read(TEXT_SAMPLE_BYTES))
        except OSError:
            return False

    @staticmethod
    def _looks_binary_bytes(data: bytes) -> bool:
        if not data:
            return False
        if b"\x00" in data:
            return True
        text_control = {7, 8, 9, 10, 12, 13, 27}
        control_count = sum(1 for byte in data if byte < 32 and byte not in text_control)
        return control_count / max(len(data), 1) > 0.30

    @staticmethod
    def _decode_text_bytes(data: bytes) -> tuple[str, str]:
        for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
            try:
                return data.decode(encoding), encoding
            except UnicodeError:
                continue
        return data.decode("utf-8", errors="replace"), "utf-8-replace"


    def save_project_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Save or create a project file."""
        project_id = payload.get("project_id") or "architectos"
        relative_path = payload.get("path", "")
        text = payload.get("text", "")

        if not relative_path:
            raise ValueError("path is required")

        root = self._project_root(project_id)
        path = self._safe_project_path(root, relative_path)

        # Create parent directories if needed
        path.parent.mkdir(parents=True, exist_ok=True)

        # Write file
        path.write_text(text, encoding="utf-8")

        return {
            "success": True,
            "project_id": project_id,
            "path": str(path.relative_to(root)),
            "size": path.stat().st_size
        }

    def delete_project_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Delete a project file."""
        project_id = payload.get("project_id") or "architectos"
        relative_path = payload.get("path", "")

        if not relative_path:
            raise ValueError("path is required")

        root = self._project_root(project_id)
        path = self._safe_project_path(root, relative_path)

        if not path.exists():
            raise ValueError("file does not exist")

        # Delete file
        path.unlink()

        return {
            "success": True,
            "project_id": project_id,
            "path": relative_path
        }
