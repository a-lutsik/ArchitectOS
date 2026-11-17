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

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .agent_hooks import (
    BRIEFING_CACHE,
    BRIEFING_CACHE_DIRTY,
    BRIEFING_CACHE_DIRTY_CLAIMED,
    HOOK_SPOOL,
    file_lock,
    hook_pack_budget,
    pack_header,
    pack_lines_shown,
    prune_hook_sessions,
    replace_file,
    spool_turn,
    write_private,
)
from .models import MemoryNode
from .search import DEFAULT_CONTEXT_CHAR_BUDGET, MIN_CONTEXT_CHAR_BUDGET, SEARCH_HIT_TEXT_CHARS, pack_memory_context

_LOG = logging.getLogger("architectos.service")

HOOK_CHAR_BUDGET = 1600
# The smallest injection whose pack budget (hook_pack_budget: minus the hook prefix and
# slack) still reaches the pack's own floor, so the whole injection fits the setting.
HOOK_CHAR_BUDGET_MIN = 2 * MIN_CONTEXT_CHAR_BUDGET - hook_pack_budget(MIN_CONTEXT_CHAR_BUDGET)
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
# The offline hook ignores a project's cached lines older than this (0 = no limit). The
# app drops changed memories from the cache itself; the age bounds what it cannot see.
HOOK_CACHE_TTL_HOURS = 24
# How long the upsert listener, after commit, waits for the cache lock to evict a cached
# memory before it marks the cache dirty instead.
HOOK_CACHE_LOCK_WAIT_S = 0.25
# How long a cache writer (a hook turn's pack, a deletion, maintenance) waits for the lock.
HOOK_CACHE_WRITE_WAIT_S = 0.5

_CACHE_LOCK = threading.Lock()
_SPOOL_LOCK = threading.Lock()


