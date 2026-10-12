"""Compaction of stored memory and the Duplicates review queue (dedup stage 4).

``compact_memory`` folds stored facts that another stored fact fully covers.
Only subject groups from the dedup index are examined (same label subject, see
``memory_dedup``), and inside a group a member is merged only into a *keeper*
that already contains everything it says — ``MemoryDedupGate.compare`` must
read the member as a ``duplicate`` of the keeper (exact, same words, or
covered). So no merge can drop a content word, number or identifier. Pinned /
favorite nodes are never merged away, pairs a person marked as different are
skipped, and each merge is a ``dedup`` event that ``undo_memory_dedup`` reverses.

``list_memory_duplicates`` / ``resolve_memory_duplicate`` back the Duplicates
view: write-time flags (possible duplicates, ``mode: suggest`` verdicts,
contradictions) plus recent decisions with undo.
"""
from __future__ import annotations

import logging
from typing import Any

from .chat_memory import _FACT_TYPE_RANK, _prefer_fact_type
from .memory_dedup import features_from_json
from .models import utc_now

_LOG = logging.getLogger(__name__)

_FLAG_KEYS = ("possible_duplicate_of", "possible_duplicate_score", "possible_duplicate_kind", "dedup_suggestion")


def _keeper_rank(node: Any, stem_count: int) -> tuple[Any, ...]:
    """Sort key: fullest first, then the node people rely on most."""
    meta = node.metadata or {}
    return (
        -stem_count,
        0 if (meta.get("pinned") or meta.get("favorite")) else 1,
        0 if meta.get("memory_tier") == "long_term" else 1,
        -int(meta.get("access_count") or 0),
        -float(node.confidence or 0.0),
        str(node.created_at or ""),
        node.id,
    )


