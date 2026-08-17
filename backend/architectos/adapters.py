from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .adapters_base import (
    LocalMemoryAdapter,
    ProviderAdapter,
    ProviderRequest,
    _chunk_text,
    _image_data_url,
    _result_with_usage,
)
from .adapters_cli import (
    CliAdapter,
    _cli_guard,
    _codex_session_auth_state,
    _gemini_session_auth_state,
    _provider_command,
)
from .adapters_extract import (
    _allow_local_urls,
    _anthropic_messages_endpoint,
    _extract_anthropic_stream_delta,
    _extract_anthropic_text,
    _extract_chat_completion_stream_delta,
    _extract_chat_completion_text,
    _extract_openai_stream_delta,
    _extract_openai_text,
    _extract_openai_tool_calls,
    _merge_native_tool_calls,
    _ollama_base_url,
    _openai_tools_payload,
    _openrouter_base_url,
)
from .netutil import outbound_policy, validate_outbound_url
from .routing import RouterPolicy, classify_role
from .ssl_util import urlopen
from .usage import normalize_usage

# Re-exported so ``from .adapters import X`` keeps working after the split into
# adapters_base (request/base adapter) and adapters_cli (subprocess adapters).
# _gemini_session_auth_state in particular is both imported by
# providers_council_service and used as a mock.patch target in the tests.
__all__ = [
    "AnthropicMessagesAdapter",
    "AzureOpenAIResponsesAdapter",
    "CliAdapter",
    "LocalMemoryAdapter",
    "OllamaAdapter",
    "OpenAIResponsesAdapter",
    "OpenRouterAdapter",
    "ProviderAdapter",
    "ProviderRequest",
    "ProviderRouter",
    "_allow_local_urls",
    "_chunk_text",
    "_cli_guard",
    "_codex_session_auth_state",
    "_gemini_session_auth_state",
    "_image_data_url",
    "_provider_command",
    "_provider_urlopen",
    "_result_with_usage",
]


def _provider_urlopen(provider: dict[str, Any], req: urllib.request.Request, *, timeout: float | int):
    """urlopen with redirect policy matching the provider's allow_local setting.

    Endpoints are already SSRF-checked when built; ``validate_initial=False``
    skips a second DNS lookup on the first hop. Redirect hops are still
    re-validated. Extra kwargs are avoided so test doubles that patch
    ``urlopen(req, timeout=...)`` keep working.

    Lives beside the HTTP adapters rather than in ``adapters_base`` so that
    patching ``adapters.urlopen`` still intercepts their requests.
    """
    allow_local = _allow_local_urls(provider)
    with outbound_policy(allow_local=allow_local, validate_initial=False):
        return urlopen(req, timeout=timeout)


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

