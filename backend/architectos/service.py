from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import fnmatch
import json
import logging
import os
import queue
import re
import secrets
import shlex
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, cast

from .adapters import ProviderRequest, ProviderRouter
from .council import CouncilOrchestrator
from .chat_memory import (
    DEFAULT_CHAT_CANDIDATE_TTL_DAYS,
    DEFAULT_CHAT_MEMORY_MODE,
    normalize_chat_memory_mode,
)
from .code_graph import CodeGraphIngestor
from .config import APP_ENV, APP_NAME, APP_VERSION, BACKUP_RETENTION
from .files import FileStore
from .lsp import CodeIntelligenceManager
from .mcp import MCPManager
from .models import Project, stable_id, utc_now
from .project_files import safe_project_path
from .embeddings import MemoryEmbeddingEngine, build_embedding_provider
from .graph_service import GraphServiceMixin
from .retrieval_service import RetrievalServiceMixin
from .chat_service import ChatServiceMixin
from .ingestion_service import IngestionServiceMixin
from .azure_sync_service import AzureSyncServiceMixin
from .project_scan_service import ProjectScanServiceMixin
from .chat_session_service import ChatSessionServiceMixin
from .tool_exec_service import ToolExecServiceMixin
from .integrations_service import IntegrationsServiceMixin
from .providers_council_service import ProvidersCouncilServiceMixin
from .settings_router_service import SettingsRouterServiceMixin
from .ai_runtime_service import AiRuntimeServiceMixin
from .search import HybridSearchStrategy
from .release import release_manifest
from .security import SecurityPolicy
from .storage import SQLiteMemoryRepository
from .tool_gateway import (
    ToolGateway,
)

from .constants import (
    CODE_EXTENSIONS,
    CODE_FILE_MAX_BYTES,
    DEFAULT_EXCLUDES,
    NOISE_FILE_SUFFIXES,
    SYSTEM_PROJECT_ID,
    TERMINAL_DANGEROUS_PATTERNS,
    TERMINAL_SHELLS,
    TEXT_EXTENSIONS,
)
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


def _token_set(text: str, stopwords: set[str], *, min_len: int = 1, strip_chars: str = "") -> set[str]:
    tokens: set[str] = set()
    for raw in TOKEN_RE.findall(text or ""):
        token = raw.lower().strip(strip_chars) if strip_chars else raw.lower()
        if len(token) < min_len or token in stopwords:
            continue
        tokens.add(token)
    return tokens


def _token_similarity(left: set[str], right: set[str], *, containment_weight: float) -> float:
    if not left or not right:
        return 0.0
    overlap = len(left & right)
    jaccard = overlap / len(left | right)
    containment = overlap / min(len(left), len(right))
    return max(jaccard, containment * containment_weight)


