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
- ``enrich`` — the same subject, and the incoming text keeps every content
  word, number and identifier of the stored one while adding more. No new
  node; the stored text is replaced by the fuller one (old text kept in the
  dedup event, undoable). The reverse — a stored fact that already covers the
  incoming one — is a ``duplicate``.
- ``new`` — anything else; ``near_id``/``near_score`` still report the closest
  lexical match so callers can flag a possible duplicate.

Numbers, negation and identifiers are protected: facts that differ in them are
never folded together. This module is stdlib-only and deterministic; the
optional semantic neighbours (embeddings) and LLM judge for the gray zone are
orchestrated by the ingestion service on top of ``gray_candidates``.
"""
from __future__ import annotations

import hashlib
import json
import math
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


# Bump when fact_features / the serialized format changes: the storage layer
# rebuilds every index row written with an older version on startup.
DEDUP_INDEX_VERSION = 1


def source_hash(label: str, text: str) -> str:
    """Identity of the text an index row was computed from."""
    return hashlib.sha1(f"{label}\x1f{text}".encode()).hexdigest()


def subject_key(features: FactFeatures) -> str:
    """Index key of the label subject: every decisive action requires equal subjects."""
    return hashlib.sha1("\x1f".join(sorted(features.label_core)).encode("utf-8")).hexdigest()[:20]


def features_to_json(features: FactFeatures) -> str:
    return json.dumps({
        "key": list(features.key),
        "label_core": sorted(features.label_core),
        "stems": sorted(features.stems),
        "numbers": sorted(features.numbers),
        "negated": features.negated,
        "identifiers": sorted(features.identifiers),
    }, ensure_ascii=False, sort_keys=True)


def features_from_json(raw: str) -> FactFeatures:
    data = json.loads(raw or "{}")
    key = list(data.get("key") or ["", ""])
    return FactFeatures(
        key=(str(key[0]), str(key[1])),
        label_core=frozenset(data.get("label_core") or ()),
        stems=frozenset(data.get("stems") or ()),
        numbers=frozenset(data.get("numbers") or ()),
        negated=bool(data.get("negated")),
        identifiers=frozenset(data.get("identifiers") or ()),
    )


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


_ACTION_RANK = {"duplicate": 4, "enrich": 3, "update": 2, "conflict": 1}

DEFAULT_MEMORY_DEDUP: dict[str, Any] = {
    # auto: apply decisions; suggest: store every fact as new and only record
    # what the gate would have done (calibration); off: no gate at all.
    "mode": "auto",
    # auto: use embedding neighbours when a real (non-hash) provider is set.
    "semantic": "auto",
    # Cosine at which a semantic neighbour is flagged as a possible duplicate.
    "semantic_flag_score": 0.86,
    # Cosine / lexical score from which a compatible neighbour goes to the judge.
    "judge_semantic_score": 0.8,
    "judge_lexical_score": 0.55,
    "llm_judge": False,
    "llm_provider_id": "",
    "llm_max_candidates": 2,
    "llm_min_confidence": 0.75,
}


def dedup_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    settings = dict(DEFAULT_MEMORY_DEDUP)
    settings.update({key: value for key, value in dict(raw or {}).items() if key in DEFAULT_MEMORY_DEDUP})
    if settings["mode"] not in {"auto", "suggest", "off"}:
        settings["mode"] = "auto"
    if settings["semantic"] not in {"auto", "off"}:
        settings["semantic"] = "auto"
    for key in ("semantic_flag_score", "judge_semantic_score", "judge_lexical_score", "llm_min_confidence"):
        try:
            settings[key] = max(0.0, min(float(settings[key]), 1.0))
        except (TypeError, ValueError):
            settings[key] = DEFAULT_MEMORY_DEDUP[key]
    try:
        settings["llm_max_candidates"] = max(0, min(int(settings["llm_max_candidates"]), 5))
    except (TypeError, ValueError):
        settings["llm_max_candidates"] = DEFAULT_MEMORY_DEDUP["llm_max_candidates"]
    settings["llm_judge"] = bool(settings["llm_judge"])
    settings["llm_provider_id"] = str(settings["llm_provider_id"] or "")
    return settings


def _set_relation(incoming: frozenset[str], existing: frozenset[str]) -> str:
    """equal | super (incoming ⊃ existing) | sub (incoming ⊂ existing) | disjoint."""
    if incoming == existing:
        return "equal"
    if incoming >= existing:
        return "super"
    if incoming <= existing:
        return "sub"
    return "disjoint"


def guards_compatible(incoming: FactFeatures, existing: FactFeatures) -> bool:
    """Whether folding the two facts could lose or flip a protected detail.

    Required before a semantic/LLM merge: same negation, and numbers and
    identifiers nested (one side may add some, neither may change them).
    """
    if incoming.negated != existing.negated:
        return False
    return (
        _set_relation(incoming.numbers, existing.numbers) != "disjoint"
        and _set_relation(incoming.identifiers, existing.identifiers) != "disjoint"
    )


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

    @staticmethod
    def node_features(node: Any) -> FactFeatures:
        return fact_features(str(node.label or ""), str(node.text or ""))

    @staticmethod
    def type_matches(stored_type: str, node_type: str) -> bool:
        # The same sentence saved once as a Lesson and once as a Decision is one
        # fact; other types (Artifact, Project, ...) only match their own type.
        return stored_type == node_type or (stored_type in _FACT_TYPE_RANK and node_type in _FACT_TYPE_RANK)

    def near_min_shared(self, stem_count: int) -> int:
        """Fewest shared stems a node needs to reach the possible-duplicate flag.

        ``near = max(J, 0.85 * containment)``. J >= t needs ``shared >= t * |incoming|``;
        the containment branch needs ``shared >= 0.847 * smaller`` with
        ``smaller >= MIN_STEMS``, i.e. at least ``MIN_STEMS`` shared stems. The
        smaller of the two bounds is therefore safe: no qualifying node is missed.
        """
        by_jaccard = math.ceil(self.NEAR_DUPLICATE_SIMILARITY * stem_count)
        return max(1, min(by_jaccard, self.MIN_STEMS))

    def classify(
        self,
        label: str,
        text: str,
        node_type: str,
        scope: str,
        project_id: str | None,
        *,
        exclude_ids: Iterable[str] = (),
    ) -> DedupDecision:
        """Relation of an incoming fact to the indexed nodes of its scope/project.

        Two indexed reads instead of a scan: duplicate/update/conflict all need
        the same label subject (one lookup by subject key), and the
        possible-duplicate flag reads stem neighbours (``near_min_shared``).
        Only active, non-structural, non-superseded nodes are indexed.
        """
        incoming = incoming_features(label, text)
        excluded = set(exclude_ids)
        best = DedupDecision()
        best_rank = 0
        subject_rows = self.repository.dedup_subject_rows(scope, project_id, subject_key(incoming))
        for row in sorted(subject_rows, key=lambda item: str(item["node_id"])):
            node_id = str(row["node_id"])
            if node_id in excluded or not self.type_matches(str(row["node_type"]), node_type):
                continue
            verdict = self.compare(incoming, features_from_json(str(row["features"])))
            if verdict.near_score > best.near_score:
                best.near_id, best.near_score = node_id, verdict.near_score
            rank = _ACTION_RANK.get(verdict.action, 0)
            if not rank:
                continue
            if verdict.action == "duplicate" and verdict.reason == "exact restatement":
                verdict.target_id, verdict.target_label = node_id, str(row["label"] or "")
                verdict.near_id, verdict.near_score = node_id, 1.0
                return verdict
            if rank > best_rank or (rank == best_rank and verdict.score > best.score):
                best.action, best.score, best.reason, best.details = (
                    verdict.action, verdict.score, verdict.reason, verdict.details,
                )
                best.target_id, best.target_label = node_id, str(row["label"] or "")
                best_rank = rank
        if incoming.stems:
            neighbours = self.repository.dedup_stem_neighbours(
                scope, project_id, sorted(incoming.stems), self.near_min_shared(len(incoming.stems)),
            )
            for row in sorted(neighbours, key=lambda item: str(item["node_id"])):
                node_id = str(row["node_id"])
                if node_id in excluded or not self.type_matches(str(row["node_type"]), node_type):
                    continue
                near = self.near_score(incoming, features_from_json(str(row["features"])))
                if near > best.near_score:
                    best.near_id, best.near_score = node_id, near
        return best

    def near_score(self, incoming: FactFeatures, existing: FactFeatures) -> float:
        left, right = incoming.stems, existing.stems
        if not left or not right:
            return 0.0
        shared = len(left & right)
        smaller = min(len(left), len(right))
        containment = shared / smaller if smaller >= self.MIN_STEMS else 0.0
        return max(shared / len(left | right), containment * 0.85)

    def compare(self, incoming: FactFeatures, existing: FactFeatures) -> DedupDecision:
        """Relation of one incoming fact to one stored fact (no repository access)."""
        if incoming.key == existing.key:
            return DedupDecision("duplicate", score=1.0, reason="exact restatement", near_score=1.0)
        left, right = incoming.stems, existing.stems
        if not left or not right:
            return DedupDecision()
        shared = len(left & right)
        jaccard = shared / len(left | right)
        near = self.near_score(incoming, existing)
        verdict = DedupDecision(near_score=near)
        if incoming.label_core != existing.label_core:
            # Different subjects: at most a near match for the possible-duplicate flag.
            return verdict
        ids = _set_relation(incoming.identifiers, existing.identifiers)
        if ids == "disjoint":
            # Different files / ids / URLs are different objects, however alike the prose.
            return verdict
        numbers = _set_relation(incoming.numbers, existing.numbers)
        if incoming.negated != existing.negated:
            if ids == "equal" and jaccard >= self.RELATION_SIMILARITY and shared >= 2:
                return DedupDecision(
                    "conflict", score=jaccard, reason="same statement with negation flipped", near_score=near,
                )
            return verdict
        if ids == "equal" and numbers == "equal" and jaccard >= 1.0 and len(left) >= self.MIN_STEMS:
            return DedupDecision("duplicate", score=1.0, reason="same content words", near_score=near)
        if min(len(left), len(right)) >= self.MIN_STEMS:
            # Nested statements: one side keeps every content word, number and
            # identifier of the other and adds more.
            if right < left and ids in {"equal", "super"} and numbers in {"equal", "super"}:
                return DedupDecision(
                    "enrich", score=jaccard, reason="same fact with added detail", near_score=near,
                    details={"added": sorted(left - right)[:20]},
                )
            if left < right and ids in {"equal", "sub"} and numbers in {"equal", "sub"}:
                return DedupDecision(
                    "duplicate", score=jaccard, reason="already covered by a fuller fact", near_score=near,
                )
        if ids != "equal" or jaccard < self.RELATION_SIMILARITY or shared < 2:
            return verdict
        if incoming.numbers and existing.numbers and numbers != "equal":
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

    def gray_candidates(
        self,
        incoming: FactFeatures,
        rows: list[dict[str, Any]],
        node_type: str,
        *,
        semantic_scores: dict[str, float],
        settings: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Undecided neighbours worth an LLM look, best first.

        A neighbour qualifies when it is of a compatible type, its protected
        details are nested (``guards_compatible``), and it is close lexically
        or semantically. Rows are dedup index rows (``node_id``, ``node_type``,
        ``label``, ``features``).
        """
        picked: list[dict[str, Any]] = []
        for row in rows:
            node_id = str(row["node_id"])
            if not self.type_matches(str(row["node_type"]), node_type):
                continue
            existing = features_from_json(str(row["features"]))
            if not guards_compatible(incoming, existing):
                continue
            lexical = self.near_score(incoming, existing)
            cosine = float(semantic_scores.get(node_id, 0.0))
            if lexical < settings["judge_lexical_score"] and cosine < settings["judge_semantic_score"]:
                continue
            picked.append({
                "node_id": node_id,
                "label": str(row["label"] or ""),
                "lexical": lexical,
                "cosine": cosine,
                "rank": max(lexical, cosine),
            })
        picked.sort(key=lambda item: (-item["rank"], item["node_id"]))
        return picked
