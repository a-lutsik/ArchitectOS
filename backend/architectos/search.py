from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, NamedTuple

from .embeddings import content_tokens, expand_query_terms, tokens
from .models import MemoryEdge, MemoryNode, SearchHit

SCOPE_BONUS = {"interface": 40.0, "project": 30.0, "shared": 12.0, "global": 8.0}
TYPE_TRUST = {
    "Decision": 12.0,
    "Constraint": 12.0,
    "Requirement": 10.0,
    "Feature": 4.0,
    "Doc": 3.0,
    "Meeting": 8.0,
    "Lesson": 0.0,
    "Artifact": -2.0,
    "Concept": 0.0,
}
SOURCE_TRUST = {
    "adr": 12.0,
    "azure-boards": 8.0,
    "azure_boards": 8.0,
    "azure-wiki": 7.0,
    "azure-git": 5.0,
    "docs": 5.0,
    "code": 3.0,
    "granola": 10.0,
    "teams-meetings": 8.0,
    "git": 2.0,
    "issues": 2.0,
    "prs": 2.0,
    "project_scan": 0.0,
    "chat": -6.0,
    "manual": 1.0,
}
DEFAULT_CONTEXT_CHAR_BUDGET = 6000
DEFAULT_NODE_TEXT_CHARS = 700
RRF_K = 60.0
RRF_SCALE = 2400.0
STABLE_PACK_TYPES = frozenset({"Constraint", "Requirement", "Decision"})
RULE_LAYER_TYPES = frozenset({"Constraint", "Decision", "Rule"})
RULE_TYPE_PRIORITY = {
    "Constraint": 0,
    "Decision": 1,
    "Rule": 2,
}
BUG_QUERY_TERMS = frozenset({
    "bug", "bugs", "defect", "defects", "баг", "баги", "ошибка", "ошибки",
    "uom", "workitem", "wi",
})
CHAT_SOURCE_KEYS = frozenset({"chat", "memory_candidate", "chat_fact", "chat_fact_keeper"})
ID_PREFIX_TERMS = frozenset({"ab", "ado"})
IDENTIFIER_NOISE_TERMS = frozenset({
    "ab", "ado", "work", "item", "workitem", "wi", "bug", "task", "requirement",
})
WORK_ITEM_QUERY_RE = re.compile(
    r"(?:(?:\b(?:ab|ado)(?:\s*(?:#|:|://))?|#))(\d{3,7})\b",
    re.IGNORECASE,
)
NODE_ID_QUERY_RE = re.compile(r"\b(?:memory://)?(node_[a-z0-9]{6,})\b", re.IGNORECASE)
BARE_WORK_ITEM_RE = re.compile(r"^\s*(\d{3,7})\s*$")
EXACT_WORK_ITEM_BONUS = 400.0
WORK_ITEM_MENTION_BONUS = 80.0
EXACT_NODE_ID_BONUS = 400.0


class MemoryIdentifiers(NamedTuple):
    work_item_ids: tuple[str, ...]
    node_ids: tuple[str, ...]
    identifier_only: bool

    @property
    def any(self) -> bool:
        return bool(self.work_item_ids or self.node_ids)


def parse_memory_identifiers(query: str) -> MemoryIdentifiers:
    """Pull Azure Boards / memory-node ids out of a lookup query like ``AB#79670``."""
    text = str(query or "").strip()
    if not text:
        return MemoryIdentifiers((), (), False)
    work_ids = list(dict.fromkeys(str(item) for item in WORK_ITEM_QUERY_RE.findall(text) if item))
    node_ids = list(dict.fromkeys(str(item).lower() for item in NODE_ID_QUERY_RE.findall(text) if item))
    if not work_ids:
        bare = BARE_WORK_ITEM_RE.fullmatch(text)
        if bare:
            work_ids = [bare.group(1)]
    remainder = text
    for work_id in work_ids:
        remainder = re.sub(
            rf"(?:(?:\b(?:ab|ado)(?:\s*(?:#|:|://))?|#))?{re.escape(work_id)}\b",
            " ",
            remainder,
            flags=re.IGNORECASE,
        )
    for node_id in node_ids:
        remainder = re.sub(rf"(?:memory://)?{re.escape(node_id)}", " ", remainder, flags=re.IGNORECASE)
    leftover = [token for token in content_tokens(remainder) if token not in IDENTIFIER_NOISE_TERMS]
    identifier_only = bool(work_ids or node_ids) and not leftover
    return MemoryIdentifiers(tuple(work_ids), tuple(node_ids), identifier_only)


