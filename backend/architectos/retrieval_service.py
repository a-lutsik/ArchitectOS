"""Search / retrieval read-layer, extracted from ``service.py``.

``RetrievalServiceMixin`` owns hybrid search (``search_memory``), context
packing, bi-temporal visibility, metadata filtering, the PageRank graph
expansion, the per-source node quota, and retrieval feedback recording. Pure mixin: every method runs on
``self`` (``repository``, ``memory_embeddings``, ``search_strategy``,
``memory_lifecycle``, ``graph_auto_linker``) provided by the concrete
service, so behavior is unchanged.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any

from .graph_analysis import personalized_pagerank
from .models import SearchHit
from .search import pack_memory_context

_LOG = logging.getLogger(__name__)


class RetrievalServiceMixin:
    """Hybrid search, context packing and graph expansion for the service."""

    @staticmethod
    def _apply_relevance_floor(
        hits: list[Any], floor_ratio: float | Any, min_score: float | None
    ) -> tuple[list[Any], dict[str, Any]]:
        """Trim low-relevance hits below a fraction of the best real hit's score.

        Graph-neighbor hits (context injected for their link, not their score) are
        always kept, and the single best hit is never dropped. Returns the surviving
        hits plus a small stats blob for the retrieval envelope.
        """
        try:
            ratio = max(0.0, min(float(floor_ratio), 0.9))
        except (TypeError, ValueError):
            ratio = 0.0
        try:
            abs_floor = float(min_score) if min_score is not None else None
        except (TypeError, ValueError):
            abs_floor = None
        info: dict[str, Any] = {"relevance_floor": round(ratio, 3)}
        if abs_floor is not None:
            info["min_score"] = round(abs_floor, 3)
        if not hits or (ratio <= 0.0 and abs_floor is None):
            return hits, info
        real = [hit for hit in hits if "graph-neighbor" not in (getattr(hit, "reasons", None) or [])]
        if len(real) <= 1:
            return hits, info
        best = max(hit.score for hit in real)
        cutoff = best * ratio
        if abs_floor is not None:
            cutoff = max(cutoff, abs_floor)
        info["floor_cutoff"] = round(cutoff, 1)
        top_id = max(real, key=lambda hit: hit.score).node.id
        kept: list[Any] = []
        dropped = 0
        for hit in hits:
            is_neighbor = "graph-neighbor" in (getattr(hit, "reasons", None) or [])
            if is_neighbor or hit.node.id == top_id or hit.score >= cutoff:
                kept.append(hit)
            else:
                dropped += 1
        info["dropped_low_relevance"] = dropped
        return kept, info

    def search_memory(
        self,
        query: str,
        project_id: str | None = None,
        scope: str | None = None,
        limit: int = 8,
        refresh: bool = False,
        filters: dict[str, Any] | None = None,
        mode: str = "search",
        as_of: str | None = None,
        expand_graph: bool = False,
        include_communities: bool = False,
        min_score: float | None = None,
        relevance_floor: float | None = None,
    ) -> dict[str, Any]:
        # FTS + vector in parallel; vector is best-effort with a hard timeout.
        # Lifecycle access bump runs inline (cheap: notify=False, no re-embedding);
        # refresh=True additionally reloads fresh lifecycle metadata into hits.
        # as_of (ISO date/datetime) = temporal point-in-time query: only nodes
        # created on/before that moment are eligible ("what was true back then").
        # expand_graph=True (LLM context paths) also loads 1-hop graph neighbors of
        # the matched nodes so linked decisions/lessons join the pack even when they
        # do not match the query lexically. Interactive search keeps it off for latency.
        limit = max(1, min(int(limit or 8), 40))
        as_of = str(as_of or "").strip() or None
        normalized_filters = self._normalize_memory_filters(filters)
        # List mode (or a filter-only call): pure metadata listing, no scoring and
        # no lifecycle access bump — browsing must not count as retrieval activity.
        if str(mode or "search").strip().lower() == "list" or (not str(query or "").strip() and normalized_filters):
            return self._list_memory_nodes(project_id, scope, limit, normalized_filters)
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

        if normalized_filters:
            nodes = [node for node in nodes if self._memory_node_matches_filters(node, normalized_filters)]
        # Bi-temporal visibility: hide superseded facts on live search; on an
        # as_of query show only what was actually true at that moment.
        nodes = [node for node in nodes if self._memory_node_visible_at(node, as_of)]
        if normalized_filters and not nodes:
            return {
                    "query": query,
                    "hits": [],
                    "retrieval": {
                        "mode": f"{mode}-filtered",
                        "fts_hits": len(fts_ids),
                        "vector_hits": len(vector_ids),
                        "vector_ms": vector_ms,
                        "vector_timed_out": vector_timed_out,
                        "scored_nodes": 0,
                        "filters": normalized_filters,
                        "embeddings": self.memory_embeddings.provider_info(),
                    },
                }

        graph_edges: list[Any] = []
        neighbor_ids: set[str] = set()
        neighbor_order: list[str] = []
        neighbor_nodes: dict[str, Any] = {}
        if expand_graph and nodes:
            graph_edges, neighbor_order = self._pagerank_graph_expansion(
                seeds=[node.id for node in nodes],
                limit=limit,
            )
            if neighbor_order:
                neighbors = self.repository.list_nodes_by_ids(list(neighbor_order))
                if normalized_filters:
                    neighbors = [node for node in neighbors if self._memory_node_matches_filters(node, normalized_filters)]
                neighbors = [node for node in neighbors if self._memory_node_visible_at(node, as_of)]
                neighbor_nodes = {node.id: node for node in neighbors}
                # Drop neighbors filtered out above; keep PageRank order for the rest.
                neighbor_order = [nid for nid in neighbor_order if nid in neighbor_nodes]
                neighbor_ids = set(neighbor_order)
                nodes = [*nodes, *neighbors]

        hits = self.search_strategy.search(
            query,
            nodes,
            graph_edges,  # populated only for expand_graph (context) calls; interactive search stays lean
            project_id,
            scope,
            limit,
            diversify=True,
            vector_scores=vector_scores,
            fts_ranks={node_id: rank for rank, node_id in enumerate(fts_ids)},
        )
        # Relevance floor: drop the weak tail (and, if a caller sets an absolute
        # min_score, sub-threshold hits) so noise/near-empty queries don't return
        # loosely-related nodes propped up by static scope/type/source bonuses.
        floor_ratio = relevance_floor if relevance_floor is not None else retrieval_settings.get("search_relevance_floor", 0.28)
        hits, floor_info = self._apply_relevance_floor(hits, floor_ratio, min_score)
        if neighbor_ids:
            existing = {hit.node.id for hit in hits}
            # Neighbors that also matched lexically get the graph tag for visibility.
            for hit in hits:
                if hit.node.id in neighbor_ids and "graph-neighbor" not in (hit.reasons or []):
                    hit.reasons = [*(hit.reasons or []), "graph-neighbor"]
            # Multi-hop neighbors have no lexical signal, so the scorer's guard drops
            # them and the strategy's own pass only reaches 1 hop. Surface the
            # highest-PageRank ones explicitly, filling spare capacity below the
            # real hits in graph-proximity order.
            if len(hits) < limit:
                base = hits[-1].score if hits else 5.0
                for offset, neighbor_id in enumerate(neighbor_order):
                    if len(hits) >= limit:
                        break
                    if neighbor_id in existing:
                        continue
                    node = neighbor_nodes.get(neighbor_id)
                    if node is None:
                        continue
                    hits.append(SearchHit(
                        node=node,
                        score=max(0.1, base - 3.0 - offset * 0.01),
                        matched_terms=[],
                        reasons=["graph-neighbor"],
                    ))
                    existing.add(neighbor_id)
        if hits:
            hit_ids = [hit.node.id for hit in hits]
            # Lifecycle access bump: cheap now (notify=False, no re-embedding) and
            # makes promote_after_hits / decay see real retrieval activity.
            try:
                self.memory_lifecycle.refresh_nodes(hit_ids, "search")
                if refresh:
                    # Sync mode (tests/tools): return fresh lifecycle metadata.
                    refreshed = {node.id: node for node in self.repository.list_nodes_by_ids(hit_ids)}
                    for hit in hits:
                        hit.node = refreshed.get(hit.node.id, hit.node)
            except Exception as exc:
                _LOG.warning("search refresh skipped: %s", exc)
        # Dual-level: attach the coarse "theme" (community) layer the hits sit in.
        communities_ctx: list[dict[str, Any]] | None = None
        if include_communities and hits:
            try:
                communities_ctx = self._communities_for_hits(project_id, [hit.node.id for hit in hits])
            except Exception as exc:
                _LOG.warning("search community context skipped: %s", exc)
        return {
            "query": query,
            "hits": [self._compact_search_hit(hit.to_dict()) for hit in hits],
            **({"communities": communities_ctx} if communities_ctx is not None else {}),
            "retrieval": {
                "mode": mode,
                "fts_hits": len(fts_ids),
                "vector_hits": len(vector_ids),
                "vector_ms": vector_ms,
                "vector_timed_out": vector_timed_out,
                "scored_nodes": len(nodes),
                "graph_neighbors": sum(1 for hit in hits if "graph-neighbor" in (hit.reasons or [])),
                **floor_info,
                "embeddings": self.memory_embeddings.provider_info(),
            },
        }

    # Multi-hop retrieval knobs: how far to walk the memory graph out from the
    # query matches, how large a subgraph to score, and the PageRank floor below
    # which a walked-in neighbor is treated as noise.
    GRAPH_EXPANSION_HOPS = 2
    GRAPH_EXPANSION_MAX_NODES = 400
    GRAPH_EXPANSION_MIN_SCORE = 1.0e-9
    # Structural spine edges (memory -> source/scope hub roots) connect everything
    # to a few god-nodes; walking them would make every node ~2 hops from every
    # other. Traverse only semantic content edges so hop distance stays meaningful.
    _STRUCTURAL_EDGE_TYPES = frozenset({"HAS_MEMORY", "DOCUMENTED_IN"})

    def _pagerank_graph_expansion(self, *, seeds: list[str], limit: int) -> tuple[list[Any], list[str]]:
        """Walk the memory graph up to N hops from the query matches and return the
        highest personalized-PageRank neighbors, ordered by graph proximity.

        Upgrade over the previous 1-hop expansion: random-walk mass flows several
        hops out from the matched ("seed") nodes, so a decision linked only
        indirectly to the query can still join the packed context — ranked by
        graph proximity instead of pulling in every direct neighbor. Bounded by a
        hop count and a node budget to keep context builds fast. Returns the
        subgraph edges plus the neighbor ids in descending PageRank order.
        """
        seed_set = {sid for sid in seeds if sid}
        if not seed_set:
            return [], []

        collected: dict[str, Any] = {}
        visited = set(seed_set)
        frontier = list(seed_set)
        for _hop in range(self.GRAPH_EXPANSION_HOPS):
            if not frontier or len(visited) >= self.GRAPH_EXPANSION_MAX_NODES:
                break
            next_frontier: set[str] = set()
            for edge in self.repository.list_edges_touching(frontier):
                if edge.type in self._STRUCTURAL_EDGE_TYPES:
                    continue
                collected[edge.id] = edge
                for endpoint in (edge.source, edge.target):
                    if endpoint not in visited:
                        next_frontier.add(endpoint)
            # Respect the node budget deterministically (smaller ids first).
            for endpoint in sorted(next_frontier):
                if len(visited) >= self.GRAPH_EXPANSION_MAX_NODES:
                    break
                visited.add(endpoint)
            frontier = [nid for nid in sorted(next_frontier) if nid in visited]

        graph_edges = list(collected.values())
        if not graph_edges:
            return [], []

        edge_pairs = [(edge.source, edge.target) for edge in graph_edges]
        ranks = personalized_pagerank(list(visited), edge_pairs, list(seed_set))
        candidates = sorted(
            (
                (nid, score)
                for nid, score in ranks.items()
                if nid not in seed_set and score > self.GRAPH_EXPANSION_MIN_SCORE
            ),
            key=lambda item: (-item[1], item[0]),
        )
        neighbor_order = [nid for nid, _score in candidates[: max(limit, 8)]]
        neighbor_ids = set(neighbor_order)
        # Restrict edges to the seed ∪ kept-neighbor subgraph so the scoring
        # strategy's own 1-hop pass sees a consistent slice.
        allowed = seed_set | neighbor_ids
        graph_edges = [edge for edge in graph_edges if edge.source in allowed and edge.target in allowed]
        return graph_edges, neighbor_order

    @staticmethod
    def _memory_node_visible_at(node: Any, as_of: str | None) -> bool:
        """Bi-temporal visibility for a memory node.

        A node is a fact with a validity window: ``created_at`` (when it became
        true) and an optional ``invalid_at`` in metadata (when a SUPERSEDES edge
        retired it). ISO-8601 strings compare lexicographically, so missing dates
        are treated as "always visible".

        - ``as_of`` set  → the node must have existed and not yet been superseded
          at that instant ("what was true back then").
        - ``as_of`` unset → live search hides facts that have been superseded.
        """
        created = str(getattr(node, "created_at", "") or "")
        invalid = str((getattr(node, "metadata", {}) or {}).get("invalid_at") or "")
        if as_of:
            if created and created > as_of:
                return False
            if invalid and invalid <= as_of:
                return False
            return True
        return not invalid

    _MEMORY_FILTER_ATTRS = {"id", "type", "scope", "status", "project_id", "label", "confidence"}

    @staticmethod
    def _normalize_memory_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(filters, dict):
            return {}
        normalized: dict[str, Any] = {}
        for key, value in filters.items():
            name = str(key or "").strip()
            if not name:
                continue
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            normalized[name] = value
        return normalized

    @staticmethod
    def _memory_filter_value_matches(actual: Any, expected: Any) -> bool:
        if isinstance(expected, bool):
            return bool(actual) == expected
        if isinstance(expected, (list, tuple, set)):
            return any(RetrievalServiceMixin._memory_filter_value_matches(actual, item) for item in expected)
        text = str(expected).strip()
        negated = text.startswith("!=")
        if negated:
            text = text[2:].strip()
        actual_text = "" if actual is None else str(actual)
        matched = any(actual_text.lower() == option.strip().lower() for option in text.split("|") if option.strip())
        return (not matched) if negated else matched

    def _memory_node_matches_filters(self, node: Any, filters: dict[str, Any]) -> bool:
        metadata = dict(getattr(node, "metadata", {}) or {})
        for key, expected in filters.items():
            if key in self._MEMORY_FILTER_ATTRS:
                actual = getattr(node, key, None)
            else:
                actual = metadata.get(key)
            if not self._memory_filter_value_matches(actual, expected):
                return False
        return True

    def _list_memory_nodes(
        self,
        project_id: str | None,
        scope: str | None,
        limit: int,
        filters: dict[str, Any],
    ) -> dict[str, Any]:
        nodes = [node for node in self.repository.list_nodes() if node.status == "active"]
        if project_id:
            nodes = [node for node in nodes if node.project_id in {project_id, None}]
        if scope:
            nodes = [node for node in nodes if str(getattr(node, "scope", "") or "") == scope]
        if filters:
            nodes = [node for node in nodes if self._memory_node_matches_filters(node, filters)]
        total = len(nodes)
        hits = [SearchHit(node=node, score=0.0, matched_terms=[], reasons=["list"]) for node in nodes[:limit]]
        return {
            "query": "",
            "hits": [self._compact_search_hit(hit.to_dict()) for hit in hits],
            "retrieval": {
                "mode": "list",
                "filters": filters,
                "matched_nodes": total,
                "scored_nodes": 0,
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

    def context(self, query: str, project_id: str | None = None, scope: str | None = None, limit: int = 8, tools_available: bool = True) -> dict[str, Any]:
        search = self.search_memory(query, project_id=project_id, scope=scope, limit=max(limit, 10), expand_graph=True)
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
            tools_available=tools_available,
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
