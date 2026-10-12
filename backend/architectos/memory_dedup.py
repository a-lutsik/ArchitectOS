"""Single dedup gate for durable memory writes.

Every path that turns text into a memory node (``add_memory``, bulk, MCP
``memory_add``, candidate promotion, chat auto-accept) asks this gate what the
incoming fact is relative to what is already stored:

- ``duplicate`` — the same fact again (identical after normalization, or the
  same content words with the same numbers, negation and identifiers). No new
  node; the existing one is reinforced.
- ``update`` — the same statement with different numbers ("cap 5000" →
  "cap 9000"). New node that SUPERSEDES the old one.
- ``conflict`` — the same statement with the negation flipped ("we use X" /
  "we do not use X"). New node plus a CONTRADICTS edge, flagged for review.
- ``new`` — anything else; ``near_id``/``near_score`` still report the closest
  lexical match so callers can flag a possible duplicate.

Numbers, negation and identifiers are protected: facts that differ in them are
never folded together. Stdlib only, deterministic; semantic (embedding / LLM)
judging of the gray zone is a later stage.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .chat_memory import _FACT_TYPE_RANK
from .memory_ingestion import DUPLICATE_STOPWORDS, _token_set
from .storage import sanitize_text

# Stripped from both ends only: inner punctuation ("5.0" vs "50") is content.
# No dashes: a leading minus is a sign ("-5 °C" vs "5 °C").
_EDGE_PUNCTUATION = " .,;:!?…\"'`«»“”„()[]"

NEGATION_WORDS = frozenset({
    "не", "нет", "никогда", "нельзя", "ни",
    "not", "no", "never", "cannot", "can't", "don't", "doesn't", "isn't", "aren't",
    "won't", "shouldn't", "mustn't", "didn't", "wasn't", "weren't", "shan't",
})

_WORD_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")
# Short upper-case words are names, not filler: "Service A" vs "Service B", "БД", "UI".
_SHORT_NAME_RE = re.compile(r"(?<!\w)[^\W\d_a-zа-яё]{1,2}(?!\w)")
_NUMBER_RE = re.compile(r"^[-+−]?v?\d+(?:[.,:/\-]\d+)*[^\W\d_%°]*[%°]?$")
_CAMEL_RE = re.compile(r"[a-zа-я][A-ZА-Я]")
_DOTTED_RE = re.compile(r"[^\W_]\.[^\W\d_]")

_RU_REFLEXIVE = ("ся", "сь")
_RU_SUFFIXES = tuple(sorted({
    "иями", "ями", "ами", "ией", "иях", "ого", "его", "ому", "ему", "ыми", "ими",
    "уем", "ует", "уют", "ешь", "ете", "ают", "яют", "ых", "их",
    "ет", "ит", "ат", "ят", "ют", "ут", "ем", "им",
    "ая", "яя", "ое", "ее", "ые", "ие", "ый", "ий", "ой", "ую", "юю",
    "ом", "ам", "ям", "ах", "ях", "ов", "ев", "ей", "ия", "ию", "ии", "ью",
    "а", "я", "о", "е", "ы", "и", "у", "ю", "ь", "й",
}, key=len, reverse=True))
_EN_SUFFIXES = (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", ""))


def normalize_fact_text(value: str) -> str:
    """Comparison key for exact restatements.

    Case, Unicode form (NFKC), ё/е, whitespace and sentence punctuation at the
    edges do not change a fact, so "Data lives in SQLite" and "data lives in
    SQLite." compare equal. Digits, signs and inner punctuation are kept.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    text = text.replace("’", "'")
    return " ".join(text.split()).strip(_EDGE_PUNCTUATION)


def stem(word: str) -> str:
    """Light inflection stripping so "хранит/хранится/хранят" share one stem.

    Not linguistics: one suffix is removed if a stem of 3+ letters remains.
    Tokens with digits or punctuation (identifiers, versions) are returned as is.
    """
    if not word.isalpha():
        return word
    if any("а" <= ch <= "я" for ch in word):
        for ending in _RU_REFLEXIVE:
            if word.endswith(ending) and len(word) - len(ending) >= 4:
                word = word[: -len(ending)]
                break
        for suffix in _RU_SUFFIXES:
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                return word[: -len(suffix)]
        return word
    for suffix, replacement in _EN_SUFFIXES:
        if suffix == "s" and word.endswith("ss"):
            continue
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)] + replacement
            break
    if word.endswith("e") and len(word) > 3:
        word = word[:-1]
    return word