class DedupReviewServiceMixin:
    # -- compaction ----------------------------------------------------------
    def compact_memory(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Merge stored facts covered by a fuller stored fact. Dry-run by default.

        Payload: ``project_id`` (omit for every project and shared/global),
        ``dry_run`` (default true), ``max_clusters`` (default from settings).
        """
        payload = dict(payload or {})
        settings = self.memory_dedup_settings()  # type: ignore[attr-defined]
        dry_run = payload.get("dry_run", True) is not False
        if settings["mode"] != "auto":
            # suggest/off: report what would merge, never apply.
            dry_run = True
        project_id = payload.get("project_id")
        project_id = str(project_id) if project_id else None
        try:
            max_clusters = max(1, min(int(payload.get("max_clusters") or settings["compaction_max_clusters"]), 500))
        except (TypeError, ValueError):
            max_clusters = settings["compaction_max_clusters"]
        repository = self.repository  # type: ignore[attr-defined]
        gate = self.memory_dedup  # type: ignore[attr-defined]
        clusters: list[dict[str, Any]] = []
        merged = 0
        groups = repository.dedup_subject_groups(project_id, limit=max_clusters * 4)
        for group in groups:
            if len(clusters) >= max_clusters:
                break
            rows = repository.dedup_subject_rows(group["scope"], group["project_key"] or None, group["subject"])
            for cluster in self._plan_subject_group(rows, gate):
                if len(clusters) >= max_clusters:
                    break
                if not dry_run:
                    applied = []
                    for member in cluster["members"]:
                        try:
                            event_id = self._merge_into_keeper(
                                member["id"], cluster["keeper"]["id"], reason=member["reason"], actor="compaction",
                            )
                        except ValueError as exc:
                            _LOG.warning("compaction merge skipped: %s", exc)
                            continue
                        applied.append({**member, "event_id": event_id})
                        merged += 1
                    cluster["members"] = applied
                    if not applied:
                        continue
                clusters.append(cluster)
        report = {
            "project_id": project_id,
            "dry_run": dry_run,
            "mode": settings["mode"],
            "groups_examined": len(groups),
            "clusters": clusters,
            "merged": merged,
            "would_merge": sum(len(cluster["members"]) for cluster in clusters) if dry_run else 0,
            "at": utc_now(),
        }
        return report

    # Planning is quadratic in the group; a generic label shared by thousands of
    # agent-written facts must not stall the daily pass. The fullest nodes are
    # the likely keepers, so they are the ones kept.
    MAX_GROUP_NODES = 500

    def _plan_subject_group(self, rows: list[dict[str, Any]], gate: Any) -> list[dict[str, Any]]:
        repository = self.repository  # type: ignore[attr-defined]
        if len(rows) > self.MAX_GROUP_NODES:
            parsed = [(row, features_from_json(str(row["features"]))) for row in rows]
            parsed.sort(key=lambda item: (-len(item[1].stems), str(item[0]["node_id"])))
            rows = [row for row, _ in parsed[: self.MAX_GROUP_NODES]]
        entries = []
        for row in rows:
            node = repository.get_node(str(row["node_id"]))
            if node is None or node.status != "active":
                continue
            features = features_from_json(str(row["features"]))
            entries.append((node, features))
        if len(entries) < 2:
            return []
        rejected = repository.dedup_rejected_pairs_for([node.id for node, _ in entries])
        entries.sort(key=lambda entry: _keeper_rank(entry[0], len(entry[1].stems)))
        assigned: set[str] = set()
        plans: list[dict[str, Any]] = []
        for keeper, keeper_features in entries:
            if keeper.id in assigned:
                continue
            members = []
            for node, features in entries:
                if node.id == keeper.id or node.id in assigned:
                    continue
                meta = node.metadata or {}
                if meta.get("pinned") or meta.get("favorite"):
                    continue
                if repository._pair_key(node.id, keeper.id) in rejected:
                    continue
                if not gate.type_matches(str(node.type or ""), str(keeper.type or "")):
                    continue
                verdict = gate.compare(features, keeper_features)
                if verdict.action != "duplicate":
                    continue
                members.append({"id": node.id, "label": node.label, "type": node.type, "reason": verdict.reason})
            if not members:
                continue
            assigned.add(keeper.id)
            assigned.update(member["id"] for member in members)
            plans.append({
                "keeper": {"id": keeper.id, "label": keeper.label, "type": keeper.type, "text": keeper.text},
                "members": members,
            })
        return plans

    def _merge_into_keeper(self, member_id: str, keeper_id: str, *, reason: str, actor: str) -> str:
        """Merge ``member_id`` into ``keeper_id`` keeping the keeper's text; returns the dedup event id."""
        repository = self.repository  # type: ignore[attr-defined]
        keeper = repository.get_node(keeper_id)
        member = repository.get_node(member_id)
        if keeper is None or member is None or keeper.status != "active" or member.status != "active":
            raise ValueError("both nodes must be active to merge")
        result = self.merge_graph_nodes(member_id, {"target_id": keeper_id, "strategy": "keep_best"})  # type: ignore[attr-defined]
        keeper = repository.get_node(keeper_id)
        meta = dict(keeper.metadata or {})
        previous_type = str(keeper.type or "")
        member_type = str(member.type or "")
        if previous_type in _FACT_TYPE_RANK and member_type in _FACT_TYPE_RANK:
            keeper.type = _prefer_fact_type(previous_type, member_type)
        previous_confidence = float(keeper.confidence or 0.0)
        keeper.confidence = max(previous_confidence, float(member.confidence or 0.0))
        meta["dedup_hits"] = int(meta.get("dedup_hits") or 0) + 1
        meta["last_dedup_at"] = utc_now()
        if meta.get("possible_duplicate_of") == member_id:
            for key in _FLAG_KEYS:
                meta.pop(key, None)
        keeper.metadata = meta
        keeper = repository.upsert_node(keeper)
        details: dict[str, Any] = {
            "action": "merge",
            "target_id": keeper_id,
            "merged_id": member_id,
            "merged_label": member.label,
            "reason": reason,
            "added_edge_ids": list(result.get("added_edge_ids") or []),
            "confidence_before": previous_confidence,
        }
        if keeper.type != previous_type:
            details.update({"type_from": previous_type, "type_to": keeper.type})
        try:
            event = repository.record_memory_event(keeper_id, "dedup", actor=actor, details=details)
        except Exception as exc:  # history is best-effort
            _LOG.warning("merge dedup event skipped: %s", exc)
            return ""
        return str(event.get("id") or "")

    def _undo_merge(self, keeper: Any, details: dict[str, Any]) -> dict[str, Any] | None:
        """Reverse a compaction / review merge: reactivate the merged node, drop added edges."""
        repository = self.repository  # type: ignore[attr-defined]
        merged = repository.get_node(str(details.get("merged_id") or ""))
        for edge_id in details.get("added_edge_ids") or []:
            repository.delete_edge(str(edge_id))
        meta = dict(keeper.metadata or {})
        meta["merged_from"] = [item for item in meta.get("merged_from") or [] if item != details.get("merged_id")]
        meta["dedup_hits"] = max(0, int(meta.get("dedup_hits") or 0) - 1)
        type_from = str(details.get("type_from") or "")
        if type_from and keeper.type == details.get("type_to"):
            keeper.type = type_from
        if "confidence_before" in details:
            keeper.confidence = float(details["confidence_before"])
        keeper.metadata = meta
        if merged is None:
            return None
        merged.status = "active"
        merged_meta = dict(merged.metadata or {})
        merged_meta.pop("merged_into", None)
        merged_meta.pop("merged_at", None)
        merged.metadata = merged_meta
        # Remember the decision so the next compaction leaves the pair alone.
        repository.add_dedup_rejected_pair(keeper.id, merged.id)
        return repository.upsert_node(merged).to_dict()

    def run_scheduled_compaction(self) -> dict[str, Any] | None:
        """Daily background pass (decay loop). Applies only in ``mode: auto``."""
        settings = self.memory_dedup_settings()  # type: ignore[attr-defined]
        if settings["mode"] == "off" or not settings["compaction_enabled"]:
            return None
        report = self.compact_memory({"dry_run": settings["mode"] != "auto"})
        summary = {key: report[key] for key in ("at", "dry_run", "mode", "groups_examined", "merged", "would_merge")}
        self.repository.set_setting("memory_dedup_compaction_last", summary)  # type: ignore[attr-defined]
        return report

    # -- review queue ----------------------------------------------------------
    def list_memory_duplicates(self, project_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        """Open review items (flags, suggestions, conflicts) and recent decisions."""
        repository = self.repository  # type: ignore[attr-defined]
        nodes = {node.id: node for node in repository.list_nodes()}
        items: list[dict[str, Any]] = []
        flagged = []
        for node in nodes.values():
            if node.status != "active":
                continue
            if project_id and node.project_id not in {project_id, None}:
                continue
            meta = node.metadata or {}
            target_id = str(meta.get("possible_duplicate_of") or meta.get("conflicts_with") or "")
            if target_id and (meta.get("possible_duplicate_of") or meta.get("needs_review") == "conflict"):
                flagged.append((node, target_id))
        rejected = repository.dedup_rejected_pairs_for([node.id for node, _ in flagged])
        for node, target_id in flagged:
            target = nodes.get(target_id)
            if target is None or target.status != "active":
                continue
            if repository._pair_key(node.id, target_id) in rejected:
                continue
            meta = node.metadata or {}
            suggestion = dict(meta.get("dedup_suggestion") or {})
            if meta.get("needs_review") == "conflict":
                kind = "conflict"
            elif suggestion:
                kind = "suggestion"
            else:
                kind = "possible_duplicate"
            items.append({
                "kind": kind,
                "node": self._review_node(node),
                "target": self._review_node(target),
                "score": meta.get("possible_duplicate_score"),
                "near_kind": meta.get("possible_duplicate_kind") or "lexical",
                "suggested_action": suggestion.get("action") or "",
                "reason": suggestion.get("reason") or "",
            })
        # Newest first, contradictions ahead of duplicate flags.
        items.sort(key=lambda item: str(item["node"]["updated_at"]), reverse=True)
        items.sort(key=lambda item: item["kind"] != "conflict")
        recent = []
        for event in repository.list_recent_dedup_events(limit=max(1, min(int(limit or 100), 200))):
            holder = nodes.get(str(event["node_id"]))
            if holder is None:
                continue
            if project_id and holder.project_id not in {project_id, None}:
                continue
            details = event["details"]
            recent.append({
                "event_id": event["id"],
                "action": details.get("action") or "",
                "node": self._review_node(holder),
                "target_id": details.get("merged_id") or details.get("target_id") or "",
                "target_label": details.get("merged_label") or details.get("target_label") or "",
                "reason": details.get("reason") or "",
                "timestamp": event["timestamp"],
                "actor": event["actor"],
                "undone": event["id"] in (holder.metadata or {}).get("dedup_undone", []),
            })
        return {
            "project_id": project_id,
            "items": items[: max(1, int(limit or 100))],
            "recent": recent,
            "compaction_last": repository.get_setting("memory_dedup_compaction_last") or {},
            "settings": self.memory_dedup_settings(),  # type: ignore[attr-defined]
        }

    @staticmethod
    def _review_node(node: Any) -> dict[str, Any]:
        return {
            "id": node.id,
            "label": node.label,
            "type": node.type,
            "text": str(node.text or "")[:600],
            "status": node.status,
            "updated_at": node.updated_at,
        }

    def resolve_memory_duplicate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Act on one review item.

        ``action``: ``merge`` (``keep``: ``target`` (default) or ``node``; the
        other node is merged into it, keeper text unchanged, undoable),
        ``distinct`` (remember the pair as different facts and clear the flag),
        ``dismiss`` (clear the flag / conflict marker only).
        """
        repository = self.repository  # type: ignore[attr-defined]
        node_id = str(payload.get("node_id") or "").strip()
        target_id = str(payload.get("target_id") or "").strip()
        action = str(payload.get("action") or "").strip().lower()
        node = repository.get_node(node_id) if node_id else None
        if node is None:
            raise ValueError("memory not found")
        if action == "merge":
            if not target_id or target_id == node_id:
                raise ValueError("target_id is required")
            keep = str(payload.get("keep") or "target").strip().lower()
            keeper_id, member_id = (target_id, node_id) if keep != "node" else (node_id, target_id)
            self._clear_review_flags(node, conflict=True)
            event_id = self._merge_into_keeper(member_id, keeper_id, reason="merged in review", actor="review")
            keeper = repository.get_node(keeper_id)
            return {"action": "merge", "event_id": event_id, "keeper": keeper.to_dict() if keeper else None}
        if action == "distinct":
            if not target_id:
                raise ValueError("target_id is required")
            repository.add_dedup_rejected_pair(node_id, target_id)
            node = self._clear_review_flags(node, conflict=False)
            return {"action": "distinct", "memory": node.to_dict()}
        if action == "dismiss":
            node = self._clear_review_flags(node, conflict=True)
            return {"action": "dismiss", "memory": node.to_dict()}
        raise ValueError("action must be merge, distinct or dismiss")

    def _clear_review_flags(self, node: Any, *, conflict: bool) -> Any:
        meta = dict(node.metadata or {})
        for key in _FLAG_KEYS:
            meta.pop(key, None)
        if conflict and meta.get("needs_review") == "conflict":
            meta.pop("needs_review", None)
            meta["conflict_reviewed_at"] = utc_now()
        node.metadata = meta
        return self.repository.upsert_node(node)  # type: ignore[attr-defined]
