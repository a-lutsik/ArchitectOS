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