def _is_number(token: str) -> bool:
    return bool(_NUMBER_RE.match(token))


def _identifier(raw: str) -> str:
    token = raw.strip(_EDGE_PUNCTUATION)
    if len(token) < 3 or _is_number(token.lower()):
        return ""
    if (
        "://" in token
        or "/" in token
        or "\\" in token
        or (token.startswith("#") and token[1:].isdigit())
        or _DOTTED_RE.search(token)
        or ("_" in token.strip("_"))
        or _CAMEL_RE.search(token)
        or ("-" in token and any(ch.isdigit() for ch in token))
    ):
        return token.lower().replace("ё", "е")
    return ""


@dataclass(frozen=True, slots=True)
class FactFeatures:
    key: tuple[str, str]
    # Label words without numbers and negation: two statements are only folded,
    # superseded or marked contradictory when they are filed under the same
    # subject, so "Service A" never swallows or retires "Service B".
    label_core: frozenset[str]
    stems: frozenset[str]
    numbers: frozenset[str]
    negated: bool
    identifiers: frozenset[str]


def fact_features(label: str, text: str) -> FactFeatures:
    combined = f"{label or ''}\n{text or ''}"
    normalized = normalize_fact_text(combined)
    numbers: set[str] = set()
    identifiers: set[str] = set()
    for raw in combined.split():
        ident = _identifier(raw)
        if ident:
            identifiers.add(ident)
            continue
        token = normalize_fact_text(raw)
        if token and _is_number(token):
            numbers.add(token.lstrip("+").replace("−", "-").replace(",", ".").removeprefix("v"))
    words = set(_WORD_RE.findall(normalized))
    negated = bool(words & NEGATION_WORDS)
    stems = {
        stem(token)
        for token in _token_set(combined, DUPLICATE_STOPWORDS)
        if token not in NEGATION_WORDS and not _is_number(token)
    }
    stems.update(f"~{word.lower()}" for word in _SHORT_NAME_RE.findall(combined))
    label_core = set()
    for raw in normalize_fact_text(label).split():
        word = raw.strip(_EDGE_PUNCTUATION)
        if word and word not in NEGATION_WORDS and not _is_number(word):
            label_core.add(stem(word))
    return FactFeatures(
        key=(normalize_fact_text(label), normalize_fact_text(text)),
        label_core=frozenset(label_core),
        stems=frozenset(stems),
        numbers=frozenset(numbers),
        negated=negated,
        identifiers=frozenset(identifiers),
    )


def incoming_features(label: str, text: str) -> FactFeatures:
    """Features of a fact about to be stored, redacted exactly like ``add_node``.

    Stored nodes hold redacted text, so comparing the raw incoming text would
    never match a fact that contains a secret.
    """
    return fact_features(sanitize_text(label)[0], sanitize_text(text)[0])


def content_salt(text: str) -> str:
    """Deterministic id salt for a second fact stored under an existing label."""
    return hashlib.sha1(normalize_fact_text(text).encode("utf-8")).hexdigest()[:12]


@dataclass(slots=True)
class DedupDecision:
    action: str = "new"
    target_id: str = ""
    target_label: str = ""
    score: float = 0.0
    reason: str = ""
    near_id: str = ""
    near_score: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": self.action}
        if self.target_id:
            payload.update({
                "target_id": self.target_id,
                "target_label": self.target_label,
                "score": round(self.score, 3),
                "reason": self.reason,
            })
        if self.near_id:
            payload["near_id"] = self.near_id
            payload["near_score"] = round(self.near_score, 3)
        if self.details:
            payload["details"] = dict(self.details)
        return payload


_ACTION_RANK = {"duplicate": 3, "update": 2, "conflict": 1}


