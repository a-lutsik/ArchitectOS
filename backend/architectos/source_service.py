"""Source registry service: CRUD, discover, test, and ingest dispatch."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .constants import SYSTEM_PROJECT_ID
from .mcp import MCPError
from .models import Source, stable_id, utc_now
from .source_capabilities import discover_mcp_capabilities, guess_tool_map, preset_tool_map
from .source_mcp_generic import ingest_generic_mcp_candidates
from .source_registry import (
    LEGACY_AUTO_SOURCE_NAMES,
    LEGACY_SOURCE_BINDING_KEYS,
    default_local_bindings,
    default_source_title,
    default_source_title_for,
    effective_ingest_cap,
    normalize_kind,
    origin_slug,
    origin_slug_from_parts,
    source_binding_key,
    unique_source_name,
)
from .source_remote import fetch_http_probe, ftp_probe, ingest_ftp_candidates, ingest_http_candidates

_LOG = logging.getLogger("architectos.source_service")

FILE_KINDS = frozenset({"docs", "code", "adr", "issues", "pull_requests", "meetings", "inbox"})


class SourceRegistryMixin:
    """Project source bindings backed by the sources table."""

    def ensure_project_sources(self, project_id: str) -> list[dict[str, Any]]:
        existing = self.repository.list_sources(project_id)
        if existing:
            self._dedupe_project_sources(project_id)
            self._ensure_missing_local_bindings(project_id)
            self._sync_preset_mcp_bindings(project_id)
            self._apply_default_source_titles(project_id)
            return [item.to_dict() for item in self.repository.list_sources(project_id)]
        seeded = [Source.from_dict(item) for item in default_local_bindings(project_id)]
        for source in seeded:
            self.repository.upsert_source(source)
        self._sync_preset_mcp_bindings(project_id)
        self._apply_default_source_titles(project_id)
        return [item.to_dict() for item in self.repository.list_sources(project_id)]

    def _ensure_missing_local_bindings(self, project_id: str) -> int:
        existing_keys = {
            str(dict(item.config or {}).get("binding_key") or source_binding_key(item.to_dict()))
            for item in self.repository.list_sources(project_id)
        }
        added = 0
        for item in default_local_bindings(project_id):
            key = str(dict(item.get("config") or {}).get("binding_key") or "")
            if not key or key in existing_keys:
                continue
            self.repository.upsert_source(Source.from_dict(item))
            existing_keys.add(key)
            added += 1
        return added

    def _dedupe_project_sources(self, project_id: str) -> int:
        """Drop duplicate bindings (same binding_key) left by cross-project upserts."""
        by_key: dict[str, list[Any]] = {}
        for item in self.repository.list_sources(project_id):
            cfg = dict(item.config or {})
            key = str(cfg.get("binding_key") or source_binding_key(item.to_dict()) or "").strip()
            if not key:
                continue
            by_key.setdefault(key, []).append(item)
        removed = 0
        for key, items in by_key.items():
            if len(items) < 2:
                continue
            canonical_id = stable_id("source", project_id, key)

            def _rank(source: Any) -> tuple[int, int, str]:
                cfg = dict(source.config or {})
                return (
                    0 if str(source.id or "") == canonical_id else 1,
                    0 if cfg.get("last_run_at") or cfg.get("last_test_at") else 1,
                    str(source.created_at or ""),
                )

            keep = sorted(items, key=_rank)[0]
            for item in items:
                if item.id == keep.id:
                    continue
                self.delete_project_source(item.id)
                removed += 1
        return removed

    def _sync_preset_mcp_bindings(self, project_id: str) -> None:
        from .source_capabilities import NAMED_MCP_PRESETS

        existing_keys = {
            str(dict(item.config or {}).get("binding_key") or source_binding_key(item.to_dict()))
            for item in self.repository.list_sources(project_id)
        }
        for server_id, presets in NAMED_MCP_PRESETS.items():
            server = None
            get_server = getattr(self.mcp_manager, "get_server", None)
            if callable(get_server):
                try:
                    server = get_server(server_id)
                except Exception:  # noqa: BLE001 - optional MCP manager in tests
                    server = None
            mcp_label = str(getattr(server, "label", None) or server_id)
            server_enabled = bool(server and getattr(server, "enabled", False))
            for adapter, kind in presets:
                binding_key = f"mcp:{server_id}:{kind}"
                if binding_key in existing_keys:
                    continue
                slug = origin_slug_from_parts(driver="mcp", adapter=adapter, mcp_server_id=server_id, mcp_label=mcp_label, preset=server_id)
                title = default_source_title(driver="mcp", adapter=adapter, kind=kind, mcp_server_id=server_id) or slug
                default_mode = "memory" if adapter in {"azure-boards", "azure-git", "azure-wiki"} else "candidates"
                self.upsert_project_source({
                    "project_id": project_id,
                    "kind": kind,
                    "reset_name": True,
                    "config": {
                        "driver": "mcp",
                        "adapter": adapter,
                        "mcp_server_id": server_id,
                        "mcp_label": mcp_label,
                        "binding_key": binding_key,
                        "tool_map": preset_tool_map(adapter, kind),
                        "enabled": server_enabled,
                        "discovered": True,
                        "ingest_mode": default_mode,
                        "name_customized": False,
                        "origin_label": slug,
                        "default_title": title,
                    },
                })
                existing_keys.add(binding_key)

    def _apply_default_source_titles(self, project_id: str) -> int:
        """Rename non-custom / legacy auto names to the current default titles."""
        updated = 0
        for item in self.repository.list_sources(project_id):
            data = item.to_dict()
            cfg = dict(data.get("config") or {})
            title = default_source_title_for(data)
            if not title:
                continue
            current = str(data.get("name") or "").strip()
            customized = bool(cfg.get("name_customized"))
            legacy = current in LEGACY_AUTO_SOURCE_NAMES or current.casefold() in {n.casefold() for n in LEGACY_AUTO_SOURCE_NAMES}
            if current == title:
                if customized and legacy:
                    cfg["name_customized"] = False
                    cfg["origin_label"] = cfg.get("origin_label") or origin_slug(data) or title
                    self.repository.upsert_source(Source(
                        id=item.id,
                        project_id=item.project_id,
                        name=title,
                        kind=item.kind,
                        config=cfg,
                        created_at=item.created_at,
                        updated_at=utc_now(),
                    ))
                    updated += 1
                continue
            if customized and not legacy:
                continue
            cfg["name_customized"] = False
            cfg["origin_label"] = cfg.get("origin_label") or origin_slug(data) or title
            self.repository.upsert_source(Source(
                id=item.id,
                project_id=item.project_id,
                name=title,
                kind=item.kind,
                config=cfg,
                created_at=item.created_at,
                updated_at=utc_now(),
            ))
            updated += 1
        return updated

    def list_project_sources(self, project_id: str | None = None, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        pid = str(project_id or "").strip()
        if pid:
            self.ensure_project_sources(pid)
        sources = self.repository.list_sources(pid or None)
        out = [item.to_dict() for item in sources]
        if enabled_only:
            out = [item for item in out if dict(item.get("config") or {}).get("enabled")]
        return out

    def get_project_source(self, source_id: str) -> dict[str, Any] | None:
        source = self.repository.get_source(source_id)
        return source.to_dict() if source else None

    def list_sources(self, project_id: str | None = None) -> list[dict[str, Any]]:
        return self.list_project_sources(project_id)

    def create_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "").strip()
        kind = normalize_kind(payload.get("kind") or "docs")
        incoming_config = dict(payload.get("config") or {})
        driver = str(incoming_config.get("driver") or payload.get("driver") or "local_files")
        incoming_config["driver"] = driver
        binding_key = str(incoming_config.get("binding_key") or source_binding_key({"kind": kind, "config": incoming_config, "id": ""}))
        source_id = str(payload.get("id") or stable_id("source", project_id, binding_key))
        existing = self.repository.get_source(source_id)
        saved = self.upsert_project_source({**payload, "id": source_id})
        return {**saved, "created": existing is None}

    def update_source(self, source_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload or {})
        payload["id"] = source_id
        return self.upsert_project_source(payload)

    def _resolve_unique_name(self, project_id: str, base: str, *, kind: str = "", exclude_id: str = "") -> str:
        names = {
            str(item.name or "")
            for item in self.repository.list_sources(project_id)
            if str(item.id or "") != exclude_id
        }
        return unique_source_name(base, names, kind=kind)

    def upsert_project_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "").strip()
        source_id = str(payload.get("id") or "").strip()
        incoming_config = dict(payload.get("config") or {})
        driver = str(incoming_config.get("driver") or payload.get("driver") or "local_files")
        incoming_config["driver"] = driver
        existing = self.repository.get_source(source_id) if source_id else None
        # Never relocate an existing source to another project (caused duplicate Ask/git rows).
        if existing:
            project_id = str(existing.project_id or "").strip()
        if not project_id:
            raise ValueError("project_id is required")
        if not self.repository.get_project(project_id):
            raise ValueError(f"project not found: {project_id}")
        kind_raw = payload.get("kind")
        if kind_raw in (None, ""):
            kind = normalize_kind(existing.kind if existing else "docs")
        else:
            kind = normalize_kind(kind_raw)
        if not source_id:
            binding_key = str(incoming_config.get("binding_key") or source_binding_key({"kind": kind, "config": incoming_config, "id": ""}))
            source_id = stable_id("source", project_id, binding_key)
            existing = self.repository.get_source(source_id)
            if kind_raw in (None, "") and existing:
                kind = normalize_kind(existing.kind)
        auto_slug = origin_slug_from_parts(
            driver=driver,
            adapter=str(incoming_config.get("adapter") or ""),
            mcp_server_id=str(incoming_config.get("mcp_server_id") or ""),
            mcp_label=str(incoming_config.get("mcp_label") or ""),
            http_url=str((incoming_config.get("http") or {}).get("url") or ""),
            ftp_url=str((incoming_config.get("ftp") or {}).get("url") or ""),
            preset=str(incoming_config.get("adapter") or incoming_config.get("mcp_server_id") or ""),
        )
        auto_title = default_source_title(
            driver=driver,
            adapter=str(incoming_config.get("adapter") or ""),
            kind=kind,
            mcp_server_id=str(incoming_config.get("mcp_server_id") or ""),
        ) or str(incoming_config.get("default_title") or "") or auto_slug or "source"
        reset_name = bool(payload.get("reset_name"))
        if reset_name:
            name_customized = False
        elif "name_customized" in incoming_config:
            name_customized = bool(incoming_config.get("name_customized"))
        elif existing and dict(existing.config or {}).get("name_customized"):
            name_customized = True
        elif payload.get("name"):
            name_customized = True
        else:
            name_customized = False
        if name_customized:
            name = str(payload.get("name") or (existing.name if existing else "") or auto_title).strip()
            incoming_config["name_customized"] = True
            incoming_config["origin_label"] = incoming_config.get("origin_label") or auto_slug or name
        else:
            name = self._resolve_unique_name(project_id, auto_title, kind=kind, exclude_id=source_id)
            incoming_config["origin_label"] = auto_slug or name
            incoming_config["name_customized"] = False
        incoming_config.pop("default_title", None)
        merged_config = dict(existing.config if existing else {})
        merged_config.update(incoming_config)
        if "enabled" in incoming_config:
            schedule = dict(merged_config.get("schedule") or {})
            schedule["enabled"] = bool(incoming_config["enabled"])
            merged_config["schedule"] = schedule
        merged_config.setdefault("enabled", False)
        merged_config.setdefault("ingest_mode", "candidates")
        merged_config.setdefault("schedule", {"enabled": False, "interval_minutes": 60, "on_startup": False})
        merged_config.setdefault("timeouts", {"item": 25, "source": 180})
        merged_config.setdefault("max_items", 0)
        source = Source(
            id=source_id,
            project_id=project_id,
            name=name,
            kind=kind,
            config=merged_config,
            created_at=existing.created_at if existing else utc_now(),
            updated_at=utc_now(),
        )
        saved = self.repository.upsert_source(source)
        return saved.to_dict()

    def delete_project_source(self, source_id: str, *, purge_memory: bool = True) -> dict[str, Any]:
        """Delete a source binding; by default also wipe its ingested memory."""
        source = self.repository.get_source(source_id)
        if not source:
            raise ValueError(f"source not found: {source_id}")
        wipe: dict[str, Any] = {}
        if purge_memory:
            wipe = self.clear_source_memory(source_id, confirm=True, delete_binding=True)
            return {"deleted": True, "id": source_id, "purge_memory": True, **wipe}
        with self.repository._connect() as conn:
            conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        return {"deleted": True, "id": source_id, "purge_memory": False}

    def discover_sources_from_mcp(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "").strip()
        server_id = str(payload.get("server_id") or payload.get("mcp_server_id") or "").strip()
        if not project_id or not server_id:
            raise ValueError("project_id and server_id are required")
        server = self.mcp_manager.get_server(server_id)
        if not server:
            raise ValueError(f"MCP server not found: {server_id}")
        tools_payload = self.mcp_manager.list_tools(server_id)
        tools = list(tools_payload.get("tools") or [])
        specs = discover_mcp_capabilities(server_id, tools)
        created: list[dict[str, Any]] = []
        for spec in specs:
            kind = str(spec.get("kind") or "docs")
            adapter = str(spec.get("adapter") or "generic")
            tool_map = preset_tool_map(adapter, kind) or (guess_tool_map(kind, tools) if adapter == "generic" else {})
            slug = origin_slug_from_parts(driver="mcp", adapter=adapter, mcp_server_id=server_id, mcp_label=server.label, preset=server_id)
            binding_key = f"mcp:{server_id}:{kind}"
            saved = self.upsert_project_source({
                "project_id": project_id,
                "kind": kind,
                "reset_name": True,
                "config": {
                    "driver": "mcp",
                    "adapter": adapter,
                    "mcp_server_id": server_id,
                    "mcp_label": server.label,
                    "binding_key": binding_key,
                    "discovered": True,
                    "needs_mapping": bool(spec.get("needs_mapping")),
                    "tool_map": tool_map,
                    "enabled": False,
                    "name_customized": False,
                    "origin_label": slug,
                },
            })
            created.append(saved)
        return {"server_id": server_id, "sources": created, "tools": [t.get("name") for t in tools]}

    def probe_source_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Test a source config without persisting (used by Add source → Test connection)."""
        cfg = dict(payload.get("config") or {})
        driver = str(payload.get("driver") or cfg.get("driver") or "").strip()
        project_id = str(payload.get("project_id") or cfg.get("project_id") or "").strip()
        started = time.monotonic()
        try:
            result = self._probe_source_driver(driver, cfg, project_id=project_id)
            latency_ms = int((time.monotonic() - started) * 1000)
            return {"ok": True, "latency_ms": latency_ms, "driver": driver, **result}
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            return {"ok": False, "latency_ms": latency_ms, "driver": driver, "error": str(exc)}

    def _probe_source_driver(self, driver: str, cfg: dict[str, Any], *, project_id: str = "") -> dict[str, Any]:
        if driver == "http":
            return fetch_http_probe(dict(cfg.get("http") or {}))
        if driver == "ftp":
            return ftp_probe(dict(cfg.get("ftp") or {}))
        if driver == "mcp":
            server_id = str(cfg.get("mcp_server_id") or "").strip()
            if not server_id:
                raise ValueError("Choose an MCP server")
            server = self.mcp_manager.get_server(server_id)
            if not server or not server.enabled:
                raise ValueError(f"Enable MCP server {server_id} before testing")
            tools = self.mcp_manager.list_tools(server_id)
            names = [str(t.get("name") or "") for t in tools.get("tools") or []][:8]
            adapter = str(cfg.get("adapter") or "").strip()
            tool_map = dict(cfg.get("tool_map") or {})
            list_tool = str(tool_map.get("list") or "").strip()
            if adapter in {"azure-boards", "azure-git", "azure-wiki"}:
                return {"ok": True, "sample": f"{adapter} adapter configured", "tools": names}
            if adapter == "granola":
                listed = self.mcp_manager.call_tool(server_id, "list_meetings", {}, timeout=20)
                return {"ok": True, "sample": "Granola reachable", "tools": names, "listed": str(listed)[:200]}
            if adapter in {"github", "gitlab"} and list_tool:
                listed = self.mcp_manager.call_tool(server_id, list_tool, {}, timeout=20)
                return {"ok": True, "sample": str(listed)[:200], "tools": names}
            if list_tool:
                listed = self.mcp_manager.call_tool(server_id, list_tool, {}, timeout=20)
                return {"ok": True, "sample": str(listed)[:200], "tools": names}
            if not names:
                raise ValueError(f"MCP server {server_id} returned no tools")
            return {
                "ok": True,
                "sample": f"MCP server reachable · {len(names)} tool(s)",
                "tools": names,
            }
        if driver == "local_files":
            project = self.repository.get_project(project_id) if project_id else None
            root = Path(str(project.root_path if project else "") or self.project_root)
            if not root.is_dir():
                raise ValueError(f"Project root missing: {root}")
            count = len(list(root.rglob("*.md"))[:20])
            return {"ok": True, "sample": str(root), "count": count}
        if driver == "local_git":
            project = self.repository.get_project(project_id) if project_id else None
            root = Path(str(project.root_path if project else "") or self.project_root)
            return {"ok": True, "sample": str(root), "git": (root / ".git").is_dir()}
        if driver == "chat":
            chats = self.repository.list_chats(project_id) if project_id else []
            return {"ok": True, "count": len(chats)}
        raise ValueError(f"Unsupported driver: {driver or '(empty)'}")

    def test_project_source(self, source_id: str) -> dict[str, Any]:
        source = self.get_project_source(source_id)
        if not source:
            raise ValueError(f"source not found: {source_id}")
        cfg = dict(source.get("config") or {})
        driver = str(cfg.get("driver") or "")
        result = self.probe_source_config({
            "driver": driver,
            "project_id": source.get("project_id"),
            "config": cfg,
        })
        cfg["last_test_at"] = utc_now()
        cfg["last_test_ok"] = bool(result.get("ok"))
        cfg["last_test_error"] = "" if result.get("ok") else str(result.get("error") or "Test failed")
        cfg["last_test_sample"] = str(result.get("sample") or result.get("title") or "")[:400]
        self.upsert_project_source({
            "id": source_id,
            "project_id": source.get("project_id"),
            "kind": source.get("kind"),
            "config": cfg,
        })
        return result

    def _sources_for_ingest_request(self, project_id: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        requested = payload.get("sources")
        all_sources = self.ensure_project_sources(project_id)
        if not requested:
            return [item for item in all_sources if dict(item.get("config") or {}).get("enabled")]
        if requested == "all" or requested == ["all"]:
            return [item for item in all_sources if dict(item.get("config") or {}).get("enabled")]
        ids: set[str] = set()
        kinds: set[str] = set()
        binding_keys: set[str] = set()
        for raw in requested if isinstance(requested, list) else [requested]:
            token = str(raw or "").strip()
            if not token:
                continue
            if token.startswith("source_"):
                ids.add(token)
            elif token in LEGACY_SOURCE_BINDING_KEYS:
                binding_keys.add(LEGACY_SOURCE_BINDING_KEYS[token])
            else:
                kinds.add(normalize_kind(token))
        selected: list[dict[str, Any]] = []
        for item in all_sources:
            cfg = dict(item.get("config") or {})
            if ids and str(item.get("id") or "") in ids:
                selected.append(item)
                continue
            if binding_keys and str(cfg.get("binding_key") or source_binding_key(item)) in binding_keys:
                selected.append(item)
                continue
            if kinds and str(item.get("kind") or "") in kinds:
                selected.append(item)
        if not selected and (kinds or binding_keys or ids):
            # Legacy path: allow ingest by kind even if binding disabled when explicitly requested.
            for item in all_sources:
                if ids and str(item.get("id") or "") in ids:
                    selected.append(item)
                elif binding_keys and str(dict(item.get("config") or {}).get("binding_key") or source_binding_key(item)) in binding_keys:
                    selected.append(item)
                elif kinds and str(item.get("kind") or "") in kinds:
                    selected.append(item)
        return selected

    def _annotate_candidates(self, candidates: list[dict[str, Any]], source: dict[str, Any]) -> list[dict[str, Any]]:
        source_id = str(source.get("id") or "")
        source_name = str(source.get("name") or "")
        kind = str(source.get("kind") or "")
        out: list[dict[str, Any]] = []
        for candidate in candidates:
            item = dict(candidate)
            meta = dict(item.get("metadata") or {})
            meta.setdefault("source_id", source_id)
            meta.setdefault("source_name", source_name)
            meta.setdefault("source_kind", kind)
            item["metadata"] = meta
            if source_id and not item.get("source_id"):
                item["source_id"] = source_id
            if kind and not item.get("source_type"):
                item["source_type"] = kind
            out.append(item)
        return out

    def ingest_source_binding(self, source: dict[str, Any], project_id: str, payload: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
        cfg = dict(source.get("config") or {})
        driver = str(cfg.get("driver") or "")
        kind = str(source.get("kind") or "")
        source_id = str(source.get("id") or "")
        source_name = str(source.get("name") or origin_slug(source))
        ingest_mode = self._normalize_ingest_mode(payload.get("ingest_mode") or payload.get("mode") or cfg.get("ingest_mode"))
        direct = ingest_mode == "memory"
        mcp_direct = ingest_mode in {"memory", "mixed"}
        timeout_key = str(cfg.get("adapter") or driver or kind)
        timed_payload = self._with_ingest_timeouts(timeout_key, {**payload, **cfg})
        item_timeout = float(timed_payload.get("_ingest_item_timeout") or 25)
        project = self.repository.get_project(project_id)
        root_raw = str(payload.get("root_path") or (project.root_path if project and project.root_path else self.project_root))
        root = Path(root_raw).resolve() if root_raw else None
        candidates: list[dict[str, Any]] = []
        imported: list[dict[str, Any]] = []
        updated = 0

        def deadline() -> bool:
            return self._ingest_deadline_expired(timed_payload, timeout_key)

        def mcp_call(server_id: str, tool: str, args: dict[str, Any], timeout: float | None) -> Any:
            return self.mcp_manager.call_tool(server_id, tool, args, timeout=timeout or item_timeout)

        try:
            cap = effective_ingest_cap(int(cfg.get("max_items") or payload.get("limit") or 0))
            if driver == "local_files" and kind in FILE_KINDS:
                if root is None or not root.is_dir():
                    raise ValueError(f"project path does not exist: {root}")
                if kind == "inbox":
                    raw = self._ingest_inbox_candidates(project_id, cap)
                else:
                    scan_sources = ["prs"] if kind == "pull_requests" else [kind if kind != "git_history" else "git"]
                    if kind == "issues":
                        scan_sources = ["issues"]
                    elif kind in {"docs", "code", "adr", "meetings"}:
                        scan_sources = [kind]
                    raw = self._ingest_file_candidates(project_id, root, scan_sources, max_items=0)
                candidates = self._annotate_candidates(raw, source)
            elif driver == "local_git" or kind == "git_history":
                if root is None:
                    raise ValueError("git source requires project root")
                raw = self._ingest_git_candidates(project_id, root, max_items=0)
                candidates = self._annotate_candidates(raw, source)
            elif driver == "chat" or kind == "chat":
                raw = self._ingest_chat_candidates(project_id, max_items=0)
                candidates = self._annotate_candidates(raw, source)
            elif driver == "http":
                raw = ingest_http_candidates(project_id, source, stable_id=stable_id, deadline_expired=deadline)
                candidates = self._annotate_candidates(raw, source)
            elif driver == "ftp":
                raw = ingest_ftp_candidates(project_id, source, stable_id=stable_id, deadline_expired=deadline)
                candidates = self._annotate_candidates(raw, source)
            elif driver == "mcp":
                adapter = str(cfg.get("adapter") or "")
                server_id = str(cfg.get("mcp_server_id") or "")

                def run_mcp(fn):
                    return self._run_source_with_timeout(timeout_key, payload, warnings, fn)

                if adapter == "granola":
                    raw = run_mcp(lambda timed: self._ingest_granola_candidates(project_id, cap, timed)) or []
                    candidates = self._annotate_candidates(list(raw), source)
                elif adapter == "azure-boards":
                    if mcp_direct:
                        result = run_mcp(lambda timed: self._import_azure_boards_to_memory(project_id, cap, timed)) or {}
                        imported = list(result.get("imported") or [])
                        updated = int(result.get("updated") or 0)
                    else:
                        raw = run_mcp(lambda timed: self._ingest_azure_boards_candidates(project_id, cap, timed)) or []
                        candidates = self._annotate_candidates(list(raw), source)
                elif adapter == "azure-git":
                    if mcp_direct:
                        result = run_mcp(lambda timed: self._import_azure_git_to_memory(project_id, cap, timed)) or {}
                        imported = list(result.get("imported") or [])
                        updated = int(result.get("updated") or 0)
                    else:
                        def _git(timed: dict[str, Any]) -> list[dict[str, Any]]:
                            ado_project = self._azure_boards_project(timed)
                            sid = server_id or self._azure_git_mcp_server_id()
                            return self._ingest_azure_git_candidates(project_id, ado_project, sid, cap, timed)
                        raw = run_mcp(_git) or []
                        candidates = self._annotate_candidates(list(raw), source)
                elif adapter == "azure-wiki":
                    if mcp_direct:
                        result = run_mcp(lambda timed: self._import_azure_wiki_to_memory(project_id, cap, timed)) or {}
                        imported = list(result.get("imported") or [])
                        updated = int(result.get("updated") or 0)
                    else:
                        raw = run_mcp(lambda timed: self._ingest_azure_wiki_candidates(project_id, cap, timed)) or []
                        candidates = self._annotate_candidates(list(raw), source)
                elif adapter in {"github", "gitlab"}:
                    def _generic(_timed: dict[str, Any]) -> list[dict[str, Any]]:
                        return ingest_generic_mcp_candidates(
                            project_id, source,
                            call_tool=mcp_call,
                            stable_id=stable_id,
                            item_timeout=item_timeout,
                            deadline_expired=deadline,
                        )
                    raw = run_mcp(_generic) or ingest_generic_mcp_candidates(
                        project_id, source, call_tool=mcp_call, stable_id=stable_id,
                        item_timeout=item_timeout, deadline_expired=deadline,
                    )
                    candidates = self._annotate_candidates(list(raw or []), source)
                else:
                    def _generic(_timed: dict[str, Any]) -> list[dict[str, Any]]:
                        return ingest_generic_mcp_candidates(
                            project_id, source,
                            call_tool=mcp_call,
                            stable_id=stable_id,
                            item_timeout=item_timeout,
                            deadline_expired=deadline,
                        )
                    raw = run_mcp(_generic) or []
                    candidates = self._annotate_candidates(list(raw), source)
            else:
                raise ValueError(f"Unsupported source driver: {driver}")
        except (MCPError, ValueError) as exc:
            warnings.append(f"{source_name}: {exc}")
            cfg["last_run_at"] = utc_now()
            cfg["last_run_error"] = str(exc)
            self.upsert_project_source({
                "id": source_id,
                "project_id": source.get("project_id") or project_id,
                "kind": kind,
                "config": cfg,
            })
            return {"candidates": [], "imported": [], "updated": 0, "warning": str(exc)}

        cfg["last_run_at"] = utc_now()
        cfg["last_run_error"] = ""
        cfg["last_run_count"] = len(candidates) + len(imported)
        self.upsert_project_source({
            "id": source_id,
            "project_id": source.get("project_id") or project_id,
            "kind": kind,
            "config": cfg,
        })
        return {"candidates": candidates, "imported": imported, "updated": updated}

    def _ingest_max_items(self, value: Any) -> int:
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError):
            parsed = 0
        return max(0, parsed)
