"""Provider management and multi-agent council orchestration.

Extracted from ``service.py``. Covers provider CRUD/testing/login and
environment auto-connect, provider selection/run-statistics helpers, and the
council orchestration entry points (``run_council``/``stream_council``). The
tool-augmented single-provider paths (``run_ai``/``stream_ai``) deliberately
remain in ``service.py``. Depends only on leaf modules; the provider router,
repository and council orchestrator are reached through ``self`` via the MRO on
:class:`ArchitectOSService`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from typing import Any

from .models import utc_now
from .rich_response import split_rich_response

_LOG = logging.getLogger("architectos.service")


class ProvidersCouncilServiceMixin:
    """Provider CRUD/selection helpers and multi-agent council orchestration."""

    def providers(self) -> dict[str, Any]:
        return {"mode": "local-memory-first", "providers": self.repository.list_providers()}

    def test_provider(self, provider_id: str) -> dict[str, Any]:
        result = self.provider_router.check(self.repository.list_providers(), provider_id)
        selected = dict(result.get("selected_provider") or {})
        last_check = {
            "ready": bool(result.get("ready")),
            "status": str(result.get("status") or "unknown"),
            "message": str(result.get("message") or ""),
            "checked_at": utc_now(),
        }
        if selected.get("id") and selected.get("id") != "local-memory":
            selected["status"] = "configured" if result.get("ready") else str(result.get("status") or "error")
            selected["last_check"] = last_check
            self.repository.upsert_provider(selected)
        return {
            "provider": {
                "id": result.get("provider_id"),
                "requested_id": result.get("requested_provider_id"),
                "selected": selected,
                "ready": bool(result.get("ready")),
                "status": result.get("status"),
            },
            "message": result.get("message") or "",
            "hint": result.get("hint") or "",
            "actions": list(result.get("actions") or []),
            "details": result.get("details") or {},
            "checked_at": last_check["checked_at"],
        }

    def start_provider_login(self, provider_id: str) -> dict[str, Any]:
        result = self.provider_router.login(self.repository.list_providers(), provider_id)
        return {
            "provider": {
                "id": result.get("provider_id"),
                "requested_id": result.get("requested_provider_id"),
                "selected": result.get("selected_provider") or {},
                "ok": bool(result.get("ok")),
                "status": result.get("status"),
            },
            "message": result.get("message") or "",
            "hint": result.get("hint") or "",
            "details": result.get("details") or {},
        }

    def test_providers(self) -> dict[str, Any]:
        providers = self.repository.list_providers()
        return {"checks": [self.test_provider(provider["id"]) for provider in providers]}

    def connect_env_providers(self) -> dict[str, Any]:
        self._load_project_env_files()
        specs = {
            "openai": {"label": "OpenAI", "api_key_env": "OPENAI_API_KEY", "model": "gpt-4.1-mini"},
            "azure-openai": {"label": "Azure OpenAI", "api_key_env": "AZURE_OPENAI_API_KEY", "endpoint_env": "AZURE_OPENAI_ENDPOINT", "model": "gpt-4.1-mini", "model_env": "AZURE_OPENAI_DEPLOYMENT"},
            "anthropic": {"label": "Anthropic", "api_key_env": "ANTHROPIC_API_KEY", "model": "claude-sonnet-4-20250514"},
            "gemini-cli": {"label": "Antigravity CLI", "api_key_env": "GEMINI_API_KEY", "model": "", "provider_type": "cli"},
        }
        connected: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        for provider_id, spec in specs.items():
            env_name = spec["api_key_env"]
            endpoint_env = spec.get("endpoint_env")
            endpoint = os.environ.get(endpoint_env or "") if endpoint_env else ""
            model = os.environ.get(str(spec.get("model_env") or "")) or spec["model"]
            available = bool(os.environ.get(env_name)) and (not endpoint_env or bool(endpoint))
            if provider_id == "gemini-cli" and not available:
                from .adapters import _gemini_session_auth_state
                gemini_auth = _gemini_session_auth_state()
                available = bool(gemini_auth.get("ready"))
            payload = {
                "label": spec["label"],
                "provider_type": spec.get("provider_type") or "api",
                "api_key_env": env_name if provider_id != "gemini-cli" else "",
                "model": model,
                "enabled": available,
                "status": "configured" if available else "missing_credentials",
                "notes": (
                    "Uses Antigravity CLI (agy) with Google account sign-in. Gemini CLI is deprecated."
                    if provider_id == "gemini-cli"
                    else f"Uses {env_name} from process environment or .env.local."
                ),
                "command": "agy -p" if provider_id == "gemini-cli" else None,
            }
            payload = {key: value for key, value in payload.items() if value is not None}
            if endpoint:
                payload["base_url"] = endpoint
            provider = self.update_provider(provider_id, payload)
            check = self.test_provider(provider_id)
            item = {
                "id": provider_id,
                "label": spec["label"],
                "api_key_env": env_name,
                "ready": bool(check["provider"]["ready"]),
                "status": check["provider"]["status"],
                "message": check["message"],
            }
            if available:
                connected.append(item)
            else:
                missing.append(item)
        return {
            "connected": connected,
            "missing": missing,
            "message": f"Connected {len(connected)} API provider(s). Missing: {', '.join(item['api_key_env'] for item in missing) or 'none'}.",
        }

    def provider_models(self, provider_id: str) -> dict[str, Any]:
        result = self.provider_router.list_models(self.repository.list_providers(), provider_id)
        selected = dict(result.get("selected_provider") or {})
        if selected.get("id") == provider_id:
            selected["available_models"] = list(result.get("models") or [])
            selected["models_checked_at"] = utc_now()
            if result.get("ready") and result.get("models") and not selected.get("model"):
                selected["model"] = str(result["models"][0]["name"])
            if result.get("ready"):
                selected["status"] = "configured" if selected.get("enabled") else "available"
            else:
                selected["status"] = str(result.get("status") or "unreachable")
            self.repository.upsert_provider(selected)
        return {
            "provider": {
                "id": result.get("provider_id"),
                "requested_id": result.get("requested_provider_id"),
                "selected": selected,
                "ready": bool(result.get("ready")),
                "status": result.get("status"),
            },
            "models": list(result.get("models") or []),
            "message": result.get("message") or "",
            "hint": result.get("hint") or "",
            "base_url": result.get("base_url") or selected.get("base_url") or "",
        }

    def update_provider(self, provider_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        provider = next((item for item in self.repository.list_providers() if item["id"] == provider_id), {"id": provider_id, "label": provider_id, "provider_type": "custom", "status": "planned", "enabled": False, "model": "", "notes": ""})
        for key in ("label", "provider_type", "status", "model", "notes", "command", "base_url", "api_key_env", "workdir_policy", "workdir"):
            if key in payload:
                provider[key] = str(payload[key])
        if "timeout_seconds" in payload:
            provider["timeout_seconds"] = max(1, int(payload["timeout_seconds"] or 120))
        if "approval_required" in payload:
            provider["approval_required"] = bool(payload["approval_required"])
        if "enabled" in payload:
            provider["enabled"] = bool(payload["enabled"])
            provider["status"] = "configured" if provider["enabled"] else provider["status"]
        return self.repository.upsert_provider(provider)

    # --- Multi-Agent ---------------------------------------------------------

    def council_config(self) -> dict[str, Any]:
        return self.council.config()

    def _agent_selectable_providers(self, providers: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        items = providers if providers is not None else self.repository.list_providers()
        selectable: list[dict[str, Any]] = [{
            "id": "auto",
            "label": "Auto (router)",
            "ready": True,
            "enabled": True,
            "status": "configured",
        }]
        for provider in items:
            provider_id = str(provider.get("id") or "")
            if not provider_id or provider_id == "local-memory":
                continue
            ready = self._provider_is_selectable(provider)
            selectable.append({
                "id": provider_id,
                "label": str(provider.get("label") or provider_id),
                "ready": ready,
                "enabled": bool(provider.get("enabled")),
                "status": str(provider.get("status") or ""),
            })
        return selectable

    def _provider_is_selectable(self, provider: dict[str, Any]) -> bool:
        if not provider or not provider.get("enabled"):
            return False
        last = provider.get("last_check") or {}
        if last.get("ready"):
            return True
        status = str(provider.get("status") or "").lower()
        return status in {"configured", "ok", "available", "ready"}

    def _provider_run_stats(self, limit: int = 240, project_id: str | None = None) -> dict[str, Any]:
        runs = self.repository.list_provider_runs(project_id, limit=max(20, min(int(limit or 240), 500)))
        by_provider: dict[str, dict[str, Any]] = {}
        by_role: dict[str, dict[str, dict[str, Any]]] = {}
        by_model: dict[str, dict[str, Any]] = {}
        totals = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "runs_with_usage": 0,
            "runs_with_cost": 0,
        }
        for run in runs:
            provider_id = str(run.get("provider_id") or "").strip()
            if not provider_id:
                continue
            status = str(run.get("status") or "").lower()
            ok = status in {"ok", "success", "completed", "done"}
            bucket = by_provider.setdefault(provider_id, {"total": 0, "ok": 0, "fail": 0})
            bucket["total"] += 1
            if ok:
                bucket["ok"] += 1
            else:
                bucket["fail"] += 1
            role = ""
            provider_meta = run.get("provider") if isinstance(run.get("provider"), dict) else {}
            selected = run.get("selected_provider") if isinstance(run.get("selected_provider"), dict) else {}
            routing = run.get("routing") if isinstance(run.get("routing"), dict) else {}
            if not routing and isinstance(provider_meta.get("routing"), dict):
                routing = provider_meta.get("routing") or {}
            if not routing and isinstance(selected.get("routing"), dict):
                routing = selected.get("routing") or {}
            role = str(routing.get("role") or run.get("role") or "").strip().lower()
            if role:
                role_bucket = by_role.setdefault(role, {})
                stats = role_bucket.setdefault(provider_id, {"total": 0, "ok": 0, "fail": 0})
                stats["total"] += 1
                if ok:
                    stats["ok"] += 1
                else:
                    stats["fail"] += 1

            usage = run.get("usage") if isinstance(run.get("usage"), dict) else {}
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or (prompt + completion))
            cost_raw = usage.get("cost_usd")
            if cost_raw is None:
                cost_raw = run.get("cost_usd")
            try:
                cost = float(cost_raw) if cost_raw is not None else None
            except (TypeError, ValueError):
                cost = None
            if prompt or completion or total_tokens:
                totals["prompt_tokens"] += prompt
                totals["completion_tokens"] += completion
                totals["total_tokens"] += total_tokens
                totals["runs_with_usage"] += 1
                if cost is not None:
                    totals["cost_usd"] += cost
                    totals["runs_with_cost"] += 1
                model = str(run.get("model") or selected.get("model") or usage.get("model") or "").strip() or "unknown"
                key = f"{provider_id}::{model}"
                model_bucket = by_model.setdefault(key, {
                    "provider_id": provider_id,
                    "model": model,
                    "runs": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                    "has_cost": False,
                })
                model_bucket["runs"] += 1
                model_bucket["prompt_tokens"] += prompt
                model_bucket["completion_tokens"] += completion
                model_bucket["total_tokens"] += total_tokens
                if cost is not None:
                    model_bucket["cost_usd"] += cost
                    model_bucket["has_cost"] = True

        for bucket in by_provider.values():
            total = max(1, int(bucket["total"]))
            bucket["success_rate"] = round(int(bucket["ok"]) / total, 3)
        for role_map in by_role.values():
            for bucket in role_map.values():
                total = max(1, int(bucket["total"]))
                bucket["success_rate"] = round(int(bucket["ok"]) / total, 3)
        model_rows = sorted(by_model.values(), key=lambda item: (-int(item["total_tokens"]), -int(item["runs"])))
        for row in model_rows:
            row["cost_usd"] = round(float(row["cost_usd"]), 6) if row.get("has_cost") else None
            row.pop("has_cost", None)
        totals["cost_usd"] = round(float(totals["cost_usd"]), 6)
        return {
            "runs_sampled": len(runs),
            "by_provider": by_provider,
            "by_role": by_role,
            "usage": {
                "totals": totals,
                "by_model": model_rows,
            },
        }

    def run_council(self, payload: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for event in self.stream_council(payload):
            if event.get("type") == "done":
                result = event.get("result") or {}
        return result

    def stream_council(self, payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
        project_id = str(payload.get("project_id") or "architectos")
        message = str(payload.get("message") or payload.get("query") or "").strip()
        chat_id = str(payload.get("chat_id") or "").strip()

        chat = self._activate_chat_session(self._chat_for_agent_turn(project_id, chat_id, message))
        chat["messages"].append({"role": "user", "text": message, "created_at": utc_now()})
        chat = self.repository.upsert_chat(chat)

        activity_trace_by_role: dict[str, dict[str, Any]] = {}
        for event in self.council.stream(payload):
            if event.get("type") == "progress":
                agent = event.get("agent") if isinstance(event.get("agent"), dict) else {}
                role = str(agent.get("role") or "")
                if role:
                    done = str(agent.get("status") or "").lower() == "done"
                    activity_trace_by_role[role] = {
                        "kind": "agent",
                        "step_id": f"council:{role}",
                        "name": role,
                        "role": role,
                        "role_name": str(agent.get("role_name") or role),
                        "ok": done,
                        "pending": not done,
                        "summary": "Finished" if done else "Running",
                        "provider": agent.get("provider") or {},
                    }
            if event.get("type") == "done":
                result = event.get("result") or {}
                assistant_text = self._council_assistant_text(result)
                display_text, structured = split_rich_response(assistant_text)
                chat["messages"].append({
                    "role": "assistant",
                    "text": display_text or assistant_text,
                    "raw_text": assistant_text,
                    "structured": structured,
                    "created_at": utc_now(),
                    "context": "",
                    "provider": {"id": "council", "status": "ok"},
                    "tool_trace": list(activity_trace_by_role.values()),
                    "tool_trace_label": "Council",
                })
                chat["last_activity_at"] = utc_now()
                chat = self.repository.upsert_chat(chat)
                event["result"]["chat"] = chat
                event["result"]["structured"] = structured
                event["result"]["response"] = display_text or assistant_text
            yield event
