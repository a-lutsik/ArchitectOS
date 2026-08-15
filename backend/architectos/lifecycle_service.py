"""Memory lifecycle engine + tier/decay entry points for :class:`ArchitectOSService`.

Extracted from ``service.py``. ``MemoryLifecycleEngine`` is a standalone class
(instantiated in ``ArchitectOSService.__init__`` as ``self.memory_lifecycle``);
``LifecycleServiceMixin`` carries the service-level methods that drive it
(decay runs, tier reclassification, long-term promotion, favorites, and the
tier/state-filtered memory item listing). Depends only on leaf modules (never
on ``service`` itself), so importing it introduces no import cycle. Repository
access in mixin methods is reached through ``self`` via the MRO.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .chat_memory import (
    DEFAULT_CHAT_CANDIDATE_TTL_DAYS,
    DEFAULT_CHAT_MEMORY_MODE,
    normalize_chat_memory_mode,
)
from .models import utc_now
from .storage import SQLiteMemoryRepository

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



class LifecycleServiceMixin:
    """Decay/reclassify/promote entry points and memory item listing."""

    def toggle_memory_favorite(self, node_id: str) -> dict[str, Any]:
        return self.repository.toggle_memory_favorite(node_id).to_dict()

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
