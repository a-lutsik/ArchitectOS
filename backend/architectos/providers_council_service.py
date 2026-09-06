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

from .ask_advice import ask_memory_advice_enabled, merge_advice_into_structured
from .models import utc_now
from .rich_response import split_rich_response
from .routing import ERROR_STATUSES, provider_is_ready

_LOG = logging.getLogger("architectos.service")

_PROVIDER_CREDENTIAL_SPECS: dict[str, dict[str, str]] = {
    "openai": {"api_key_env": "OPENAI_API_KEY", "base_url_env": "OPENAI_BASE_URL"},
    "azure-openai": {"api_key_env": "AZURE_OPENAI_API_KEY", "base_url_env": "AZURE_OPENAI_ENDPOINT", "model_env": "AZURE_OPENAI_DEPLOYMENT"},
    "anthropic": {"api_key_env": "ANTHROPIC_API_KEY"},
    "openrouter": {"api_key_env": "OPENROUTER_API_KEY"},
    "ollama": {"base_url_env": "OLLAMA_HOST"},
    "gemini-cli": {"api_key_env": "GEMINI_API_KEY"},
}


class ProvidersCouncilServiceMixin:
    """Provider CRUD/selection helpers and multi-agent council orchestration."""

    def providers(self) -> dict[str, Any]:
        return {
            "mode": "local-memory-first",
            "providers": [self._annotate_provider_credentials(item) for item in self.repository.list_providers()],
        }

    def _provider_credential_fields(self, provider: dict[str, Any]) -> dict[str, Any]:
        provider_id = str(provider.get("id") or "")
        spec = _PROVIDER_CREDENTIAL_SPECS.get(provider_id, {})
        api_key_env = str(provider.get("api_key_env") or spec.get("api_key_env") or "").strip()
        base_url_env = str(spec.get("base_url_env") or "").strip()
        model_env = str(spec.get("model_env") or "").strip()
        key_set = bool(api_key_env and str(os.environ.get(api_key_env) or "").strip())
        if provider_id == "gemini-cli" and not key_set:
            from .adapters import _gemini_session_auth_state
            key_set = bool(_gemini_session_auth_state().get("ready"))
        base_url = str(provider.get("base_url") or (os.environ.get(base_url_env) if base_url_env else "") or "").strip()
        if provider_id == "ollama" and not base_url:
            base_url = str(os.environ.get("OLLAMA_BASE_URL") or os.environ.get("OLLAMA_HOST") or "").strip()
        model = str(provider.get("model") or (os.environ.get(model_env) if model_env else "") or "").strip()
        accepts_api_key = bool(api_key_env) and provider_id in {"openai", "azure-openai", "anthropic", "openrouter", "gemini-cli"}
        accepts_base_url = provider_id in {"openai", "azure-openai", "openrouter", "ollama"}
        accepts_model = provider_id == "azure-openai"
        if provider_id == "azure-openai":
            ready = key_set and bool(base_url)
        elif provider_id == "ollama":
            ready = True
        elif accepts_api_key:
            ready = key_set
        else:
            ready = bool(provider.get("status") == "configured")
        return {
            "api_key_env": api_key_env,
            "api_key_set": key_set if accepts_api_key else False,
            "base_url_env": base_url_env,
            "base_url": base_url,
            "model_env": model_env,
            "model": model,
            "accepts_api_key": accepts_api_key,
            "accepts_base_url": accepts_base_url,
            "accepts_model": accepts_model,
            "ready": ready,
        }

    def _annotate_provider_credentials(self, provider: dict[str, Any]) -> dict[str, Any]:
        item = dict(provider)
        fields = self._provider_credential_fields(item)
        item["credentials_set"] = bool(fields["api_key_set"] if fields["accepts_api_key"] else fields["ready"])
        item["credentials"] = fields
        return item

    def _apply_provider_secrets(self, provider_id: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        payload = dict(payload or {})
        provider = next(
            (item for item in self.repository.list_providers() if item["id"] == provider_id),
            {"id": provider_id, "label": provider_id, "provider_type": "custom", "status": "planned", "enabled": False, "model": "", "notes": ""},
        )
        fields = self._provider_credential_fields(provider)
        updates: dict[str, str] = {}
        api_key = str(payload.get("api_key") or "").strip()
        if api_key and fields["api_key_env"]:
            updates[fields["api_key_env"]] = api_key
        base_url = str(payload.get("base_url") or payload.get("endpoint") or "").strip()
        if base_url:
            if fields["base_url_env"]:
                updates[fields["base_url_env"]] = base_url
            if provider_id == "ollama":
                updates["OLLAMA_BASE_URL"] = base_url
                updates.setdefault("OLLAMA_HOST", base_url)
            provider["base_url"] = base_url
        model = str(payload.get("model") or payload.get("deployment") or "").strip()
        if model:
            if fields["model_env"]:
                updates[fields["model_env"]] = model
            provider["model"] = model
        if updates:
            self._upsert_env_local(updates)
        fields = self._provider_credential_fields(provider)
        if fields["ready"]:
            provider["enabled"] = True
            provider["status"] = "configured"
            if fields["api_key_env"]:
                provider["notes"] = f"Uses {fields['api_key_env']} from Setup → Providers or .env.local."
        elif fields["api_key_set"] or api_key:
            provider["enabled"] = True
            provider["status"] = "missing_model" if provider_id == "azure-openai" else "missing_credentials"
        return self.repository.upsert_provider(provider)

    def save_provider_credentials(self, provider_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(payload or {})
        provider = self._apply_provider_secrets(provider_id, payload)
        annotated = self._annotate_provider_credentials(provider)
        result: dict[str, Any] = {
            "provider": annotated,
            "credentials_set": bool(annotated.get("credentials_set")),
            "message": "Provider credentials saved.",
        }
        if payload.get("test", True):
            check = self.test_provider(provider_id)
            result["check"] = check
            result["message"] = str(check.get("message") or result["message"])
        return result

    def test_provider(self, provider_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if payload and any(str(payload.get(key) or "").strip() for key in ("api_key", "base_url", "endpoint", "model", "deployment")):
            self._apply_provider_secrets(provider_id, payload)
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
            "azure-openai": {"label": "Azure OpenAI", "api_key_env": "AZURE_OPENAI_API_KEY", "endpoint_env": "AZURE_OPENAI_ENDPOINT", "model": "", "model_env": "AZURE_OPENAI_DEPLOYMENT"},
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
                    "Antigravity: Google sign-in once, then headless `agy -p`. Do not keep the agy TUI running."
                    if provider_id == "gemini-cli"
                    else f"Uses {env_name} from Setup → Providers or .env.local."
                ),
                "command": "agy -p" if provider_id == "gemini-cli" else None,
            }
            payload = {key: value for key, value in payload.items() if value is not None}
            if endpoint:
                payload["base_url"] = endpoint
            self.update_provider(provider_id, payload)
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
        if "allow_local" in payload:
            # Opt-in for OpenAI-compatible servers on loopback/LAN (LM Studio,
            # llama.cpp, LiteLLM) — see netutil.validate_outbound_url.
            provider["allow_local"] = bool(payload["allow_local"])
        if "enabled" in payload:
            provider["enabled"] = bool(payload["enabled"])
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
        return provider_is_ready(provider)

    def remember_provider_runtime_status(self, result: dict[str, Any]) -> None:
        """Persist last_check after Ask so a 404 cannot stay green Ready."""
        selected = result.get("selected_provider") if isinstance(result.get("selected_provider"), dict) else {}
        provider_id = str(result.get("provider_id") or selected.get("id") or "").strip()
        if not provider_id or provider_id == "local-memory":
            return
        status = str(result.get("status") or "").lower()
        if status in {"approval_required"}:
            return
        if status not in {"error", "ok", "fallback"}:
            return
        provider = self._lookup_provider(provider_id)
        if not provider:
            return
        text = str(result.get("text") or result.get("message") or "").strip()
        if status == "ok":
            check_status = "ok"
            ready = True
            message = str((provider.get("last_check") or {}).get("message") or "") or f"{provider.get('label') or provider_id} responded."
        else:
            check_status = self._runtime_check_status(text)
            ready = False
            message = text[:500] or f"{provider_id} failed."
        provider["status"] = "configured" if ready else check_status
        provider["last_check"] = {
            "ready": ready,
            "status": check_status,
            "message": message,
            "checked_at": utc_now(),
        }
        self.repository.upsert_provider(provider)

    def _runtime_check_status(self, text: str) -> str:
        lowered = (text or "").lower()
        if "is not set" in lowered or "missing_credentials" in lowered:
            return "missing_credentials"
        if "deployment was not found" in lowered or "deploymentnotfound" in lowered or "deployment name is not set" in lowered:
            return "missing_model"
        if "unreachable" in lowered or "timed out" in lowered:
            return "unreachable"
        for token in ERROR_STATUSES:
            if token in lowered or token.replace("_", " ") in lowered:
                return token
        return "error"

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
        economy_runs = 0
        saved_pct_sum = 0.0
        saved_tokens_sum = 0
        corpus_tokens_sum = 0
        packed_tokens_sum = 0
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

            economy = run.get("token_economy") if isinstance(run.get("token_economy"), dict) else None
            if economy and (economy.get("corpus_tokens") or economy.get("packed_tokens")):
                economy_runs += 1
                try:
                    saved_pct_sum += float(economy.get("saved_pct") or 0)
                except (TypeError, ValueError):
                    pass
                saved_tokens_sum += int(economy.get("saved_tokens") or 0)
                corpus_tokens_sum += int(economy.get("corpus_tokens") or 0)
                packed_tokens_sum += int(economy.get("packed_tokens") or 0)

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
        memory_token_economy = {
            "runs": economy_runs,
            "avg_saved_pct": round(saved_pct_sum / economy_runs, 1) if economy_runs else 0.0,
            "saved_tokens": saved_tokens_sum,
            "corpus_tokens": corpus_tokens_sum,
            "packed_tokens": packed_tokens_sum,
        }
        return {
            "runs_sampled": len(runs),
            "by_provider": by_provider,
            "by_role": by_role,
            "usage": {
                "totals": totals,
                "by_model": model_rows,
                "memory_token_economy": memory_token_economy,
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
        raw_message = str(payload.get("message") or payload.get("query") or "").strip()
        message_security = self.security_policy.inspect_text(raw_message)
        message = str(message_security["text"])
        chat_id = str(payload.get("chat_id") or "").strip()
        payload = {**payload, "message": message, "query": message}

        chat = self._activate_chat_session(self._chat_for_agent_turn(project_id, chat_id, message))
        user_message: dict[str, Any] = {"role": "user", "text": message, "created_at": utc_now()}
        if message_security["redacted"]:
            user_message["security"] = {"redacted": True, "findings": message_security["findings"]}
        chat["messages"].append(user_message)
        chat = self.repository.upsert_chat(chat)

        # One advice pass on the original user query; panel members reuse it.
        advice: list[dict[str, Any]] = []
        if ask_memory_advice_enabled(dict(self.repository.get_setting("ui") or {})):
            try:
                from .ask_advice import collect_advice_candidates
                pack = self.context(message, project_id=project_id, limit=int(payload.get("limit") or 6), tools_available=False)
                candidates = collect_advice_candidates(
                    pack.get("hits") if isinstance(pack.get("hits"), list) else [],
                    pack.get("stable_hits") if isinstance(pack.get("stable_hits"), list) else [],
                    pack.get("rule_layer_hits") if isinstance(pack.get("rule_layer_hits"), list) else [],
                )
                judge = getattr(self, "_run_ask_advice_judge", None)
                if candidates and callable(judge):
                    advice = judge(message, candidates, project_id=project_id, provider_id=None)
            except Exception as exc:  # noqa: BLE001 - advice is best-effort
                _LOG.warning("council memory advice skipped: %s", exc)
        council_payload = {**payload, "advice": advice}

        activity_trace_by_role: dict[str, dict[str, Any]] = {}
        for event in self.council.stream(council_payload):
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
                result_security = self.security_policy.inspect_text(assistant_text)
                assistant_text = str(result_security["text"])
                display_text, structured = split_rich_response(assistant_text)
                structured = merge_advice_into_structured(structured, advice)
                security = {
                    "redacted": bool(message_security["redacted"] or result_security["redacted"]),
                    "message_redacted": bool(message_security["redacted"]),
                    "result_redacted": bool(result_security["redacted"]),
                    "message_findings": message_security["findings"] if message_security["redacted"] else [],
                }
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
                    "security": security,
                })
                chat["last_activity_at"] = utc_now()
                chat = self.repository.upsert_chat(chat)
                result["chat"] = chat
                result["structured"] = structured
                result["response"] = display_text or assistant_text
                result["security"] = security
                result["advice"] = advice
                event["result"] = result
            yield event
