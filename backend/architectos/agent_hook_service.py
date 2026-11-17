"""Cheap per-turn memory pack for agent hooks.

A hook fires on every prompt of every agent session, so its pack is smaller and
faster than the ``context()`` pack Ask uses: a tight character budget, short node
excerpts, a short vector-search timeout and a relevance floor below which a hit is
not worth the tokens. Settings live under the ``agent`` key.

The app also serves the hook while it is closed: it keeps each project's latest
briefing and hook pack in ``data/briefing_cache.json`` for offline injection, and
replays the captures the hook spooled to ``data/hook_spool.jsonl``. Pure mixin:
search, the stable layer and the repository are reached through ``self``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .agent_hooks import BRIEFING_CACHE, HOOK_SPOOL, hook_pack_budget, prune_hook_sessions, split_pack, spool_turn
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
# A spooled capture that keeps failing is dropped after this many tries.
HOOK_SPOOL_MAX_ATTEMPTS = 3
# capture_memory_turn's project for a folder no project claims; the offline hook mirrors it.
HOOK_DEFAULT_PROJECT = "architectos"

_CACHE_LOCK = threading.Lock()
_SPOOL_LOCK = threading.Lock()


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
        So is graph expansion: on the gold queries over a real 11k-node project it
        left the expected hit and MRR unchanged while doubling the search time, and
        the ~2 hits that fit the budget are direct matches anyway.
        """
        started = time.perf_counter()
        settings = self.agent_hook_settings()
        excluded = {str(item) for item in exclude_ids if item}
        search = self.search_memory(
            query,
            project_id=project_id,
            scope=scope,
            limit=max(limit, 10),
            expand_graph=False,
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
                char_budget=hook_pack_budget(int(settings["hook_char_budget"])),
                node_text_chars=HOOK_NODE_TEXT_CHARS,
                tools_available=True,
                stable_hits=stable,
            )
        pack_done = time.perf_counter()
        if context and project_id:
            self.remember_hook_pack(project_id, pack=context)
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
                "pack_ms": round((pack_done - stable_done) * 1000.0, 1),
                "cache_ms": round((finished - pack_done) * 1000.0, 1),
            },
        }

    def hook_briefing(self, *, project_id: str | None = None, cwd: str | None = None, limit: int = 6) -> dict[str, Any]:
        """Session-start briefing; also refreshes the project's stable layer in the offline cache."""
        briefing = self.memory_briefing(project_id=project_id, cwd=cwd, limit=limit)
        self.remember_hook_pack(str(briefing.get("project_id") or ""), briefing=str(briefing.get("context") or ""))
        return briefing

    def remember_hook_pack(self, project_id: str, *, briefing: str = "", pack: str = "") -> None:
        """Keep a project's latest briefing (stable lines) and hook pack (hit lines) for the offline hook.

        Every write also refreshes the folder -> project roots and the hook
        settings, so the hook can resolve a project and size a pack without the
        store. With nothing new the file is only rewritten when those changed.
        Never raises: the cache is a convenience, the turn must not fail on it.
        """
        path = Path(self.project_root) / "data" / BRIEFING_CACHE
        try:
            with _CACHE_LOCK:
                try:
                    cache = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    cache = {}
                cache = cache if isinstance(cache, dict) else {}
                settings = self.agent_hook_settings()
                roots = self._hook_project_roots()
                shared = {"roots": roots, "default_project": HOOK_DEFAULT_PROJECT, "settings": {
                    "hook_char_budget": settings["hook_char_budget"],
                    "hook_stable_every_turns": settings["hook_stable_every_turns"],
                }}
                if not (project_id and (briefing or pack)) and all(cache.get(key) == value for key, value in shared.items()):
                    return
                raw_projects = cache.get("projects")
                projects: dict[str, Any] = raw_projects if isinstance(raw_projects, dict) else {}
                if project_id and (briefing or pack):
                    raw_entry = projects.get(project_id)
                    entry: dict[str, Any] = raw_entry if isinstance(raw_entry, dict) else {}
                    for text, key in ((briefing, "stable"), (pack, "hits")):
                        if text:
                            parsed = split_pack(text)
                            entry["header"], entry[key] = parsed["header"], parsed[key]
                    entry["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    projects[project_id] = entry
                cache.update(shared, version=1, projects=projects,
                             updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
                path.parent.mkdir(parents=True, exist_ok=True)
                scratch = path.with_suffix(".tmp")
                scratch.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
                os.replace(scratch, path)
            _LOG.debug(
                "briefing cache updated project=%s briefing=%s pack=%s projects=%s roots=%s",
                project_id or "-", bool(briefing), bool(pack), len(projects), len(roots),
            )
        except Exception as exc:  # noqa: BLE001 - the offline cache must never break a turn
            _LOG.warning("briefing cache not updated for project %s: %s", project_id or "-", exc)

    def _hook_project_roots(self) -> dict[str, str]:
        roots: dict[str, str] = {}
        for project in self.repository.list_projects():
            raw = str(getattr(project, "root_path", "") or "").strip()
            if not raw:
                continue
            try:
                roots[str(project.id)] = str(Path(raw).expanduser().resolve())
            except (OSError, RuntimeError, ValueError):
                continue
        return roots

    def drain_hook_spool(self) -> dict[str, int]:
        """Capture the turns the hook spooled while the app was closed, each into its own project.

        The spool is renamed before reading, so the hook keeps appending to a
        fresh file meanwhile; a batch left by an interrupted drain is replayed
        first. A failing record is re-spooled up to ``HOOK_SPOOL_MAX_ATTEMPTS``.
        """
        data = Path(self.project_root) / "data"
        stats = {"records": 0, "captured": 0, "failed": 0, "dropped": 0}
        with _SPOOL_LOCK:
            batches = sorted(data.glob(f"{HOOK_SPOOL}.*.draining"))
            spool = data / HOOK_SPOOL
            if spool.exists():
                batch = data / f"{HOOK_SPOOL}.{time.time_ns()}.draining"
                try:
                    os.replace(spool, batch)
                    batches.append(batch)
                except OSError as exc:
                    _LOG.warning("hook spool not taken for replay: %s", exc)
            if not batches:
                return stats
            started = time.perf_counter()
            for batch in batches:
                try:
                    lines = batch.read_text(encoding="utf-8").splitlines()
                except OSError as exc:
                    _LOG.error("hook spool batch unreadable, kept for the next drain: %s (%s)", batch.name, exc)
                    continue
                for line in lines:
                    if line.strip():
                        stats[self._replay_spooled_turn(line)] += 1
                        stats["records"] += 1
                batch.unlink(missing_ok=True)
        _LOG.info(
            "hook spool drained: records=%s captured=%s failed=%s dropped=%s batches=%s ms=%.0f",
            stats["records"], stats["captured"], stats["failed"], stats["dropped"], len(batches),
            (time.perf_counter() - started) * 1000.0,
        )
        return stats

    def _replay_spooled_turn(self, line: str) -> str:
        try:
            record = json.loads(line)
        except ValueError:
            record = None
        if not isinstance(record, dict) or not (record.get("user_text") or record.get("assistant_text")):
            _LOG.warning("hook spool record skipped: not a capture")
            return "dropped"
        project_id = str(record.get("project_id") or "") or None
        try:
            self.capture_memory_turn(
                str(record.get("user_text") or ""),
                str(record.get("assistant_text") or ""),
                project_id=project_id,
                cwd=str(record.get("cwd") or "") or None,
                client=str(record.get("client") or "") or None,
                hook=True,
                include_stable=False,
            )
            return "captured"
        except Exception as exc:  # noqa: BLE001 - one bad record must not stop the replay
            attempts = int(record.get("attempts") or 0) + 1
            if attempts >= HOOK_SPOOL_MAX_ATTEMPTS:
                _LOG.error("hook spool record dropped after %s attempts: project=%s error=%s", attempts, project_id, exc)
                return "dropped"
            _LOG.error("hook spool record failed, re-spooled: project=%s attempt=%s error=%s", project_id, attempts, exc)
            spool_turn(Path(self.project_root), {**record, "attempts": attempts})
            return "failed"

    def hook_maintenance(self) -> None:
        """App start and once a minute: replay spooled captures, prune old sessions, refresh cached roots."""
        try:
            self.drain_hook_spool()
            prune_hook_sessions(Path(self.project_root))
            self.remember_hook_pack("")
        except Exception as exc:  # noqa: BLE001 - background maintenance must not crash the app
            _LOG.error("hook maintenance failed: %s", exc, exc_info=True)
