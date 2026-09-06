"""Provider adapter foundations: the request object, base adapter, and shared helpers.

Split out of ``adapters`` so the CLI and HTTP adapter families can each import the
base without importing one another. Everything here is re-exported from
``adapters`` for backward-compatible imports.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .usage import enrich_usage, normalize_usage


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


def _chunk_text(text: str, size: int = 18) -> Iterator[str]:
    for index in range(0, len(text), size):
        yield text[index:index + size]


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


LOCAL_MEMORY_NO_HITS = (
    "This is Local Memory fallback, not a language model. I found no matching memory for that question.\n\n"
    "Enable a provider in Providers so Agent can think, then scan the project or add memory and ask again."
)

LOCAL_MEMORY_CANNOT_INSTALL = (
    "Local Memory cannot run commands or install MCP servers. This reply is not from a language model — "
    "ArchitectOS only searched local memory, which does not execute npx.\n\n"
    "Install Confluence and Jira from SETUP → MCP (catalog entries already use "
    "`npx -y mcp-confluence` and `npx -y mcp-jira`). For a one-shot command, use SETUP → Run command.\n\n"
    "To have Agent reason about this, enable a provider in Providers (Azure OpenAI, OpenAI, Anthropic, and so on) "
    "and keep the Agent tab selected."
)

_CLI_INSTALL_MARKERS = (
    "npx ",
    "npx\t",
    "npm install",
    "mcp-confluence",
    "mcp-jira",
    "install mcp",
    "install the mcp",
    "execute command",
    "run command",
    "run this command",
    "run the command",
    "please execute",
    "just install",
)


def looks_like_cli_install(message: str) -> bool:
    lowered = (message or "").lower()
    if any(marker in lowered for marker in _CLI_INSTALL_MARKERS):
        return True
    if "mcp" in lowered and any(word in lowered for word in ("install", "add", "setup", "enable", "npx", "jira", "confluence")):
        return True
    return False


def is_local_memory_stub_text(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    return value.startswith("This is Local Memory fallback") or value.startswith("Local Memory cannot run commands") or value.startswith("Local context is ready") or value.startswith("Local Memory (not a language model)")


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
        if looks_like_cli_install(request.message):
            return {"provider_id": self.provider_id, "status": "fallback", "text": LOCAL_MEMORY_CANNOT_INSTALL, "raw": None}
        lines = [line for line in request.context.splitlines() if line.startswith("- [")]
        labels = []
        for line in lines[:4]:
            head = line.split(":", 1)[0]
            labels.append(head.replace("- ", ""))
        if labels:
            text = (
                f"Local Memory (not a language model) found related notes for: {request.message}. "
                f"Strongest matches: {', '.join(labels)}. "
                "Use the Context tab for the full pack. Connect a model in Providers for Agent to reason beyond retrieval."
            )
            return {"provider_id": self.provider_id, "status": "ok", "text": text, "raw": None}
        return {"provider_id": self.provider_id, "status": "fallback", "text": LOCAL_MEMORY_NO_HITS, "raw": None}
