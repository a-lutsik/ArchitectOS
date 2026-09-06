from __future__ import annotations

from typing import Any

# Approximate public list prices USD per 1M tokens (input, output).
# Prefer provider-reported cost (OpenRouter) when present.
MODEL_PRICE_PER_1M: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-nano": (0.10, 0.40),
    "o4-mini": (1.10, 4.40),
    "o3-mini": (1.10, 4.40),
    # Anthropic
    "claude-sonnet-4-20250514": (3.00, 15.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-haiku-4": (1.00, 5.00),
    "claude-opus-4": (15.00, 75.00),
    # Common OpenRouter aliases
    "openai/gpt-4o-mini": (0.15, 0.60),
    "openai/gpt-4o": (2.50, 10.00),
    "openai/gpt-4.1-mini": (0.40, 1.60),
    "anthropic/claude-sonnet-4": (3.00, 15.00),
    "anthropic/claude-3.5-sonnet": (3.00, 15.00),
    "google/gemini-2.0-flash": (0.10, 0.40),
    "google/gemini-2.5-flash": (0.15, 0.60),
    "google/gemini-2.5-pro": (1.25, 10.00),
}


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_usage(payload: Any) -> dict[str, Any] | None:
    """Normalize provider usage blobs into a common shape.

    Returns None when no token counts can be found.
    """
    if not isinstance(payload, dict):
        return None

    # Already normalized.
    if any(key in payload for key in ("prompt_tokens", "completion_tokens", "total_tokens")) and (
        payload.get("prompt_tokens") is not None or payload.get("completion_tokens") is not None or payload.get("total_tokens") is not None
    ):
        prompt: int | None = _as_int(payload.get("prompt_tokens")) or 0
        completion: int | None = _as_int(payload.get("completion_tokens")) or 0
        total = _as_int(payload.get("total_tokens"))
        if total is None:
            total = (prompt or 0) + (completion or 0)
        if prompt == 0 and completion == 0 and total == 0:
            return None
        out: dict[str, Any] = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "source": str(payload.get("source") or "normalized"),
        }
        cost = _as_float(payload.get("cost_usd") if payload.get("cost_usd") is not None else payload.get("cost"))
        if cost is not None:
            out["cost_usd"] = cost
        return out

    # Nested usage object (OpenAI chat, Anthropic, OpenRouter, Responses).
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
    if usage is None and isinstance(payload.get("response"), dict):
        nested = payload.get("response") or {}
        if isinstance(nested.get("usage"), dict):
            usage = nested.get("usage")
            payload = nested

    # OpenAI Responses stream: response.completed carries response.usage
    if usage is None and str(payload.get("type") or "") in {"response.completed", "response.done"}:
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        if isinstance(response.get("usage"), dict):
            usage = response.get("usage")

    # Anthropic stream message_delta / message_start
    if usage is None and isinstance(payload.get("usage"), dict):
        usage = payload.get("usage")
    if usage is None and isinstance(payload.get("message"), dict) and isinstance((payload.get("message") or {}).get("usage"), dict):
        usage = (payload.get("message") or {}).get("usage")

    prompt = None
    completion = None
    total = None
    reported_cost = None
    source = "unknown"

    if isinstance(usage, dict):
        prompt = _as_int(usage.get("prompt_tokens"))
        if prompt is None:
            prompt = _as_int(usage.get("input_tokens"))
        completion = _as_int(usage.get("completion_tokens"))
        if completion is None:
            completion = _as_int(usage.get("output_tokens"))
        total = _as_int(usage.get("total_tokens"))
        reported_cost = _as_float(usage.get("cost"))
        if reported_cost is None:
            reported_cost = _as_float(usage.get("total_cost"))
        source = "usage"

    # Ollama generate
    if prompt is None and completion is None:
        prompt = _as_int(payload.get("prompt_eval_count"))
        completion = _as_int(payload.get("eval_count"))
        if prompt is not None or completion is not None:
            source = "ollama"

    if prompt is None and completion is None and total is None:
        return None

    prompt = prompt or 0
    completion = completion or 0
    if total is None:
        total = prompt + completion
    if prompt == 0 and completion == 0 and total == 0:
        return None

    out = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "source": source,
    }
    if reported_cost is not None:
        out["cost_usd"] = reported_cost
    return out


