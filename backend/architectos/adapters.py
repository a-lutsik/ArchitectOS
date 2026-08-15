from __future__ import annotations

import base64
import json
import os
import platform
import queue
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .netutil import outbound_policy, validate_outbound_url
from .routing import RouterPolicy, classify_role
from .ssl_util import urlopen
from .usage import enrich_usage, normalize_usage


def _provider_urlopen(provider: dict[str, Any], req: urllib.request.Request, *, timeout: float | int):
    """urlopen with redirect policy matching the provider's allow_local setting.

    Endpoints are already SSRF-checked when built; ``validate_initial=False``
    skips a second DNS lookup on the first hop. Redirect hops are still
    re-validated. Extra kwargs are avoided so test doubles that patch
    ``urlopen(req, timeout=...)`` keep working.
    """
    allow_local = _allow_local_urls(provider)
    with outbound_policy(allow_local=allow_local, validate_initial=False):
        return urlopen(req, timeout=timeout)


def _result_with_usage(result: dict[str, Any], *, model: str | None = None, usage_payload: Any = None) -> dict[str, Any]:
    """Attach normalized usage (+ estimated cost) onto an adapter result."""
    out = dict(result)
    usage = None
    if usage_payload is not None:
        usage = normalize_usage(usage_payload)
    if usage is None and isinstance(out.get("usage"), dict):
        usage = normalize_usage(out.get("usage"))
    if usage is None and isinstance(out.get("raw"), dict):
        usage = normalize_usage(out.get("raw"))
    enriched = enrich_usage(usage, model=model)
    if enriched:
        out["usage"] = enriched
    return out


@dataclass(slots=True)
class ProviderRequest:
    message: str
    context: str
    project_id: str
    run_id: str = ""
    approved: bool = False
    role: str = ""
    cancel_requested: Callable[[], bool] | None = None
    images: list[dict[str, Any]] = field(default_factory=list)
    # Tool schemas ({name, description, parameters}) for providers with native tool calling.
    tools: list[dict[str, Any]] = field(default_factory=list)


def _image_data_url(image: dict[str, Any]) -> str:
    return f"data:{image.get('mime') or 'image/png'};base64,{image.get('b64') or ''}"