def search_query_terms(query: str) -> list[str]:
    """Tokenize a memory query, dropping the ``AB#`` prefix when a work-item id is present."""
    idents = parse_memory_identifiers(query)
    terms = expand_query_terms(content_tokens(query))
    if idents.work_item_ids:
        terms = [term for term in terms if term not in ID_PREFIX_TERMS]
        for work_id in reversed(idents.work_item_ids):
            if work_id not in terms:
                terms.insert(0, work_id)
    return terms


def _mentions_work_item(label: str, text: str, work_id: str) -> bool:
    if not work_id:
        return False
    haystack = f"{label} {text}"
    return bool(re.search(rf"(?:AB#|#)?{re.escape(work_id)}\b", haystack, flags=re.IGNORECASE))


def query_has_bug_intent(query_terms: list[str]) -> bool:
    return any(term in BUG_QUERY_TERMS for term in query_terms)


def reciprocal_rank_fusion(
    fts_ranks: dict[str, int] | None,
    vector_ranks: dict[str, int] | None,
    *,
    k: float = RRF_K,
) -> dict[str, float]:
    """Fuse 0-based FTS and vector ranks with classic RRF (1-based ranks, k=60)."""
    k = max(1.0, float(k or RRF_K))
    fused: dict[str, float] = {}
    for node_id, rank in dict(fts_ranks or {}).items():
        fused[str(node_id)] = fused.get(str(node_id), 0.0) + 1.0 / (k + max(0, int(rank)) + 1)
    for node_id, rank in dict(vector_ranks or {}).items():
        fused[str(node_id)] = fused.get(str(node_id), 0.0) + 1.0 / (k + max(0, int(rank)) + 1)
    return fused


def vector_ranks_from_scores(vector_scores: dict[str, float] | None) -> dict[str, int]:
    ordered = sorted(
        ((str(node_id), float(score)) for node_id, score in dict(vector_scores or {}).items()),
        key=lambda item: item[1],
        reverse=True,
    )
    return {node_id: index for index, (node_id, _score) in enumerate(ordered)}


def is_rule_layer_node(node: dict[str, Any] | None) -> bool:
    """Constraint / Decision / Rule — governing limits for Ask advice."""
    if not isinstance(node, dict):
        return False
    return str(node.get("type") or "") in RULE_LAYER_TYPES


def is_stable_pack_node(node: dict[str, Any] | None) -> bool:
    """Pinned / Constraint / long-term decisions — the always-on pack layer."""
    if not isinstance(node, dict):
        return False
    metadata = dict(node.get("metadata") or {})
    if metadata.get("favorite") or metadata.get("pinned"):
        return True
    node_type = str(node.get("type") or "")
    if node_type in STABLE_PACK_TYPES:
        return True
    return str(metadata.get("memory_tier") or "") == "long_term" and node_type in STABLE_PACK_TYPES


