from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

CHAT_MEMORY_MODES = ("off", "strict", "aggressive")
DEFAULT_CHAT_MEMORY_MODE = "strict"
DEFAULT_CHAT_CANDIDATE_TTL_DAYS = 7

# Exact / near-exact greetings and acknowledgements (user message only).
_CHITCHAT_EXACT = {
    "hi", "hello", "hey", "yo", "sup", "hola",
    "привет", "привіт", "здравствуй", "здравствуйте", "хай", "хелло",
    "שלום", "היי",
    "ok", "okay", "k", "kk", "thanks", "thank you", "thx", "ty",
    "спасибо", "дякую", "ок", "хорошо", "понял", "поняла", "ясно",
    "да", "нет", "ага", "угу", "лол", "lol", "cool", "nice",
    "how are you", "how's it going", "whats up", "what's up",
    "как дела", "как ты", "що робиш", "что делаешь",
    "good morning", "good evening", "доброе утро", "добрый вечер",
    "bye", "пока", "до свидания",
}

_CHITCHAT_PREFIXES = (
    "hi ", "hello ", "hey ", "привет", "привіт", "как дела", "how are you",
    "what's up", "whats up", "доброе ", "добрый ",
)

# Durable project-memory signals (user or assistant).
_DURABLE_PATTERNS = (
    r"\bdecision\b", r"\bdecided\b", r"\badr\b", r"\barchitecture\b",
    r"\brequirement\b", r"\bconstraint\b", r"\brule\b", r"\bpolicy\b",
    r"\bremember\b", r"\bimportant\b", r"\bmust\b", r"\bnever\b", r"\balways\b",
    r"\bbug\b", r"\bfix\b", r"\bimplement\b", r"\bapi\b", r"\bschema\b",
    r"\bmigration\b", r"\bdeadline\b", r"\bsla\b", r"\bsecurity\b",
    r"решен", r"архитект", r"требован", r"огранич", r"правил", r"запом",
    r"важн", r"должн", r"нельзя", r"всегда", r"баг", r"исправ", r"реализ",
    r"we (?:will|should|must|decided)",
    r"use (?:redis|postgres|kafka|grpc|rest|oauth)",
    r"(?:выбрали|используем|нельзя|запрещено)",
)

_FACT_LINE_PATTERNS = (
    re.compile(r"(?im)^(?:[-*•]|\d+[.)])\s*(.{20,240})$"),
    re.compile(
        r"(?i)\b(?:we (?:will|should|must|decided to)|decision:|rule:|constraint:|"
        r"requirement:|важно:|решение:|правило:|ограничение:)\s*(.{15,240})"
    ),
)

_DURABLE_RE = [re.compile(pattern, re.I) for pattern in _DURABLE_PATTERNS]
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(slots=True)
class ChatMemoryVerdict:
    keep: bool
    reason: str
    mode: str
    score: float = 0.0
    facts: list[dict[str, str]] = field(default_factory=list)


def normalize_chat_memory_mode(value: Any) -> str:
    mode = str(value or DEFAULT_CHAT_MEMORY_MODE).strip().lower()
    if mode in {"none", "disabled", "false", "0"}:
        return "off"
    if mode in {"loose", "all", "lenient"}:
        return "aggressive"
    if mode not in CHAT_MEMORY_MODES:
        return DEFAULT_CHAT_MEMORY_MODE
    return mode


def _normalize_user(text: str) -> str:
    cleaned = _WHITESPACE_RE.sub(" ", str(text or "").strip().lower())
    return cleaned.strip("!.?…,~ ")


def is_chitchat_user_message(user_text: str) -> bool:
    lowered = _normalize_user(user_text)
    if not lowered:
        return True
    if lowered in _CHITCHAT_EXACT:
        return True
    if len(lowered) <= 28 and any(lowered.startswith(prefix.strip()) for prefix in _CHITCHAT_PREFIXES):
        # Short greeting-like openers without durable content.
        if not has_durable_signal(lowered):
            return True
    # Pure emoji / punctuation
    if len(re.sub(r"[\W_]+", "", lowered, flags=re.U)) < 3:
        return True
    return False


def has_durable_signal(text: str) -> bool:
    haystack = str(text or "")
    return any(pattern.search(haystack) for pattern in _DURABLE_RE)


def chat_turn_score(user_text: str, assistant_text: str) -> float:
    """Heuristic salience 0..1 for durable project memory."""
    user = str(user_text or "").strip()
    assistant = str(assistant_text or "").strip()
    if is_chitchat_user_message(user):
        return 0.0
    score = 0.0
    combined = f"{user}\n{assistant}"
    if has_durable_signal(user):
        score += 0.45
    if has_durable_signal(assistant):
        score += 0.25
    if len(user) >= 80:
        score += 0.15
    if len(user) >= 160:
        score += 0.1
    # Long assistant alone should not push chitchat over the line.
    if len(assistant) >= 400 and has_durable_signal(combined):
        score += 0.1
    if re.search(r"(?i)\b(?:must|never|always|должн|нельзя|всегда)\b", combined):
        score += 0.1
    return max(0.0, min(1.0, score))