def _cache_signature(node: Mapping[str, Any]) -> str:
    """What decides whether, and how, a cached memory line may still be shown.

    Lifecycle telemetry (access counts, scores) is left out, so a search's access
    bump never evicts the lines it was just cached with. The text counts up to
    what a search hit carries, so a hit and its stored node sign alike; a pack
    line never shows more of it.
    """
    meta = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    fields = [node.get(key) for key in ("status", "scope", "project_id", "author_project_id", "label")]
    fields.append(str(node.get("text") or "")[:SEARCH_HIT_TEXT_CHARS])
    raw = json.dumps([*fields, (meta or {}).get("invalid_at")], sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _bounded(raw: Any, default: float, low: float, high: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(value, high))


def _cached_lines(cache: Mapping[str, Any]) -> dict[str, int]:
    """Cached memory id -> how many lines the cache holds for it, over all projects."""
    counts: dict[str, int] = {}
    entries = cache.get("projects")
    for entry in (entries.values() if isinstance(entries, dict) else ()):
        for key in ("stable", "hits"):
            for item in (entry.get(key) or []) if isinstance(entry, dict) else []:
                if isinstance(item, list) and len(item) >= 2:
                    counts[str(item[0])] = counts.get(str(item[0]), 0) + 1
    return counts


def _drop_cached_lines(cache: dict[str, Any], evict: Callable[[str, str], bool]) -> int:
    """Remove the lines ``evict(node_id, signature)`` picks from every project; how many went."""
    removed = 0
    entries = cache.get("projects")
    for entry in (entries.values() if isinstance(entries, dict) else ()):
        for key in ("stable", "hits"):
            items = entry.get(key) if isinstance(entry, dict) else None
            if not isinstance(items, list):
                continue
            kept = [item for item in items if not (
                isinstance(item, list) and len(item) >= 2 and evict(str(item[0]), str(item[2]) if len(item) > 2 else "")
            )]
            removed += len(items) - len(kept)
            entry[key] = kept
    return removed


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
            "hook_cache_ttl_hours": _bounded(config.get("hook_cache_ttl_hours"), HOOK_CACHE_TTL_HOURS, 0.0, 24.0 * 30),
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
        rendered: dict[str, tuple[str, str]] = {}
        if hits or stable:
            context = pack_memory_context(
                hits,
                project_id=project_id,
                scope=scope,
                char_budget=hook_pack_budget(int(settings["hook_char_budget"])),
                node_text_chars=HOOK_NODE_TEXT_CHARS,
                tools_available=True,
                stable_hits=stable,
                rendered=rendered,
            )
        pack_done = time.perf_counter()
        if context and project_id:
            self.remember_hook_pack(
                project_id,
                pack=context,
                lines=rendered,
                nodes=[hit["node"] for hit in hits + stable],
                revalidate_stable=bool(stable),
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
            "shown": pack_lines_shown(context, [(node_id, section, line) for node_id, (section, line) in rendered.items()]),
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
        rendered: dict[str, tuple[str, str]] = {}
        briefing = self.memory_briefing(project_id=project_id, cwd=cwd, limit=limit, rendered=rendered)
        self.remember_hook_pack(
            str(briefing.get("project_id") or ""),
            briefing=str(briefing.get("context") or ""),
            lines=rendered,
            nodes=[hit["node"] for hit in briefing.get("stable") or [] if isinstance(hit, dict) and isinstance(hit.get("node"), dict)],
        )
        context = str(briefing.get("context") or "")
        return {**briefing, "shown": pack_lines_shown(context, [(node_id, section, line) for node_id, (section, line) in rendered.items()])}

    def remember_hook_pack(
        self,
        project_id: str,
        *,
        briefing: str = "",
        pack: str = "",
        lines: Mapping[str, tuple[str, str]] | None = None,
        nodes: Iterable[Mapping[str, Any]] = (),
        revalidate_stable: bool = False,
    ) -> None:
        """Keep a project's latest briefing (stable lines) and hook pack (hit lines) for the offline hook.

        The cached lines are the pack's own memory lines (``lines``: node id ->
        (section, line), from pack_memory_context), never re-parsed from its text,
        and each keeps the signature of its node (``nodes``), so a later change that
        matters evicts it. Every write also refreshes the folder -> project
        roots and the hook settings, so the hook can resolve a project and size a
        pack without the store; with nothing new the file is only rewritten when
        those changed. ``revalidate_stable`` (a prompt that got the stable layer)
        re-checks the cached stable lines against the store and renews their age,
        which otherwise only a briefing does. Never raises: the turn must not fail
        on the cache.
        """
        try:
            settings = self.agent_hook_settings()
            registered = self.repository.list_projects()
            roots = self._hook_project_roots(registered)
            registered_ids = {str(project.id) for project in registered}
            shared = {"roots": roots, "default_project": HOOK_DEFAULT_PROJECT, "settings": {
                "hook_char_budget": settings["hook_char_budget"],
                "hook_stable_every_turns": settings["hook_stable_every_turns"],
                "hook_cache_ttl_hours": settings["hook_cache_ttl_hours"],
            }}
            signatures = {str(node.get("id") or ""): _cache_signature(node) for node in nodes}

            def change(cache: dict[str, Any]) -> bool:
                if not (project_id and (briefing or pack)) and all(cache.get(key) == value for key, value in shared.items()):
                    return False
                raw_projects = cache.get("projects")
                projects: dict[str, Any] = raw_projects if isinstance(raw_projects, dict) else {}
                raw_entry = projects.get(project_id) if project_id else None
                entry: dict[str, Any] = raw_entry if isinstance(raw_entry, dict) else {}
                held = [item for item in entry.get("stable") or [] if isinstance(item, list) and len(item) >= 2] if revalidate_stable else []
                # Under the cache lock, against the store: a memory another process archived
                # or moved since this pack was built (and evicted meanwhile) stays out.
                checked = list({*signatures, *(str(item[0]) for item in held)})
                current = {node.id: _cache_signature(node.to_dict()) for node in self.repository.list_nodes_by_ids(checked)}
                fresh = {node_id for node_id, signature in signatures.items() if current.get(node_id) == signature}
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                if project_id and (briefing or pack):
                    if held:
                        entry["stable"] = [item for item in held if len(item) > 2 and current.get(str(item[0])) == item[2]]
                        entry["stable_at"] = now
                    for text, key in ((briefing, "stable"), (pack, "hits")):
                        if text:
                            entry["header"] = pack_header(text)
                            entry[key] = [
                                [node_id, line, signatures[node_id]]
                                for node_id, (section, line) in (lines or {}).items()
                                if section == key and node_id in fresh
                            ]
                            entry[f"{key}_at"] = now
                    entry["updated_at"] = now
                    # Maintenance drops the entry once a registered project is deleted; an id
                    # from a bind file or an explicit project_id may never have had a row.
                    entry["registered"] = project_id in registered_ids
                    projects[project_id] = entry
                cache.update(shared, version=2, projects=projects, updated_at=now)
                return True

            self._update_briefing_cache(change, recheck=set(signatures))
            _LOG.debug(
                "briefing cache updated project=%s briefing=%s pack=%s roots=%s",
                project_id or "-", bool(briefing), bool(pack), len(roots),
            )
        except Exception as exc:  # noqa: BLE001 - the offline cache must never break a turn
            _LOG.warning("briefing cache not updated for project %s: %s", project_id or "-", exc)

    def forget_hook_memories(self, node_ids: Iterable[str] = (), *, projects: Iterable[str] = ()) -> None:
        """Drop deleted memories, and whole projects, from the offline cache. Never raises."""
        gone, dropped = set(node_ids), set(projects)
        if not (gone or dropped):
            return
        try:
            self._evict_hook_cache(lambda node_id, signature: node_id in gone, dropped)
        except Exception as exc:  # noqa: BLE001 - the offline cache must never break a write
            _LOG.warning("briefing cache not pruned: %s", exc)

    def _forget_changed_hook_node(self, node: MemoryNode) -> None:
        """Upsert listener: a cached memory whose status, scope, project or text changed leaves the cache.

        Runs in every process that writes memory (the app, each MCP server), after
        commit, so it never waits long. The ids the cache holds are read without the
        lock (re-read only when the file changed); a memory the cache does not hold
        costs that read and nothing else. A writer that is writing a line for it
        right now, built before this change, re-checks its lines against the store
        once the file is replaced (see _rewrite_briefing_cache). A cached memory
        whose signature changed is evicted under the lock, waited for at most
        HOOK_CACHE_LOCK_WAIT_S; when the lock stays held, the cache is marked dirty
        instead (see BRIEFING_CACHE_DIRTY) and the next locked write re-checks it;
        while that marker waits, a later change only tries the lock once.
        """
        try:
            cached = self._hook_cache_index().get(node.id)
            if cached is None:
                return
            signature = _cache_signature(node.to_dict())
            if cached == {signature}:
                return
            # Already marked and not yet claimed: the write that claims it reads the store
            # after this change, so a held lock is not waited for (nor warned about) again;
            # a free one is taken to evict and re-check now, since an MCP server alone
            # never runs the app's maintenance or hook turns.
            dirty = (Path(self.project_root) / "data" / BRIEFING_CACHE_DIRTY).exists()
            wait = 0.0 if dirty else HOOK_CACHE_LOCK_WAIT_S
            started = time.monotonic()
            if not _CACHE_LOCK.acquire(timeout=wait):
                self._hook_cache_lock_busy(node.id, dirty)
                return
            try:
                lock = self._briefing_cache_path().with_name(f"{BRIEFING_CACHE}.lock")
                with file_lock(lock, wait=max(0.0, wait - (time.monotonic() - started))) as held:
                    if not held:
                        self._hook_cache_lock_busy(node.id, dirty)
                        return
                    if self._hook_cache_index().get(node.id, {signature}) != {signature}:
                        self._evict_hook_cache(lambda node_id, old: node_id == node.id and old != signature, set(), locked=True)
                    elif dirty:
                        self._rewrite_briefing_cache(lambda cache: False)  # claims the marker, re-checks all
            finally:
                _CACHE_LOCK.release()
        except Exception as exc:  # noqa: BLE001 - a listener must never break the write
            _LOG.warning("briefing cache not pruned for node %s: %s", node.id, exc)

    def _hook_cache_lock_busy(self, node_id: str, marked: bool) -> None:
        if marked:
            _LOG.debug("briefing cache lock held, node %s left to the pending re-check", node_id)
            return
        _LOG.warning("briefing cache lock held past %ss, eviction of node %s deferred", HOOK_CACHE_LOCK_WAIT_S, node_id)
        self._mark_hook_cache_dirty()

    def _registered_hook_cache_projects(self) -> set[str]:
        """Cached projects that had a projects row when they were cached."""
        try:
            cache = json.loads((Path(self.project_root) / "data" / BRIEFING_CACHE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        projects = cache.get("projects") if isinstance(cache, dict) else None
        if not isinstance(projects, dict):
            return set()
        return {str(project_id) for project_id, entry in projects.items() if isinstance(entry, dict) and entry.get("registered")}

    def _hook_cache_index(self) -> dict[str, set[str]]:
        """Cached memory id -> the signatures its lines were cached with, re-read when the file changes."""
        path = Path(self.project_root) / "data" / BRIEFING_CACHE
        try:
            stat = path.stat()
        except OSError:
            return {}
        # A coarse clock gives two writes one mtime; each write replaces the file
        # (a new inode) and its size usually moves too.
        stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        memo = getattr(self, "_hook_cache_memo", None)
        if memo is not None and memo[0] == stamp:
            return memo[1]
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        index: dict[str, set[str]] = {}
        for entry in ((cache.get("projects") or {}) if isinstance(cache, dict) else {}).values():
            for key in ("stable", "hits"):
                for item in (entry.get(key) or []) if isinstance(entry, dict) else []:
                    if isinstance(item, list) and len(item) >= 2:
                        index.setdefault(str(item[0]), set()).add(str(item[2]) if len(item) > 2 else "")
        self._hook_cache_memo = (stamp, index)
        return index

    def _evict_hook_cache(self, evict: Callable[[str, str], bool], projects: set[str], *, locked: bool = False) -> None:
        """Remove the cached lines ``evict(node_id, signature)`` picks, and the entries of ``projects``.

        ``locked``: the caller already holds the cache locks. An eviction that cannot
        be written marks the cache dirty (see BRIEFING_CACHE_DIRTY) before it raises.
        """
        removed = {"lines": 0, "projects": 0}

        def change(cache: dict[str, Any]) -> bool:
            entries = cache.get("projects")
            if not isinstance(entries, dict):
                return False
            for project_id in projects & set(entries):
                del entries[project_id]
                removed["projects"] += 1
            removed["lines"] += _drop_cached_lines(cache, evict)
            return bool(removed["lines"] or removed["projects"])

        try:
            (self._rewrite_briefing_cache if locked else self._update_briefing_cache)(change)
        except Exception as exc:
            _LOG.warning("briefing cache eviction not written, cache marked for revalidation: %s", exc)
            self._mark_hook_cache_dirty()
            raise
        if removed["lines"] or removed["projects"]:
            _LOG.info("briefing cache pruned: lines=%s projects=%s", removed["lines"], removed["projects"])

    def _mark_hook_cache_dirty(self) -> None:
        """Keep the offline hook off the cached lines until a locked write re-checks them all."""
        path = Path(self.project_root) / "data" / BRIEFING_CACHE_DIRTY
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
            _LOG.info("briefing cache marked for revalidation")
        except OSError as exc:
            _LOG.error("briefing cache may keep a changed memory until its TTL: marker not written (%s)", exc)

    def _revalidate_hook_cache(self, cache: dict[str, Any], only: set[str] | None = None) -> int:
        """Drop the cached lines (of ``only``, else all) whose memory is gone or no longer matches its signature."""
        ids = [node_id for node_id in _cached_lines(cache) if only is None or node_id in only]
        current = {node.id: _cache_signature(node.to_dict()) for node in self.repository.list_nodes_by_ids(ids)} if ids else {}
        dropped = _drop_cached_lines(
            cache, lambda node_id, signature: node_id in ids and current.get(node_id) != signature
        )
        log = _LOG.info if only is None or dropped else _LOG.debug
        log("briefing cache re-checked: memories=%s dropped_lines=%s scope=%s", len(ids), dropped, "all" if only is None else "written")
        return dropped

    def _update_briefing_cache(self, change: Callable[[dict[str, Any]], bool], *, recheck: set[str] | None = None) -> None:
        """Read, ``change`` and rewrite data/briefing_cache.json; ``change`` returns False to skip the write.

        Serialized across threads and, through a lock file, across processes.
        The locks are waited for at most HOOK_CACHE_WRITE_WAIT_S (a stuck holder
        must not hang the app's hook turns), then TimeoutError: a pack is then just
        not cached, an eviction marks the cache dirty (_evict_hook_cache).
        ``recheck``: see _rewrite_briefing_cache.
        """
        started = time.monotonic()
        if not _CACHE_LOCK.acquire(timeout=HOOK_CACHE_WRITE_WAIT_S):
            raise TimeoutError(f"briefing cache lock held in this process past {HOOK_CACHE_WRITE_WAIT_S}s")
        try:
            lock = self._briefing_cache_path().with_name(f"{BRIEFING_CACHE}.lock")
            with file_lock(lock, wait=max(0.0, HOOK_CACHE_WRITE_WAIT_S - (time.monotonic() - started))) as held:
                if not held:
                    raise TimeoutError(f"briefing cache lock held by another process past {HOOK_CACHE_WRITE_WAIT_S}s")
                self._rewrite_briefing_cache(change, recheck=recheck)
        finally:
            _CACHE_LOCK.release()

    def _briefing_cache_path(self) -> Path:
        return Path(self.project_root) / "data" / BRIEFING_CACHE

    def _rewrite_briefing_cache(self, change: Callable[[dict[str, Any]], bool], *, recheck: set[str] | None = None) -> None:
        """The body of _update_briefing_cache, for a caller that holds its locks.

        Invariant: any state of the file that may hold a line older than its last
        re-check against the store reads as dirty (briefing_cache_dirty).

        ``recheck``: memories whose lines ``change`` may have added. Once the file is
        replaced, their lines are checked against the store again: a memory that
        changed after this write read the store was not in the file yet when its
        listener looked, so this check, not the listener, drops it. The claimed
        marker is written before that replace and removed only once the re-check is
        written, so the file reads as dirty all along, and stays dirty if this
        process dies in between.

        A cache marked dirty is re-checked whole. The marker is claimed (renamed)
        after the write and before the store is read: a mark made while this write
        ran is covered, one made later is a new marker for the next write. A claimed
        marker this write did not make was left by a writer that died: the whole
        cache is re-checked then too.
        """
        path = self._briefing_cache_path()
        claimed = Path(self.project_root) / "data" / BRIEFING_CACHE_DIRTY_CLAIMED
        left = claimed.exists()
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
        cache = cache if isinstance(cache, dict) else {}
        written = change(cache)
        holds = bool(written and recheck)
        if holds:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(claimed, os.O_WRONLY | os.O_CREAT, 0o600))
        if written:
            self._write_briefing_cache(cache)
        whole = self._claim_hook_cache_dirty() or left
        only = None if whole else recheck if holds else set()
        if only is not None and not only:
            return
        if self._revalidate_hook_cache(cache, only):
            self._write_briefing_cache(cache)
        try:
            os.unlink(claimed)
        except OSError as exc:  # stays dirty: the next write re-checks once more
            _LOG.debug("briefing cache dirty marker not removed: %s", exc)

    def _write_briefing_cache(self, cache: dict[str, Any]) -> None:
        path = self._briefing_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch = path.with_name(f"{BRIEFING_CACHE}.{os.getpid()}.tmp")
        write_private(scratch, json.dumps(cache, ensure_ascii=False))
        replace_file(scratch, path)

    def _claim_hook_cache_dirty(self) -> bool:
        """Rename the dirty marker to its claimed name; True when there was one."""
        data = Path(self.project_root) / "data"
        try:
            replace_file(data / BRIEFING_CACHE_DIRTY, data / BRIEFING_CACHE_DIRTY_CLAIMED)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:  # Windows: its writer still has it open; the next write claims it
            _LOG.debug("briefing cache dirty marker not claimed yet: %s", exc)
            return False

    @staticmethod
    def _hook_project_roots(projects: Iterable[Any]) -> dict[str, str]:
        roots: dict[str, str] = {}
        for project in projects:
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
                    replace_file(spool, batch)
                    batches.append(batch)
                except OSError as exc:
                    _LOG.warning("hook spool not taken for replay: %s", exc)
            if not batches:
                return stats
            started = time.perf_counter()
            for batch in batches:
                try:
                    # The hook writes JSON with raw U+2028/U+2029/U+0085 (ensure_ascii=False),
                    # which splitlines() would also break on: only "\n" ends a record.
                    lines = batch.read_text(encoding="utf-8").split("\n")
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
        """App start and once a minute: replay spooled captures, prune old sessions, refresh cached roots.

        Also drops cache entries of registered projects that no longer exist
        (deleted, or removed by a migration); an id that never had a projects row
        (a bind file, an explicit project_id) keeps its entry until the TTL.
        """
        try:
            self.drain_hook_spool()
            prune_hook_sessions(Path(self.project_root))
            self.remember_hook_pack("")
            known = {str(project.id) for project in self.repository.list_projects()} | {HOOK_DEFAULT_PROJECT}
            self.forget_hook_memories(projects=self._registered_hook_cache_projects() - known)
        except Exception as exc:  # noqa: BLE001 - background maintenance must not crash the app
            _LOG.error("hook maintenance failed: %s", exc, exc_info=True)