class MemoryIngestionEngine:
    def __init__(self, repository: SQLiteMemoryRepository) -> None:
        self.repository = repository

    def prepare_candidates(self, project_id: str, candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        references = self._reference_items(project_id)
        by_fingerprint = {
            str(reference.get("fingerprint") or ""): reference
            for reference in references
            if reference.get("fingerprint")
        }
        prepared: list[dict[str, Any]] = []
        seen_fingerprints: set[str] = set()
        for candidate in candidates:
            enriched = self._enrich_candidate(candidate)
            fingerprint = str(enriched["metadata"]["fingerprint"])
            if fingerprint in seen_fingerprints:
                enriched["metadata"]["duplicate"] = True
                enriched["metadata"]["duplicate_reason"] = "same ingestion batch fingerprint"
            duplicate = self._find_duplicate(enriched, references, by_fingerprint)
            if duplicate:
                enriched["metadata"].update(duplicate)
                enriched["confidence"] = min(float(enriched.get("confidence") or 0.62), 0.45)
            prepared.append(enriched)
            seen_fingerprints.add(fingerprint)
            reference = self._reference_from_candidate(enriched)
            references.append(reference)
            by_fingerprint.setdefault(fingerprint, reference)
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

    def _find_duplicate(
        self,
        candidate: dict[str, Any],
        references: list[dict[str, Any]],
        by_fingerprint: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        metadata = dict(candidate.get("metadata") or {})
        candidate_tokens = set(metadata.get("tokens") or [])
        candidate_fingerprint = str(metadata.get("fingerprint") or "")
        candidate_meeting_id = str(metadata.get("meeting_id") or "").strip()
        candidate_work_item_id = str(metadata.get("work_item_id") or "").strip()
        candidate_source = str(candidate.get("source_type") or metadata.get("source") or "")
        if by_fingerprint is not None and candidate_fingerprint:
            # Fast path: exact fingerprint match without scanning every reference.
            exact = by_fingerprint.get(candidate_fingerprint)
            if exact and str(exact.get("id") or "") != str(candidate.get("id") or ""):
                return {
                    "duplicate": True,
                    "duplicate_score": 1.0,
                    "duplicate_of": exact.get("id"),
                    "duplicate_label": exact.get("label"),
                    "duplicate_kind": exact.get("kind"),
                }
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
            if by_fingerprint is None and reference.get("fingerprint") == candidate_fingerprint:
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
        return _token_set(text, DUPLICATE_STOPWORDS)

    def _fingerprint(self, text: str) -> str:
        counts = Counter(self._tokens(text))
        return "|".join(token for token, _count in counts.most_common(18))

    def _similarity(self, left: set[str], right: set[str]) -> float:
        return _token_similarity(left, right, containment_weight=0.85)

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
    "chat_session_idle_minutes": 30,
    "chat_candidate_ttl_days": DEFAULT_CHAT_CANDIDATE_TTL_DAYS,
    "candidate_rejected_purge_days": 30,
    "candidate_promoted_purge_days": 90,
    "chat_store_facts_only": True,
    "chat_auto_accept": False,
    "long_term_types": ["Decision", "Constraint", "Requirement"],
    "architecture_keywords": ["architecture", "adr", "decision", "constraint", "security", "provider", "routing"],
}


class MemoryLifecycleEngine:
    def __init__(self, repository: SQLiteMemoryRepository) -> None:
        self.repository = repository

    def settings(self) -> dict[str, Any]:
        configured = self.repository.get_setting("memory_lifecycle") or {}
        settings: dict[str, Any] = dict(DEFAULT_MEMORY_LIFECYCLE)
        settings.update({key: value for key, value in configured.items() if key in settings})
        settings["enabled"] = bool(settings.get("enabled"))
        settings["refresh_on_access"] = bool(settings.get("refresh_on_access"))
        settings["auto_rescan_on_startup"] = bool(settings.get("auto_rescan_on_startup", True))
        settings["auto_rescan_all_projects"] = bool(settings.get("auto_rescan_all_projects", True))
        settings["chat_store_facts_only"] = bool(settings.get("chat_store_facts_only", True))
        settings["chat_auto_accept"] = bool(settings.get("chat_auto_accept", False))
        settings["chat_memory_mode"] = normalize_chat_memory_mode(settings.get("chat_memory_mode"))
        for key in ("short_term_ttl_days", "archive_after_days", "delete_after_days", "promote_after_hits", "auto_rescan_limit", "chat_candidate_ttl_days", "chat_session_idle_minutes", "candidate_rejected_purge_days", "candidate_promoted_purge_days"):
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
            label=str(getattr(node, "label", "") or ""),
        )
        return self.repository.upsert_node(node)

    def seed_metadata(
        self,
        *,
        node_type: str,
        created_at: str,
        metadata: dict[str, Any] | None = None,
        source: str = "memory",
        label: str = "",
    ) -> dict[str, Any]:
        """Attach lifecycle fields without writing — used to avoid a second upsert on promote."""
        seeded = dict(metadata or {})
        if seeded.get("memory_tier"):
            return seeded
        settings = self.settings()
        # Minimal stand-in so existing helpers can score/classify.
        class _Probe:
            type: str
            label: str
            created_at: str
            metadata: dict[str, Any]

        probe = _Probe()
        probe.type = node_type
        probe.label = label
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
            # Metadata-only bump (label/text unchanged): skip embedding reindex.
            self.repository.upsert_node(node, notify=False)
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
        return {"enabled": True, "changed": changed, "items": items, "settings": settings, "dry_run": dry_run, **self.expire_stale_chat_candidates(project_id, dry_run=dry_run), **self.purge_memory_candidates(project_id, dry_run=dry_run)}

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
            if source_type not in {"chat", "chat_favorite", "rules_keeper"} and template not in {
                "chat_turn_keeper",
                "chat_fact_keeper",
                "chat_session_summary",
                "rules_keeper",
                "assistant_favorite",
            }:
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

    def purge_memory_candidates(self, project_id: str | None = None, dry_run: bool = False) -> dict[str, Any]:
        """Hard-delete decided candidates past their retention window.

        Rejected candidates are dead weight after 30 days; promoted ones live on
        as memory nodes, so the queue row is purged after 90 days.
        """
        settings = self.settings()
        windows = {
            "rejected": int(settings.get("candidate_rejected_purge_days") or 0),
            "promoted": int(settings.get("candidate_promoted_purge_days") or 0),
        }
        now = datetime.now(timezone.utc)
        purged = 0
        items: list[dict[str, Any]] = []
        for status, days in windows.items():
            if days <= 0:
                continue
            decided_key = "rejected_at" if status == "rejected" else "promoted_at"
            for candidate in self.repository.list_memory_candidates(project_id, status, None):
                raw = str(candidate.get(decided_key) or candidate.get("updated_at") or candidate.get("created_at") or "")
                then = self._parse_time(raw) if raw else now
                age_days = max(0, int((now - then).total_seconds() // 86400))
                if age_days < days:
                    continue
                purged += 1
                if len(items) < 100:
                    items.append({"id": candidate.get("id"), "status": status, "age_days": age_days})
                if not dry_run:
                    self.repository.delete_memory_candidate(str(candidate.get("id") or ""))
        return {"candidates_purged": purged, "candidate_purge_items": items}

    def reclassify_memory_tiers(self, project_id: str | None = None, dry_run: bool = True) -> dict[str, Any]:
        """Re-evaluate memory_tier for every active node under the current rules.

        Dry-run by default: reports which nodes would flip tier without writing.
        Favorites/pins and nodes promoted by access count are never demoted.
        """
        settings = self.settings()
        changes: list[dict[str, Any]] = []
        changed = 0
        for node in self.repository.list_nodes():
            if project_id and node.project_id not in {project_id, None}:
                continue
            if node.status != "active":
                continue
            metadata = dict(node.metadata or {})
            current = str(metadata.get("memory_tier") or "")
            if not current:
                continue  # uninitialized — initialize_node seeds it on the next pass
            if metadata.get("favorite") or metadata.get("pinned"):
                continue
            if int(metadata.get("access_count") or 0) >= settings["promote_after_hits"]:
                continue
            target = "long_term" if self._should_be_long_term(node, settings) else "short_term"
            if target == current:
                continue
            changed += 1
            if len(changes) < 500:
                changes.append({"id": node.id, "label": node.label, "from": current, "to": target})
            if not dry_run:
                metadata["memory_tier"] = target
                if target == "long_term":
                    metadata["lifecycle_state"] = "stable"
                    metadata["decay_enabled"] = False
                    metadata["retention_policy"] = "keep"
                    metadata["promoted_to_long_term_at"] = utc_now()
                    metadata["promotion_reason"] = "reclassify"
                else:
                    metadata["lifecycle_state"] = "fresh"
                    metadata["decay_enabled"] = True
                    metadata["retention_policy"] = "delete" if str(getattr(node, "type", "") or "") == "Artifact" else "archive"
                    metadata.pop("promoted_to_long_term_at", None)
                metadata["memory_score"] = self._score(node, metadata, settings)
                node.metadata = metadata
                self.repository.upsert_node(node, notify=False)
        return {"dry_run": dry_run, "changed": changed, "items": changes}

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
        if node_type == "Artifact":
            # Code/doc artifacts are volatile snapshots — never keyword-promote them.
            return False
        # Keyword match on type+label only: scanning body/metadata text made almost
        # every node look "architectural" and inflated long_term to ~95% of the corpus.
        haystack = f"{node_type} {getattr(node, 'label', '')}".lower()
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
    "inbox": {"label": "Inbox", "text": "Files dropped into the inbox folder for this scope."},
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
            root = self._project_root_node(
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
        root = self._project_root_node(project_id or "architectos", nodes_snapshot=nodes, root_cache=project_root_cache)

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
        # Code artifacts are excluded: path/token overlap between source files created
        # tens of thousands of meaningless RELATED_TO edges (33k on the live corpus).
        similarity_nodes = [node for node in content_nodes if not self._is_code_artifact(node)]
        token_map = {node.id: self._similarity_tokens(node) for node in similarity_nodes}
        inverted: dict[str, list[str]] = {}
        for node_id, tokens in token_map.items():
            for token in tokens:
                inverted.setdefault(token, []).append(node_id)
        # Drop ultra-common tokens so one frequent word cannot explode candidates back toward O(n²).
        max_df = max(48, len(similarity_nodes) // 40)
        inverted = {token: ids for token, ids in inverted.items() if 1 < len(ids) <= max_df}
        candidate_cap = max(24, similarity_limit * 16)
        max_expansions = 1800

        for node in similarity_nodes:
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

    def _is_code_artifact(self, node: Any) -> bool:
        if str(getattr(node, "type", "") or "") != "Artifact":
            return False
        return self._source_key_for_node(node) == "code"

    def prune_code_artifact_similarity_edges(self, project_id: str | None = None, dry_run: bool = True) -> dict[str, Any]:
        """Drop RELATED_TO edges that touch code artifacts (legacy noise from before
        code artifacts were excluded from similarity linking). Dry-run by default."""
        nodes = self._active_nodes_for_project(project_id)
        by_id = {node.id: node for node in nodes}
        code_ids = {node.id for node in nodes if self._is_code_artifact(node)}
        if not code_ids:
            return {"dry_run": dry_run, "pruned": 0, "items": []}
        pruned = 0
        items: list[dict[str, Any]] = []
        for edge in self.repository.list_edges():
            if str(getattr(edge, "type", "") or "") != "RELATED_TO":
                continue
            if edge.source not in code_ids and edge.target not in code_ids:
                continue
            if edge.source not in by_id and edge.target not in by_id:
                continue
            pruned += 1
            if len(items) < 100:
                items.append({"id": edge.id, "source": edge.source, "target": edge.target})
            if not dry_run:
                self.repository.delete_edge(edge.id)
        return {"dry_run": dry_run, "pruned": pruned, "items": items}

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

    def _project_root_node(
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
        if source_key in {"docs", "adr", "meetings", "azure-wiki", "granola", "chat", "teams-meetings", "inbox"}:
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
        return _token_set(text, GRAPH_STOPWORDS, min_len=3, strip_chars="._-/#")

    def _similarity(self, left: set[str], right: set[str]) -> float:
        return _token_similarity(left, right, containment_weight=0.72)

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
        existing: set[str] = existing_edge_ids if existing_edge_ids is not None else {edge.id for edge in self.repository.list_edges()}
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


class ArchitectOSService(AiRuntimeServiceMixin, SettingsRouterServiceMixin, ProvidersCouncilServiceMixin, IntegrationsServiceMixin, ToolExecServiceMixin, ProjectScanServiceMixin, AzureSyncServiceMixin, IngestionServiceMixin, ChatSessionServiceMixin, ChatServiceMixin, RetrievalServiceMixin, GraphServiceMixin):
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

    # Ingest-time hygiene: an exact restatement of an existing fact updates it in
    # place instead of spawning a duplicate; a close-but-not-identical fact is
    # flagged (never silently merged) so a value change like "cap 5k -> 9k" stays a
    # distinct fact the consolidation/supersede path can reason about.
    NEAR_DUPLICATE_SIMILARITY = 0.72

    def add_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        label = str(payload.get("label") or "").strip()
        text = str(payload.get("text") or "").strip()
        scope = str(payload.get("scope") or "project")
        if not label:
            raise ValueError("memory label is required")
        if not text:
            raise ValueError("memory text is required")
        node_type = str(payload.get("type") or "Lesson")
        project_id = self._memory_project_id_for_scope(scope, payload.get("project_id") or "architectos")

        dedup_enabled = payload.get("dedup", True) is not False
        exact, near_id, near_score = (
            self._ingest_dedup_scan(label, text, node_type, scope, project_id)
            if dedup_enabled
            else (None, "", 0.0)
        )
        if exact is not None:
            return self._register_duplicate_add(exact, payload)

        node = self.repository.add_node(
            node_type,
            label,
            scope,
            text,
            project_id,
            payload.get("interface_id"),
            float(payload.get("confidence") or 0.8),
            {"source": str(payload.get("source") or "ui"), "source_type": str(payload.get("source_type") or payload.get("source") or "manual")},
        )
        node = self.memory_lifecycle.initialize_node(node, "ui")
        if near_id and near_score >= self.NEAR_DUPLICATE_SIMILARITY:
            node.metadata["possible_duplicate_of"] = near_id
            node.metadata["possible_duplicate_score"] = round(near_score, 3)
            node = self.repository.upsert_node(node)
        self.graph_auto_linker.link_node(node, payload.get("project_id") or "architectos")
        return node.to_dict()

    def _ingest_dedup_scan(
        self, label: str, text: str, node_type: str, scope: str, project_id: str | None
    ) -> tuple[Any | None, str, float]:
        """Scan existing facts for an exact restatement or a near-duplicate.

        Returns ``(exact_node, best_near_id, best_near_score)``. "Exact" means the
        same fingerprint (top content tokens) within the same type/scope/project —
        deterministic and dependency-free. Near-duplicate is the best token
        similarity among same-type facts, surfaced for flagging only.
        """
        engine = self.ingestion_engine
        incoming_tokens = engine._tokens(f"{label} {text}")
        incoming_key = (self._normalize_fact_text(label), self._normalize_fact_text(text))
        best_near_id, best_near_score = "", 0.0
        for node in self.repository.list_nodes():
            if node.status != "active":
                continue
            meta = dict(node.metadata or {})
            if meta.get("structural") or meta.get("hub") or meta.get("scope_root"):
                continue
            if node.scope != scope or node.project_id != project_id:
                continue
            if node.type != node_type:
                continue
            # Exact = identical normalized label + text. Deliberately NOT the token
            # fingerprint: the fingerprint tokenizer drops digits, so facts that
            # differ only by a number ("cap 5000" vs "cap 9000", "node 0" vs
            # "node 1") share a fingerprint and must never be auto-merged.
            if (self._normalize_fact_text(node.label), self._normalize_fact_text(node.text)) == incoming_key:
                return node, node.id, 1.0
            node_tokens = set(meta.get("tokens") or []) or engine._tokens(f"{node.label} {node.text}")
            score = engine._similarity(incoming_tokens, node_tokens)
            if score > best_near_score:
                best_near_id, best_near_score = node.id, score
        return None, best_near_id, best_near_score

    @staticmethod
    def _normalize_fact_text(value: str) -> str:
        return " ".join(str(value or "").split()).strip().lower()

    def _register_duplicate_add(self, existing: Any, payload: dict[str, Any]) -> dict[str, Any]:
        """Fold a re-added identical fact into the node it duplicates.

        Non-destructive update: bump confidence toward the incoming value, record
        a dedup hit, refresh lifecycle access, and return the surviving node marked
        ``dedup_merged`` so callers can tell no new node was created.
        """
        meta = dict(existing.metadata or {})
        meta["dedup_hits"] = int(meta.get("dedup_hits") or 0) + 1
        meta["last_dedup_at"] = utc_now()
        existing.metadata = meta
        try:
            existing.confidence = max(float(existing.confidence or 0.0), float(payload.get("confidence") or 0.0))
        except (TypeError, ValueError):
            pass
        existing = self.repository.upsert_node(existing)
        try:
            self.memory_lifecycle.refresh_nodes([existing.id], "dedup")
        except Exception as exc:  # lifecycle bump is best-effort
            _LOG.warning("dedup refresh skipped: %s", exc)
        result = existing.to_dict()
        result["dedup_merged"] = True
        return result

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
            label=str(payload.get("label") or candidate.get("label") or ""),
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
        self._link_chat_session_relations(node, candidate)
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
            "auto_accepted",
            "chat_id",
            "session_revision",
            "session_start_message_index",
            "session_end_message_index",
        ):
            if meta.get(key) not in (None, "", [], {}):
                payload[key] = meta[key]
        return payload

    def _link_chat_session_relations(self, node, candidate: dict[str, Any]) -> None:
        """Link promoted summaries from the same chat in revision order."""
        metadata = dict(candidate.get("metadata") or {})
        if str(metadata.get("template") or "") != "chat_session_summary":
            return
        chat_id = str(metadata.get("chat_id") or candidate.get("source_ref") or "").strip()
        revision = int(metadata.get("session_revision") or 0)
        if not chat_id or revision <= 1:
            return
        previous: tuple[int, str] | None = None
        for item in self.repository.list_memory_candidates(str(candidate.get("project_id") or "architectos"), status=None, limit=500):
            item_meta = dict(item.get("metadata") or {})
            if str(item_meta.get("template") or "") != "chat_session_summary":
                continue
            if str(item_meta.get("chat_id") or item.get("source_ref") or "") != chat_id:
                continue
            item_revision = int(item_meta.get("session_revision") or 0)
            node_id = str(item.get("promoted_node_id") or "")
            if not node_id or item_revision >= revision:
                continue
            if previous is None or item_revision > previous[0]:
                previous = (item_revision, node_id)
        if previous:
            self.repository.add_edge(previous[1], node.id, "NEXT_SESSION", str(candidate.get("scope") or "project"), 0.95)

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
        # Share indexes for the batch; embedding indexing is queued asynchronously
        # by the node-upsert listener, so no listener pausing is needed.
        by_work_item: dict[str, Any] | None = None
        hub_cache: dict[tuple[str, str | None, str], Any] | None = None
        existing_edge_ids: set[str] | None = None
        nodes_snapshot: list[Any] | None = None
        project_root_cache: dict[str, Any] | None = None
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
        # Fetch the edge table once; the per-view subsets are derived in memory.
        table_edges = self.repository.list_edges()
        all_edges = [edge for edge in table_edges if edge.source in allowed and edge.target in allowed]

        # Exact ID search: pull in 1-hop neighbors so the hit isn't an isolated dot.
        if exact_id_hits:
            neighbor_ids: set[str] = set()
            for edge in table_edges:
                if edge.source in exact_id_hits:
                    neighbor_ids.add(edge.target)
                if edge.target in exact_id_hits:
                    neighbor_ids.add(edge.source)
            if neighbor_ids:
                by_id = {node.id: node for node in all_nodes}
                for node_id in neighbor_ids:
                    if node_id in allowed:
                        continue
                    neighbor = by_id.get(node_id)
                    if neighbor is None:
                        continue
                    filtered_nodes.append(neighbor)
                    allowed.add(node_id)
                all_edges = [
                    edge
                    for edge in table_edges
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
                    
        communities = self._annotate_graph_analytics(graph_nodes, graph_edges)

        return {
            "nodes": graph_nodes,
            "edges": graph_edges,
            "types": types,
            "scopes": scopes,
            "sources": sources,
            "communities": communities,
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

    def reclassify_memory(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        project_id = str(payload.get("project_id") or "architectos")
        dry_run = bool(payload.get("dry_run", True))
        tiers = self.memory_lifecycle.reclassify_memory_tiers(project_id, dry_run=dry_run)
        edges = self.graph_auto_linker.prune_code_artifact_similarity_edges(project_id, dry_run=dry_run)
        return {"project_id": project_id, "dry_run": dry_run, "tiers": tiers, "code_similarity_edges": edges}

    def promote_memory_long_term(self, node_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        reason = str((payload or {}).get("reason") or "manual")
        return self.memory_lifecycle.promote_long_term(node_id, reason)

    def _noise_artifact_reason(self, node: Any) -> str:
        """Return a reason string if a node is an accidentally-indexed file artifact.

        Only file-import artifacts (project scan / code|docs ingestion) are eligible —
        curated Decisions/Lessons/Constraints are never touched. A node is noise when
        its underlying path is a build/runtime artifact (``.log``, lockfile, sourcemap)
        or lives under a tooling scratch dir (``.playwright-mcp/``, ``test-results/`` …)
        that should have been excluded at ingest.
        """
        if node is None or getattr(node, "status", "") != "active":
            return ""
        meta = dict(getattr(node, "metadata", None) or {})
        if meta.get("favorite") or meta.get("pinned"):
            return ""  # user-curated: never auto-purge
        source = str(meta.get("source") or meta.get("source_type") or "").lower()
        template = str(meta.get("template") or "").lower()
        file_backed = (
            source in {"project_scan", "code", "docs"}
            or template in {"generic", "inbox"}
            or bool(meta.get("path"))
        )
        if not file_backed:
            return ""
        label = str(getattr(node, "label", "") or "")
        raw_path = str(meta.get("path") or meta.get("source_ref") or "")
        if not raw_path:
            # Fall back to the label ("Code: <rel>" / "Doc: <rel>" / bare relative path).
            raw_path = re.sub(r"^(?:Code|Doc|Inbox):\s*", "", label).strip()
        rel = raw_path.replace("\\", "/").strip()
        name = rel.rsplit("/", 1)[-1]
        if self._is_noise_file_name(name):
            return f"artifact file: {name}"
        parts = {segment for segment in rel.split("/") if segment}
        hit = parts & DEFAULT_EXCLUDES
        if hit:
            return f"excluded dir: {sorted(hit)[0]}"
        ext = str(meta.get("extension") or "").lower()
        if ext in NOISE_FILE_SUFFIXES:
            return f"artifact ext: {ext}"
        return ""

    def purge_noise_nodes(
        self, project_id: str | None = None, dry_run: bool = False, hard: bool = False
    ) -> dict[str, Any]:
        """Clean up accidentally-indexed artifacts (e.g. ``.playwright-mcp/*.log``).

        Archives (default) or hard-deletes file-backed nodes that never should have
        been ingested. Reversible by default: archived nodes drop out of active search
        but stay in the store; ``hard=True`` also strips their edges and marks deleted.
        """
        items: list[dict[str, Any]] = []
        scanned = 0
        for node in self.repository.list_nodes():
            if node.status != "active":
                continue
            if project_id and node.project_id not in {project_id, None}:
                continue
            scanned += 1
            reason = self._noise_artifact_reason(node)
            if not reason:
                continue
            record: dict[str, Any] = {"id": node.id, "label": node.label, "reason": reason, "type": node.type}
            items.append(record)
            if dry_run:
                continue
            metadata = dict(node.metadata or {})
            deleted_edges = 0
            if hard:
                deleted_edges = self.repository.delete_node_edges(node.id)
                node.status = "deleted"
                metadata["deleted_at"] = utc_now()
            else:
                node.status = "archived"
                metadata["archived_at"] = metadata.get("archived_at") or utc_now()
            metadata["archived_reason"] = "noise_artifact"
            metadata["noise_reason"] = reason
            node.metadata = metadata
            self.repository.upsert_node(node)
            record["deleted_edges"] = deleted_edges
        return {
            "scanned": scanned,
            "purged": len(items),
            "dry_run": dry_run,
            "hard": hard,
            "items": items[:200],
        }

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
        if edge_type == "SUPERSEDES":
            # source SUPERSEDES target → target stopped being true when source began.
            self._apply_supersede(source_node, target_node)
        return {"edge": edge.to_dict()}

    def _apply_supersede(self, source_node: Any, target_node: Any) -> None:
        """Mark the superseded fact invalid from the moment the new fact began.

        Bi-temporal, non-destructive: the retired node stays in the store (so
        as_of queries can still surface it as historical truth) but carries an
        ``invalid_at`` timestamp and a ``superseded_by`` back-reference. The
        earliest retirement instant wins if it is superseded more than once.
        """
        when = str(getattr(source_node, "created_at", "") or "") or utc_now()
        meta = dict(getattr(target_node, "metadata", {}) or {})
        existing = str(meta.get("invalid_at") or "")
        if existing and existing <= when:
            return
        meta["invalid_at"] = when
        meta["superseded_by"] = getattr(source_node, "id", "")
        target_node.metadata = meta
        self.repository.upsert_node(target_node)

    LINK_EDGE_TYPES = {"RELATED_TO", "DEPENDS_ON", "PART_OF", "SUPPORTS", "CONTRADICTS", "SUPERSEDES", "EXAMPLE_OF"}

    def suggest_memory_links(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """LLM-assisted linking of contextually related but unlinked memory nodes.

        Individually added memories rarely share direct references. This pass
        1) picks a pool of low-degree (least linked) active content nodes,
        2) finds candidate pairs by token similarity that have no edge yet,
        3) asks the configured LLM whether each pair is related and how,
        4) optionally (``apply=true``) writes the accepted pairs as graph edges.

        Default is a dry run: suggestions are returned without writes.
        """
        payload = payload or {}
        project_id = str(payload.get("project_id") or "architectos")
        limit = max(2, min(int(payload.get("limit") or 24), 200))
        pair_limit = max(1, min(int(payload.get("pair_limit") or 12), 60))
        min_similarity = max(0.05, min(float(payload.get("min_similarity") or 0.22), 0.95))
        apply = bool(payload.get("apply") or False)
        provider_id = str(payload.get("provider_id") or "").strip() or None

        linker = self.graph_auto_linker
        nodes = [n for n in self.repository.list_nodes() if n.status == "active"]
        nodes = [n for n in nodes if not linker._is_structural_node(n) and not linker._is_code_artifact(n)]
        if str(payload.get("scope_all") or "") != "1" and not payload.get("all_projects"):
            nodes = [
                n for n in nodes
                if n.project_id in {project_id, None} or str(getattr(n, "scope", "") or "") in {"shared", "global"}
            ]

        edges = self.repository.list_edges()
        degree: Counter[str] = Counter()
        linked_pairs: set[tuple[str, ...]] = set()
        for edge in edges:
            degree[edge.source] += 1
            degree[edge.target] += 1
            linked_pairs.add(tuple(sorted((edge.source, edge.target))))

        # Least-linked nodes first — exactly the isolated items this pass targets.
        nodes.sort(key=lambda n: (degree[n.id], str(getattr(n, "created_at", "") or "")))
        pool = nodes[:limit]

        token_map = {node.id: linker._similarity_tokens(node) for node in pool}
        pair_candidates: list[tuple[float, Any, Any]] = []
        for index, left in enumerate(pool):
            left_tokens = token_map.get(left.id) or set()
            if not left_tokens:
                continue
            for right in pool[index + 1:]:
                if tuple(sorted((left.id, right.id))) in linked_pairs:
                    continue
                score = linker._similarity(left_tokens, token_map.get(right.id) or set())
                if score >= min_similarity:
                    pair_candidates.append((score, left, right))
        pair_candidates.sort(key=lambda item: -item[0])
        pair_candidates = pair_candidates[:pair_limit]

        # Judge pairs concurrently: each verdict is an independent LLM call and
        # sequential judging made the UI look dead (10 pairs × ~5 s each).
        # Providers are listed once here so worker threads never touch the repository.
        providers = self.repository.list_providers()
        workers = max(1, min(4, len(pair_candidates)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            verdicts = list(executor.map(
                lambda pair: self._judge_link_with_llm(pair[1], pair[2], project_id, provider_id, providers),
                pair_candidates,
            ))
        suggestions: list[dict[str, Any]] = []
        for (score, left, right), verdict in zip(pair_candidates, verdicts):
            suggestion = {
                "source_id": left.id,
                "target_id": right.id,
                "source_label": left.label,
                "target_label": right.label,
                "similarity": round(score, 3),
                "related": bool(verdict.get("related")),
                "edge_type": verdict.get("edge_type") or "RELATED_TO",
                "reason": str(verdict.get("reason") or ""),
                "confidence": round(float(verdict.get("confidence") or 0.6), 3),
                "llm_error": str(verdict.get("error") or ""),
            }
            suggestions.append(suggestion)

        created: list[dict[str, Any]] = []
        if apply:
            for suggestion in suggestions:
                if not suggestion["related"] or suggestion["llm_error"]:
                    continue
                edge_type = suggestion["edge_type"] if suggestion["edge_type"] in self.LINK_EDGE_TYPES else "RELATED_TO"
                try:
                    created_edge = self.create_graph_edge({
                        "source": suggestion["source_id"],
                        "target": suggestion["target_id"],
                        "type": edge_type,
                        "confidence": suggestion["confidence"],
                    })
                    created.append(created_edge["edge"])
                except ValueError:
                    continue
        return {
            "project_id": project_id,
            "applied": apply,
            "pool_size": len(pool),
            "pairs_considered": len(pair_candidates),
            "suggestions": suggestions,
            "created": created,
        }

    def _judge_link_with_llm(self, left: Any, right: Any, project_id: str, provider_id: str | None, providers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        prompt = (
            "You maintain a project memory graph. Decide whether the two memory nodes below are "
            "contextually related (same decision/topic/cause, one explains or depends on the other, "
            "one supersedes or contradicts the other). Answer with a single JSON object only:\n"
            '{"related": true|false, "edge_type": "RELATED_TO|DEPENDS_ON|PART_OF|SUPPORTS|CONTRADICTS|SUPERSEDES|EXAMPLE_OF", '
            '"reason": "<short>", "confidence": 0.0-1.0}\n\n'
            f"Node A [{left.type}] {left.label}:\n{(left.text or '')[:900]}\n\n"
            f"Node B [{right.type}] {right.label}:\n{(right.text or '')[:900]}"
        )
        request = ProviderRequest(message=prompt, context="", project_id=project_id, role="review")
        result = self.provider_router.route(providers if providers is not None else self.repository.list_providers(), request, provider_id)
        text = str(result.get("text") or "")
        if not text.strip():
            return {"related": False, "error": str(result.get("status") or "empty response")}
        return self._parse_link_verdict(text)

    @staticmethod
    def _parse_link_verdict(text: str) -> dict[str, Any]:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {"related": False, "error": "no JSON in LLM response"}
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return {"related": False, "error": "invalid JSON in LLM response"}
        if not isinstance(data, dict):
            return {"related": False, "error": "unexpected LLM response shape"}
        edge_type = str(data.get("edge_type") or "RELATED_TO").strip().upper()
        try:
            confidence = float(data.get("confidence") or 0.6)
        except (TypeError, ValueError):
            confidence = 0.6
        return {
            "related": bool(data.get("related")),
            "edge_type": edge_type if edge_type in ArchitectOSService.LINK_EDGE_TYPES else "RELATED_TO",
            "reason": str(data.get("reason") or "")[:300],
            "confidence": max(0.0, min(confidence, 1.0)),
        }

    CONSOLIDATION_ACTIONS = {"merge", "contradicts", "keep"}

    def suggest_consolidations(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """LLM-assisted consolidation: find duplicate/conflicting memory nodes.

        Complements :meth:`suggest_memory_links` (which links *related* nodes):
        this pass targets *redundant* and *conflicting* knowledge, like mem0's
        UPDATE/DELETE consolidation. Pairs above a high similarity threshold are
        judged by the LLM: ``merge`` (same fact twice — pick a keeper),
        ``contradicts`` (statements in conflict — link with CONTRADICTS), or
        ``keep`` (distinct facts). Dry-run by default; ``apply=true`` merges via
        :meth:`merge_graph_nodes` or creates CONTRADICTS edges.
        """
        payload = payload or {}
        project_id = str(payload.get("project_id") or "architectos")
        limit = max(2, min(int(payload.get("limit") or 40), 300))
        pair_limit = max(1, min(int(payload.get("pair_limit") or 10), 40))
        min_similarity = max(0.2, min(float(payload.get("min_similarity") or 0.45), 0.98))
        apply = bool(payload.get("apply") or False)
        provider_id = str(payload.get("provider_id") or "").strip() or None

        linker = self.graph_auto_linker
        nodes = [n for n in self.repository.list_nodes() if n.status == "active"]
        nodes = [n for n in nodes if not linker._is_structural_node(n) and not linker._is_code_artifact(n)]
        if not payload.get("all_projects"):
            nodes = [
                n for n in nodes
                if n.project_id in {project_id, None} or str(getattr(n, "scope", "") or "") in {"shared", "global"}
            ]
        # Recent first: duplicates mostly arrive from repeated ingestion.
        nodes.sort(key=lambda n: str(getattr(n, "created_at", "") or ""), reverse=True)
        pool = nodes[:limit]

        contradicts_pairs: set[tuple[str, ...]] = set()
        for edge in self.repository.list_edges():
            if edge.type == "CONTRADICTS":
                contradicts_pairs.add(tuple(sorted((edge.source, edge.target))))

        token_map = {node.id: linker._similarity_tokens(node) for node in pool}
        pair_candidates: list[tuple[float, Any, Any]] = []
        for index, left in enumerate(pool):
            left_tokens = token_map.get(left.id) or set()
            if not left_tokens:
                continue
            for right in pool[index + 1:]:
                if tuple(sorted((left.id, right.id))) in contradicts_pairs:
                    continue
                score = linker._similarity(left_tokens, token_map.get(right.id) or set())
                if score >= min_similarity:
                    pair_candidates.append((score, left, right))
        pair_candidates.sort(key=lambda item: -item[0])
        pair_candidates = pair_candidates[:pair_limit]

        # Concurrent judging, same rationale as suggest_memory_links.
        providers = self.repository.list_providers()
        workers = max(1, min(4, len(pair_candidates)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            verdicts = list(executor.map(
                lambda pair: self._judge_consolidation_with_llm(pair[1], pair[2], project_id, provider_id, providers),
                pair_candidates,
            ))
        suggestions: list[dict[str, Any]] = []
        for (score, left, right), verdict in zip(pair_candidates, verdicts):
            suggestions.append({
                "source_id": left.id,
                "target_id": right.id,
                "source_label": left.label,
                "target_label": right.label,
                "similarity": round(score, 3),
                "action": verdict.get("action") or "keep",
                "keep": verdict.get("keep") or "",
                "reason": str(verdict.get("reason") or ""),
                "confidence": round(float(verdict.get("confidence") or 0.6), 3),
                "llm_error": str(verdict.get("error") or ""),
            })

        merged: list[dict[str, Any]] = []
        contradictions: list[dict[str, Any]] = []
        if apply:
            merged_away: set[str] = set()
            # Merges first: later pairs referencing a merged-away node are skipped.
            for suggestion in suggestions:
                if suggestion["llm_error"] or suggestion["action"] != "merge":
                    continue
                if suggestion["source_id"] in merged_away or suggestion["target_id"] in merged_away:
                    continue
                try:
                    if suggestion["keep"] == "A":
                        result = self.merge_graph_nodes(suggestion["target_id"], {"target_id": suggestion["source_id"]})
                    else:
                        result = self.merge_graph_nodes(suggestion["source_id"], {"target_id": suggestion["target_id"]})
                    merged.append({"kept": result["target"]["id"], "merged": result["source"]["id"], "labels": [suggestion["source_label"], suggestion["target_label"]]})
                    merged_away.add(result["source"]["id"])
                except ValueError:
                    continue
            for suggestion in suggestions:
                if suggestion["llm_error"] or suggestion["action"] != "contradicts":
                    continue
                if suggestion["source_id"] in merged_away or suggestion["target_id"] in merged_away:
                    continue
                try:
                    created_edge = self.create_graph_edge({
                        "source": suggestion["source_id"],
                        "target": suggestion["target_id"],
                        "type": "CONTRADICTS",
                        "confidence": suggestion["confidence"],
                    })
                    contradictions.append(created_edge["edge"])
                except ValueError:
                    continue
        return {
            "project_id": project_id,
            "applied": apply,
            "pool_size": len(pool),
            "pairs_considered": len(pair_candidates),
            "suggestions": suggestions,
            "merged": merged,
            "contradictions": contradictions,
        }

    def _judge_consolidation_with_llm(self, left: Any, right: Any, project_id: str, provider_id: str | None, providers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        prompt = (
            "You maintain a project memory graph. Two memory nodes are lexically similar. Decide:\n"
            '- "merge": they state the same fact twice (duplicates). Pick which node to KEEP '
            '("A" = first, "B" = second — prefer the more complete/precise one).\n'
            '- "contradicts": they make conflicting statements about the same subject '
            "(different values, dates, decisions).\n"
            '- "keep": they are distinct facts, no action needed.\n'
            "Answer with a single JSON object only:\n"
            '{"action": "merge"|"contradicts"|"keep", "keep": "A"|"B", "reason": "<short>", "confidence": 0.0-1.0}\n\n'
            f"Node A [{left.type}] {left.label}:\n{(left.text or '')[:900]}\n\n"
            f"Node B [{right.type}] {right.label}:\n{(right.text or '')[:900]}"
        )
        request = ProviderRequest(message=prompt, context="", project_id=project_id, role="review")
        result = self.provider_router.route(providers if providers is not None else self.repository.list_providers(), request, provider_id)
        text = str(result.get("text") or "")
        if not text.strip():
            return {"action": "keep", "error": str(result.get("status") or "empty response")}
        return self._parse_consolidation_verdict(text)

    @staticmethod
    def _parse_consolidation_verdict(text: str) -> dict[str, Any]:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {"action": "keep", "error": "no JSON in LLM response"}
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return {"action": "keep", "error": "invalid JSON in LLM response"}
        if not isinstance(data, dict):
            return {"action": "keep", "error": "unexpected LLM response shape"}
        action = str(data.get("action") or "keep").strip().lower()
        if action not in ArchitectOSService.CONSOLIDATION_ACTIONS:
            action = "keep"
        keep = str(data.get("keep") or "").strip().upper()
        try:
            confidence = float(data.get("confidence") or 0.6)
        except (TypeError, ValueError):
            confidence = 0.6
        return {
            "action": action,
            "keep": keep if keep in {"A", "B"} else ("A" if action == "merge" else ""),
            "reason": str(data.get("reason") or "")[:300],
            "confidence": max(0.0, min(confidence, 1.0)),
        }

    def merge_graph_nodes(self, source_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        target_id = str(payload.get("target_id") or "").strip()
        if not target_id or target_id == source_id:
            raise ValueError("target_id is required")
        source = self.repository.get_node(source_id)
        target = self.repository.get_node(target_id)
        if not source or not target:
            raise ValueError("source and target graph nodes are required")
        # Edge relinks + node updates must commit as one unit (no half-merged graphs).
        with self.repository.transaction():
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
            # Delete + re-add as one unit so a failure cannot drop the old edge alone.
            with self.repository.transaction():
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
                # Edge removal + tombstone update as one unit (no half-deleted graphs).
                with self.repository.transaction():
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
        raw_index: Any = payload.get("message_index")
        index = int(raw_index)
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
        return self.finalize_chat_session(chat_id, force=True, trigger="manual_retry")

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
        skipped: list[dict[str, Any]] = []
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
            stdout, stdout_redacted = self.security_policy.redact_text(cast(str, exc.stdout or "")[:80_000])
            stderr, stderr_redacted = self.security_policy.redact_text(cast(str, exc.stderr or "")[:20_000])
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
        return safe_project_path(root, relative_path)
