"""Cheap per-turn memory pack for agent hooks.

A hook fires on every prompt of every agent session, so its pack is smaller and
faster than the ``context()`` pack Ask uses: a tight character budget, short node
excerpts, a short vector-search timeout and a relevance floor below which a hit is
not worth the tokens. Settings live under the ``agent`` key. Pure mixin: search,
the stable layer and the repository are reached through ``self``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

from .search import DEFAULT_CONTEXT_CHAR_BUDGET, pack_memory_context

_LOG = logging.getLogger("architectos.service")

HOOK_CHAR_BUDGET = 1600
# pack_memory_context never packs below 1200 characters (its header alone is ~420).
HOOK_CHAR_BUDGET_MIN = 1200
HOOK_NODE_TEXT_CHARS = 220
HOOK_VECTOR_TIMEOUT_MS = 800
HOOK_STABLE_LIMIT = 4
# A session gets the stable layer again after this many prompts without it (0 = never).
HOOK_STABLE_EVERY_TURNS = 20
# Static bonuses (scope, type, source, recency, lifecycle) plus an FTS rank alone add
# up to ~115, so 120 asks for at least one real query-term match or a vector signal.
HOOK_MIN_SCORE = 120.0


def _bounded(raw: Any, default: float, low: float, high: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(value, high))


class AgentHookServiceMixin:
    """Hook-sized memory pack and the ``agent`` settings that shape it."""

    def agent_hook_settings(self) -> dict[str, Any]:
        raw = self.repository.get_setting("agent")
        config: dict[str, Any] = raw if isinstance(raw, dict) else {}
        return {
            "hook_char_budget": int(_bounded(
                config.get("hook_char_budget"), HOOK_CHAR_BUDGET, HOOK_CHAR_BUDGET_MIN, DEFAULT_CONTEXT_CHAR_BUDGET
            )),
            "hook_min_score": _bounded(config.get("hook_min_score"), HOOK_MIN_SCORE, 0.0, 1000.0),
            "hook_stable_every_turns": int(_bounded(config.get("hook_stable_every_turns"), HOOK_STABLE_EVERY_TURNS, 0, 1000)),
        }

    def hook_context(
        self,
        query: str,
        *,
        project_id: str | None,
        scope: str | None = None,
        limit: int = 8,
        include_stable: bool = True,
        exclude_ids: Iterable[str] = (),
        vector_timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """Pack for one hook turn: new hits above the relevance floor, plus the stable layer on request.

        ``exclude_ids`` are hits the session already holds, so the budget goes to
        what it has not seen. A requested stable layer ignores them: it is asked
        for again exactly when the session may have lost it. An empty ``context``
        means there is nothing worth injecting. Open tasks, providers and the
        token-economy pass of ``context()`` are skipped: they do not fit the budget.
        """
        started = time.perf_counter()
        settings = self.agent_hook_settings()
        excluded = {str(item) for item in exclude_ids if item}
        search = self.search_memory(
            query,
            project_id=project_id,
            scope=scope,
            limit=max(limit, 10),
            expand_graph=True,
            vector_timeout_ms=HOOK_VECTOR_TIMEOUT_MS if vector_timeout_ms is None else vector_timeout_ms,
        )
        searched = time.perf_counter()
        floor = float(settings["hook_min_score"])
        found = search.get("hits") or []
        hits = [
            hit for hit in found
            if str(hit["node"].get("id") or "") not in excluded and float(hit.get("score") or 0.0) >= floor
        ]
        stable: list[dict[str, Any]] = []
        if include_stable:
            stable = self.list_stable_memory(project_id=project_id, limit=HOOK_STABLE_LIMIT, query=query, scope=scope)
        stable_done = time.perf_counter()
        context = ""
        if hits or stable:
            context = pack_memory_context(
                hits,
                project_id=project_id,
                scope=scope,
                char_budget=int(settings["hook_char_budget"]),
                node_text_chars=HOOK_NODE_TEXT_CHARS,
                tools_available=True,
                stable_hits=stable,
            )
        finished = time.perf_counter()
        retrieval = search.get("retrieval") or {}
        _LOG.debug(
            "hook pack project=%s found=%s kept=%s below_floor=%s excluded=%s stable=%s chars=%s vector_timed_out=%s",
            project_id, len(found), len(hits), sum(1 for hit in found if float(hit.get("score") or 0.0) < floor),
            len(excluded), len(stable), len(context), retrieval.get("vector_timed_out"),
        )
        return {
            "project_id": project_id,
            "context": context,
            "hits": hits,
            "stable": stable,
            "stable_every_turns": settings["hook_stable_every_turns"],
            "retrieval": retrieval,
            "timing": {
                "search_ms": round((searched - started) * 1000.0, 1),
                "stable_ms": round((stable_done - searched) * 1000.0, 1),
                "pack_ms": round((finished - stable_done) * 1000.0, 1),
            },
        }
