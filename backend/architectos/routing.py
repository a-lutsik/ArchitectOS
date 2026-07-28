from __future__ import annotations

import re
from typing import Any

READY_STATUSES = {"configured", "ok", "available", "ready", "fallback"}

DEFAULT_WEIGHTS = {"quality": 0.4, "cost": 0.3, "speed": 0.2, "availability": 0.1}

DEFAULT_STRATEGY = "balanced"

STRATEGY_WEIGHTS = {
    "balanced": DEFAULT_WEIGHTS,
    "quality": {"quality": 0.62, "cost": 0.1, "speed": 0.1, "availability": 0.18},
    "cost": {"quality": 0.18, "cost": 0.56, "speed": 0.12, "availability": 0.14},
    "speed": {"quality": 0.2, "cost": 0.14, "speed": 0.52, "availability": 0.14},
}

# Relative provider profiles. cost/latency are normalized 0..1 where lower is better,
# quality 0..1 where higher is better. strengths bias role-aware routing.
PROVIDER_PROFILES: dict[str, dict[str, Any]] = {
    "local-memory": {"cost": 0.0, "latency": 0.05, "quality": 0.35, "strengths": {"docs", "memory"}},
    "ollama": {"cost": 0.02, "latency": 0.4, "quality": 0.6, "strengths": {"code", "docs"}},
    "openai": {"cost": 0.55, "latency": 0.35, "quality": 0.88, "strengths": {"review", "code", "architecture"}},
    "azure-openai": {"cost": 0.55, "latency": 0.35, "quality": 0.88, "strengths": {"review", "code", "architecture"}},
    "anthropic": {"cost": 0.6, "latency": 0.4, "quality": 0.9, "strengths": {"architecture", "review", "docs"}},
    "openrouter": {"cost": 0.4, "latency": 0.45, "quality": 0.82, "strengths": {"code", "review"}},
    "codex-cli": {"cost": 0.5, "latency": 0.55, "quality": 0.86, "strengths": {"code"}},
    "claude-code": {"cost": 0.62, "latency": 0.6, "quality": 0.9, "strengths": {"architecture", "code", "review"}},
    "gemini-cli": {"cost": 0.35, "latency": 0.5, "quality": 0.8, "strengths": {"docs", "review"}},
}

DEFAULT_PROFILE = {"cost": 0.5, "latency": 0.5, "quality": 0.6, "strengths": set()}

_ROLE_KEYWORDS = {
    "code": ("code", "implement", "bug", "function", "refactor", "test", "compile", "stack trace", "exception"),
    "architecture": ("architecture", "design", "adr", "decision", "trade-off", "tradeoff", "scal", "pattern", "diagram"),
    "docs": ("doc", "documentation", "readme", "explain", "summary", "write-up", "guide"),
    "review": ("review", "risk", "security", "audit", "quality", "correctness", "regression"),
}


def classify_role(text: str) -> str:
    lowered = (text or "").lower()
    scores: dict[str, int] = {}
    for role, keywords in _ROLE_KEYWORDS.items():
        scores[role] = sum(1 for keyword in keywords if keyword in lowered)
    best = max(scores, key=lambda role: scores[role])
    return best if scores[best] else "code"


