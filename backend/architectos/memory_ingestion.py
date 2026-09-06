"""Candidate dedup engine and shared token helpers for memory ingest.

Split out of ``ingestion_service`` so the scheduling/rescan mixin no longer
carries the fingerprint/similarity engine. ``graph_autolinker`` and
``ArchitectOSService`` import from here (with re-exports kept on
``ingestion_service`` for compatibility).
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .candidate_identity import (
    CHAT_DUMP_TEMPLATE,
    CHAT_KEEPER_TEMPLATES,
    FACT_ATOM_TEMPLATES,
    candidate_origin_key,
    origin_key_from_memory_node,
)
from .code_revision import (
    build_content_metadata,
    code_revision_delta,
    content_hash,
)
from .models import stable_id
from .storage import SQLiteMemoryRepository

CANDIDATE_DUPLICATE_KEYS = (
    "duplicate",
    "duplicate_kind",
    "duplicate_of",
    "duplicate_label",
    "duplicate_score",
    "duplicate_reason",
)

CANDIDATE_REVISION_KEYS = (
    "revision",
    "revision_of",
    "revision_of_label",
    "change_kind",
    "change_summary",
)

# Path-sticky sources that re-open as a revision when content changes significantly.
REVISIONABLE_SOURCE_TYPES = frozenset({
    "code", "docs", "adr", "issue", "pr", "meeting", "inbox",
})

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_\-/#.]{2,}", re.IGNORECASE)

DUPLICATE_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "will", "shall",
    "should", "memory", "candidate", "architectos", "project", "file", "imported",
    # Granola / meeting template boilerplate — shared across unrelated meetings.
    "granola", "meeting", "meetings", "attendees", "attendee", "creator", "note",
    "notes", "date", "gmt", "utc", "gmail.com", "summary", "action", "items",
}

CHAT_FACT_BOILERPLATE_PREFIXES = (
    "Durable fact extracted from a finished chat (not a full transcript).",
    "Durable fact extracted from Ask (not a full transcript).",
    "Durable fact captured from an MCP agent turn (not a full transcript).",
    "Durable fact extracted from chat (not a full transcript).",
)


def _strip_chat_fact_boilerplate(text: str) -> str:
    stripped = (text or "").strip()
    for prefix in CHAT_FACT_BOILERPLATE_PREFIXES:
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip()
    return stripped


def _fingerprintable_text(label: str, body: str, metadata: dict[str, Any] | None = None) -> str:
    meta = dict(metadata or {})
    template = str(meta.get("template") or "").strip().lower()
    if template in FACT_ATOM_TEMPLATES:
        fact_key = str(meta.get("fact_key") or "").strip()
        if fact_key:
            return fact_key
    return f"{label or ''} {_strip_chat_fact_boilerplate(body or '')}".strip()


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
        self.skipped_reviewed = 0

    def prepare_candidates(self, project_id: str, candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        references = self._reference_items(project_id)
        by_fingerprint: dict[str, dict[str, Any]] = {}
        for reference in references:
            fingerprint = str(reference.get("fingerprint") or "")
            if not fingerprint:
                continue
            existing = by_fingerprint.get(fingerprint)
            if existing is None or (existing.get("kind") != "memory" and reference.get("kind") == "memory"):
                by_fingerprint[fingerprint] = reference
        decided_origins, pending_by_origin, live_by_origin = self._origin_indexes(project_id)
        prepared: list[dict[str, Any]] = []
        seen_fingerprints: set[str] = set()
        skipped_reviewed = 0
        for candidate in candidates:
            enriched = self._enrich_candidate(candidate)
            metadata = dict(enriched.get("metadata") or {})
            template = str(metadata.get("template") or "").strip().lower()
            is_fact_atom = template in FACT_ATOM_TEMPLATES
            origin_key = candidate_origin_key(enriched)
            if origin_key:
                enriched["metadata"]["origin_key"] = origin_key
                revision_hit = self._classify_origin_revision(enriched, origin_key, live_by_origin)
                if revision_hit is False:
                    # Identical or noise-only change against a live/decided origin.
                    skipped_reviewed += 1
                    continue
                if isinstance(revision_hit, dict):
                    if revision_hit:
                        enriched["metadata"].update(revision_hit)
                        for key in CANDIDATE_DUPLICATE_KEYS:
                            enriched["metadata"].pop(key, None)
                        enriched["metadata"]["duplicate"] = False
                    # Empty dict = allow re-queue after reject with changed content.
                elif origin_key in decided_origins:
                    skipped_reviewed += 1
                    continue
                existing_pending = pending_by_origin.get(origin_key)
                if existing_pending and existing_pending.get("id"):
                    enriched["id"] = existing_pending["id"]
            fingerprint = str(enriched["metadata"]["fingerprint"])
            if fingerprint in seen_fingerprints:
                if is_fact_atom:
                    skipped_reviewed += 1
                    continue
                if not enriched["metadata"].get("revision"):
                    enriched["metadata"]["duplicate"] = True
                    enriched["metadata"]["duplicate_kind"] = "candidate"
                    enriched["metadata"]["duplicate_reason"] = "same ingestion batch fingerprint"
            if not enriched["metadata"].get("revision"):
                duplicate = self._find_duplicate(enriched, references, by_fingerprint)
                if duplicate:
                    subject_rev = self._fact_subject_revision(enriched, duplicate, references)
                    if subject_rev:
                        enriched["metadata"].update(subject_rev)
                        for key in CANDIDATE_DUPLICATE_KEYS:
                            enriched["metadata"].pop(key, None)
                        enriched["metadata"]["duplicate"] = False
                    else:
                        dup_kind = str(duplicate.get("duplicate_kind") or "")
                        if is_fact_atom and dup_kind in {"candidate", "memory"}:
                            skipped_reviewed += 1
                            continue
                        enriched["metadata"].update(duplicate)
                        enriched["confidence"] = min(float(enriched.get("confidence") or 0.62), 0.45)
            if (
                enriched["metadata"].get("duplicate")
                and str(enriched["metadata"].get("duplicate_kind") or "") == "candidate"
                and not enriched.get("id")
            ):
                # Keep extras visible without overwriting the unmarked original's stable id.
                extra_key = str(enriched["metadata"].get("duplicate_of") or fingerprint)
                enriched["id"] = stable_id("candidate-dup", extra_key, str(len(prepared)))
            prepared.append(enriched)
            seen_fingerprints.add(fingerprint)
            if origin_key:
                pending_by_origin.setdefault(origin_key, enriched)
            reference = self._reference_from_candidate(enriched)
            references.append(reference)
            if fingerprint not in by_fingerprint:
                by_fingerprint[fingerprint] = reference
            if limit > 0 and len(prepared) >= limit:
                break
        self.skipped_reviewed = skipped_reviewed
        return prepared

    def chat_ids_with_keeper(self, project_id: str) -> set[str]:
        """Chat threads already owned by session-keeper (or promoted chat memory)."""
        owned: set[str] = set()
        for item in self.repository.list_memory_candidates(project_id, status=None, limit=None):
            meta = dict(item.get("metadata") or {})
            if str(meta.get("template") or "") not in CHAT_KEEPER_TEMPLATES:
                continue
            chat_id = str(meta.get("chat_id") or item.get("source_ref") or "").strip()
            if chat_id:
                owned.add(chat_id)
        for node in self.repository.list_nodes():
            if node.status != "active" or node.project_id not in {project_id, None}:
                continue
            meta = dict(node.metadata or {})
            template = str(meta.get("template") or "")
            chat_id = str(meta.get("chat_id") or "").strip()
            if template not in CHAT_KEEPER_TEMPLATES and not chat_id:
                continue
            if not chat_id:
                chat_id = str(meta.get("source_ref") or "").strip()
            if chat_id:
                owned.add(chat_id)
        return owned

    def retire_autoscan_chat_dumps(self, project_id: str, chat_id: str | None = None) -> list[str]:
        """Drop pending AutoScan transcript dumps once the session is finalized.

        Pass ``chat_id`` after Ask finalize (including when facts were already
        captured as MCP and no Ask keeper card was written). Without ``chat_id``,
        only dumps whose thread already has a session-keeper card are removed.
        """
        if chat_id:
            target = {str(chat_id).strip()}
            target.discard("")
        else:
            target = self.chat_ids_with_keeper(project_id)
        if not target:
            return []
        removed: list[str] = []
        for item in self.repository.list_memory_candidates(project_id, status="candidate", limit=None):
            meta = dict(item.get("metadata") or {})
            if str(meta.get("template") or "") != CHAT_DUMP_TEMPLATE:
                continue
            item_chat = str(meta.get("chat_id") or item.get("source_ref") or "").strip()
            if not item_chat or item_chat not in target:
                continue
            cid = str(item.get("id") or "")
            if cid and self.repository.delete_memory_candidate(cid):
                removed.append(cid)
        if removed:
            self.refresh_candidate_duplicate_flags(project_id)
        return removed

    def refresh_candidate_duplicate_flags(self, project_id: str) -> int:
        """Recompute duplicate badges and leave exactly one unmarked keeper per cluster."""
        pending = self.repository.list_memory_candidates(project_id, status="candidate", limit=None)
        if not pending:
            return 0

        # Memory-only references for "already in memory" checks.
        memory_refs: list[dict[str, Any]] = []
        by_fingerprint: dict[str, dict[str, Any]] = {}
        for node in self.repository.list_nodes():
            if node.status != "active" or node.project_id not in {project_id, None}:
                continue
            meta = dict(node.metadata or {})
            fp_text = _fingerprintable_text(str(node.label or ""), str(node.text or ""), meta)
            ref = {
                "id": node.id,
                "label": node.label,
                "kind": "memory",
                "tokens": self._tokens(fp_text),
                "fingerprint": self._fingerprint(fp_text),
                "meeting_id": str(meta.get("meeting_id") or ""),
                "work_item_id": str(meta.get("work_item_id") or ""),
                "source_type": str(meta.get("source") or meta.get("source_type") or ""),
                "source_ref": str(meta.get("source_ref") or meta.get("granola_url") or ""),
                "origin_key": origin_key_from_memory_node(node),
            }
            memory_refs.append(ref)
            fp = str(ref.get("fingerprint") or "")
            if fp and fp not in by_fingerprint:
                by_fingerprint[fp] = ref

        by_id = {str(item.get("id") or ""): item for item in pending if item.get("id")}
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        fingerprint_groups: dict[str, list[str]] = {}
        for item in pending:
            cid = str(item.get("id") or "")
            if not cid:
                continue
            parent.setdefault(cid, cid)
            meta = dict(item.get("metadata") or {})
            dup_of = str(meta.get("duplicate_of") or "").strip()
            if dup_of and dup_of in by_id:
                union(cid, dup_of)
            # Also link via similarity against other pending (exact fingerprint).
            fp = str(
                meta.get("fingerprint")
                or self._fingerprint(_fingerprintable_text(
                    str(item.get("label") or ""),
                    str(item.get("text") or ""),
                    dict(item.get("metadata") or {}),
                ))
            ).strip()
            if fp:
                fingerprint_groups.setdefault(fp, []).append(cid)

        for ids in fingerprint_groups.values():
            if len(ids) < 2:
                continue
            root = ids[0]
            for other in ids[1:]:
                union(root, other)

        # Similarity linking for near-duplicates without shared fingerprint.
        pending_refs = [self._reference_from_candidate(item) for item in pending]
        for i, left in enumerate(pending):
            left_id = str(left.get("id") or "")
            if not left_id:
                continue
            left_meta = dict(left.get("metadata") or {})
            left_tokens = set(left_meta.get("tokens") or pending_refs[i].get("tokens") or [])
            for j in range(i + 1, len(pending)):
                right = pending[j]
                right_id = str(right.get("id") or "")
                if not right_id or find(left_id) == find(right_id):
                    continue
                right_meta = dict(right.get("metadata") or {})
                right_tokens = set(right_meta.get("tokens") or pending_refs[j].get("tokens") or [])
                score = self._similarity(left_tokens, right_tokens)
                threshold = 0.82 if str(left.get("source_type") or "") == "granola" and len(left_tokens) < 16 else 0.68
                if score >= threshold:
                    union(left_id, right_id)

        clusters: dict[str, list[dict[str, Any]]] = {}
        for item in pending:
            cid = str(item.get("id") or "")
            if not cid:
                continue
            clusters.setdefault(find(cid), []).append(item)

        updated = 0
        for members in clusters.values():
            memory_hits = self._cluster_memory_hits(members, memory_refs, by_fingerprint)
            updated += self._apply_cluster_duplicate_tags(members, memory_hits)
        return updated

    def _cluster_memory_hits(
        self,
        members: list[dict[str, Any]],
        memory_refs: list[dict[str, Any]],
        by_fingerprint: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        memory_hits: dict[str, dict[str, Any]] = {}
        for item in members:
            probe = dict(item)
            probe_meta = {
                k: v
                for k, v in dict(item.get("metadata") or {}).items()
                if k not in CANDIDATE_DUPLICATE_KEYS
            }
            probe_meta["duplicate"] = False
            fp_text = _fingerprintable_text(
                str(item.get("label") or ""),
                str(item.get("text") or ""),
                dict(item.get("metadata") or {}),
            )
            probe_meta.setdefault("fingerprint", self._fingerprint(fp_text))
            probe_meta.setdefault("tokens", sorted(self._tokens(fp_text)))
            probe["metadata"] = probe_meta
            hit = self._find_duplicate(probe, memory_refs, by_fingerprint)
            if hit:
                memory_hits[str(item.get("id") or "")] = hit
        return memory_hits

    def _clear_duplicate_metadata(self, meta: dict[str, Any]) -> dict[str, Any]:
        new_meta = dict(meta)
        new_meta["duplicate"] = False
        for key in CANDIDATE_DUPLICATE_KEYS:
            if key != "duplicate":
                new_meta.pop(key, None)
        return new_meta

    def _apply_cluster_duplicate_tags(
        self,
        members: list[dict[str, Any]],
        memory_hits: dict[str, dict[str, Any]],
    ) -> int:
        if not members:
            return 0

        updated = 0
        if len(members) == 1:
            item = members[0]
            cid = str(item.get("id") or "")
            meta = dict(item.get("metadata") or {})
            mem_hit = memory_hits.get(cid)
            new_meta = dict(meta)
            if mem_hit:
                new_meta.update(mem_hit)
            else:
                new_meta = self._clear_duplicate_metadata(meta)
            if new_meta != meta:
                item["metadata"] = new_meta
                self.repository.update_memory_candidate(item)
                updated += 1
            return updated

        if memory_hits:
            hit_members = [item for item in members if str(item.get("id") or "") in memory_hits]
            representative = self._pick_cluster_keeper(hit_members, prefer_unmarked=False)
            if not representative:
                return 0
            rep_id = str(representative.get("id") or "")
            rep_label = str(representative.get("label") or "")
            rep_hit = dict(memory_hits.get(rep_id) or {})

            for item in members:
                cid = str(item.get("id") or "")
                meta = dict(item.get("metadata") or {})
                new_meta = dict(meta)
                if cid == rep_id:
                    new_meta.update(rep_hit)
                else:
                    new_meta.update({
                        "duplicate": True,
                        "duplicate_kind": "candidate",
                        "duplicate_of": rep_id,
                        "duplicate_label": rep_label,
                        "duplicate_score": rep_hit.get("duplicate_score", 1.0),
                        "duplicate_reason": "canonicalized pending cluster with memory match",
                    })
                if new_meta != meta:
                    item["metadata"] = new_meta
                    self.repository.update_memory_candidate(item)
                    updated += 1
            return updated

        keeper = self._pick_cluster_keeper(members, prefer_unmarked=True)
        if not keeper:
            return 0
        keeper_id = str(keeper.get("id") or "")
        keeper_label = str(keeper.get("label") or "")

        for item in members:
            cid = str(item.get("id") or "")
            meta = dict(item.get("metadata") or {})
            new_meta = dict(meta)
            if cid == keeper_id:
                new_meta = self._clear_duplicate_metadata(meta)
            else:
                new_meta.update({
                    "duplicate": True,
                    "duplicate_kind": "candidate",
                    "duplicate_of": keeper_id,
                    "duplicate_label": keeper_label,
                    "duplicate_score": 1.0,
                    "duplicate_reason": "canonicalized pending cluster",
                })
            if new_meta != meta:
                item["metadata"] = new_meta
                self.repository.update_memory_candidate(item)
                updated += 1
        return updated

    def _pick_cluster_keeper(
        self,
        members: list[dict[str, Any]],
        *,
        prefer_unmarked: bool = True,
    ) -> dict[str, Any] | None:
        if not members:
            return None

        def sort_key(item: dict[str, Any]) -> tuple:
            meta = dict(item.get("metadata") or {})
            is_dup = 1 if (prefer_unmarked and bool(meta.get("duplicate"))) else 0
            created = str(item.get("created_at") or "")
            cid = str(item.get("id") or "")
            return (is_dup, created, cid)

        return sorted(members, key=sort_key)[0]

    def _origin_indexes(
        self, project_id: str
    ) -> tuple[set[str], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        decided: set[str] = set()
        pending: dict[str, dict[str, Any]] = {}
        live_by_origin: dict[str, dict[str, Any]] = {}
        for item in self.repository.list_memory_candidates(project_id, status=None, limit=None):
            key = candidate_origin_key(item)
            if not key:
                continue
            status = str(item.get("status") or "")
            if status in {"promoted", "rejected"}:
                decided.add(key)
            elif status in {"candidate", "duplicate"} and key not in pending:
                pending[key] = item
        for node in self.repository.list_nodes():
            if node.status != "active" or node.project_id not in {project_id, None}:
                continue
            meta = dict(node.metadata or {})
            if meta.get("invalid_at"):
                # Superseded facts stay in store for as_of but are not live origins.
                continue
            key = origin_key_from_memory_node(node)
            if not key:
                continue
            decided.add(key)
            live_by_origin[key] = {
                "id": node.id,
                "label": node.label,
                "kind": "memory",
                "metadata": meta,
                "fingerprint": self._fingerprint(
                    _fingerprintable_text(str(node.label or ""), str(node.text or ""), meta)
                ),
                "created_at": str(getattr(node, "created_at", "") or ""),
            }
        return decided, pending, live_by_origin

    def _classify_origin_revision(
        self,
        candidate: dict[str, Any],
        origin_key: str,
        live_by_origin: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | bool | None:
        """Return revision metadata, False to skip, or None for default sticky skip.

        - False → skip (identical / noise)
        - dict (non-empty) → significant revision against live memory
        - dict (empty) → allow re-queue (e.g. rejected origin, content changed)
        - None → not revisionable; caller applies sticky decided skip
        """
        metadata = dict(candidate.get("metadata") or {})
        source_type = str(candidate.get("source_type") or metadata.get("source_type") or "").strip().lower()
        is_fact_subject = origin_key.startswith("fact_subject:")
        is_revisionable = source_type in REVISIONABLE_SOURCE_TYPES or is_fact_subject
        if not is_revisionable:
            return None

        live = live_by_origin.get(origin_key)
        if not live:
            return self._revision_vs_decided_candidate(candidate, origin_key)

        old_meta = dict(live.get("metadata") or {})
        old_hash = str(old_meta.get("content_hash") or "").strip()
        new_hash = str(metadata.get("content_hash") or "").strip()
        extension = str(metadata.get("extension") or ".py")
        source_text = str(metadata.get("source_text") or candidate.get("text") or "")

        if is_fact_subject:
            old_claim = str(old_meta.get("fact_key") or live.get("fingerprint") or "").strip()
            new_claim = str(metadata.get("fact_key") or metadata.get("fingerprint") or "").strip()
            if old_claim and new_claim and old_claim == new_claim:
                return False
            if old_claim and new_claim and old_claim != new_claim:
                return {
                    "revision": True,
                    "revision_of": live.get("id"),
                    "revision_of_label": live.get("label"),
                    "change_kind": "logic",
                    "change_summary": {
                        "text": "claim updated for same subject",
                        "old_claim": old_claim[:200],
                        "new_claim": new_claim[:200],
                    },
                    "content_hash": new_hash or content_hash(new_claim),
                }
            return None

        if not new_hash and source_text:
            built = build_content_metadata(source_text, extension)
            metadata.update(built)
            candidate["metadata"] = metadata
            new_hash = str(built.get("content_hash") or "")

        if old_hash and new_hash and old_hash == new_hash:
            return False

        delta = code_revision_delta(old_meta, source_text, extension=extension, new_meta=metadata)
        if not delta.significant:
            return False
        payload = delta.to_metadata()
        payload["revision"] = True
        payload["revision_of"] = live.get("id")
        payload["revision_of_label"] = live.get("label")
        return payload

    def _revision_vs_decided_candidate(
        self,
        candidate: dict[str, Any],
        origin_key: str,
    ) -> dict[str, Any] | bool | None:
        """When origin was rejected/promoted but no live node remains, compare hashes."""
        metadata = dict(candidate.get("metadata") or {})
        new_hash = str(metadata.get("content_hash") or "").strip()
        best_hash = ""
        best_status = ""
        for item in self.repository.list_memory_candidates(
            str(candidate.get("project_id") or ""), status=None, limit=None
        ):
            if candidate_origin_key(item) != origin_key:
                continue
            status = str(item.get("status") or "")
            if status not in {"promoted", "rejected"}:
                continue
            item_hash = str(dict(item.get("metadata") or {}).get("content_hash") or "").strip()
            if item_hash:
                best_hash = item_hash
                best_status = status
        if not best_hash:
            return None
        if new_hash and new_hash == best_hash:
            return False
        if new_hash and new_hash != best_hash:
            if best_status == "rejected":
                return {}
            return None
        return None

    def _fact_subject_revision(
        self,
        candidate: dict[str, Any],
        duplicate: dict[str, Any],
        references: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Promote a near-dup to revision when both share fact_subject and differ in claim."""
        metadata = dict(candidate.get("metadata") or {})
        subject = str(metadata.get("fact_subject") or "").strip()
        if not subject:
            return None
        template = str(metadata.get("template") or "").strip().lower()
        if template not in FACT_ATOM_TEMPLATES:
            return None
        dup_id = str(duplicate.get("duplicate_of") or "")
        ref = next((r for r in references if str(r.get("id") or "") == dup_id), None)
        if not ref:
            return None
        ref_subject = ""
        old_claim = ""
        if ref.get("kind") == "memory":
            node = self.repository.get_node(dup_id)
            if node:
                node_meta = dict(node.metadata or {})
                ref_subject = str(node_meta.get("fact_subject") or "").strip()
                old_claim = str(node_meta.get("fact_key") or "").strip()
        else:
            pending = self.repository.get_memory_candidate(dup_id)
            if pending:
                pending_meta = dict(pending.get("metadata") or {})
                ref_subject = str(pending_meta.get("fact_subject") or "").strip()
                old_claim = str(pending_meta.get("fact_key") or "").strip()
        if not ref_subject or ref_subject.lower() != subject.lower():
            return None
        new_claim = str(metadata.get("fact_key") or "").strip()
        if new_claim and old_claim and new_claim == old_claim:
            return None
        if not new_claim or not old_claim:
            if str(metadata.get("fingerprint") or "") == str(ref.get("fingerprint") or ""):
                return None
        return {
            "revision": True,
            "revision_of": dup_id,
            "revision_of_label": duplicate.get("duplicate_label") or ref.get("label"),
            "change_kind": "logic",
            "change_summary": {
                "text": "claim updated for same subject",
                "old_claim": (old_claim or str(ref.get("fingerprint") or ""))[:200],
                "new_claim": (new_claim or str(metadata.get("fingerprint") or ""))[:200],
            },
        }

    def _reference_items(self, project_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for node in self.repository.list_nodes():
            if node.status != "active" or node.project_id not in {project_id, None}:
                continue
            meta = dict(node.metadata or {})
            if meta.get("invalid_at"):
                continue
            fp_text = _fingerprintable_text(str(node.label or ""), str(node.text or ""), meta)
            items.append({
                "id": node.id,
                "label": node.label,
                "kind": "memory",
                "tokens": self._tokens(fp_text),
                "fingerprint": self._fingerprint(fp_text),
                "meeting_id": str(meta.get("meeting_id") or ""),
                "work_item_id": str(meta.get("work_item_id") or ""),
                "source_type": str(meta.get("source") or meta.get("source_type") or ""),
                "source_ref": str(meta.get("source_ref") or meta.get("granola_url") or ""),
                "origin_key": origin_key_from_memory_node(node),
                "content_hash": str(meta.get("content_hash") or ""),
                "fact_subject": str(meta.get("fact_subject") or ""),
            })
        for candidate in self.repository.list_memory_candidates(project_id, status=None, limit=500):
            if candidate.get("status") not in {"candidate", "duplicate"}:
                continue
            items.append(self._reference_from_candidate(candidate))
        return items

    def _reference_from_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(candidate.get("metadata") or {})
        fp_text = _fingerprintable_text(str(candidate.get("label") or ""), str(candidate.get("text") or ""), metadata)
        return {
            "id": str(candidate.get("id") or ""),
            "label": str(candidate.get("label") or ""),
            "kind": "candidate",
            "tokens": set(metadata.get("tokens") or self._tokens(fp_text)),
            "fingerprint": str(metadata.get("fingerprint") or self._fingerprint(fp_text)),
            "meeting_id": str(metadata.get("meeting_id") or ""),
            "work_item_id": str(metadata.get("work_item_id") or ""),
            "source_type": str(candidate.get("source_type") or metadata.get("source") or ""),
            "source_ref": str(candidate.get("source_ref") or metadata.get("granola_url") or ""),
            "origin_key": candidate_origin_key(candidate) or str(metadata.get("origin_key") or ""),
            "content_hash": str(metadata.get("content_hash") or ""),
            "fact_subject": str(metadata.get("fact_subject") or ""),
        }

    def _is_same_queue_item(self, candidate: dict[str, Any], reference: dict[str, Any]) -> bool:
        """True when `reference` is this card being refreshed, not a second copy."""
        if str(reference.get("kind") or "") == "memory":
            return False
        cand_id = str(candidate.get("id") or "")
        ref_id = str(reference.get("id") or "")
        if cand_id and ref_id and cand_id == ref_id:
            return True
        metadata = dict(candidate.get("metadata") or {})
        cand_origin = candidate_origin_key(candidate) or str(metadata.get("origin_key") or "")
        ref_origin = str(reference.get("origin_key") or "")
        if cand_origin and ref_origin and cand_origin == ref_origin:
            return True
        cand_ref = str(candidate.get("source_ref") or metadata.get("granola_url") or "").strip()
        ref_ref = str(reference.get("source_ref") or "").strip()
        cand_source = str(candidate.get("source_type") or metadata.get("source") or "").strip().lower()
        ref_source = str(reference.get("source_type") or "").strip().lower()
        cand_label = str(candidate.get("label") or "").strip()
        ref_label = str(reference.get("label") or "").strip()
        if cand_ref and ref_ref and cand_ref == ref_ref and cand_source == ref_source and cand_label == ref_label:
            return True
        return False

    def _duplicate_hit(self, reference: dict[str, Any], score: float, reason: str = "") -> dict[str, Any]:
        kind = str(reference.get("kind") or "memory")
        if kind not in {"memory", "candidate"}:
            kind = "memory"
        hit = {
            "duplicate": True,
            "duplicate_score": score if score == 1.0 else round(float(score), 3),
            "duplicate_of": reference.get("id"),
            "duplicate_label": reference.get("label"),
            "duplicate_kind": kind,
        }
        if reason:
            hit["duplicate_reason"] = reason
        return hit

    def _enrich_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(candidate)
        metadata = dict(enriched.get("metadata") or {})
        fp_text = _fingerprintable_text(str(enriched.get("label") or ""), str(enriched.get("text") or ""), metadata)
        tokens = sorted(self._tokens(fp_text))
        meeting_id = str(metadata.get("meeting_id") or "").strip()
        work_item_id = str(metadata.get("work_item_id") or "").strip()
        fingerprint = self._fingerprint(fp_text)
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
            if exact and not self._is_same_queue_item(candidate, exact):
                return self._duplicate_hit(exact, 1.0)
        best: dict[str, Any] | None = None
        for reference in references:
            if self._is_same_queue_item(candidate, reference):
                continue
            ref_meeting_id = str(reference.get("meeting_id") or "").strip()
            ref_work_item_id = str(reference.get("work_item_id") or "").strip()
            # Distinct Granola/meeting identities are never textual duplicates of each other.
            if candidate_meeting_id and ref_meeting_id and candidate_meeting_id != ref_meeting_id:
                continue
            if candidate_work_item_id and ref_work_item_id and candidate_work_item_id != ref_work_item_id:
                continue
            if candidate_work_item_id and ref_work_item_id and candidate_work_item_id == ref_work_item_id:
                return self._duplicate_hit(reference, 1.0, "same Azure Boards work item id")
            if by_fingerprint is None and reference.get("fingerprint") == candidate_fingerprint:
                score = 1.0
            else:
                score = self._similarity(candidate_tokens, set(reference.get("tokens") or []))
            # Short Granola stubs are mostly metadata; demand a stricter match.
            threshold = 0.82 if candidate_source == "granola" and len(candidate_tokens) < 16 else 0.68
            if score >= threshold and (not best or score > float(best["duplicate_score"])):
                best = self._duplicate_hit(reference, score)
        return best

    def _tokens(self, text: str) -> set[str]:
        return _token_set(text, DUPLICATE_STOPWORDS)

    def _fingerprint(self, text: str) -> str:
        counts = Counter(self._tokens(text))
        return "|".join(token for token, _count in counts.most_common(18))

    def _similarity(self, left: set[str], right: set[str]) -> float:
        return _token_similarity(left, right, containment_weight=0.85)