class HybridSearchStrategy:
    def __init__(self) -> None:
        self._feedback_scores: dict[str, float] = {}

    def set_feedback_scores(self, scores: dict[str, float] | None) -> None:
        self._feedback_scores = dict(scores or {})

    def search(
        self,
        query: str,
        nodes: list[MemoryNode],
        edges: list[MemoryEdge],
        project_id: str | None,
        scope: str | None,
        limit: int = 8,
        *,
        candidate_ids: set[str] | None = None,
        fts_ranks: dict[str, int] | None = None,
        diversify: bool = True,
        vector_scores: dict[str, float] | None = None,
    ) -> list[SearchHit]:
        idents = parse_memory_identifiers(query)
        query_terms = search_query_terms(query)
        if not query_terms and not idents.any:
            return []

        vector_scores = dict(vector_scores or {})
        fts_ranks = dict(fts_ranks or {})
        rrf_scores = reciprocal_rank_fusion(fts_ranks, vector_ranks_from_scores(vector_scores))
        bug_intent = query_has_bug_intent(query_terms)
        work_item_ids = set(idents.work_item_ids)
        node_ids = set(idents.node_ids)
        eligible_nodes = [
            node
            for node in nodes
            if node.status == "active" and self._scope_matches(node, project_id, scope)
        ]
        if candidate_ids is not None:
            # Keep pinned/favorites even if outside FTS hits.
            forced = {
                node.id
                for node in eligible_nodes
                if bool((node.metadata or {}).get("favorite") or (node.metadata or {}).get("pinned"))
            }
            keep = set(candidate_ids) | forced
            eligible_nodes = [node for node in eligible_nodes if node.id in keep]
        eligible_ids = {node.id for node in eligible_nodes}
        eligible_edges = [edge for edge in edges if edge.source in eligible_ids and edge.target in eligible_ids]
        document_frequency = self._document_frequency(eligible_nodes, query_terms)
        hits: list[SearchHit] = []

        for node in eligible_nodes:
            score, matched_terms, reasons = self._score_node(
                node,
                query_terms,
                document_frequency,
                max(1, len(eligible_nodes)),
                vector_score=vector_scores.get(node.id),
                fts_rank=fts_ranks.get(node.id),
                rrf_score=rrf_scores.get(node.id),
                bug_intent=bug_intent,
                work_item_ids=work_item_ids,
                node_ids=node_ids,
            )
            if score > 0:
                hits.append(SearchHit(node=node, score=score, matched_terms=matched_terms, reasons=reasons))

        hits.sort(key=lambda item: item.score, reverse=True)
        pool = max(limit * 3, limit)
        ranked = hits[:pool]
        exact_ranked = [
            hit for hit in ranked
            if "exact-work-item" in (hit.reasons or []) or "exact-node-id" in (hit.reasons or [])
        ]
        if diversify and ranked:
            exact_ids = {hit.node.id for hit in exact_ranked}
            mixed = self._diversify_hits([hit for hit in ranked if hit.node.id not in exact_ids], limit=max(0, limit))
            combined: list[SearchHit] = []
            seen: set[str] = set()
            for hit in [*exact_ranked, *mixed]:
                if hit.node.id in seen:
                    continue
                seen.add(hit.node.id)
                combined.append(hit)
                if len(combined) >= limit:
                    break
            ranked = combined
        else:
            ranked = ranked[:limit]
        expanded = self._expand_graph(ranked, eligible_nodes, eligible_edges, limit)
        return expanded[:limit]

    def _score_node(
        self,
        node: MemoryNode,
        query_terms: list[str],
        document_frequency: dict[str, int],
        total_nodes: int,
        *,
        vector_score: float | None = None,
        fts_rank: int | None = None,
        rrf_score: float | None = None,
        bug_intent: bool = False,
        work_item_ids: set[str] | None = None,
        node_ids: set[str] | None = None,
    ) -> tuple[float, list[str], list[str]]:
        label_terms = set(tokens(node.label))
        text_terms = set(tokens(node.text))
        label_lower = node.label.lower()
        text_lower = node.text.lower()
        matched_terms: list[str] = []
        reasons: list[str] = []
        score = 0.0

        for term in query_terms:
            weight = math.log((total_nodes + 1) / (document_frequency.get(term, 0) + 1)) + 1.0
            term_score = 0.0
            if term in label_terms:
                term_score += 14.0 * weight
            elif term in label_lower:
                term_score += 5.0 * weight
            if term in text_terms:
                term_score += 7.0 * weight
            elif term in text_lower:
                term_score += 2.5 * weight
            if term_score:
                matched_terms.append(term)
                score += term_score

        # RRF fuses FTS and vector *ranks*; trust bonuses below stay on top.
        if rrf_score is not None and float(rrf_score) > 0:
            rrf_bonus = RRF_SCALE * float(rrf_score)
            score += rrf_bonus
            reasons.append(f"rrf:{rrf_bonus:.0f}")
        if vector_score is not None:
            reasons.append(f"embed:{float(vector_score):.2f}")
        if fts_rank is not None:
            reasons.append(f"fts-rank:{int(fts_rank)}")
        if matched_terms:
            reasons.append("lexical")
        score += SCOPE_BONUS.get(node.scope, 0.0)
        score += 2.0 * node.confidence
        type_bonus = TYPE_TRUST.get(node.type, 0.0)
        if type_bonus:
            score += type_bonus
            reasons.append(f"type:{node.type}")
        metadata = dict(node.metadata or {})
        source_key = str(metadata.get("source") or metadata.get("source_type") or metadata.get("source_key") or "").lower()
        template = str(metadata.get("template") or "").lower()
        source_bonus = SOURCE_TRUST.get(source_key, 0.0)
        if source_bonus:
            score += source_bonus
            reasons.append(f"source:{source_key}")
        # Chat-derived Rules/facts match conversational wrappers too easily; demote them
        # when the user is asking about a real bug / work item.
        chat_derived = source_key in CHAT_SOURCE_KEYS or template in CHAT_SOURCE_KEYS or source_key.startswith("chat")
        if bug_intent and chat_derived:
            score -= 28.0
            reasons.append("chat-penalty")
        if bug_intent and (
            node.type == "Constraint"
            or str(metadata.get("work_item_type") or "").lower() in {"bug", "defect"}
            or source_key in {"azure-boards", "azure_boards"}
        ):
            score += 22.0
            reasons.append("boards-bug-boost")
        lifecycle_score = float(metadata.get("memory_score") or 50.0)
        score += max(-18.0, min(18.0, (lifecycle_score - 50.0) / 2.5))
        tier = str(metadata.get("memory_tier") or "")
        state = str(metadata.get("lifecycle_state") or "")
        if tier == "long_term":
            score += 8.0
            reasons.append("long-term")
        elif tier == "short_term":
            reasons.append("short-term")
        if state == "fresh":
            score += 4.0
        elif state == "fading":
            score -= 4.0
            reasons.append("fading")
        elif state == "stale":
            score -= 10.0
            reasons.append("stale")
        if metadata.get("favorite") or metadata.get("pinned"):
            score += 10.0
            reasons.append("pinned")
        work_ids = {str(item) for item in (work_item_ids or ()) if str(item)}
        exact_node_ids = {str(item) for item in (node_ids or ()) if str(item)}
        node_work_id = str(metadata.get("work_item_id") or metadata.get("workItemId") or "").strip()
        exact_work_item = bool(work_ids and node_work_id in work_ids)
        exact_node = bool(exact_node_ids and node.id in exact_node_ids)
        if exact_work_item:
            score += EXACT_WORK_ITEM_BONUS
            reasons.append("exact-work-item")
        elif exact_node:
            score += EXACT_NODE_ID_BONUS
            reasons.append("exact-node-id")
        elif work_ids and any(_mentions_work_item(node.label, node.text, work_id) for work_id in work_ids):
            score += WORK_ITEM_MENTION_BONUS
            reasons.append("work-item-mention")
        if metadata.get("duplicate"):
            score -= 15.0
            reasons.append("duplicate")
        # Recency: half-life of 60 days counted from the last access (fallback: creation).
        recency_raw = str(metadata.get("last_accessed_at") or getattr(node, "created_at", "") or "")
        if recency_raw:
            try:
                ref = datetime.fromisoformat(recency_raw.replace("Z", "+00:00"))
                if ref.tzinfo is None:
                    ref = ref.replace(tzinfo=timezone.utc)
                age_days = max(0.0, (datetime.now(timezone.utc) - ref).total_seconds() / 86400.0)
                recency_bonus = 8.0 * (0.5 ** (age_days / 60.0))
                score += recency_bonus
                if recency_bonus >= 1.0:
                    reasons.append(f"recency:+{recency_bonus:.0f}")
            except ValueError:
                pass
        feedback_bonus = float(getattr(self, "_feedback_scores", {}).get(node.id, 0.0) or 0.0)
        if feedback_bonus:
            score += max(-8.0, min(12.0, feedback_bonus * 2.0))
            reasons.append(f"feedback:{feedback_bonus:+.1f}")
        exact_identifier = exact_work_item or exact_node
        # Drop vector-only noise that barely cleared min_score and has no lexical/pin/FTS signal.
        if not exact_identifier and not matched_terms and fts_rank is None and vector_score is not None and float(vector_score) < 0.45:
            if not (metadata.get("favorite") or metadata.get("pinned")):
                score *= 0.25
        # No content-term overlap and no strong retrieval signal → keep out of the pack.
        if not exact_identifier and not matched_terms and fts_rank is None and (vector_score is None or float(vector_score) < 0.42):
            if not (metadata.get("favorite") or metadata.get("pinned")):
                return 0.0, [], reasons
        return score, sorted(set(matched_terms)), reasons
    def _scope_matches(self, node: MemoryNode, project_id: str | None, requested_scope: str | None) -> bool:
        if requested_scope:
            if node.scope != requested_scope:
                return False
            if node.scope in {"project", "interface"} and project_id and node.project_id != project_id:
                return False
            return True
        if node.scope in {"project", "interface"} and project_id and node.project_id != project_id:
            return False
        return True

    def _document_frequency(self, nodes: list[MemoryNode], query_terms: list[str]) -> dict[str, int]:
        frequency = dict.fromkeys(query_terms, 0)
        for node in nodes:
            haystack = set(tokens(f"{node.label} {node.text}"))
            for term in query_terms:
                if term in haystack:
                    frequency[term] += 1
        return frequency

    def _diversify_hits(self, hits: list[SearchHit], limit: int, similarity: float = 0.72) -> list[SearchHit]:
        """MMR-ish: keep high-score hits that are not near-duplicates of already chosen ones."""
        selected: list[SearchHit] = []
        selected_bags: list[set[str]] = []
        for hit in hits:
            bag = set(tokens(f"{hit.node.label} {hit.node.text}"))
            if not bag:
                continue
            too_similar = False
            for prior in selected_bags:
                if not prior:
                    continue
                overlap = len(bag & prior) / max(1, len(bag | prior))
                if overlap >= similarity:
                    too_similar = True
                    break
            if too_similar:
                continue
            selected.append(hit)
            selected_bags.append(bag)
            if len(selected) >= limit:
                break
        if len(selected) < limit:
            seen = {item.node.id for item in selected}
            for hit in hits:
                if hit.node.id in seen:
                    continue
                selected.append(hit)
                seen.add(hit.node.id)
                if len(selected) >= limit:
                    break
        return selected

    def _expand_graph(
        self,
        hits: list[SearchHit],
        nodes: list[MemoryNode],
        edges: list[MemoryEdge],
        limit: int,
    ) -> list[SearchHit]:
        if len(hits) >= limit:
            return hits
        by_id = {node.id: node for node in nodes}
        seen = {hit.node.id for hit in hits}
        out = list(hits)
        for hit in hits:
            for edge in edges:
                neighbor_id = ""
                if edge.source == hit.node.id:
                    neighbor_id = edge.target
                elif edge.target == hit.node.id:
                    neighbor_id = edge.source
                if neighbor_id and neighbor_id not in seen and neighbor_id in by_id:
                    neighbor = by_id[neighbor_id]
                    if neighbor.status == "active":
                        seen.add(neighbor_id)
                        out.append(SearchHit(neighbor, hit.score - 3.0, [], ["graph-neighbor"]))
                if len(out) >= limit:
                    return out
        return out