class RouterPolicy:
    """Scores providers by cost, speed, quality, and availability for auto routing."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = dict(settings or {})

    def weights(self) -> dict[str, float]:
        strategy = str(self.settings.get("strategy") or DEFAULT_STRATEGY)
        base = dict(STRATEGY_WEIGHTS.get(strategy, DEFAULT_WEIGHTS))
        for key, value in dict(self.settings.get("weights") or {}).items():
            if key in base:
                try:
                    base[key] = max(0.0, float(value))
                except (TypeError, ValueError):
                    continue
        total = sum(base.values()) or 1.0
        return {key: value / total for key, value in base.items()}

    def profile(self, provider: dict[str, Any]) -> dict[str, Any]:
        base = dict(PROVIDER_PROFILES.get(str(provider.get("id")), DEFAULT_PROFILE))
        base["strengths"] = set(base.get("strengths") or set())
        for key in ("cost", "latency", "quality"):
            raw = provider.get(f"route_{key}")
            if raw is None and key == "latency":
                raw = provider.get("route_speed")
            if raw is not None:
                try:
                    base[key] = max(0.0, min(1.0, float(raw)))
                except (TypeError, ValueError):
                    pass
        extra = provider.get("route_strengths")
        if isinstance(extra, (list, tuple, set)):
            base["strengths"] = base["strengths"] | {str(item).lower() for item in extra}
        return base

    def availability(self, provider: dict[str, Any]) -> float:
        if not provider.get("enabled"):
            return 0.0
        status = str(provider.get("status") or "").lower()
        if status in READY_STATUSES:
            return 1.0
        last_check = provider.get("last_check") or {}
        if str(last_check.get("status") or "").lower() in READY_STATUSES:
            return 1.0
        return 0.35

    def score_provider(self, provider: dict[str, Any], weights: dict[str, float], role: str | None = None) -> dict[str, Any]:
        profile = self.profile(provider)
        availability = self.availability(provider)
        quality = float(profile["quality"])
        if role and role in profile["strengths"]:
            quality = min(1.0, quality + 0.08)
        components = {
            "quality": quality,
            "cost": 1.0 - float(profile["cost"]),
            "speed": 1.0 - float(profile["latency"]),
            "availability": availability,
        }
        score = sum(components[key] * weights.get(key, 0.0) for key in components)
        return {
            "provider_id": provider.get("id"),
            "label": provider.get("label") or provider.get("id"),
            "score": round(score, 4),
            "components": {key: round(value, 4) for key, value in components.items()},
            "cost": round(float(profile["cost"]), 3),
            "latency": round(float(profile["latency"]), 3),
            "quality": round(quality, 3),
            "availability": round(availability, 3),
            "enabled": bool(provider.get("enabled")),
            "matches_role": bool(role and role in profile["strengths"]),
        }

    def rank(self, providers: list[dict[str, Any]], role: str | None = None) -> list[dict[str, Any]]:
        weights = self.weights()
        scored = [self.score_provider(provider, weights, role) for provider in providers]
        scored.sort(key=lambda item: (item["availability"] > 0, item["score"]), reverse=True)
        return scored

    def select(
        self,
        providers: list[dict[str, Any]],
        provider_id: str | None,
        role: str | None = None,
    ) -> dict[str, Any]:
        by_id = {provider["id"]: provider for provider in providers if provider.get("id")}
        weights = self.weights()
        strategy = str(self.settings.get("strategy") or DEFAULT_STRATEGY)
        if provider_id and provider_id != "auto":
            provider = by_id.get(provider_id) or {"id": provider_id, "label": provider_id, "enabled": True}
            decision = {
                "mode": "explicit",
                "strategy": strategy,
                "weights": weights,
                "role": role,
                "selected": provider_id,
                "reason": f"Provider {provider_id} was requested explicitly.",
                "ranked": self.rank(providers, role)[:6],
            }
            return {"provider": provider, "decision": decision}

        ranked = self.rank(providers, role)
        best = next((item for item in ranked if item["availability"] > 0 and item["enabled"]), None)
        if best:
            provider = by_id[best["provider_id"]]
            reason = (
                f"Auto-selected {best['label']} (score {best['score']}) using '{strategy}' strategy: "
                f"quality {best['quality']}, cost {best['cost']}, latency {best['latency']}"
                + (f", matches {role} role." if best["matches_role"] else ".")
            )
            decision = {
                "mode": "auto",
                "strategy": strategy,
                "weights": weights,
                "role": role,
                "selected": best["provider_id"],
                "reason": reason,
                "ranked": ranked[:6],
            }
            return {"provider": provider, "decision": decision}

        fallback = {"id": "local-memory", "label": "Local Memory", "provider_type": "local", "enabled": True, "status": "fallback", "model": ""}
        decision = {
            "mode": "fallback",
            "strategy": strategy,
            "weights": weights,
            "role": role,
            "selected": "local-memory",
            "reason": "No enabled/available provider found. Falling back to local memory.",
            "ranked": ranked[:6],
        }
        return {"provider": by_id.get("local-memory", fallback), "decision": decision}
