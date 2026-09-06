from __future__ import annotations

from typing import Any, Callable, Iterator

from .adapters_base import is_local_memory_stub_text
from .rich_response import RESPONSE_FORMAT_POLICY

# Model fusion / council: ask the same question to several MODELS, then a judge
# model synthesizes one grounded answer (second opinion, fewer hallucinations).

MAX_PANEL = 4
DEFAULT_PANEL_SIZE = 3


def _usable_council_answer(answer: dict[str, Any]) -> bool:
    text = str(answer.get("text") or "").strip()
    if not text:
        return False
    status = str(answer.get("status") or "").lower()
    if status in {"error", "approval_required", "fallback"}:
        return False
    provider = answer.get("provider") if isinstance(answer.get("provider"), dict) else {}
    provider_id = str(provider.get("id") or answer.get("provider_id") or "")
    if provider_id == "local-memory":
        return False
    if is_local_memory_stub_text(text):
        return False
    return True


def _is_stub_synthesis(text: str, provider: dict[str, Any] | None) -> bool:
    provider_id = str((provider or {}).get("id") or "")
    if provider_id == "local-memory":
        return True
    return is_local_memory_stub_text(text)


class CouncilOrchestrator:
    """Runs one request across several models in parallel-of-thought, then judges.

    The actual provider calls are delegated to `run_ai`, so routing, streaming
    audit, and security redaction are reused from the service layer. `panel_provider`
    returns the list of selectable providers (same shape as the Ask model picker).
    """

    def __init__(
        self,
        run_ai: Callable[[dict[str, Any]], dict[str, Any]],
        panel_provider: Callable[[], list[dict[str, Any]]],
    ) -> None:
        self._run_ai = run_ai
        self._panel_provider = panel_provider

    def selectable_models(self) -> list[dict[str, Any]]:
        return [m for m in (self._panel_provider() or []) if str(m.get("id")) != "auto"]

    def default_models(self, limit: int = DEFAULT_PANEL_SIZE) -> list[str]:
        ready = [str(m.get("id")) for m in self.selectable_models() if m.get("ready")]
        picked: list[str] = []
        for pid in ready:
            if pid and pid not in picked:
                picked.append(pid)
            if len(picked) >= max(1, min(limit, MAX_PANEL)):
                break
        return picked

    def config(self) -> dict[str, Any]:
        return {
            "models": self.selectable_models(),
            "default": self.default_models(),
            "max_panel": MAX_PANEL,
        }

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        final_result: dict[str, Any] = {}
        for event in self.stream(payload):
            if event.get("type") == "done":
                final_result = event.get("result") or {}
        return final_result

    def stream(self, payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
        project_id = str(payload.get("project_id") or "architectos")
        query = str(payload.get("message") or payload.get("query") or "").strip()
        if not query:
            raise ValueError("council message is required")

        models = [str(m).strip() for m in (payload.get("models") or []) if str(m).strip()]
        models = list(dict.fromkeys(models))
        labels = {str(m.get("id")): str(m.get("label") or m.get("id")) for m in self.selectable_models()}
        ready_ids = {str(m.get("id")) for m in self.selectable_models() if m.get("ready")}
        if models:
            models = [model_id for model_id in models if model_id in ready_ids]
        else:
            models = self.default_models()
        judge_provider_id = str(payload.get("judge_provider_id") or "auto").strip() or "auto"
        if not models:
            yield {
                "type": "done",
                "result": {
                    "project_id": project_id,
                    "query": query,
                    "answers": [],
                    "models": [],
                    "synthesis": (
                        "No ready models for Council. Enable a provider with valid credentials in Providers, then Test it. "
                        "Cards with missing API keys are not included."
                    ),
                    "synthesis_provider": None,
                    "judge_provider_id": judge_provider_id,
                },
            }
            return
        models = models[:MAX_PANEL]
        attachments = payload.get("attachments")
        limit = int(payload.get("limit") or 6)

        answers: list[dict[str, Any]] = []
        for model_id in models:
            label = labels.get(model_id, model_id)
            yield {
                "type": "progress",
                "status": f"Asking {label}...",
                "agent": {"role": model_id, "role_name": label, "status": "running"},
            }
            result = self._run_ai({
                "project_id": project_id,
                "chat_id": payload.get("chat_id"),
                "message": self._member_prompt(query),
                "provider_id": model_id,
                "role": "council",
                "limit": limit,
                "allow_cli": payload.get("allow_cli"),
                "attachments": attachments,
                "advice": list(payload.get("advice") or []),
            })
            provider = result.get("provider") or {}
            answer = {
                "provider_id": model_id,
                "label": label,
                "provider": provider,
                "status": (provider.get("status") if isinstance(provider, dict) else "") or "unknown",
                "text": str(result.get("text") or ""),
                "run_id": result.get("run_id") or "",
            }
            answers.append(answer)
            yield {
                "type": "progress",
                "status": f"{label} responded.",
                "agent": {
                    "role": model_id,
                    "role_name": label,
                    "status": "done",
                    "provider": provider,
                    "text": answer["text"],
                },
            }

        valid = [answer for answer in answers if _usable_council_answer(answer)]
        synthesis = ""
        synthesis_provider: dict[str, Any] | None = None
        if len(valid) >= 2:
            yield {
                "type": "progress",
                "status": "Judge is synthesizing a grounded answer...",
                "agent": {"role": "judge", "role_name": "Judge", "status": "running"},
            }
            synthesis_result = self._run_ai({
                "project_id": project_id,
                "chat_id": payload.get("chat_id"),
                "message": self._judge_prompt(query, valid),
                "provider_id": judge_provider_id,
                "role": "review",
                "limit": limit,
                "allow_cli": payload.get("allow_cli"),
                "advice": list(payload.get("advice") or []),
            })
            synthesis = str(synthesis_result.get("text") or "")
            synthesis_provider = synthesis_result.get("provider") or {}
            if _is_stub_synthesis(synthesis, synthesis_provider):
                synthesis = ""
                synthesis_provider = None
            yield {
                "type": "progress",
                "status": "Judge finished.",
                "agent": {
                    "role": "judge",
                    "role_name": "Judge",
                    "status": "done",
                    "provider": synthesis_provider,
                    "text": synthesis,
                },
            }
        elif len(valid) == 1:
            synthesis = valid[0]["text"]
            synthesis_provider = valid[0]["provider"]

        yield {
            "type": "done",
            "result": {
                "project_id": project_id,
                "query": query,
                "answers": answers,
                "models": models,
                "synthesis": synthesis,
                "synthesis_provider": synthesis_provider,
                "judge_provider_id": judge_provider_id,
            },
        }

    def _member_prompt(self, query: str) -> str:
        return (
            "You are one independent expert on an ArchitectOS council.\n"
            "Answer the question on your own, as accurately as possible.\n"
            "Ground claims in the provided ArchitectOS memory/context; if you are unsure or "
            "lack evidence, say so explicitly instead of guessing.\n"
            "Be concise and specific. Use Markdown; a table when comparing items.\n"
            f"{RESPONSE_FORMAT_POLICY}\n\n"
            f"Question: {query}"
        )

    def _judge_prompt(self, query: str, answers: list[dict[str, Any]]) -> str:
        blocks = []
        for idx, answer in enumerate(answers, start=1):
            blocks.append(f"### Candidate {idx} — {answer['label']}\n{answer['text']}")
        joined = "\n\n".join(blocks)
        return (
            "You are the judge of an ArchitectOS model council. Several models answered the "
            "same question independently (below).\n"
            f"Original question: {query}\n\n"
            "Produce ONE final answer that a user can trust:\n"
            "- Keep only claims that are consistent across candidates or clearly best-supported.\n"
            "- Explicitly flag disagreements or unverified claims; do not invent a compromise.\n"
            "- Prefer the most specific, evidence-grounded response; drop hallucinated detail.\n"
            "- End with concrete next actions when useful.\n"
            "Return one final Markdown answer (table of related work items when relevant, "
            "optional mermaid, and a single architectos actions block).\n"
            f"{RESPONSE_FORMAT_POLICY}\n\n"
            f"{joined}"
        )