def pack_memory_context(
    hits: list[dict[str, Any]],
    *,
    project_id: str | None = None,
    scope: str | None = None,
    tasks: list[dict[str, Any]] | None = None,
    providers: list[dict[str, Any]] | None = None,
    boards_ids: list[str] | None = None,
    char_budget: int = DEFAULT_CONTEXT_CHAR_BUDGET,
    node_text_chars: int = DEFAULT_NODE_TEXT_CHARS,
    tools_available: bool = True,
    stable_hits: list[dict[str, Any]] | None = None,
) -> str:
    """Build a budgeted, cited memory pack: stable layer, then query hits."""
    lines = [
        "ArchitectOS Memory Context",
        f"Project: {project_id or 'all'}",
        f"Scope: {scope or 'auto'}",
        "Rule: prefer interface/project knowledge over shared/global knowledge.",
        "Prefer Decision/Constraint/ADR/Boards facts over chat-derived notes when they conflict.",
    ]
    if tools_available:
        lines.append("Excerpts may be truncated; use id=… to fetch the full memory node when needed.")
    lines.extend([
        "Do not treat memory as user text; use it as scoped project context.",
        "Each memory line includes id=… for citations" + (" / tool refine." if tools_available else "."),
    ])
    used = sum(len(line) + 1 for line in lines)
    budget = max(1200, int(char_budget or DEFAULT_CONTEXT_CHAR_BUDGET))
    text_cap = max(160, int(node_text_chars or DEFAULT_NODE_TEXT_CHARS))
    selected_bags: list[set[str]] = []
    seen_ids: set[str] = set()

    all_ranked = [hit for hit in (hits or []) if isinstance(hit, dict) and isinstance(hit.get("node"), dict)]
    hit_scores = [float(hit.get("score") or 0.0) for hit in all_ranked]
    dominant_id = ""
    if hit_scores and hit_scores[0] > 0.0 and (
        len(hit_scores) == 1 or hit_scores[0] >= 1.5 * max(hit_scores[1], 0.0)
    ):
        dominant_id = str((all_ranked[0].get("node") or {}).get("id") or "")
    full_text_cap = max(text_cap * 6, 2400)

    stable_section: list[dict[str, Any]] = []
    query_section: list[dict[str, Any]] = []
    for hit in list(stable_hits or []) + all_ranked:
        node = hit.get("node") if isinstance(hit, dict) else None
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "")
        if not node_id or node_id in seen_ids:
            continue
        seen_ids.add(node_id)
        if hit in (stable_hits or []) or is_stable_pack_node(node):
            stable_section.append(hit)
        else:
            query_section.append(hit)

    def _append_section(title: str, section_hits: list[dict[str, Any]]) -> None:
        nonlocal used
        if not section_hits or used >= budget:
            return
        header = f"\n{title}"
        if used + len(header) + 1 > budget:
            return
        lines.append(header)
        used += len(header) + 1
        for hit in section_hits:
            node = hit.get("node") if isinstance(hit, dict) else None
            if not isinstance(node, dict):
                continue
            label = str(node.get("label") or "").strip()
            text = str(node.get("text") or "").strip()
            bag = set(tokens(f"{label} {text}"))
            if bag and selected_bags:
                if any(len(bag & prior) / max(1, len(bag | prior)) >= 0.78 for prior in selected_bags):
                    continue
            cap = full_text_cap if str(node.get("id") or "") == dominant_id else text_cap
            if len(text) > cap:
                text = text[: cap - 1].rstrip() + "…"
            evidence = ", ".join(str(item) for item in (node.get("evidence") or []) if item) or "memory.db"
            meta = dict(node.get("metadata") or {})
            source = str(meta.get("source") or meta.get("source_type") or "").strip() or "memory"
            score = hit.get("score")
            score_bit = f" score={float(score):.1f}" if isinstance(score, (int, float)) else ""
            line = (
                f"- id={node.get('id')} [{node.get('type')}|{node.get('scope')}|{source}] "
                f"{label}: {text} (evidence: {evidence}{score_bit})"
            )
            if used + len(line) + 1 > budget:
                remaining = budget - used - 40
                if remaining < 80:
                    lines.append("- …[context budget reached]")
                    used = budget
                    return
                line = line[:remaining].rstrip() + "…"
                lines.append(line)
                used = budget
                return
            lines.append(line)
            used += len(line) + 1
            if bag:
                selected_bags.append(bag)

    _append_section("Stable memory (pinned / constraints / long-term):", stable_section)
    _append_section("Retrieved for this query:", query_section)
    if not stable_section and not query_section:
        lines.append("")
        lines.append("Retrieved for this query:")
        lines.append("- (no memory hits)")

    if tasks:
        block = ["", "Open Tasks:"]
        for task in tasks[:5]:
            block.append(
                f"- [{task.get('priority')}/{task.get('status')}] {task.get('title')}: {task.get('detail', '')}"
            )
        chunk = "\n".join(block)
        if used + len(chunk) + 1 <= budget:
            lines.extend(block)
            used += len(chunk) + 1

    if providers:
        block = ["", "Enabled Providers:"]
        for provider in providers[:5]:
            block.append(
                f"- {provider.get('label')} ({provider.get('provider_type')}) "
                f"model={provider.get('model') or 'default'}"
            )
        chunk = "\n".join(block)
        if used + len(chunk) + 1 <= budget:
            lines.extend(block)
            used += len(chunk) + 1

    if boards_ids:
        line = f"Known Boards IDs from memory: {', '.join(boards_ids)}"
        if used + len(line) + 2 <= budget:
            lines.extend(["", line])

    return "\n".join(lines).strip()
