"""LLM-assisted memory link and consolidation suggestions."""
from __future__ import annotations

import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .adapters import ProviderRequest


class GraphSuggestServiceMixin:
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
        for (score, left, right), verdict in zip(pair_candidates, verdicts, strict=True):
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
            "edge_type": edge_type if edge_type in GraphSuggestServiceMixin.LINK_EDGE_TYPES else "RELATED_TO",
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
        for (score, left, right), verdict in zip(pair_candidates, verdicts, strict=True):
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
        if action not in GraphSuggestServiceMixin.CONSOLIDATION_ACTIONS:
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