def extract_durable_facts(user_text: str, assistant_text: str, *, limit: int = 3) -> list[dict[str, str]]:
    """Pull short durable facts instead of storing the full turn."""
    facts: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(text: str, fact_type: str, source: str) -> None:
        cleaned = _WHITESPACE_RE.sub(" ", str(text or "").strip())
        if len(cleaned) < 18:
            return
        key = cleaned.lower()
        if key in seen:
            return
        if is_chitchat_user_message(cleaned):
            return
        if not has_durable_signal(cleaned) and source != "user_explicit":
            # Keep explicit user statements that look like directives even without keywords
            # only when long enough and not chitchat.
            if source != "user_statement" or len(cleaned) < 40:
                return
        seen.add(key)
        facts.append({
            "type": fact_type,
            "text": cleaned[:400],
            "source": source,
        })

    user = str(user_text or "").strip()
    assistant = str(assistant_text or "").strip()

    # Prefer explicit user directives as first-class facts.
    if user and not is_chitchat_user_message(user) and (has_durable_signal(user) or len(user) >= 60):
        add(user, classify_fact_type(user), "user_statement")

    for pattern in _FACT_LINE_PATTERNS:
        for match in pattern.finditer(assistant):
            snippet = match.group(1) if match.lastindex else match.group(0)
            add(snippet, classify_fact_type(snippet), "assistant_extract")
            if len(facts) >= limit:
                return facts[:limit]

    # Fallback: one compact synthesis if we already know the turn is durable.
    if not facts and has_durable_signal(f"{user}\n{assistant}"):
        compact = f"{user[:160]} → {assistant[:200]}".strip(" →")
        add(compact, classify_fact_type(compact), "turn_compact")

    return facts[:limit]


def classify_fact_type(text: str) -> str:
    haystack = str(text or "").lower()
    if any(word in haystack for word in ("decision", "decided", "решен", "adr", "architecture", "архитект", "выбрал")):
        return "Decision"
    if any(word in haystack for word in ("requirement", "требован", "must", "должн")):
        return "Requirement"
    if any(word in haystack for word in ("constraint", "огранич", "rule", "правил", "never", "нельзя", "forbidden")):
        return "Constraint"
    return "Lesson"


def evaluate_chat_turn(user_text: str, assistant_text: str, *, mode: str = DEFAULT_CHAT_MEMORY_MODE) -> ChatMemoryVerdict:
    mode = normalize_chat_memory_mode(mode)
    user = str(user_text or "").strip()
    assistant = str(assistant_text or "").strip()
    if mode == "off":
        return ChatMemoryVerdict(False, "chat_memory_mode=off", mode)
    if not user and not assistant:
        return ChatMemoryVerdict(False, "empty_turn", mode)
    if is_chitchat_user_message(user):
        return ChatMemoryVerdict(False, "chitchat", mode, score=0.0)

    score = chat_turn_score(user, assistant)
    facts = extract_durable_facts(user, assistant)

    if mode == "strict":
        # Strict: need durable signal on the USER side or extracted facts with decent score.
        if score < 0.45 and not facts:
            return ChatMemoryVerdict(False, "low_salience", mode, score=score)
        if not facts and not has_durable_signal(user):
            return ChatMemoryVerdict(False, "no_user_durable_signal", mode, score=score)
        if not facts:
            facts = extract_durable_facts(user, assistant) or [{
                "type": classify_fact_type(user),
                "text": user[:400],
                "source": "user_statement",
            }]
        return ChatMemoryVerdict(True, "strict_keep", mode, score=score, facts=facts)

    # aggressive: keep more, but still block chitchat; require either length or signal
    if score < 0.25 and len(user) < 100:
        return ChatMemoryVerdict(False, "aggressive_low_value", mode, score=score)
    if not facts:
        facts = [{
            "type": classify_fact_type(f"{user} {assistant}"),
            "text": f"User: {user[:220]}\nAssistant: {assistant[:280]}".strip(),
            "source": "turn_compact",
        }]
    return ChatMemoryVerdict(True, "aggressive_keep", mode, score=score, facts=facts)


def default_chat_memory_settings() -> dict[str, Any]:
    return {
        "chat_memory_mode": DEFAULT_CHAT_MEMORY_MODE,
        "chat_candidate_ttl_days": DEFAULT_CHAT_CANDIDATE_TTL_DAYS,
        "chat_store_facts_only": True,
    }