def _price_for_model(model: str | None) -> tuple[float, float] | None:
    name = str(model or "").strip().lower()
    if not name:
        return None
    if name in MODEL_PRICE_PER_1M:
        return MODEL_PRICE_PER_1M[name]
    # Strip date suffixes / vendor prefixes loosely.
    for key, price in MODEL_PRICE_PER_1M.items():
        if name.endswith("/" + key) or name.startswith(key) or key in name:
            return price
    return None


def estimate_cost_usd(
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    reported_cost: float | None = None,
) -> float | None:
    if reported_cost is not None:
        try:
            return round(float(reported_cost), 6)
        except (TypeError, ValueError):
            pass
    price = _price_for_model(model)
    if not price:
        return None
    input_rate, output_rate = price
    cost = (max(0, int(prompt_tokens)) / 1_000_000.0) * input_rate + (max(0, int(completion_tokens)) / 1_000_000.0) * output_rate
    return round(cost, 6)


def enrich_usage(usage: dict[str, Any] | None, *, model: str | None = None) -> dict[str, Any] | None:
    if not usage:
        return None
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    reported = _as_float(usage.get("cost_usd"))
    cost = estimate_cost_usd(model, prompt, completion, reported_cost=reported)
    out = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "source": str(usage.get("source") or "normalized"),
    }
    if model:
        out["model"] = model
    if cost is not None:
        out["cost_usd"] = cost
        out["cost_estimated"] = reported is None
    return out


def usage_from_result(result: dict[str, Any] | None, *, model: str | None = None) -> dict[str, Any] | None:
    """Pull usage from an adapter result (usage field or raw payload)."""
    if not isinstance(result, dict):
        return None
    raw_selected: Any = result.get("selected_provider")
    selected = raw_selected if isinstance(raw_selected, dict) else {}
    model_name = model or str(selected.get("model") or result.get("model") or "") or None
    direct = normalize_usage(result.get("usage")) if isinstance(result.get("usage"), dict) else None
    if direct is None:
        direct = normalize_usage(result.get("raw"))
    return enrich_usage(direct, model=model_name)


# Rough tokenizer stand-in used only for memory-pack vs corpus comparisons.
# Real tokenizer counts differ by model; 4 chars/token is the usual English/code heuristic.
CHARS_PER_TOKEN = 4


def estimate_tokens_from_chars(chars: int) -> int:
    chars = max(0, int(chars or 0))
    if chars <= 0:
        return 0
    return max(1, int(round(chars / float(CHARS_PER_TOKEN))))


def estimate_tokens_from_text(text: str | None) -> int:
    return estimate_tokens_from_chars(len(str(text or "")))


def memory_token_economy(
    *,
    corpus_chars: int,
    packed_chars: int,
    corpus_nodes: int = 0,
    packed_nodes: int = 0,
    pack_budget_chars: int = 0,
) -> dict[str, Any]:
    """Prompt-input savings versus dumping the whole active memory corpus.

    This is not “tokens vs last month” and it ignores completion tokens,
    chat history, tools, and attached files — those sit on top of the pack.
    """
    corpus_tokens = estimate_tokens_from_chars(corpus_chars)
    packed_tokens = estimate_tokens_from_chars(packed_chars)
    saved_tokens = max(0, corpus_tokens - packed_tokens)
    saved_pct = 0.0 if corpus_tokens <= 0 else round(100.0 * saved_tokens / corpus_tokens, 1)
    return {
        "baseline": "full_memory_dump",
        "corpus_nodes": max(0, int(corpus_nodes or 0)),
        "corpus_chars": max(0, int(corpus_chars or 0)),
        "corpus_tokens": corpus_tokens,
        "packed_nodes": max(0, int(packed_nodes or 0)),
        "packed_chars": max(0, int(packed_chars or 0)),
        "packed_tokens": packed_tokens,
        "saved_tokens": saved_tokens,
        "saved_pct": saved_pct,
        "pack_budget_chars": max(0, int(pack_budget_chars or 0)),
        "chars_per_token": CHARS_PER_TOKEN,
    }