class MemoryDedupGate:
    # Containment/Jaccard score above which a new node is flagged as a
    # possible duplicate (unchanged from the former add_memory near check).
    NEAR_DUPLICATE_SIMILARITY = 0.72
    # Content-word Jaccard needed before a number or negation difference is
    # read as an update/conflict of the same statement rather than another fact.
    RELATION_SIMILARITY = 0.8
    MIN_STEMS = 3

    def __init__(self, repository: Any) -> None:
        self.repository = repository
        self._cache: dict[str, tuple[str, FactFeatures]] = {}

    def node_features(self, node: Any) -> FactFeatures:
        stamp = f"{getattr(node, 'updated_at', '')}|{getattr(node, 'label', '')}|{len(str(getattr(node, 'text', '') or ''))}"
        cached = self._cache.get(node.id)
        if cached and cached[0] == stamp:
            return cached[1]
        if len(self._cache) > 100_000:
            self._cache.clear()
        features = fact_features(str(node.label or ""), str(node.text or ""))
        self._cache[node.id] = (stamp, features)
        return features

    @staticmethod
    def comparable(node: Any, scope: str, project_id: str | None, node_type: str) -> bool:
        if node.status != "active":
            return False
        meta = dict(node.metadata or {})
        if meta.get("structural") or meta.get("hub") or meta.get("scope_root") or meta.get("invalid_at"):
            return False
        if node.scope != scope or node.project_id != project_id:
            return False
        # The same sentence saved once as a Lesson and once as a Decision is one
        # fact; other types (Artifact, Project, ...) only match their own type.
        return node.type == node_type or (node.type in _FACT_TYPE_RANK and node_type in _FACT_TYPE_RANK)

    def classify(
        self,
        label: str,
        text: str,
        node_type: str,
        scope: str,
        project_id: str | None,
        *,
        nodes: Iterable[Any] | None = None,
        exclude_ids: Iterable[str] = (),
    ) -> DedupDecision:
        incoming = incoming_features(label, text)
        excluded = set(exclude_ids)
        best = DedupDecision()
        best_rank = 0
        for node in self.repository.list_nodes() if nodes is None else nodes:
            if node.id in excluded or not self.comparable(node, scope, project_id, node_type):
                continue
            verdict = self.compare(incoming, self.node_features(node))
            if verdict.near_score > best.near_score:
                best.near_id, best.near_score = node.id, verdict.near_score
            rank = _ACTION_RANK.get(verdict.action, 0)
            if not rank:
                continue
            if verdict.action == "duplicate" and verdict.score >= 1.0 and verdict.reason == "exact restatement":
                verdict.target_id, verdict.target_label = node.id, str(node.label or "")
                verdict.near_id, verdict.near_score = node.id, 1.0
                return verdict
            better = rank > best_rank or (rank == best_rank and verdict.score > best.score)
            if better:
                best.action, best.score, best.reason, best.details = (
                    verdict.action, verdict.score, verdict.reason, verdict.details,
                )
                best.target_id, best.target_label = node.id, str(node.label or "")
                best_rank = rank
        return best

    def compare(self, incoming: FactFeatures, existing: FactFeatures) -> DedupDecision:
        """Relation of one incoming fact to one stored fact (no repository access)."""
        if incoming.key == existing.key:
            return DedupDecision("duplicate", score=1.0, reason="exact restatement", near_score=1.0)
        left, right = incoming.stems, existing.stems
        if not left or not right:
            return DedupDecision()
        shared = len(left & right)
        jaccard = shared / len(left | right)
        smaller = min(len(left), len(right))
        containment = shared / smaller if smaller >= self.MIN_STEMS else 0.0
        near = max(jaccard, containment * 0.85)
        verdict = DedupDecision(near_score=near)
        if incoming.identifiers and existing.identifiers and incoming.identifiers != existing.identifiers:
            # Different files / ids / URLs are different objects, however alike the prose.
            return verdict
        if incoming.label_core != existing.label_core:
            # Different subjects: at most a near match for the possible-duplicate flag.
            return verdict
        if jaccard >= 1.0 and len(left) >= self.MIN_STEMS:
            if incoming.numbers == existing.numbers and incoming.negated == existing.negated:
                return DedupDecision("duplicate", score=1.0, reason="same content words", near_score=near)
        if jaccard < self.RELATION_SIMILARITY or shared < 2:
            return verdict
        if incoming.negated != existing.negated:
            return DedupDecision(
                "conflict", score=jaccard, reason="same statement with negation flipped", near_score=near,
            )
        if incoming.numbers and existing.numbers and incoming.numbers != existing.numbers:
            return DedupDecision(
                "update",
                score=jaccard,
                reason="same statement with changed values",
                near_score=near,
                details={
                    "values_from": sorted(existing.numbers - incoming.numbers),
                    "values_to": sorted(incoming.numbers - existing.numbers),
                },
            )
        return verdict