class ProviderAdapter:
    provider_id = "base"

    def run(self, provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> dict[str, Any]:
        raise NotImplementedError

    def check(self, provider: dict[str, Any], project_root: Path) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "ready": False,
            "status": "unknown",
            "message": "No provider readiness check is implemented.",
            "hint": "Use local-memory or add a concrete adapter before enabling this provider.",
            "actions": ["Select another provider", "Implement an adapter for this provider id"],
            "details": {},
        }

    def stream(self, provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> Iterator[dict[str, Any]]:
        result = self.run(provider, request, project_root)
        for chunk in _chunk_text(str(result.get("text") or "")):
            yield {"type": "delta", "text": chunk}
        yield {"type": "done", "result": result}

    def list_models(self, provider: dict[str, Any], project_root: Path) -> dict[str, Any]:
        return {"provider_id": self.provider_id, "ready": False, "status": "unsupported", "models": [], "message": "Model discovery is not supported by this provider."}

    def build_prompt(self, request: ProviderRequest) -> str:
        return (
            "You are ArchitectOS, a local-first AI workspace for software engineers.\n"
            "Work autonomously. Use the provided project context; when it is not enough and tools are listed, "
            "read the project files and memory nodes yourself instead of asking the user for permission.\n"
            "Never ask to confirm read-only lookups and never defer work to a later turn — do it now, then answer. "
            "Ask a question only when the user must make a product decision you cannot make.\n"
            "If no tools are available and context is missing, say briefly which files or sources are needed.\n"
            "Describe your actions in plain language (\"reading PrimaryEnergyChartRefBuilder.java\"); "
            "never mention internal tool names in the answer.\n\n"
            f"{request.context}\n\n"
            f"User request:\n{request.message}\n"
        )


class LocalMemoryAdapter(ProviderAdapter):
    provider_id = "local-memory"

    def check(self, provider: dict[str, Any], project_root: Path) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "ready": True,
            "status": "ok",
            "message": "Local memory fallback is always available.",
            "hint": "Use this provider when external tools or API keys are not configured yet.",
            "actions": ["Scan project files", "Add durable memory", "Ask with project context"],
            "details": {"storage": str(project_root / "data" / "architectos.db")},
        }

    def run(self, provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> dict[str, Any]:
        lines = [line for line in request.context.splitlines() if line.startswith("- [")]
        labels = []
        for line in lines[:4]:
            head = line.split(":", 1)[0]
            labels.append(head.replace("- ", ""))
        if labels:
            text = (
                f"Local context is ready for: {request.message}. "
                f"Strongest memory matches: {', '.join(labels)}. "
                "Use the Context tab for the full pack."
            )
        else:
            text = "Local context is ready, but I found no matching memory yet. Scan the project or add memory, then ask again."
        return {"provider_id": self.provider_id, "status": "ok", "text": text, "raw": None}


class OpenAIResponsesAdapter(ProviderAdapter):
    provider_id = "openai"
    service_name = "OpenAI"
    default_api_key_env = "OPENAI_API_KEY"
    default_model = "gpt-4.1-mini"
    default_endpoint = "https://api.openai.com/v1/responses"

    def _openai_input(self, request: ProviderRequest) -> Any:
        prompt = self.build_prompt(request)
        if not request.images:
            return prompt
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        for image in request.images:
            content.append({"type": "input_image", "image_url": _image_data_url(image)})
        return [{"role": "user", "content": content}]

    def _api_key_env(self, provider: dict[str, Any]) -> str:
        return str(provider.get("api_key_env") or self.default_api_key_env)

    def _model(self, provider: dict[str, Any]) -> str:
        return str(provider.get("model") or self.default_model)

    def _responses_endpoint(self, provider: dict[str, Any]) -> str:
        endpoint = str(provider.get("base_url") or self.default_endpoint).rstrip("/")
        return validate_outbound_url(endpoint, allow_local=_allow_local_urls(provider))

    def _request_headers(self, api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def _http_error_text(self, code: int, body: str) -> str:
        return f"{self.service_name} HTTP {code}: {body[:500]}"

    def check(self, provider: dict[str, Any], project_root: Path) -> dict[str, Any]:
        api_key_env = self._api_key_env(provider)
        model = self._model(provider)
        if not os.environ.get(api_key_env):
            return {
                "provider_id": self.provider_id,
                "ready": False,
                "status": "missing_credentials",
                "message": f"{api_key_env} is not set.",
                "hint": f"Set {api_key_env} in the environment before starting ArchitectOS.",
                "actions": [f"Set {api_key_env}", "Restart ArchitectOS", "Run provider test again"],
                "details": {"api_key_env": api_key_env, "model": model},
            }
        return {
            "provider_id": self.provider_id,
            "ready": True,
            "status": "configured",
            "message": f"{api_key_env} is available for model {model}.",
            "hint": "Credentials are present. Use chat or /api/ai/run to verify model-level access.",
            "actions": ["Send a short chat prompt", "Check model name if execution fails"],
            "details": {"api_key_env": api_key_env, "model": model, "base_url": self._responses_endpoint(provider)},
        }

    def run(self, provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> dict[str, Any]:
        api_key_env = self._api_key_env(provider)
        api_key = os.environ.get(api_key_env)
        if not api_key:
            return {"provider_id": self.provider_id, "status": "error", "text": f"{api_key_env} is not set.", "raw": None}
        payload = {
            "model": self._model(provider),
            "input": self._openai_input(request),
        }
        tools = _openai_tools_payload(request)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        endpoint = self._responses_endpoint(provider)
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._request_headers(api_key),
            method="POST",
        )
        try:
            with _provider_urlopen(provider, req, timeout=int(provider.get("timeout_seconds") or 120)) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return {"provider_id": self.provider_id, "status": "error", "text": self._http_error_text(exc.code, body), "raw": body}
        except OSError as exc:
            return {"provider_id": self.provider_id, "status": "error", "text": f"{self.service_name} request failed: {exc}", "raw": None}
        text = _merge_native_tool_calls(_extract_openai_text(data), _extract_openai_tool_calls(data))
        return _result_with_usage(
            {"provider_id": self.provider_id, "status": "ok", "text": text, "raw": data},
            model=self._model(provider),
            usage_payload=data,
        )

    def stream(self, provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> Iterator[dict[str, Any]]:
        api_key_env = self._api_key_env(provider)
        api_key = os.environ.get(api_key_env)
        if not api_key:
            result = {"provider_id": self.provider_id, "status": "error", "text": f"{api_key_env} is not set.", "raw": None}
            yield {"type": "delta", "text": result["text"]}
            yield {"type": "done", "result": result}
            return
        model = self._model(provider)
        payload = {
            "model": model,
            "input": self._openai_input(request),
            "stream": True,
        }
        endpoint = self._responses_endpoint(provider)
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._request_headers(api_key),
            method="POST",
        )
        chunks: list[str] = []
        usage_payload: Any = None
        try:
            with _provider_urlopen(provider, req, timeout=int(provider.get("timeout_seconds") or 120)) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if normalize_usage(event):
                        usage_payload = event
                    text = _extract_openai_stream_delta(event)
                    if text:
                        chunks.append(text)
                        yield {"type": "delta", "text": text}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            result = {"provider_id": self.provider_id, "status": "error", "text": self._http_error_text(exc.code, body), "raw": body}
            yield {"type": "delta", "text": result["text"]}
            yield {"type": "done", "result": result}
            return
        except OSError as exc:
            result = {"provider_id": self.provider_id, "status": "error", "text": f"{self.service_name} request failed: {exc}", "raw": None}
            yield {"type": "delta", "text": result["text"]}
            yield {"type": "done", "result": result}
            return
        yield {
            "type": "done",
            "result": _result_with_usage(
                {"provider_id": self.provider_id, "status": "ok", "text": "".join(chunks), "raw": None},
                model=model,
                usage_payload=usage_payload,
            ),
        }


class AzureOpenAIResponsesAdapter(OpenAIResponsesAdapter):
    provider_id = "azure-openai"
    service_name = "Azure OpenAI"
    default_api_key_env = "AZURE_OPENAI_API_KEY"
    default_model = "gpt-4.1-mini"
    default_endpoint = ""

    def _responses_endpoint(self, provider: dict[str, Any]) -> str:
        base_url = str(provider.get("base_url") or os.environ.get("AZURE_OPENAI_ENDPOINT") or "").rstrip("/")
        if not base_url:
            return ""
        validate_outbound_url(base_url, allow_local=_allow_local_urls(provider))
        if base_url.endswith("/responses"):
            return base_url
        return f"{base_url}/responses"

    def _request_headers(self, api_key: str) -> dict[str, str]:
        return {"api-key": api_key, "Content-Type": "application/json"}

    def _http_error_text(self, code: int, body: str) -> str:
        if "DeploymentNotFound" in body:
            return (
                f"Azure OpenAI HTTP {code}: deployment was not found. "
                "Set the provider Model field to the Azure deployment name, or set AZURE_OPENAI_DEPLOYMENT in .env, then Connect Env again."
            )
        return super()._http_error_text(code, body)

    def check(self, provider: dict[str, Any], project_root: Path) -> dict[str, Any]:
        api_key_env = self._api_key_env(provider)
        model = self._model(provider)
        endpoint = self._responses_endpoint(provider)
        if not os.environ.get(api_key_env):
            return {
                "provider_id": self.provider_id,
                "ready": False,
                "status": "missing_credentials",
                "message": f"{api_key_env} is not set.",
                "hint": f"Set {api_key_env} in the environment before starting ArchitectOS.",
                "actions": [f"Set {api_key_env}", "Restart ArchitectOS", "Run provider test again"],
                "details": {"api_key_env": api_key_env, "model": model, "base_url": endpoint},
            }
        if not endpoint:
            return {
                "provider_id": self.provider_id,
                "ready": False,
                "status": "missing_endpoint",
                "message": "AZURE_OPENAI_ENDPOINT is not set.",
                "hint": "Set AZURE_OPENAI_ENDPOINT to your Azure OpenAI /openai/v1 endpoint.",
                "actions": ["Set AZURE_OPENAI_ENDPOINT", "Restart ArchitectOS", "Run provider test again"],
                "details": {"api_key_env": api_key_env, "model": model, "base_url": endpoint},
            }
        return {
            "provider_id": self.provider_id,
            "ready": True,
            "status": "configured",
            "message": f"{api_key_env} and AZURE_OPENAI_ENDPOINT are available for model/deployment {model}.",
            "hint": "Credentials are present. If execution fails, set Model to the Azure deployment name.",
            "actions": ["Send a short chat prompt", "Check Azure deployment/model name if execution fails"],
            "details": {"api_key_env": api_key_env, "model": model, "base_url": endpoint},
        }


class AnthropicMessagesAdapter(ProviderAdapter):
    provider_id = "anthropic"

    def _messages(self, request: ProviderRequest) -> list[dict[str, Any]]:
        prompt = self.build_prompt(request)
        if not request.images:
            return [{"role": "user", "content": prompt}]
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in request.images:
            content.append({"type": "image", "source": {"type": "base64", "media_type": image.get("mime") or "image/png", "data": image.get("b64") or ""}})
        return [{"role": "user", "content": content}]

    def check(self, provider: dict[str, Any], project_root: Path) -> dict[str, Any]:
        api_key_env = str(provider.get("api_key_env") or "ANTHROPIC_API_KEY")
        model = str(provider.get("model") or "claude-sonnet-4-20250514")
        if not os.environ.get(api_key_env):
            return {
                "provider_id": self.provider_id,
                "ready": False,
                "status": "missing_credentials",
                "message": f"{api_key_env} is not set.",
                "hint": f"Set {api_key_env} before starting ArchitectOS.",
                "actions": [f"Set {api_key_env}", "Restart ArchitectOS", "Run provider test again"],
                "details": {"api_key_env": api_key_env, "model": model},
            }
        return {
            "provider_id": self.provider_id,
            "ready": True,
            "status": "configured",
            "message": f"{api_key_env} is available for model {model}.",
            "hint": "Credentials are present. Send a short chat prompt to verify model access.",
            "actions": ["Send a short chat prompt", "Check model name if execution fails"],
            "details": {"api_key_env": api_key_env, "model": model, "base_url": str(provider.get("base_url") or "https://api.anthropic.com")},
        }

    def run(self, provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> dict[str, Any]:
        api_key_env = str(provider.get("api_key_env") or "ANTHROPIC_API_KEY")
        api_key = os.environ.get(api_key_env)
        if not api_key:
            return {"provider_id": self.provider_id, "status": "error", "text": f"{api_key_env} is not set.", "raw": None}
        payload = {
            "model": provider.get("model") or "claude-sonnet-4-20250514",
            "max_tokens": int(provider.get("max_tokens") or 2048),
            "messages": self._messages(request),
        }
        endpoint = _anthropic_messages_endpoint(provider)
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "x-api-key": api_key,
                "anthropic-version": str(provider.get("anthropic_version") or "2023-06-01"),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with _provider_urlopen(provider, req, timeout=int(provider.get("timeout_seconds") or 120)) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return {"provider_id": self.provider_id, "status": "error", "text": f"Anthropic HTTP {exc.code}: {body[:500]}", "raw": body}
        except OSError as exc:
            return {"provider_id": self.provider_id, "status": "error", "text": f"Anthropic request failed: {exc}", "raw": None}
        model = str(provider.get("model") or "claude-sonnet-4-20250514")
        return _result_with_usage(
            {"provider_id": self.provider_id, "status": "ok", "text": _extract_anthropic_text(data), "raw": data},
            model=model,
            usage_payload=data,
        )

    def stream(self, provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> Iterator[dict[str, Any]]:
        api_key_env = str(provider.get("api_key_env") or "ANTHROPIC_API_KEY")
        api_key = os.environ.get(api_key_env)
        if not api_key:
            result = {"provider_id": self.provider_id, "status": "error", "text": f"{api_key_env} is not set.", "raw": None}
            yield {"type": "delta", "text": result["text"]}
            yield {"type": "done", "result": result}
            return
        model = str(provider.get("model") or "claude-sonnet-4-20250514")
        payload = {
            "model": model,
            "max_tokens": int(provider.get("max_tokens") or 2048),
            "messages": self._messages(request),
            "stream": True,
        }
        req = urllib.request.Request(
            _anthropic_messages_endpoint(provider),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "x-api-key": api_key,
                "anthropic-version": str(provider.get("anthropic_version") or "2023-06-01"),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        chunks: list[str] = []
        usage_acc: dict[str, Any] = {}
        try:
            with _provider_urlopen(provider, req, timeout=int(provider.get("timeout_seconds") or 120)) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    piece = normalize_usage(event)
                    if piece:
                        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                            if piece.get(key):
                                usage_acc[key] = max(int(usage_acc.get(key) or 0), int(piece.get(key) or 0))
                        if piece.get("cost_usd") is not None:
                            usage_acc["cost_usd"] = piece.get("cost_usd")
                        usage_acc["source"] = piece.get("source") or usage_acc.get("source") or "usage"
                    text = _extract_anthropic_stream_delta(event)
                    if text:
                        chunks.append(text)
                        yield {"type": "delta", "text": text}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            result = {"provider_id": self.provider_id, "status": "error", "text": f"Anthropic HTTP {exc.code}: {body[:500]}", "raw": body}
            yield {"type": "delta", "text": result["text"]}
            yield {"type": "done", "result": result}
            return
        except OSError as exc:
            result = {"provider_id": self.provider_id, "status": "error", "text": f"Anthropic request failed: {exc}", "raw": None}
            yield {"type": "delta", "text": result["text"]}
            yield {"type": "done", "result": result}
            return
        if usage_acc:
            usage_acc["total_tokens"] = int(usage_acc.get("total_tokens") or 0) or (
                int(usage_acc.get("prompt_tokens") or 0) + int(usage_acc.get("completion_tokens") or 0)
            )
        yield {
            "type": "done",
            "result": _result_with_usage(
                {"provider_id": self.provider_id, "status": "ok", "text": "".join(chunks), "raw": None},
                model=model,
                usage_payload=usage_acc or None,
            ),
        }


class OpenRouterAdapter(ProviderAdapter):
    provider_id = "openrouter"

    def _messages(self, request: ProviderRequest) -> list[dict[str, Any]]:
        prompt = self.build_prompt(request)
        if not request.images:
            return [{"role": "user", "content": prompt}]
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in request.images:
            content.append({"type": "image_url", "image_url": {"url": _image_data_url(image)}})
        return [{"role": "user", "content": content}]

    def check(self, provider: dict[str, Any], project_root: Path) -> dict[str, Any]:
        api_key_env = str(provider.get("api_key_env") or "OPENROUTER_API_KEY")
        model = str(provider.get("model") or "openai/gpt-4o-mini")
        if not os.environ.get(api_key_env):
            return {
                "provider_id": self.provider_id,
                "ready": False,
                "status": "missing_credentials",
                "message": f"{api_key_env} is not set.",
                "hint": f"Set {api_key_env} before starting ArchitectOS.",
                "actions": [f"Set {api_key_env}", "Restart ArchitectOS", "Run provider test again"],
                "details": {"api_key_env": api_key_env, "model": model},
            }
        return {
            "provider_id": self.provider_id,
            "ready": True,
            "status": "configured",
            "message": f"{api_key_env} is available for model {model}.",
            "hint": "Credentials are present. Refresh models or send a short chat prompt.",
            "actions": ["Refresh models", "Send a short chat prompt", "Check model slug if execution fails"],
            "details": {"api_key_env": api_key_env, "model": model, "base_url": _openrouter_base_url(provider)},
        }

    def list_models(self, provider: dict[str, Any], project_root: Path) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get(str(provider.get("api_key_env") or "OPENROUTER_API_KEY"))
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(f"{_openrouter_base_url(provider)}/models", headers=headers, method="GET")
        try:
            with _provider_urlopen(provider, req, timeout=min(20, int(provider.get("timeout_seconds") or 20))) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return {"provider_id": self.provider_id, "ready": False, "status": "error", "models": [], "message": f"OpenRouter HTTP {exc.code}: {body[:300]}", "hint": "Check API key, network access, and Base URL.", "base_url": _openrouter_base_url(provider)}
        except OSError as exc:
            return {"provider_id": self.provider_id, "ready": False, "status": "unreachable", "models": [], "message": f"OpenRouter is not reachable: {exc}", "hint": "Check network access and Base URL.", "base_url": _openrouter_base_url(provider)}
        models = []
        for item in data.get("data") or data.get("models") or []:
            name = str(item.get("id") or item.get("name") or "")
            if not name:
                continue
            models.append({
                "name": name,
                "model": name,
                "label": str(item.get("name") or name),
                "context_length": item.get("context_length"),
                "pricing": item.get("pricing") or {},
            })
        models.sort(key=lambda item: item["name"])
        return {"provider_id": self.provider_id, "ready": True, "status": "ok", "models": models, "message": f"Found {len(models)} OpenRouter model(s).", "hint": "Choose a model slug and save it on the provider.", "base_url": _openrouter_base_url(provider)}

    def run(self, provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> dict[str, Any]:
        api_key_env = str(provider.get("api_key_env") or "OPENROUTER_API_KEY")
        api_key = os.environ.get(api_key_env)
        if not api_key:
            return {"provider_id": self.provider_id, "status": "error", "text": f"{api_key_env} is not set.", "raw": None}
        payload = {
            "model": provider.get("model") or "openai/gpt-4o-mini",
            "messages": self._messages(request),
        }
        req = urllib.request.Request(
            f"{_openrouter_base_url(provider)}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with _provider_urlopen(provider, req, timeout=int(provider.get("timeout_seconds") or 120)) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return {"provider_id": self.provider_id, "status": "error", "text": f"OpenRouter HTTP {exc.code}: {body[:500]}", "raw": body}
        except OSError as exc:
            return {"provider_id": self.provider_id, "status": "error", "text": f"OpenRouter request failed: {exc}", "raw": None}
        model = str(provider.get("model") or "openai/gpt-4o-mini")
        return _result_with_usage(
            {"provider_id": self.provider_id, "status": "ok", "text": _extract_chat_completion_text(data), "raw": data},
            model=model,
            usage_payload=data,
        )

    def stream(self, provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> Iterator[dict[str, Any]]:
        api_key_env = str(provider.get("api_key_env") or "OPENROUTER_API_KEY")
        api_key = os.environ.get(api_key_env)
        if not api_key:
            result = {"provider_id": self.provider_id, "status": "error", "text": f"{api_key_env} is not set.", "raw": None}
            yield {"type": "delta", "text": result["text"]}
            yield {"type": "done", "result": result}
            return
        model = str(provider.get("model") or "openai/gpt-4o-mini")
        payload = {
            "model": model,
            "messages": self._messages(request),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        req = urllib.request.Request(
            f"{_openrouter_base_url(provider)}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        chunks: list[str] = []
        usage_payload: Any = None
        try:
            with _provider_urlopen(provider, req, timeout=int(provider.get("timeout_seconds") or 120)) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event.get("usage"), dict) or normalize_usage(event):
                        usage_payload = event
                    text = _extract_chat_completion_stream_delta(event)
                    if text:
                        chunks.append(text)
                        yield {"type": "delta", "text": text}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            result = {"provider_id": self.provider_id, "status": "error", "text": f"OpenRouter HTTP {exc.code}: {body[:500]}", "raw": body}
            yield {"type": "delta", "text": result["text"]}
            yield {"type": "done", "result": result}
            return
        except OSError as exc:
            result = {"provider_id": self.provider_id, "status": "error", "text": f"OpenRouter request failed: {exc}", "raw": None}
            yield {"type": "delta", "text": result["text"]}
            yield {"type": "done", "result": result}
            return
        yield {
            "type": "done",
            "result": _result_with_usage(
                {"provider_id": self.provider_id, "status": "ok", "text": "".join(chunks), "raw": None},
                model=model,
                usage_payload=usage_payload,
            ),
        }


class OllamaAdapter(ProviderAdapter):
    provider_id = "ollama"

    def check(self, provider: dict[str, Any], project_root: Path) -> dict[str, Any]:
        base_url = _ollama_base_url(provider)
        model = str(provider.get("model") or "")
        req = urllib.request.Request(f"{base_url}/api/tags", method="GET")
        try:
            with _provider_urlopen(provider, req, timeout=min(5, int(provider.get("timeout_seconds") or 5))) as response:
                data = json.loads(response.read().decode("utf-8"))
        except OSError as exc:
            return {
                "provider_id": self.provider_id,
                "ready": False,
                "status": "unreachable",
                "message": f"Ollama is not reachable at {base_url}: {exc}",
                "hint": "Start the Ollama daemon or update the provider Base URL.",
                "actions": ["Start Ollama", "Verify Base URL", "Run provider test again"],
                "details": {"base_url": base_url, "model": model},
            }
        models = [str(item.get("name") or item.get("model") or "") for item in data.get("models") or []]
        ready = not model or model in models
        return {
            "provider_id": self.provider_id,
            "ready": ready,
            "status": "configured" if ready else "missing_model",
            "message": "Ollama is reachable." if ready else f"Ollama is reachable, but model {model} was not found.",
            "hint": "Model is ready for local generation." if ready else "Pull the configured model or choose one from the available model list.",
            "actions": ["Use chat with Ollama"] if ready else [f"Pull {model}", "Change provider model", "Run provider test again"],
            "details": {"base_url": base_url, "model": model, "models": models[:20]},
        }

    def list_models(self, provider: dict[str, Any], project_root: Path) -> dict[str, Any]:
        base_url = _ollama_base_url(provider)
        req = urllib.request.Request(f"{base_url}/api/tags", method="GET")
        try:
            with _provider_urlopen(provider, req, timeout=min(5, int(provider.get("timeout_seconds") or 5))) as response:
                data = json.loads(response.read().decode("utf-8"))
        except OSError as exc:
            return {
                "provider_id": self.provider_id,
                "ready": False,
                "status": "unreachable",
                "models": [],
                "message": f"Ollama is not reachable at {base_url}: {exc}",
                "hint": "Start Ollama or update the provider Base URL.",
                "base_url": base_url,
            }
        models = []
        for item in data.get("models") or []:
            name = str(item.get("name") or item.get("model") or "")
            if not name:
                continue
            models.append({
                "name": name,
                "model": str(item.get("model") or name),
                "size": item.get("size"),
                "digest": item.get("digest"),
                "modified_at": item.get("modified_at"),
                "details": item.get("details") or {},
            })
        models.sort(key=lambda item: item["name"])
        return {
            "provider_id": self.provider_id,
            "ready": True,
            "status": "ok",
            "models": models,
            "message": f"Found {len(models)} local Ollama model(s).",
            "hint": "Choose a model and save it on the Ollama provider.",
            "base_url": base_url,
        }

    def run(self, provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> dict[str, Any]:
        base_url = _ollama_base_url(provider)
        payload = {
            "model": provider.get("model") or "llama3.1",
            "prompt": self.build_prompt(request),
            "stream": False,
        }
        if request.images:
            payload["images"] = [image.get("b64") or "" for image in request.images]
        req = urllib.request.Request(
            f"{base_url}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with _provider_urlopen(provider, req, timeout=int(provider.get("timeout_seconds") or 120)) as response:
                data = json.loads(response.read().decode("utf-8"))
        except OSError as exc:
            return {"provider_id": self.provider_id, "status": "error", "text": f"Ollama request failed: {exc}", "raw": None}
        model = str(provider.get("model") or "llama3.1")
        return _result_with_usage(
            {"provider_id": self.provider_id, "status": "ok", "text": str(data.get("response") or "").strip(), "raw": data},
            model=model,
            usage_payload=data,
        )

    def stream(self, provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> Iterator[dict[str, Any]]:
        base_url = _ollama_base_url(provider)
        model = str(provider.get("model") or "llama3.1")
        payload = {
            "model": model,
            "prompt": self.build_prompt(request),
            "stream": True,
        }
        if request.images:
            payload["images"] = [image.get("b64") or "" for image in request.images]
        req = urllib.request.Request(
            f"{base_url}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        chunks: list[str] = []
        usage_payload: Any = None
        try:
            with _provider_urlopen(provider, req, timeout=int(provider.get("timeout_seconds") or 120)) as response:
                for raw_line in response:
                    if not raw_line.strip():
                        continue
                    data = json.loads(raw_line.decode("utf-8"))
                    text = str(data.get("response") or "")
                    if text:
                        chunks.append(text)
                        yield {"type": "delta", "text": text}
                    if data.get("done"):
                        usage_payload = data
                        break
        except OSError as exc:
            result = {"provider_id": self.provider_id, "status": "error", "text": f"Ollama request failed: {exc}", "raw": None}
            yield {"type": "delta", "text": result["text"]}
            yield {"type": "done", "result": result}
            return
        yield {
            "type": "done",
            "result": _result_with_usage(
                {"provider_id": self.provider_id, "status": "ok", "text": "".join(chunks), "raw": None},
                model=model,
                usage_payload=usage_payload,
            ),
        }


class CliAdapter(ProviderAdapter):
    def __init__(self, provider_id: str, default_command: list[str]) -> None:
        self.provider_id = provider_id
        self.default_command = default_command

    def check(self, provider: dict[str, Any], project_root: Path) -> dict[str, Any]:
        command = _provider_command(provider, self.default_command)
        if not command:
            return {
                "provider_id": self.provider_id,
                "ready": False,
                "status": "missing_command",
                "message": "CLI command is not configured.",
                "hint": "Set the command field to the CLI invocation ArchitectOS should run.",
                "actions": ["Set command", "Run provider test again"],
                "details": {},
            }
        executable = shutil.which(command[0])
        if not executable:
            return {
                "provider_id": self.provider_id,
                "ready": False,
                "status": "missing_cli",
                "message": f"CLI executable not found: {command[0]}",
                "hint": f"Install {command[0]} or update the command to an executable on PATH.",
                "actions": [f"Install {command[0]}", "Update command", "Restart ArchitectOS if PATH changed"],
                "details": {"command": command},
            }
        if self.provider_id == "gemini-cli":
            return self._check_gemini_auth(executable, command, project_root)
        if self.provider_id == "codex-cli":
            return self._check_codex_auth(executable, command, project_root)
        return {
            "provider_id": self.provider_id,
            "ready": True,
            "status": "configured",
            "message": f"CLI executable is available: {executable}",
            "hint": "CLI is available. Use a short chat prompt to verify auth/session state.",
            "actions": ["Send a short chat prompt", "Review stderr if execution fails"],
            "details": {"command": [executable, *command[1:]], "workdir": str(project_root)},
        }

    def login(self, provider: dict[str, Any], project_root: Path) -> dict[str, Any]:
        command = _provider_command(provider, self.default_command)
        if not command:
            return {"provider_id": self.provider_id, "ok": False, "status": "missing_command", "message": "CLI command is not configured."}
        executable = shutil.which(command[0])
        if not executable:
            return {
                "provider_id": self.provider_id,
                "ok": False,
                "status": "missing_cli",
                "message": f"CLI executable not found: {command[0]}",
                "hint": f"Install {command[0]} first, then sign in again.",
            }
        if self.provider_id == "gemini-cli":
            return self._start_gemini_login(executable, project_root)
        if self.provider_id == "codex-cli":
            return self._start_codex_login(executable, project_root)
        return {
            "provider_id": self.provider_id,
            "ok": False,
            "status": "unsupported",
            "message": "This provider does not support in-app session login.",
            "hint": "Configure API credentials in the environment, or install and authenticate the CLI manually.",
        }

    def _check_gemini_auth(self, executable: str, command: list[str], project_root: Path) -> dict[str, Any]:
        auth = _gemini_session_auth_state()
        details = {
            "command": [executable, *command[1:]],
            "workdir": str(project_root),
            "auth_method": auth.get("method") or "",
            "auth_email": auth.get("email") or "",
            "supports_login": True,
            "login_label": "Sign in with Google",
            "cli": "antigravity",
        }
        if auth.get("ready"):
            method = str(auth.get("method") or "session")
            email = str(auth.get("email") or "")
            if method == "google-account":
                who = f" as {email}" if email else ""
                message = f"Antigravity CLI (agy) is signed in with Google{who}."
                hint = "Google account session is ready for ArchitectOS chats. Gemini CLI API keys are no longer required."
            else:
                message = "Antigravity CLI can use an environment API token, but Google sign-in is preferred."
                hint = "Prefer Sign in with Google. GEMINI_API_KEY is ignored by agy."
            return {
                "provider_id": self.provider_id,
                "ready": True,
                "status": "configured",
                "message": message,
                "hint": hint,
                "actions": ["Send a short chat prompt", "Sign in with Google again to refresh session"],
                "details": details,
            }
        return {
            "provider_id": self.provider_id,
            "ready": False,
            "status": "missing_credentials",
            "message": "Antigravity CLI (agy) is installed, but no Google account session was found.",
            "hint": "Click Sign in with Google, choose Google OAuth in the terminal, paste the browser code, then Test again.",
            "actions": ["Sign in with Google", "Run provider test again"],
            "details": details,
        }

    def _start_gemini_login(self, executable: str, project_root: Path) -> dict[str, Any]:
        launched = _launch_cli_login_terminal(
            shell_command=(
                f'cd {shlex.quote(str(project_root))} && '
                f'echo "Antigravity login: choose Google OAuth, then paste the browser code if asked." && '
                f'{shlex.quote(executable)}'
            ),
            title="Antigravity Sign in with Google",
        )
        return {
            "provider_id": self.provider_id,
            "ok": bool(launched.get("ok")),
            "status": "login_started" if launched.get("ok") else "login_failed",
            "message": launched.get("message") or "Started Antigravity Google sign-in.",
            "hint": "In the opened terminal choose Google OAuth, finish the browser flow (paste the code if prompted), then click Test in ArchitectOS.",
            "details": {
                "auth_method": "google-account",
                "supports_login": True,
                "login_label": "Sign in with Google",
                "launcher": launched.get("launcher") or "",
                "cli": "antigravity",
            },
        }

    def _check_codex_auth(self, executable: str, command: list[str], project_root: Path) -> dict[str, Any]:
        auth = _codex_session_auth_state(executable)
        details = {
            "command": [executable, *command[1:]],
            "workdir": str(project_root),
            "auth_method": auth.get("method") or "",
            "auth_email": auth.get("email") or "",
            "supports_login": True,
            "login_label": "Sign in with ChatGPT",
        }
        if auth.get("ready"):
            method = str(auth.get("method") or "session")
            email = str(auth.get("email") or "")
            who = f" ({email})" if email else ""
            label = "ChatGPT" if method == "chatgpt" else method.replace("-", " ")
            return {
                "provider_id": self.provider_id,
                "ready": True,
                "status": "configured",
                "message": f"Codex CLI is signed in via {label}{who}.",
                "hint": "Account session is ready for ArchitectOS chats.",
                "actions": ["Send a short chat prompt", "Sign in with ChatGPT again to refresh session"],
                "details": details,
            }
        return {
            "provider_id": self.provider_id,
            "ready": False,
            "status": "missing_credentials",
            "message": "Codex CLI is installed, but no ChatGPT/API session was found.",
            "hint": "Click Sign in with ChatGPT, complete browser login, then Test again.",
            "actions": ["Sign in with ChatGPT", "Run provider test again"],
            "details": details,
        }

    def _start_codex_login(self, executable: str, project_root: Path) -> dict[str, Any]:
        launched = _launch_cli_login_terminal(
            shell_command=f'cd {shlex.quote(str(project_root))} && {shlex.quote(executable)} login',
            title="Codex Sign in with ChatGPT",
        )
        return {
            "provider_id": self.provider_id,
            "ok": bool(launched.get("ok")),
            "status": "login_started" if launched.get("ok") else "login_failed",
            "message": launched.get("message") or "Started Codex ChatGPT sign-in.",
            "hint": "Complete the browser login in the opened terminal, then click Test in ArchitectOS.",
            "details": {
                "auth_method": "chatgpt",
                "supports_login": True,
                "login_label": "Sign in with ChatGPT",
                "launcher": launched.get("launcher") or "",
            },
        }

    def _uses_prompt_arg(self) -> bool:
        return self.provider_id == "gemini-cli"

    def run(self, provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> dict[str, Any]:
        guard = _cli_guard(provider, request, project_root)
        if guard.get("error"):
            return {"provider_id": self.provider_id, "status": guard["status"], "text": guard["text"], "raw": guard}
        prompt = self.build_prompt(request)
        command = list(guard["command"])
        stdin_text = None
        if self._uses_prompt_arg():
            command = _cli_command_with_prompt_arg(command, prompt, int(provider.get("timeout_seconds") or 180))
        else:
            stdin_text = prompt
        try:
            proc = subprocess.run(
                command,
                input=stdin_text,
                text=True,
                capture_output=True,
                cwd=str(guard["workdir"]),
                timeout=int(provider.get("timeout_seconds") or 180),
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            return {"provider_id": self.provider_id, "status": "timeout", "text": f"CLI timed out after {exc.timeout} seconds.", "raw": {"command": command, "workdir": str(guard["workdir"])}}
        except OSError as exc:
            return {"provider_id": self.provider_id, "status": "error", "text": f"CLI execution failed: {exc}", "raw": {"command": command, "workdir": str(guard["workdir"])}}
        output = (proc.stdout or "").strip()
        error = (proc.stderr or "").strip()
        status = "ok" if proc.returncode == 0 else "error"
        text = output if output else error
        return {"provider_id": self.provider_id, "status": status, "text": text, "raw": {"returncode": proc.returncode, "stderr": error, "command": command, "workdir": str(guard["workdir"])}}

    def stream(self, provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> Iterator[dict[str, Any]]:
        if self._uses_prompt_arg():
            result = self.run(provider, request, project_root)
            text = str(result.get("text") or "")
            if text:
                yield {"type": "delta", "text": text}
            yield {"type": "done", "result": result}
            return
        guard = _cli_guard(provider, request, project_root)
        if guard.get("error"):
            result = {"provider_id": self.provider_id, "status": guard["status"], "text": guard["text"], "raw": guard}
            yield {"type": "delta", "text": result["text"]}
            yield {"type": "done", "result": result}
            return
        command = list(guard["command"])
        chunks: list[str] = []
        stderr = ""
        returncode = 1
        status = "error"
        started = time.monotonic()
        timeout = int(provider.get("timeout_seconds") or 180)
        output_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(guard["workdir"]),
                shell=False,
            )
            assert proc.stdin is not None
            assert proc.stdout is not None
            assert proc.stderr is not None

            stdout_stream = proc.stdout

            def pump_stdout() -> None:
                while True:
                    char = stdout_stream.read(1)
                    if not char:
                        break
                    output_queue.put(("stdout", char))

            reader = threading.Thread(target=pump_stdout, daemon=True)
            reader.start()
            proc.stdin.write(self.build_prompt(request))
            proc.stdin.close()
            while proc.poll() is None or not output_queue.empty():
                if request.cancel_requested and request.cancel_requested():
                    proc.terminate()
                    status = "cancelled"
                    break
                if time.monotonic() - started > timeout:
                    proc.kill()
                    status = "timeout"
                    break
                try:
                    kind, chunk = output_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if kind == "stdout":
                    chunks.append(chunk)
                    yield {"type": "delta", "text": chunk}
            try:
                returncode = proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                returncode = proc.wait()
                status = "timeout"
            stderr = proc.stderr.read().strip()
        except OSError as exc:
            result = {"provider_id": self.provider_id, "status": "error", "text": f"CLI execution failed: {exc}", "raw": {"command": command, "workdir": str(guard["workdir"])}}
            yield {"type": "delta", "text": result["text"]}
            yield {"type": "done", "result": result}
            return
        if status not in {"cancelled", "timeout"}:
            status = "ok" if returncode == 0 else "error"
        text = "".join(chunks).strip() or stderr
        yield {"type": "done", "result": {"provider_id": self.provider_id, "status": status, "text": text, "raw": {"returncode": returncode, "stderr": stderr, "command": command, "workdir": str(guard["workdir"])}}}


class ProviderRouter:
    def __init__(self, project_root: Path, router_settings: dict[str, Any] | None = None) -> None:
        self.project_root = project_root
        self.router_settings = dict(router_settings or {})
        self.adapters: dict[str, ProviderAdapter] = {
            "local-memory": LocalMemoryAdapter(),
            "openai": OpenAIResponsesAdapter(),
            "azure-openai": AzureOpenAIResponsesAdapter(),
            "anthropic": AnthropicMessagesAdapter(),
            "openrouter": OpenRouterAdapter(),
            "ollama": OllamaAdapter(),
            "codex-cli": CliAdapter("codex-cli", ["codex", "exec", "--skip-git-repo-check", "-"]),
            "claude-code": CliAdapter("claude-code", ["claude", "--print"]),
            "gemini-cli": CliAdapter("gemini-cli", ["agy", "-p"]),
        }

    def policy(self) -> RouterPolicy:
        return RouterPolicy(self.router_settings)

    def plan(self, providers: list[dict[str, Any]], provider_id: str | None, role: str | None = None) -> dict[str, Any]:
        return self.policy().select(providers, provider_id, role)

    def route(
        self,
        providers: list[dict[str, Any]],
        request: ProviderRequest,
        provider_id: str | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        resolved_role = role or self._role(request)
        last_result: dict[str, Any] | None = None
        approved = bool(getattr(request, "approved", False))
        for provider, decision in self._iter_provider_attempts(providers, provider_id, resolved_role, approved):
            adapter = self.adapters.get(provider["id"], self.adapters["local-memory"])
            result = adapter.run(provider, request, self.project_root)
            if not self._is_provider_failure(result):
                result["requested_provider_id"] = provider_id or "auto"
                result["selected_provider"] = provider
                result["routing"] = decision
                return result
            result.setdefault("selected_provider", provider)
            result.setdefault("routing", decision)
            last_result = result
        result = dict(last_result or {"provider_id": provider_id or "auto", "status": "error", "text": "No provider could respond.", "raw": None})
        result["requested_provider_id"] = provider_id or "auto"
        result["selected_provider"] = result.get("selected_provider") or {"id": provider_id or "auto", "label": provider_id or "auto"}
        result["routing"] = result.get("routing") or {"mode": "error", "reason": "All provider attempts failed."}
        return result

    def check(self, providers: list[dict[str, Any]], provider_id: str | None) -> dict[str, Any]:
        provider, decision = self._select(providers, provider_id, None)
        adapter = self.adapters.get(provider["id"], self.adapters["local-memory"])
        result = adapter.check(provider, self.project_root)
        result["requested_provider_id"] = provider_id or "auto"
        result["selected_provider"] = provider
        result["routing"] = decision
        return result

    def login(self, providers: list[dict[str, Any]], provider_id: str) -> dict[str, Any]:
        provider, decision = self._select(providers, provider_id, None)
        adapter = self.adapters.get(provider["id"], self.adapters["local-memory"])
        login = getattr(adapter, "login", None)
        if not callable(login):
            result = {
                "provider_id": provider["id"],
                "ok": False,
                "status": "unsupported",
                "message": "This provider does not support in-app session login.",
                "hint": "Configure API credentials in the environment instead.",
            }
        else:
            result = login(provider, self.project_root)
        result["requested_provider_id"] = provider_id
        result["selected_provider"] = provider
        result["routing"] = decision
        return result

    def list_models(self, providers: list[dict[str, Any]], provider_id: str | None) -> dict[str, Any]:
        provider, decision = self._select(providers, provider_id, None)
        adapter = self.adapters.get(provider["id"], self.adapters["local-memory"])
        result = adapter.list_models(provider, self.project_root)
        result["requested_provider_id"] = provider_id or "auto"
        result["selected_provider"] = provider
        result["routing"] = decision
        return result

    def stream(
        self,
        providers: list[dict[str, Any]],
        request: ProviderRequest,
        provider_id: str | None = None,
        role: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        resolved_role = role or self._role(request)
        last_result: dict[str, Any] | None = None
        approved = bool(getattr(request, "approved", False))
        for provider, decision in self._iter_provider_attempts(providers, provider_id, resolved_role, approved):
            adapter = self.adapters.get(provider["id"], self.adapters["local-memory"])
            buffered: list[dict[str, Any]] = []
            failed = False
            for event in adapter.stream(provider, request, self.project_root):
                if event.get("type") == "done":
                    result = dict(event.get("result") or {})
                    if self._is_provider_failure(result):
                        result.setdefault("selected_provider", provider)
                        result.setdefault("routing", decision)
                        last_result = result
                        failed = True
                        break
                    yield {
                        "type": "start",
                        "requested_provider_id": provider_id or "auto",
                        "selected_provider": provider,
                        "provider_id": provider["id"],
                        "routing": decision,
                    }
                    for buffered_event in buffered:
                        yield buffered_event
                    result["requested_provider_id"] = provider_id or "auto"
                    result["selected_provider"] = provider
                    result["routing"] = decision
                    yield {"type": "done", "result": result}
                    return
                buffered.append(event)
            if not failed:
                return
        result = dict(last_result or {"provider_id": provider_id or "auto", "status": "error", "text": "No provider could respond.", "raw": None})
        result["requested_provider_id"] = provider_id or "auto"
        result["routing"] = {"mode": "error", "reason": "All provider attempts failed."}
        yield {
            "type": "start",
            "requested_provider_id": provider_id or "auto",
            "selected_provider": {"id": result.get("provider_id"), "label": result.get("provider_id")},
            "provider_id": result.get("provider_id"),
            "routing": result["routing"],
        }
        yield {"type": "done", "result": result}

    def _role(self, request: ProviderRequest) -> str:
        return getattr(request, "role", "") or classify_role(getattr(request, "message", "") or "")

    def _is_provider_failure(self, result: dict[str, Any]) -> bool:
        # approval_required counts as a failure so auto-routing can fall back to an API provider
        # instead of stalling the turn with an approval notice.
        return str(result.get("status") or "").lower() in {"error", "approval_required"}

    def _needs_cli_approval(self, provider: dict[str, Any], approved: bool) -> bool:
        if approved:
            return False
        if str(provider.get("provider_type") or "").lower() != "cli":
            return False
        return bool(provider.get("approval_required", True))

    def _iter_provider_attempts(
        self,
        providers: list[dict[str, Any]],
        provider_id: str | None,
        role: str | None,
        approved: bool = True,
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        policy = self.policy()
        plan = policy.select(providers, provider_id, role)
        by_id = {provider["id"]: provider for provider in providers if provider.get("id")}
        initial_provider = plan["provider"]
        initial_id = str(initial_provider.get("id") or "")
        tried: set[str] = set()

        def build_decision(selected: dict[str, Any], mode: str, reason: str, fallback_from: str | None = None) -> dict[str, Any]:
            decision = dict(plan["decision"])
            decision.update({
                "mode": mode,
                "selected": selected.get("id"),
                "reason": reason,
                "ranked": decision.get("ranked") or policy.rank(providers, role)[:6],
            })
            if fallback_from:
                decision["fallback_from"] = fallback_from
                decision["requested_mode"] = plan["decision"].get("mode")
            return decision

        explicit = plan["decision"].get("mode") == "explicit"
        skipped_for_approval = False
        if initial_id:
            tried.add(initial_id)
            if explicit or not self._needs_cli_approval(initial_provider, approved):
                yield initial_provider, plan["decision"]
            else:
                skipped_for_approval = True

        if explicit:
            return

        ranked = policy.rank(providers, role)
        for item in ranked:
            candidate_id = str(item.get("provider_id") or "")
            if not candidate_id or candidate_id in tried:
                continue
            if not item.get("enabled") or float(item.get("availability") or 0) <= 0:
                continue
            provider = by_id.get(candidate_id)
            if not provider:
                continue
            if self._needs_cli_approval(provider, approved):
                continue
            tried.add(candidate_id)
            label = provider.get("label") or candidate_id
            if skipped_for_approval:
                reason = f"Using {label} because {initial_id or 'the preferred provider'} needs CLI approval."
            else:
                reason = f"Falling back to {label} after {initial_id or 'previous provider'} failed."
            yield provider, build_decision(provider, "fallback", reason, initial_id or None)

        if "local-memory" not in tried:
            fallback = by_id.get("local-memory") or {
                "id": "local-memory",
                "label": "Local Memory",
                "provider_type": "local",
                "enabled": True,
                "status": "fallback",
                "model": "",
            }
            reason = f"Falling back to local memory after {initial_id or 'previous provider'} failed."
            yield fallback, build_decision(fallback, "fallback", reason, initial_id or None)

    def _select(self, providers: list[dict[str, Any]], provider_id: str | None, role: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
        plan = self.policy().select(providers, provider_id, role)
        return plan["provider"], plan["decision"]


def _cli_guard(provider: dict[str, Any], request: ProviderRequest, project_root: Path) -> dict[str, Any]:
    if bool(provider.get("approval_required", True)) and not request.approved:
        return {"error": True, "status": "approval_required", "text": "CLI run approval is required before ArchitectOS can execute this provider.", "approval_required": True}
    command = _provider_command(provider, [])
    if not command:
        return {"error": True, "status": "error", "text": "CLI command is not configured."}
    executable = shutil.which(command[0])
    if not executable:
        return {"error": True, "status": "error", "text": f"CLI executable not found: {command[0]}", "command": command}
    command[0] = executable
    workdir, error = _provider_workdir(provider, project_root)
    if error:
        return {"error": True, "status": "workdir_rejected", "text": error, "command": command}
    return {"error": False, "command": command, "workdir": workdir}


def _jwt_email_claim(token: str | None) -> str:
    raw = str(token or "").strip()
    if not raw or raw.count(".") < 2:
        return ""
    try:
        payload = raw.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return ""
    email = str(data.get("email") or data.get("preferred_username") or "").strip()
    return email if "@" in email else ""


def _gemini_home() -> Path:
    return Path.home() / ".gemini"


def _gemini_oauth_path() -> Path:
    return _gemini_home() / "oauth_creds.json"


def _gemini_settings_path() -> Path:
    return _gemini_home() / "settings.json"


def _gemini_session_auth_state() -> dict[str, Any]:
    api_key = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or os.environ.get("ANTIGRAVITY_TOKEN"))
    oauth_path = _gemini_oauth_path()
    oauth_ready = False
    email = ""
    if oauth_path.is_file():
        try:
            data = json.loads(oauth_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if isinstance(data, dict) and (data.get("refresh_token") or data.get("access_token")):
            oauth_ready = True
            email = _jwt_email_claim(str(data.get("id_token") or ""))
    accounts_path = _gemini_home() / "google_accounts.json"
    if accounts_path.is_file() and not email:
        try:
            accounts = json.loads(accounts_path.read_text(encoding="utf-8"))
            active = accounts.get("active") if isinstance(accounts, dict) else None
            if isinstance(active, dict):
                email = str(active.get("email") or active.get("account") or "").strip()
            elif isinstance(active, str) and "@" in active:
                email = active.strip()
        except Exception:
            pass
    selected = ""
    settings_path = _gemini_settings_path()
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            selected = str((((settings or {}).get("security") or {}).get("auth") or {}).get("selectedType") or "")
        except Exception:
            selected = ""
    # Antigravity uses the same ~/.gemini oauth session.
    if oauth_ready:
        return {"ready": True, "method": "google-account", "email": email, "selected_type": selected or "oauth-personal"}
    if api_key:
        return {"ready": True, "method": "api-key", "email": "", "selected_type": selected or "api-key"}
    return {"ready": False, "method": "", "email": "", "selected_type": selected}


def _ensure_gemini_oauth_personal_setting() -> None:
    path = _gemini_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    settings: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                settings = loaded
        except Exception:
            settings = {}
    security = settings.get("security")
    if not isinstance(security, dict):
        security = {}
        settings["security"] = security
    auth = security.get("auth")
    if not isinstance(auth, dict):
        auth = {}
        security["auth"] = auth
    auth["selectedType"] = "oauth-personal"
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def _codex_session_auth_state(executable: str) -> dict[str, Any]:
    auth_path = Path.home() / ".codex" / "auth.json"
    email = ""
    method = ""
    if auth_path.is_file():
        try:
            data = json.loads(auth_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if isinstance(data, dict):
            method = str(data.get("auth_mode") or "").strip().lower()
            tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
            email = _jwt_email_claim(str((tokens or {}).get("id_token") or ""))
            if method or (tokens or {}).get("access_token") or (tokens or {}).get("refresh_token") or data.get("OPENAI_API_KEY"):
                if not method:
                    method = "api-key" if data.get("OPENAI_API_KEY") else "session"
                return {"ready": True, "method": method, "email": email}
    try:
        proc = subprocess.run(
            [executable, "login", "status"],
            text=True,
            capture_output=True,
            timeout=8,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"ready": False, "method": "", "email": ""}
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}".strip()
    lower = text.lower()
    if proc.returncode == 0 and ("logged in" in lower or "chatgpt" in lower or "authenticated" in lower):
        if "chatgpt" in lower:
            method = "chatgpt"
        elif "api" in lower:
            method = "api-key"
        else:
            method = method or "session"
        return {"ready": True, "method": method, "email": email}
    return {"ready": False, "method": "", "email": ""}


def _launch_cli_login_terminal(*, shell_command: str, title: str) -> dict[str, Any]:
    system = platform.system().lower()
    try:
        if system == "darwin":
            script = (
                f'tell application "Terminal"\n'
                f'activate\n'
                f'do script {json.dumps(shell_command)}\n'
                f'end tell'
            )
            subprocess.Popen(["osascript", "-e", script], shell=False)
            return {"ok": True, "launcher": "Terminal.app", "message": f"Opened Terminal for {title}."}
        if system == "linux":
            for candidate in ("x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal", "xterm"):
                exe = shutil.which(candidate)
                if not exe:
                    continue
                if candidate == "gnome-terminal":
                    subprocess.Popen([exe, "--", "bash", "-lc", shell_command], shell=False)
                else:
                    subprocess.Popen([exe, "-e", f"bash -lc {shlex.quote(shell_command)}"], shell=False)
                return {"ok": True, "launcher": candidate, "message": f"Opened {candidate} for {title}."}
            return {
                "ok": False,
                "launcher": "",
                "message": "No graphical terminal found. Run the CLI login command manually in your shell.",
            }
        if system == "windows":
            subprocess.Popen(["cmd", "/c", "start", "cmd", "/k", shell_command], shell=False)
            return {"ok": True, "launcher": "cmd", "message": f"Opened Command Prompt for {title}."}
    except OSError as exc:
        return {"ok": False, "launcher": "", "message": f"Could not open login terminal: {exc}"}
    return {
        "ok": False,
        "launcher": "",
        "message": f"Unsupported OS for automatic login terminal launch ({system}). Run the CLI login command manually.",
    }


def _provider_workdir(provider: dict[str, Any], project_root: Path) -> tuple[Path, str]:
    policy = str(provider.get("workdir_policy") or "project-root")
    if policy != "custom":
        return project_root, ""
    raw = str(provider.get("workdir") or "").strip()
    if not raw:
        return project_root, ""
    candidate = Path(raw).expanduser().resolve()
    root = project_root.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return root, f"CLI workdir rejected by policy: {candidate} is outside {root}."
    if not candidate.exists() or not candidate.is_dir():
        return root, f"CLI workdir does not exist: {candidate}."
    return candidate, ""


def _chunk_text(text: str, size: int = 18) -> Iterator[str]:
    for index in range(0, len(text), size):
        yield text[index:index + size]


def _cli_command_with_prompt_arg(command: list[str], prompt: str, timeout_seconds: int) -> list[str]:
    """Build an agy/gemini-style invocation where the prompt is a -p/--print argument."""
    cleaned = [part for part in command if part != "-"]
    if not cleaned:
        cleaned = ["agy", "-p"]
    # Migrate legacy gemini executable to agy when present on PATH.
    if cleaned[0] in {"gemini", "gemini-cli"} and shutil.which("agy"):
        cleaned[0] = "agy"
    flags = {"-p", "--print", "--prompt"}
    out: list[str] = []
    prompt_attached = False
    index = 0
    while index < len(cleaned):
        token = cleaned[index]
        out.append(token)
        if token in flags:
            next_token = cleaned[index + 1] if index + 1 < len(cleaned) else None
            if next_token is not None and not str(next_token).startswith("-"):
                out.append(prompt)
                prompt_attached = True
                index += 2
                continue
            out.append(prompt)
            prompt_attached = True
            index += 1
            continue
        index += 1
    if not prompt_attached:
        out.extend(["-p", prompt])
    # Prefer a print timeout close to ArchitectOS provider timeout.
    if "--print-timeout" not in out:
        seconds = max(30, int(timeout_seconds or 180))
        out.extend(["--print-timeout", f"{seconds}s"])
    return out


def _provider_command(provider: dict[str, Any], default_command: list[str]) -> list[str]:
    if isinstance(provider.get("command"), list):
        command = [str(part) for part in provider["command"] if str(part)]
        if command:
            return _normalize_cli_command(command, default_command)
    if isinstance(provider.get("command"), str) and provider["command"].strip():
        return _normalize_cli_command(provider["command"].strip().split(), default_command)
    return list(default_command)


def _normalize_cli_command(command: list[str], default_command: list[str]) -> list[str]:
    if not command:
        return list(default_command)
    # Auto-migrate stored Gemini CLI commands to Antigravity (agy).
    if command[0] in {"gemini", "gemini-cli"}:
        if shutil.which("agy"):
            rest = [part for part in command[1:] if part != "-"]
            if not rest or rest == ["-p"] or rest == ["-p", "-"]:
                return ["agy", "-p"]
            return ["agy", *rest]
        return command
    return command


def _allow_local_urls(provider: dict[str, Any]) -> bool:
    """Operator opt-in for loopback provider endpoints (LM Studio, llama.cpp, LiteLLM)."""
    if provider.get("allow_local"):
        return True
    return os.environ.get("ARCHITECTOS_ALLOW_LOCAL_URLS", "").strip().lower() in {"1", "true", "yes"}


def _anthropic_messages_endpoint(provider: dict[str, Any]) -> str:
    base_url = validate_outbound_url(
        str(provider.get("base_url") or "https://api.anthropic.com").rstrip("/"),
        allow_local=_allow_local_urls(provider),
    )
    if base_url.endswith("/v1/messages"):
        return base_url
    return f"{base_url}/v1/messages"


def _openrouter_base_url(provider: dict[str, Any]) -> str:
    return validate_outbound_url(
        str(provider.get("base_url") or "https://openrouter.ai/api/v1").rstrip("/"),
        allow_local=_allow_local_urls(provider),
    )


def _ollama_base_url(provider: dict[str, Any]) -> str:
    # Ollama is local by design, so loopback targets are fine; metadata endpoints are not.
    base_url = str(provider.get("base_url") or os.environ.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")
    return validate_outbound_url(base_url, allow_local=True)


def _extract_anthropic_text(data: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in data.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            chunks.append(str(item.get("text") or ""))
    return "".join(chunks).strip()


def _extract_anthropic_stream_delta(data: dict[str, Any]) -> str:
    delta = data.get("delta")
    if isinstance(delta, dict) and delta.get("type") == "text_delta":
        return str(delta.get("text") or "")
    if data.get("type") == "content_block_delta" and isinstance(delta, dict):
        return str(delta.get("text") or "")
    return ""


def _extract_chat_completion_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return "".join(str(item.get("text") or "") for item in content if isinstance(item, dict)).strip()
    return str(content or "").strip()


def _extract_chat_completion_stream_delta(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    if isinstance(content, list):
        return "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    return str(content or "")


def _extract_openai_stream_delta(data: dict[str, Any]) -> str:
    if data.get("type") == "response.output_text.delta":
        return str(data.get("delta") or "")
    if "delta" in data and isinstance(data["delta"], str):
        return str(data["delta"])
    for item in data.get("output") or []:
        for content in item.get("content") or []:
            if "text" in content:
                return str(content["text"])
    return ""


def _extract_openai_text(data: dict[str, Any]) -> str:
    if data.get("output_text"):
        return str(data["output_text"]).strip()
    chunks: list[str] = []
    for item in data.get("output") or []:
        for content in item.get("content") or []:
            if "text" in content:
                chunks.append(str(content["text"]))
    return "\n".join(chunks).strip()


def _openai_tools_payload(request: ProviderRequest) -> list[dict[str, Any]]:
    """Convert ArchitectOS tool specs into Responses API function tools."""
    tools: list[dict[str, Any]] = []
    for spec in request.tools or []:
        name = str(spec.get("name") or "").strip()
        if not name:
            continue
        tools.append({
            "type": "function",
            "name": name,
            "description": str(spec.get("description") or ""),
            "parameters": spec.get("parameters") or {"type": "object", "properties": {}},
        })
    return tools


def _extract_openai_tool_calls(data: Any) -> list[dict[str, Any]]:
    """Collect function_call items from a Responses API payload."""
    if not isinstance(data, dict):
        return []
    calls: list[dict[str, Any]] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or str(item.get("type") or "") != "function_call":
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        raw_args = item.get("arguments")
        arguments: dict[str, Any] = {}
        if isinstance(raw_args, dict):
            arguments = raw_args
        elif isinstance(raw_args, str) and raw_args.strip():
            try:
                parsed = json.loads(raw_args)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                arguments = parsed
        calls.append({"name": name, "arguments": arguments})
    return calls


def _merge_native_tool_calls(text: str, calls: list[dict[str, Any]]) -> str:
    """Re-emit native tool calls in the JSON protocol the tool loop already parses."""
    if not calls:
        return text
    protocol = json.dumps({"tool_calls": calls}, ensure_ascii=False)
    body = str(text or "").strip()
    return f"{body}\n{protocol}" if body else protocol
