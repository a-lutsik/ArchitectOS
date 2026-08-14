"""Settings, embeddings maintenance, AI router and workflow orchestration.

Extracted from ``service.py``. Covers the public settings surface and its
private helpers, embedding-engine reload/backfill/index maintenance, the memory
decay loop, the AI router settings/preview helpers, and the high-level workflow
runner. Depends only on leaf modules; the repository, providers and embedding
engine are reached through ``self`` via the MRO on
:class:`ArchitectOSService`.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

from .embeddings import MemoryEmbeddingEngine, build_embedding_provider
from .models import utc_now
from .routing import RouterPolicy, classify_role

_LOG = logging.getLogger("architectos.service")


class SettingsRouterServiceMixin:
    """Settings, embeddings maintenance, AI router and workflow helpers."""

    def workflows(self) -> dict[str, Any]:
        return {"workflows": list(self._workflow_definitions().values())}

    def run_workflow(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        definitions = self._workflow_definitions()
        workflow = definitions.get(workflow_id)
        if not workflow:
            raise ValueError("workflow not found")
        project_id = str(payload.get("project_id") or "architectos")
        query = self.security_policy.redact_text(str(payload.get("query") or workflow["name"]).strip())[0]
        if not query:
            raise ValueError("workflow query is required")
        provider_id = str(payload.get("provider_id") or "auto")
        scan_limit = max(0, int(payload.get("scan_limit") or workflow.get("scan_limit") or 8))
        steps: list[dict[str, Any]] = []
        artifacts: dict[str, Any] = {"query": query, "workflow": workflow}

        scan_result = {"count": 0, "imported": [], "root": str(self.project_root)}
        if scan_limit:
            try:
                scan_result = self.scan_project({"project_id": project_id, "limit": scan_limit})
                steps.append(self._workflow_step("scan", "Scan Project", "done", f"Imported {scan_result['count']} file summary node(s).", {"count": scan_result["count"], "root": scan_result["root"]}))
            except ValueError as exc:
                steps.append(self._workflow_step("scan", "Scan Project", "skipped", str(exc), {"root": str(self.project_root)}))
        else:
            steps.append(self._workflow_step("scan", "Scan Project", "skipped", "Project scan disabled for this run.", {"count": 0}))
        artifacts["scan"] = scan_result

        context = self.context(query, project_id=project_id, limit=int(payload.get("limit") or 8))
        steps.append(self._workflow_step("context", "Build Context", "done", f"Built context pack with {len(context['hits'])} memory hit(s).", {"hits": len(context["hits"])}))
        artifacts["context"] = context["context"]

        task = self.create_task({
            "project_id": project_id,
            "title": f"Workflow {workflow['name']}: {query[:64]}",
            "priority": str(payload.get("priority") or "medium"),
            "status": "doing",
            "detail": f"Workflow run started for {workflow['name']}. Provider: {provider_id}.",
        })
        steps.append(self._workflow_step("task", "Write Task", "done", f"Created workflow task: {task['title']}", {"task_id": task["id"]}))

        provider_message = self._workflow_prompt(workflow, query, context["context"])
        provider_result = self.run_ai({
            "project_id": project_id,
            "message": provider_message,
            "provider_id": provider_id,
            "limit": int(payload.get("limit") or 8),
            "allow_cli": payload.get("allow_cli"),
        })
        provider_status = str(provider_result.get("provider", {}).get("status") or "unknown")
        steps.append(self._workflow_step("provider", "Ask Provider", "done" if provider_result.get("text") else "empty", f"Provider {provider_result.get('provider', {}).get('id')} finished with {provider_status}.", {"run_id": provider_result.get("run_id"), "provider": provider_result.get("provider")}))
        artifacts["provider_result"] = provider_result

        memory_text = (
            f"Workflow: {workflow['name']}\n"
            f"Goal: {query}\n"
            f"Provider: {provider_result.get('provider', {}).get('id')} / {provider_status}\n\n"
            f"{provider_result.get('text') or 'No provider response text.'}"
        )
        memory = self.add_memory({
            "project_id": project_id,
            "type": "Lesson",
            "label": f"Workflow result: {query[:56]}",
            "scope": "project",
            "text": memory_text[:3200],
            "source": "workflow",
        })
        steps.append(self._workflow_step("memory", "Write Memory", "done", f"Persisted workflow memory: {memory['label']}", {"memory_id": memory["id"]}))

        task = self.update_task(task["id"], {"status": "done", "detail": f"Workflow completed. Memory: {memory['id']}. Provider run: {provider_result.get('run_id') or 'n/a'}."})
        review_task = self.create_task({
            "project_id": project_id,
            "title": f"Review workflow result: {query[:56]}",
            "priority": "medium",
            "status": "todo",
            "detail": f"Review memory {memory['id']} and promote follow-up work from workflow {workflow['name']}.",
            "linked_memory_ids": [memory["id"]],
        })
        steps.append(self._workflow_step("review", "Review", "done", f"Created review task: {review_task['title']}", {"task_id": review_task["id"], "memory_id": memory["id"]}))

        return {
            "workflow_id": workflow_id,
            "workflow": workflow,
            "status": "ok",
            "project_id": project_id,
            "query": query,
            "context": context["context"],
            "steps": steps,
            "task": task,
            "review_task": review_task,
            "memory": memory,
            "provider": provider_result.get("provider"),
            "response": provider_result.get("text") or "",
            "artifacts": artifacts,
        }

    def _workflow_definitions(self) -> dict[str, dict[str, Any]]:
        base_steps = [
            {"id": "scan", "name": "Scan Project"},
            {"id": "context", "name": "Build Context"},
            {"id": "task", "name": "Write Task"},
            {"id": "provider", "name": "Ask Provider"},
            {"id": "memory", "name": "Write Memory"},
            {"id": "review", "name": "Review"},
        ]
        return {
            "intro": {"id": "intro", "name": "Intro", "description": "Build onboarding context for a project or feature.", "prompt": "Produce a concise onboarding brief with known decisions, risks, and next actions.", "scan_limit": 8, "steps": base_steps},
            "access": {"id": "access", "name": "Access", "description": "Check provider, secret, and environment readiness.", "prompt": "Review provider setup and identify concrete access checks without exposing secrets.", "scan_limit": 0, "steps": base_steps},
            "rules": {"id": "rules", "name": "Rules", "description": "Extract project rules, decisions, and constraints into memory.", "prompt": "Extract durable engineering rules, constraints, and decisions from the context.", "scan_limit": 10, "steps": base_steps},
            "help": {"id": "help", "name": "Help", "description": "Create a context pack and implementation brief for a coding task.", "prompt": "Create an implementation brief with assumptions, plan, risks, and verification steps.", "scan_limit": 8, "steps": base_steps},
            "review": {"id": "review", "name": "Review", "description": "Prepare architecture/code review notes.", "prompt": "Review the context for correctness risks, missing tests, and follow-up tasks.", "scan_limit": 8, "steps": base_steps},
        }

    def _workflow_prompt(self, workflow: dict[str, Any], query: str, context: str) -> str:
        return (
            f"Run ArchitectOS workflow: {workflow['name']}\n"
            f"Goal: {query}\n"
            f"Instruction: {workflow.get('prompt') or workflow['description']}\n\n"
            "Use this context and return: summary, decisions, risks, next actions, and verification.\n\n"
            f"{context}"
        )

    def _workflow_step(self, step_id: str, name: str, status: str, message: str, artifact: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"id": step_id, "name": name, "status": status, "message": message, "artifact": artifact or {}, "finished_at": utc_now()}

    def settings(self) -> dict[str, Any]:
        settings = self.repository.all_settings()
        settings.setdefault("memory_lifecycle", self.memory_lifecycle.settings())
        settings.setdefault("memory_retrieval", self.memory_embeddings.settings())
        settings["memory_embeddings"] = self.memory_embeddings_status()
        return self._public_settings(settings)

    def memory_embeddings_status(self, project_id: str | None = None) -> dict[str, Any]:
        return self.memory_embeddings.status(project_id)

    def rebuild_memory_embeddings(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        if payload.get("provider") or payload.get("model") or payload.get("embedding_provider") or payload.get("embedding_model"):
            current = dict(self.repository.get_setting("memory_retrieval") or {})
            if payload.get("provider") or payload.get("embedding_provider"):
                current["embedding_provider"] = str(payload.get("provider") or payload.get("embedding_provider") or current.get("embedding_provider") or "auto")
            if payload.get("model") or payload.get("embedding_model"):
                current["embedding_model"] = str(payload.get("model") or payload.get("embedding_model") or "")
            if payload.get("dimensions") or payload.get("embedding_dimensions"):
                current["embedding_dimensions"] = int(payload.get("dimensions") or payload.get("embedding_dimensions") or 768)
            self.repository.set_setting("memory_retrieval", current)
            self._reload_embedding_engine(rebuild=False)
        if payload.get("clear"):
            self.repository.rebuild_memory_embeddings()
        if payload.get("async", True):
            threading.Thread(target=self._backfill_embeddings_safe, name="embed-rebuild-api", daemon=True).start()
            return {"started": True, "async": True, **self.memory_embeddings.provider_info()}
        result = self.memory_embeddings.rebuild_all() if payload.get("full") else self.memory_embeddings.ensure_indexed()
        return {"started": False, "async": False, **result}

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        prev_retrieval = dict(self.repository.get_setting("memory_retrieval") or {})
        for key, value in payload.items():
            if not isinstance(value, dict):
                raise ValueError("settings values must be objects")
            if key in {"memory_lifecycle", "ui", "security", "workspace", "router", "memory_retrieval"}:
                current = dict(self.repository.get_setting(key) or {})
                current.update(value)
                self.repository.set_setting(key, current)
            else:
                self.repository.set_setting(key, value)
        if "memory_retrieval" in payload:
            # Rebuild only when the embedding identity changes — timeout/pool/min_score are live.
            identity_keys = ("embeddings_enabled", "embedding_provider", "embedding_model", "embedding_dimensions")
            next_retrieval = dict(self.repository.get_setting("memory_retrieval") or {})
            needs_rebuild = any(
                str(prev_retrieval.get(key) or "") != str(next_retrieval.get(key) or "")
                for key in identity_keys
            )
            self._reload_embedding_engine(rebuild=needs_rebuild)
        self._sync_router_settings()
        return self.settings()

    def _reload_embedding_engine(self, *, rebuild: bool = False) -> None:
        retrieval_settings = dict(self.repository.get_setting("memory_retrieval") or {})
        self.embedding_provider = build_embedding_provider(retrieval_settings)
        # The queue listener resolves self.memory_embeddings lazily, so swapping
        # the engine needs no listener re-registration.
        self.memory_embeddings = MemoryEmbeddingEngine(self.repository, self.embedding_provider)
        if rebuild and self.memory_embeddings.enabled():
            threading.Thread(target=self._backfill_embeddings_safe, name="embed-rebuild", daemon=True).start()

    def _warmup_embeddings_safe(self) -> None:
        """Prime the embedding provider so the first Search does not burn the timeout."""
        try:
            self.memory_embeddings.provider.embed("warmup", purpose="query")
            # Load unpacked vectors into RAM once (7k×1024 is fine locally).
            cache_size = len(self.memory_embeddings._load_vector_cache())  # noqa: SLF001
            _LOG.info(
                "embedding warmup ok · provider=%s model=%s dims=%s cache=%s",
                getattr(self.memory_embeddings.provider, "provider_id", ""),
                getattr(self.memory_embeddings.provider, "model", ""),
                getattr(self.memory_embeddings.provider, "dimensions", 0),
                cache_size,
            )
        except Exception as exc:
            _LOG.warning("embedding warmup skipped: %s", exc)

    def _memory_decay_loop(self, *, initial_delay_s: float = 600.0, interval_s: float = 86400.0) -> None:
        """Daily lifecycle decay so fresh/fading/stale/archived states progress."""
        if self._decay_stop.wait(initial_delay_s):
            return
        while not self._decay_stop.is_set():
            try:
                if self.memory_lifecycle.settings().get("enabled"):
                    result = self.memory_lifecycle.run_decay(None, dry_run=False)
                    _LOG.info("memory decay done · changed=%s", result.get("changed"))
            except Exception as exc:  # noqa: BLE001 - background worker must not crash the app
                _LOG.warning("memory decay failed: %s", exc)
            if self._decay_stop.wait(interval_s):
                return

    def _enqueue_embedding_index(self, node: Any) -> None:
        """Node-upsert listener: queue embedding indexing off the request path."""
        try:
            if not self.memory_embeddings.enabled():
                return
            # Symbol nodes are only embedded when the code-graph policy marks
            # them (metadata.cg_embed); this keeps embedding cost bounded even
            # though symbols outnumber files 10-50x.
            if getattr(node, "type", "") == "Symbol" and not (getattr(node, "metadata", None) or {}).get("cg_embed"):
                return
            if not self._embedding_worker_started:
                # No background worker (tests, short-lived use): index inline.
                self.memory_embeddings.index_node(node)
                return
            self._embedding_index_queue.put(str(node.id))
        except Exception as exc:
            _LOG.warning("embedding index enqueue failed: %s", exc)

    def _embedding_index_worker(self) -> None:
        while True:
            try:
                self._drain_embedding_index_queue(block=True)
            except Exception as exc:  # noqa: BLE001 - background worker must not crash the app
                _LOG.warning("embedding index worker failed: %s", exc)

    def _drain_embedding_index_queue(self, *, block: bool = False) -> int:
        """Index queued nodes; bursts are coalesced so each node is embedded once."""
        try:
            first = self._embedding_index_queue.get(block=block)
        except queue.Empty:
            return 0
        batch = {first}
        while len(batch) < 200:
            try:
                batch.add(self._embedding_index_queue.get_nowait())
            except queue.Empty:
                break
        processed = 0
        for node_id in batch:
            try:
                node = self.repository.get_node(node_id)
                if node:
                    self.memory_embeddings.index_node(node)
                    processed += 1
            except Exception as exc:  # noqa: BLE001 - keep draining on single failures
                _LOG.warning("embedding index failed for %s: %s", node_id, exc)
        return processed

    def _backfill_embeddings_safe(self) -> None:
        try:
            # Drop vectors from a previous provider/model so dim mismatches don't linger.
            info = self.memory_embeddings.provider_info()
            provider = str(info.get("provider") or "")
            model = str(info.get("model") or "")
            with self.repository._connect() as conn:  # noqa: SLF001 - intentional maintenance
                if provider and model:
                    conn.execute(
                        "DELETE FROM memory_embeddings WHERE provider != ? OR model != ?",
                        (provider, model),
                    )
            # Index in chunks so a single restart can eventually cover the full corpus.
            total = 0
            empty_rounds = 0
            for _ in range(40):
                result = self.memory_embeddings.ensure_indexed()
                indexed = int(result.get("indexed") or 0)
                pending = int(result.get("pending") or 0)
                total += indexed
                if indexed <= 0:
                    empty_rounds += 1
                    # Provider down / all attempts failing — stop instead of spinning 40×.
                    if pending > 0 or empty_rounds >= 2:
                        break
                else:
                    empty_rounds = 0
            _LOG.info("embedding backfill done · indexed=%s provider=%s model=%s", total, provider, model)
        except Exception as exc:
            _LOG.warning("embedding backfill failed: %s", exc)

    def _public_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        public = dict(settings)
        mcp_settings = dict(public.get("mcp_servers") or {})
        servers = []
        for server in mcp_settings.get("servers") or []:
            item = dict(server)
            auth = dict(item.get("auth") or {})
            item["auth"] = {
                "status": auth.get("status") or ("authorized" if auth.get("access_token") else ""),
                "expires_at": auth.get("expires_at") or 0,
                "has_access_token": bool(auth.get("access_token")),
                "has_refresh_token": bool(auth.get("refresh_token")),
            } if auth else {}
            servers.append(item)
        if mcp_settings:
            public["mcp_servers"] = {**mcp_settings, "servers": servers}
        return public

    def _sync_router_settings(self) -> None:
        self.provider_router.router_settings = self.repository.get_setting("router") or {}

    # --- AI Router -----------------------------------------------------------

    def router_settings(self) -> dict[str, Any]:
        return {"router": self.repository.get_setting("router") or {}, "profiles": self._router_profiles()}

    def _router_profiles(self) -> dict[str, Any]:
        from .routing import PROVIDER_PROFILES
        return {key: {"cost": value["cost"], "latency": value["latency"], "quality": value["quality"], "strengths": sorted(value.get("strengths") or [])} for key, value in PROVIDER_PROFILES.items()}

    def update_router_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = dict(self.repository.get_setting("router") or {})
        if "strategy" in payload:
            current["strategy"] = str(payload["strategy"])
        if isinstance(payload.get("weights"), dict):
            current["weights"] = {key: float(value) for key, value in payload["weights"].items()}
        if "role_aware" in payload:
            current["role_aware"] = bool(payload["role_aware"])
        self.repository.set_setting("router", current)
        self._sync_router_settings()
        return self.router_settings()

    def routing_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._sync_router_settings()
        query = str(payload.get("message") or payload.get("query") or "").strip()
        role = str(payload.get("role") or "") or (classify_role(query) if query else None)
        provider_id = str(payload.get("provider_id") or "auto")
        policy = RouterPolicy(self.repository.get_setting("router") or {})
        plan = policy.select(self.repository.list_providers(), provider_id, role)
        return {"query": query, "role": role, "decision": plan["decision"], "selected_provider": {"id": plan["provider"].get("id"), "label": plan["provider"].get("label")}}

