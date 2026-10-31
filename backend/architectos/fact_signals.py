"""What makes a dialog line worth remembering — the capture criteria in one place.

Both the extractor (``chat_memory``) and the capture policy (``memory_capture``)
read these tables, so the Settings hint, the review card and the code agree.
Modelled on how Claude Code's auto memory sorts its notes: ``user`` (who the
person is and how they like to work), ``feedback`` (corrections and confirmed
approaches), ``project`` (decisions and context not derivable from the code)
and ``reference`` (where to look outside the repository).

Positive signals raise a fact's confidence and pick its kind; negative signals
(derivable from the code, transient) lower it so the policy sends it to review
rather than writing it at once. Russian and English, stdlib ``re`` only.
"""
from __future__ import annotations

import re
from typing import Any

FACT_KINDS = ("user", "feedback", "project", "reference")

_FLAGS = re.IGNORECASE


def _compile(patterns: tuple[str, ...]) -> list[re.Pattern[str]]:
    return [re.compile(pattern, _FLAGS) for pattern in patterns]


# "No, not like that" — the person corrected an approach. Highest-value signal.
CORRECTION_PATTERNS = _compile((
    r"^(?:no|nope|нет|неа)[,!.:—-]\s*(?!thanks|thank you|thx|спасибо|не надо,? спасибо)\S.{5,}",
    r"\bnot like (?:that|this)\b", r"\bthat'?s (?:wrong|not right|incorrect)\b",
    r"\b(?:don'?t|do not) (?:use|add|run|commit|push|write|do|call|touch)\b",
    r"\binstead of\b", r"\brather than\b", r"\bstop (?:doing|using|adding|writing)\b",
    r"\buse \S+,? not \S+", r"\bshould have\b", r"\bactually,\s", r"\bwrong\b",
    r"\bне так\b", r"\bне надо\b", r"\bне нужно\b", r"\bвместо\b", r"\bбольше не\b",
    r"\bперестань", r"\bнеправильно\b", r"\bне (?:используй|добавляй|коммить|пиши|делай|трогай)",
    r"\bа не\b", r"\bисправь\b", r"\bнадо (?:было|иначе|по-другому)\b",
))

# "Yes, keep it that way" — an approach the person confirmed.
CONFIRMATION_PATTERNS = _compile((
    r"\byes,? (?:keep|leave|exactly|that'?s (?:right|it)|do it (?:that|this) way)\b",
    r"\bthat'?s (?:right|correct|exactly it)\b", r"\bkeep it (?:that way|like this|as is)\b",
    r"\b(?:good|great|perfect),? keep\b", r"\bexactly (?:like|what) (?:that|this|i)\b",
    r"\bда,? (?:так и )?(?:оставь|оставляй|делай)\b", r"\bтак и (?:оставь|делай|надо)\b",
    r"\bименно так\b", r"\b(?:верно|правильно),? (?:так|оставь|делай)\b",
))

# First-person preferences and facts about the person → kind ``user`` (global scope).
USER_PATTERNS = _compile((
    r"\bi (?:prefer|like|usually|always|never|want|tend to|work (?:as|with|in)|am|'m)\b",
    r"\bmy (?:role|style|preference|preferences|workflow|setup|timezone|name)\b",
    r"\bcall me\b", r"\b(?:answer|reply|respond|write)(?: to me)? in (?:russian|english|ukrainian|hebrew|german)\b",
    r"\bя (?:предпочитаю|люблю|обычно|всегда|никогда|хочу|привык|привыкла|работаю|пишу)\b",
    r"\bмне (?:удобнее|нравится|привычнее|проще)\b", r"\bмо[яйиё] (?:роль|стиль|привычк|рабоч|часовой пояс|имя)",
    r"\b(?:отвечай|пиши|говори)(?: мне)? (?:по-русски|по-английски|на русском|на английском|на украинском)\b",
    r"\bобращайся ко мне\b", r"\bзови меня\b",
))

# "Prefer" in any person: a convention, slightly more trustworthy than a plain statement.
PREFERENCE_VERB_PATTERNS = _compile((r"\bprefer", r"\bпредпочита"))

# Where to look outside the repository → kind ``reference``.
REFERENCE_PATTERNS = _compile((
    r"https?://\S+",
    r"\b(?:jira|confluence|notion|grafana|kibana|datadog|sentry|trello|asana|linear|figma|miro|youtrack|gitlab|github issues?)\b",
    r"\b(?:wiki|dashboard|tracker|runbook|board)\b", r"\b(?:slack|teams|telegram|discord) (?:channel|канал)",
    r"\bв канале\b", r"\b(?:трекер|дашборд|вики|ранбук|доск[аеи])\b",
))

# "Remember: …" — the person asked for this to be stored. Written at once in
# auto mode whatever the kind (Claude Code does the same for "remember that").
EXPLICIT_SAVE_PATTERNS = _compile((
    r"\bremember\b", r"\bkeep in mind\b", r"\bзапомни", r"\bимей в виду\b", r"\bсохрани в память\b",
))

