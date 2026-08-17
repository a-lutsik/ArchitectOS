"""Response/stream text extraction helpers for HTTP provider adapters."""
from __future__ import annotations

import json
import os
from typing import Any

from .adapters_base import ProviderRequest
from .netutil import validate_outbound_url


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
