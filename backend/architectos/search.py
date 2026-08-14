from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

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
BUG_QUERY_TERMS = frozenset({
    "bug", "bugs", "defect", "defects", "баг", "баги", "ошибка", "ошибки",
    "uom", "workitem", "wi",
})
CHAT_SOURCE_KEYS = frozenset({"chat", "memory_candidate", "chat_fact", "chat_fact_keeper"})


def query_has_bug_intent(query_terms: list[str]) -> bool:
    return any(term in BUG_QUERY_TERMS for term in query_terms)


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
        query_terms = expand_query_terms(content_tokens(query))
        if not query_terms:
            return []

        vector_scores = dict(vector_scores or {})
        fts_ranks = dict(fts_ranks or {})
        bug_intent = query_has_bug_intent(query_terms)
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
                bug_intent=bug_intent,
            )
            if score > 0:
                hits.append(SearchHit(node=node, score=score, matched_terms=matched_terms, reasons=reasons))

        hits.sort(key=lambda item: item.score, reverse=True)
        pool = max(limit * 3, limit)
        ranked = hits[:pool]
        if diversify and ranked:
            ranked = self._diversify_hits(ranked, limit=limit)
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
        bug_intent: bool = False,
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

        # Persisted embedding prefilter scores (Cloudflare/Gemini/etc.) — no live re-embed.
        if vector_score is not None:
            score += 42.0 * max(float(vector_score), 0.0)
            reasons.append(f"embed:{float(vector_score):.2f}")

        if fts_rank is not None:
            # BM25 order matters: rank 0 keeps the full bonus, deep hits decay to a floor.
            fts_bonus = max(15.0, 55.0 - 4.0 * float(fts_rank))
            score += fts_bonus
            reasons.append(f"fts:{fts_bonus:.0f}")
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
        # Drop vector-only noise that barely cleared min_score and has no lexical/pin/FTS signal.
        if not matched_terms and fts_rank is None and vector_score is not None and float(vector_score) < 0.45:
            if not (metadata.get("favorite") or metadata.get("pinned")):
                score *= 0.25
        # No content-term overlap and no strong retrieval signal → keep out of the pack.
        if not matched_terms and fts_rank is None and (vector_score is None or float(vector_score) < 0.42):
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
) -> str:
    """Build a budgeted, cited memory pack for Ask / providers."""
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
        "",
        "Relevant Memory:",
    ])
    used = sum(len(line) + 1 for line in lines)
    budget = max(1200, int(char_budget or DEFAULT_CONTEXT_CHAR_BUDGET))
    text_cap = max(160, int(node_text_chars or DEFAULT_NODE_TEXT_CHARS))
    selected_bags: list[set[str]] = []

    # A dominant top-1 hit (score ≥ 1.5× the runner-up) earns its full text so a
    # clearly-best answer is not weakened by excerpt truncation.
    hit_scores = [
        float(hit.get("score") or 0.0)
        for hit in (hits or [])
        if isinstance(hit, dict) and isinstance(hit.get("node"), dict)
    ]
    dominant_top = bool(hit_scores) and hit_scores[0] > 0.0 and (
        len(hit_scores) == 1 or hit_scores[0] >= 1.5 * max(hit_scores[1], 0.0)
    )
    full_text_cap = max(text_cap * 6, 2400)

    for index, hit in enumerate(hits or []):
        node = hit.get("node") if isinstance(hit, dict) else None
        if not isinstance(node, dict):
            continue
        label = str(node.get("label") or "").strip()
        text = str(node.get("text") or "").strip()
        bag = set(tokens(f"{label} {text}"))
        if bag and selected_bags:
            if any(len(bag & prior) / max(1, len(bag | prior)) >= 0.78 for prior in selected_bags):
                continue
        cap = full_text_cap if (index == 0 and dominant_top) else text_cap
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
                break
            line = line[:remaining].rstrip() + "…"
            lines.append(line)
            used = budget
            break
        lines.append(line)
        used += len(line) + 1
        if bag:
            selected_bags.append(bag)

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