# Explicit markers people use when they mean "this is a rule / decision".
EXPLICIT_MARKER_PATTERNS = _compile((
    r"\b(?:decision|rule|constraint|requirement|policy|important|note)\s*:",
    r"\b(?:решение|правило|ограничение|требование|политика|важно|заметка)\s*:",
    r"\badr\b",
))

IMPERATIVE_PATTERNS = _compile((
    r"\b(?:always|never|must(?: not)?|only|forbidden)\b",
    r"\b(?:всегда|никогда|нельзя|должн[аоы]?|обязательно|только|запрещено)\b",
))

# Derivable from the code or git: Claude skips these and so do we (negative).
DERIVABLE_PATTERNS = _compile((
    r"\b(?:is |are )?(?:defined|located|declared|implemented|found) in\b",
    r"\b(?:находится|находятся|объявлен|объявлена|определ[её]н|определена|реализован|реализована|лежит) в\b",
    r"\bfixed (?:a |the )?(?:typo|bug|lint|import|test)\b", r"\bисправил[аи]? (?:опечатк|баг|линт|импорт|тест)",
    r"\btraceback\b", r"\bline \d+\b", r"\bстрок[аеи] \d+\b", r"```",
    r"^\s*(?:def|class|function|const|let|var|import|from|return)\s",
    r"\.(?:py|js|ts|tsx|go|rs|java|kt)\b[^.]{0,80}\b(?:contains|returns|calls|imports|содержит|возвращает|вызывает|импортирует)\b",
))

# True only for the moment: not worth a durable memory (negative).
TRANSIENT_PATTERNS = _compile((
    r"\bfor now\b", r"\btemporarily\b", r"\bthis time\b", r"\bright now\b", r"\bjust this once\b",
    r"\bпока что\b", r"\bвременно\b", r"\bв этот раз\b", r"\bна этот раз\b", r"\bпока не\b",
))

_SIGNAL_TABLE = {
    "correction": CORRECTION_PATTERNS,
    "confirmation": CONFIRMATION_PATTERNS,
    "user": USER_PATTERNS,
    "preference": PREFERENCE_VERB_PATTERNS,
    "reference": REFERENCE_PATTERNS,
    "marker": EXPLICIT_MARKER_PATTERNS,
    "explicit_save": EXPLICIT_SAVE_PATTERNS,
    "imperative": IMPERATIVE_PATTERNS,
    "derivable": DERIVABLE_PATTERNS,
    "transient": TRANSIENT_PATTERNS,
}


def detect_signals(text: str) -> dict[str, bool]:
    haystack = " ".join(str(text or "").split())
    return {name: any(pattern.search(haystack) for pattern in patterns) for name, patterns in _SIGNAL_TABLE.items()}


def has_capture_signal(text: str) -> bool:
    """A line the extractor must keep even without the classic durable keywords.

    Corrections, confirmations, first-person preferences and references are
    exactly what keyword lists miss ("use pnpm, not npm" has no keyword).
    """
    signals = detect_signals(text)
    return bool(signals["correction"] or signals["confirmation"] or signals["user"] or signals["reference"])


def classify_fact_kind(text: str, signals: dict[str, bool] | None = None) -> str:
    signals = signals or detect_signals(text)
    if signals["user"]:
        return "user"
    if signals["correction"] or signals["confirmation"]:
        return "feedback"
    if signals["reference"]:
        return "reference"
    return "project"


_SOURCE_BASE = {
    "user_explicit": 0.80,
    "memory_add_split": 0.80,
    "user_line": 0.72,
    "user_statement": 0.72,
    "assistant_extract": 0.62,
    "turn_compact": 0.55,
}


def fact_confidence(text: str, source: str, signals: dict[str, bool] | None = None) -> float:
    """0.5–0.95: how sure we are this line is a durable, correct memory.

    Who said it matters most (the person's own words beat text mined from a
    reply); explicit markers, imperatives, corrections and preferences add;
    code-derivable or transient wording subtracts.
    """
    signals = signals or detect_signals(text)
    score = _SOURCE_BASE.get(str(source or ""), 0.62)
    if signals["marker"]:
        score += 0.08
    if signals["explicit_save"]:
        score += 0.10
    if signals["imperative"]:
        score += 0.05
    if signals["correction"] or signals["confirmation"]:
        score += 0.06
    if signals["preference"] or signals["user"]:
        score += 0.05
    length = len(str(text or "").strip())
    if 40 <= length <= 300:
        score += 0.04
    if signals["derivable"]:
        score -= 0.15
    if signals["transient"]:
        score -= 0.10
    return round(max(0.5, min(0.95, score)), 2)


def signal_names(signals: dict[str, bool]) -> list[str]:
    return [name for name, hit in signals.items() if hit]


def describe_fact(text: str, source: str) -> dict[str, Any]:
    """kind, confidence and the signals behind them, for one extracted fact."""
    signals = detect_signals(text)
    return {
        "kind": classify_fact_kind(text, signals),
        "confidence": fact_confidence(text, source, signals),
        "signals": signal_names(signals),
    }
