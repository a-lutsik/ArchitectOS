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

from .storage import SQLiteMemoryRepository

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_\-/#.]{2,}", re.IGNORECASE)

DUPLICATE_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "will", "shall",
    "should", "memory", "candidate", "architectos", "project", "file", "imported",
    # Granola / meeting template boilerplate — shared across unrelated meetings.
    "granola", "meeting", "meetings", "attendees", "attendee", "creator", "note",
    "notes", "date", "gmt", "utc", "gmail.com", "summary", "action", "items",
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
