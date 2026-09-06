"""Per-source ingest timeout budgets and deadline helpers."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any

from .constants import DEFAULT_INGEST_TIMEOUTS

_LOG = logging.getLogger("architectos.service")


class IngestionTimeoutsMixin:
    def _normalize_ingest_mode(self, value: Any) -> str:
        raw = str(value or "").strip().lower().replace("-", "_")
        if raw in {"memory", "direct", "direct_memory", "direct_to_memory", "add_to_memory"}:
            return "memory"
        if raw in {"candidate", "candidates", "review", "review_queue"}:
            return "candidates"
        if raw in {"", "mixed", "legacy", "auto"}:
            return "mixed"
        raise ValueError("ingest_mode must be candidates, memory, or mixed")

    def _resolve_ingest_timeouts(self, source: str, payload: dict[str, Any] | None = None) -> tuple[float, float]:
        """Return (item_timeout_seconds, source_timeout_seconds) for an ingest source."""
        payload = payload or {}
        defaults = DEFAULT_INGEST_TIMEOUTS.get(source) or {"item": 30, "source": 300}
        item_default = float(defaults.get("item") or 30)
        source_default = float(defaults.get("source") or 300)

        per_source = {}
        raw_map = payload.get("timeouts")
        if isinstance(raw_map, dict):
            entry = raw_map.get(source) or raw_map.get(source.replace("-", "_"))
            if isinstance(entry, dict):
                per_source = entry
            elif isinstance(entry, (int, float, str)):
                per_source = {"source": entry}

        source_key = source.replace("-", "_")
        item_raw = (
            per_source.get("item")
            or per_source.get("item_timeout")
            or payload.get(f"{source_key}_item_timeout")
            or payload.get("item_timeout")
            or item_default
        )
        source_raw = (
            per_source.get("source")
            or per_source.get("source_timeout")
            or payload.get(f"{source_key}_source_timeout")
            or payload.get("source_timeout")
            or source_default
        )
        try:
            item_timeout = float(item_raw)
        except (TypeError, ValueError):
            item_timeout = item_default
        try:
            source_timeout = float(source_raw)
        except (TypeError, ValueError):
            source_timeout = source_default
        item_timeout = max(5.0, min(item_timeout, 600.0))
        source_timeout = max(1.0, min(source_timeout, 7200.0))
        return item_timeout, source_timeout

    def _with_ingest_timeouts(self, source: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        enriched = dict(payload or {})
        item_timeout, source_timeout = self._resolve_ingest_timeouts(source, enriched)
        enriched["_ingest_source"] = source
        enriched["_ingest_item_timeout"] = item_timeout
        enriched["_ingest_source_timeout"] = source_timeout
        enriched["_ingest_source_deadline"] = time.monotonic() + source_timeout
        return enriched

    @staticmethod
    def _ingest_item_timeout(payload: dict[str, Any] | None, default: float = 30.0) -> float:
        payload = payload or {}
        try:
            value = float(payload.get("_ingest_item_timeout") or default)
        except (TypeError, ValueError):
            value = default
        return max(5.0, min(value, 600.0))

    @staticmethod
    def _ingest_deadline_remaining(payload: dict[str, Any] | None) -> float | None:
        payload = payload or {}
        deadline = payload.get("_ingest_source_deadline")
        if deadline is None:
            return None
        try:
            return max(0.0, float(deadline) - time.monotonic())
        except (TypeError, ValueError):
            return None

    def _ingest_call_timeout(self, payload: dict[str, Any] | None, item_timeout: float) -> float:
        """Cap a single MCP call to the remaining source budget so item timeouts cannot overrun the source."""
        try:
            item = float(item_timeout or 5.0)
        except (TypeError, ValueError):
            item = 5.0
        item = max(0.5, min(item, 600.0))
        remaining = self._ingest_deadline_remaining(payload)
        if remaining is None:
            return item
        return max(0.5, min(item, remaining))

    def _ingest_deadline_expired(self, payload: dict[str, Any] | None, source: str = "") -> bool:
        remaining = self._ingest_deadline_remaining(payload)
        if remaining is None:
            return False
        if remaining > 0:
            return False
        label = source or str((payload or {}).get("_ingest_source") or "source")
        self._log_ingest(
            f"{label} source timeout reached — skipping remaining work.",
            level="warn",
            source=label,
            current=label,
        )
        return True

    def _abort_ingest_source(self, source: str) -> None:
        """Best-effort: kill wedged MCP sessions so a hung item cannot block later sources."""
        session_ids = {
            "azure-boards": ("azure-devops",),
            "azure-git": ("azure-devops-git", "azure-devops"),
            "azure-wiki": ("azure-devops",),
            "granola": ("granola",),
        }.get(source, ())
        for server_id in session_ids:
            try:
                self.mcp_manager.close_session(server_id)
            except Exception:
                pass

    def _set_ingest_partial(self, payload: dict[str, Any] | None, result: Any) -> None:
        """Publish a salvageable mid-flight result for source-timeout recovery."""
        box = (payload or {}).get("_ingest_partial")
        if isinstance(box, dict):
            box["result"] = result

    def _run_source_with_timeout(
        self,
        source: str,
        payload: dict[str, Any] | None,
        warnings: list[str],
        fn: Callable[[dict[str, Any]], Any],
    ) -> Any:
        """Run one ingest source under a hard source-level timeout.

        Important: never use ``with ThreadPoolExecutor(...)`` here. Its ``__exit__``
        calls ``shutdown(wait=True)``, which would block past the timeout while a
        wedged MCP worker keeps running.

        Sources should call ``_set_ingest_partial`` as items complete so a timeout
        can still return already-fetched candidates instead of discarding them.
        """
        timed = self._with_ingest_timeouts(source, payload)
        item_timeout = float(timed["_ingest_item_timeout"])
        source_timeout = float(timed["_ingest_source_timeout"])
        partial_box: dict[str, Any] = {"result": None}
        timed["_ingest_partial"] = partial_box
        timed["_ingest_warnings"] = warnings
        self._log_ingest(
            f"Timeouts · item={item_timeout:g}s · source={source_timeout:g}s",
            source=source,
            current=source,
        )
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(fn, timed)
            try:
                return future.result(timeout=source_timeout)
            except FuturesTimeoutError:
                message = f"{source} source timeout after {source_timeout:g}s"
                warnings.append(message)
                self._log_ingest(message, level="warn", source=source, current=source)
                self._abort_ingest_source(source)
                # Brief grace: inner path may be returning partial candidates right now.
                try:
                    result = future.result(timeout=0.4)
                    if result is not None:
                        self._log_ingest(
                            f"{source} kept partial result after timeout.",
                            level="warn",
                            source=source,
                            current=source,
                        )
                        return result
                except FuturesTimeoutError:
                    pass
                except Exception:
                    pass
                salvaged = partial_box.get("result")
                if salvaged is not None:
                    count = len(salvaged) if isinstance(salvaged, list) else (
                        len(salvaged.get("imported") or []) if isinstance(salvaged, dict) else 1
                    )
                    self._log_ingest(
                        f"{source} salvaged {count} partial item(s) after timeout.",
                        level="warn",
                        source=source,
                        current=source,
                    )
                    return salvaged
                future.cancel()
                return None
        finally:
            # Detach immediately — do not wait for hung MCP/stdio workers.
            executor.shutdown(wait=False, cancel_futures=True)
