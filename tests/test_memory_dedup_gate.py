"""Stage 1 dedup gate: one classifier for every memory write path."""
from __future__ import annotations

import io
import itertools
import json
import re
import tempfile
import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Any
from unittest import mock

from backend.architectos import memory_dedup
from backend.architectos.candidate_identity import origin_key_from_memory_node
from backend.architectos.mcp_server import MemoryMCPServer
from backend.architectos.memory_dedup import MemoryDedupGate, content_salt, fact_features, stem
from backend.architectos.models import MemoryNode, owned_by, stable_id
from backend.architectos.sdk import ArchitectOS
from backend.architectos.server import ArchitectOSHandler
from backend.architectos.service import ArchitectOSService


# Pairs of atoms with the verdict and the auto-capture outcome the design requires
# (docs/notes/auto-capture-architect.md, Q1). ``a`` is stored, ``b`` is incoming.
DEDUP_PAIRS: list[dict[str, Any]] = [
    json.loads(line)
    for line in (Path(__file__).parent / "data" / "dedup_pairs.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
ATOM_TEMPLATES = ("chat_session_atom", "mcp_turn_atom")
_PAIR_PROJECTS = itertools.count()
_FILLER_WORD = {"en": "zebra", "ru": "зебра"}
_ADDED_WORDS = {
    "en": " and the same holds for every other deployment environment",
    "ru": " и то же самое верно для каждого другого окружения развертывания",
}
_NEGATION_INSERT = {"en": "do not", "ru": "не"}


def with_single_number(text: str, number: int) -> str:
    """``text`` with exactly one number: its own one replaced, or ``(number)`` appended."""
    if len(re.findall(r"\d+", text)) == 1:
        return re.sub(r"\d+", str(number), text, count=1)
    return f"{text.rstrip('.')} ({number})"


def with_replaced_word(text: str, lang: str) -> str:
    """``text`` with its longest plain word (not the first) swapped for a filler word."""
    words = text.split()
    index = max((i for i in range(1, len(words)) if words[i].isalpha()), key=lambda i: len(words[i]))
    words[index] = _FILLER_WORD[lang]
    return " ".join(words)


def with_negation(text: str, lang: str) -> str:
    first, rest = text.split(" ", 1)
    return f"{first} {_NEGATION_INSERT[lang]} {rest}"


def with_added_words(text: str, lang: str) -> str:
    return text.rstrip(".") + _ADDED_WORDS[lang]


class GateClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = MemoryDedupGate(repository=None)

    def _compare(self, old: tuple[str, str], new: tuple[str, str]) -> Any:
        return self.gate.compare(fact_features(*new), fact_features(*old))

    def test_light_stemming_folds_inflections(self) -> None:
        self.assertEqual({stem(w) for w in ("хранит", "хранится", "хранят")}, {"хран"})
        self.assertEqual({stem(w) for w in ("данные", "данных", "данными")}, {"данн"})
        self.assertEqual(stem("file.py"), "file.py")

    def test_reordered_inflected_restatement_is_duplicate(self) -> None:
        verdict = self._compare(("Кэш", "Мы используем Redis для кэша"), ("Кэш", "Для кэша мы используем Redis"))
        self.assertEqual(verdict.action, "duplicate")

    def test_changed_number_is_update(self) -> None:
        verdict = self._compare(("Таймаут", "Таймаут сессии 30 минут"), ("Таймаут", "Таймаут сессии 45 минут"))
        self.assertEqual(verdict.action, "update")
        self.assertEqual(verdict.details, {"values_from": ["30"], "values_to": ["45"]})

    def test_flipped_negation_is_conflict(self) -> None:
        verdict = self._compare(("Кэш", "Используем Redis для кэша сессий"), ("Кэш", "Не используем Redis для кэша сессий"))
        self.assertEqual(verdict.action, "conflict")

    def test_different_identifiers_are_different_facts(self) -> None:
        verdict = self._compare(("Config", "Config lives in settings.py"), ("Config", "Config lives in settings.yaml"))
        self.assertEqual(verdict.action, "new")

    def test_short_names_keep_similar_facts_apart(self) -> None:
        verdict = self._compare(("Порты", "Сервис A слушает порт 8080"), ("Порты", "Сервис B слушает порт 8080"))
        self.assertEqual(verdict.action, "new")

    def test_different_subjects_are_never_updated_or_merged(self) -> None:
        verdict = self._compare(("Сервис A", "Слушает порт 8080"), ("Сервис B", "Слушает порт 8081"))
        self.assertEqual(verdict.action, "new")

    def test_added_detail_under_same_subject_is_enrich(self) -> None:
        verdict = self._compare(
            ("Деплой", "Деплой через GitHub Actions"),
            ("Деплой", "Деплой через GitHub Actions на каждый push в main ветку"),
        )
        self.assertEqual(verdict.action, "enrich")
        self.assertGreaterEqual(verdict.near_score, MemoryDedupGate.NEAR_DUPLICATE_SIMILARITY)

    def test_shorter_restatement_is_covered_duplicate(self) -> None:
        verdict = self._compare(
            ("Деплой", "Деплой через GitHub Actions на каждый push в main ветку"),
            ("Деплой", "Деплой через GitHub Actions"),
        )
        self.assertEqual((verdict.action, verdict.reason), ("duplicate", "already covered by a fuller fact"))

    def test_added_detail_with_changed_value_is_not_enrich(self) -> None:
        verdict = self._compare(
            ("Лимит", "Лимит запросов 5000 в час"),
            ("Лимит", "Лимит запросов 9000 в час и 10 в секунду"),
        )
        self.assertNotIn(verdict.action, {"enrich", "duplicate"})

    def test_added_identifier_is_enrich_changed_identifier_is_not(self) -> None:
        added = self._compare(("Config", "Config lives in settings.py"), ("Config", "Config lives in settings.py and settings.yaml"))
        changed = self._compare(("Config", "Config lives in settings.py"), ("Config", "Config lives in settings.yaml"))
        self.assertEqual(added.action, "enrich")
        self.assertEqual(changed.action, "new")


class GateWritePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = ArchitectOSService(Path(self._tmp.name))
        self.repo = self.service.repository

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add(self, label: str, text: str, node_type: str = "Decision", **extra: Any) -> dict[str, Any]:
        return self.service.add_memory({"label": label, "text": text, "type": node_type, **extra})

    def _edges(self, source: str, target: str, edge_type: str) -> list[Any]:
        return [
            edge for edge in self.repo.list_edges_touching([source])
            if edge.source == source and edge.target == target and edge.type == edge_type
        ]

    def test_second_fact_under_same_label_does_not_overwrite_the_first(self) -> None:
        first = self._add("Решение", "Бэкенд работает только на stdlib")
        second = self._add("Решение", "Фронтенд собирается без бандлера")
        self.assertNotEqual(first["id"], second["id"])
        kept = self.repo.get_node(first["id"])
        assert kept is not None
        self.assertEqual(kept.text, "Бэкенд работает только на stdlib")
        self.assertEqual(second["dedup"]["action"], "new")

    def test_reordered_restatement_folds_into_existing_node(self) -> None:
        first = self._add("Кэш", "Мы используем Redis для кэша")
        again = self._add("Кэш", "Для кэша мы используем Redis", source="mcp")
        self.assertTrue(again["dedup_merged"])
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["dedup"]["action"], "duplicate")
        self.assertEqual(again["metadata"]["dedup_sources"][-1]["source"], "mcp")

    def test_update_supersedes_old_value_and_undo_restores_it(self) -> None:
        old = self._add("Лимит API", "Лимит запросов 5000 в час", "Constraint")
        new = self._add("Лимит API", "Лимит запросов 9000 в час", "Constraint")
        self.assertNotEqual(old["id"], new["id"])
        self.assertEqual(new["dedup"]["action"], "update")
        self.assertEqual(new["dedup"]["target_id"], old["id"])
        stored_old = self.repo.get_node(old["id"])
        assert stored_old is not None
        self.assertTrue(stored_old.metadata.get("invalid_at"))
        self.assertEqual(stored_old.metadata.get("superseded_by"), new["id"])
        self.assertEqual(len(self._edges(new["id"], old["id"], "SUPERSEDES")), 1)

        undo = self.service.undo_memory_dedup({"event_id": new["dedup"]["event_id"]})
        self.assertEqual(undo["action"], "update")
        restored_old = self.repo.get_node(old["id"])
        assert restored_old is not None
        self.assertFalse(restored_old.metadata.get("invalid_at"))
        self.assertEqual(self._edges(new["id"], old["id"], "SUPERSEDES"), [])
        with self.assertRaises(ValueError):
            self.service.undo_memory_dedup({"event_id": new["dedup"]["event_id"]})

    def test_conflict_links_contradiction_and_flags_review(self) -> None:
        old = self._add("Кэш", "Используем Redis для кэша сессий")
        new = self._add("Кэш", "Не используем Redis для кэша сессий")
        self.assertEqual(new["dedup"]["action"], "conflict")
        self.assertEqual(new["metadata"]["needs_review"], "conflict")
        self.assertEqual(len(self._edges(new["id"], old["id"], "CONTRADICTS")), 1)
        stored_old = self.repo.get_node(old["id"])
        assert stored_old is not None
        self.assertFalse(stored_old.metadata.get("invalid_at"))

        self.service.undo_memory_dedup({"event_id": new["dedup"]["event_id"]})
        self.assertEqual(self._edges(new["id"], old["id"], "CONTRADICTS"), [])
        stored_new = self.repo.get_node(new["id"])
        assert stored_new is not None
        self.assertNotIn("needs_review", stored_new.metadata)

    def test_undo_duplicate_restores_swallowed_fact_and_type(self) -> None:
        lesson = self._add("Деплой", "Деплой идёт через GitHub Actions", "Lesson")
        decision = self._add("Деплой", "деплой идет через github actions", "Decision")
        self.assertTrue(decision["dedup_merged"])
        self.assertEqual(decision["type"], "Decision")

        undo = self.service.undo_memory_dedup({"event_id": decision["dedup"]["event_id"]})
        survivor = self.repo.get_node(lesson["id"])
        assert survivor is not None
        self.assertEqual(survivor.type, "Lesson")
        self.assertEqual(survivor.metadata.get("dedup_hits"), 0)
        restored = undo["restored"]
        self.assertIsNotNone(restored)
        self.assertNotEqual(restored["id"], lesson["id"])
        self.assertEqual(restored["type"], "Decision")
        self.assertEqual(restored["text"], "деплой идет через github actions")

    def test_undo_snapshot_is_redacted(self) -> None:
        text = "Ключ OPENAI_API_KEY=sk-proj-abcdef1234567890abcdef1234 для тестов"
        self._add("Ключи", text)
        again = self._add("Ключи", text)
        event = self.repo.get_memory_event(again["dedup"]["event_id"])
        assert event is not None
        self.assertNotIn("sk-proj", event["details"]["incoming"]["text"])

    def test_unknown_event_cannot_be_undone(self) -> None:
        with self.assertRaises(ValueError):
            self.service.undo_memory_dedup({"event_id": "event_missing"})

    def test_preview_does_not_write(self) -> None:
        old = self._add("Лимит API", "Лимит запросов 5000 в час", "Constraint")
        before = len(self.repo.list_nodes())
        preview = self.service.preview_memory_dedup(
            {"label": "Лимит API", "text": "Лимит запросов 7000 в час", "type": "Constraint"}
        )
        self.assertEqual(preview["dedup"]["action"], "update")
        self.assertEqual(preview["dedup"]["target_id"], old["id"])
        self.assertEqual(len(self.repo.list_nodes()), before)

    def test_bulk_reports_gate_actions_and_dedups_within_batch(self) -> None:
        self._add("Лимит API", "Лимит запросов 5000 в час", "Constraint")
        result = self.service.add_memory_bulk({"items": [
            {"label": "Лимит API", "text": "Лимит запросов 9000 в час", "type": "Constraint"},
            {"label": "Порт", "text": "Сервер слушает порт 8765", "type": "Decision"},
            {"label": "Порт", "text": "Сервер слушает порт 8765.", "type": "Decision"},
        ]})
        stats = result["stats"]
        self.assertEqual(stats["created"], 2)
        self.assertEqual(stats["deduplicated"], 1)
        self.assertEqual(stats["superseded"], 1)
        self.assertEqual(result["results"][0]["dedup_action"], "update")

    def _candidate(self, cid: str, label: str, text: str) -> dict[str, Any]:
        return self.repo.upsert_memory_candidate({
            "id": cid,
            "project_id": "architectos",
            "source_type": "docs",
            "source_ref": f"{cid}.md",
            "label": label,
            "type": "Decision",
            "scope": "project",
            "text": text,
            "status": "candidate",
            "metadata": {},
        })

    def test_promote_keeps_existing_node_under_same_label(self) -> None:
        existing = self._add("Архитектура", "Сервер и UI работают локально")
        self._candidate("cand-other", "Архитектура", "Эмбеддинги считаются офлайн")
        promoted = self.service.promote_memory_candidate("cand-other")
        self.assertNotEqual(promoted["memory"]["id"], existing["id"])
        kept = self.repo.get_node(existing["id"])
        assert kept is not None
        self.assertEqual(kept.text, "Сервер и UI работают локально")

    def test_promote_of_identical_fact_folds_into_existing_node(self) -> None:
        existing = self._add("Архитектура", "Сервер и UI работают локально")
        self._candidate("cand-same", "Архитектура", "сервер и UI работают локально.")
        promoted = self.service.promote_memory_candidate("cand-same")
        self.assertEqual(promoted["memory"]["id"], existing["id"])
        self.assertTrue(promoted["memory"]["dedup_merged"])
        self.assertEqual(promoted["candidate"]["status"], "promoted")
        self.assertEqual(promoted["candidate"]["promoted_node_id"], existing["id"])

    def test_chat_auto_accept_holds_value_updates_for_review(self) -> None:
        self._add("Лимит API", "Лимит запросов 5000 в час", "Constraint")
        update = {
            "label": "Лимит API", "text": "Лимит запросов 9000 в час", "type": "Constraint",
            "scope": "project", "project_id": "architectos", "metadata": {},
        }
        fresh = dict(update, label="Шрифт", text="Интерфейс использует шрифт Inter")
        self.assertTrue(self.service._auto_accept_near_duplicate(update))
        self.assertFalse(self.service._auto_accept_near_duplicate(fresh))

    def _atom(self, template: str, fact_text: str) -> tuple[str, str, str]:
        """Label, body and type of the atom ``_enqueue_session_fact_atoms`` builds for one fact."""
        built: list[dict[str, Any]] = []

        def capture(project_id: str, candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
            built.extend(candidates)
            return []

        from_mcp = {"source_type": "mcp", "template": template, "id_kind": template} if template == "mcp_turn_atom" else {}
        with mock.patch.object(self.service.ingestion_engine, "prepare_candidates", side_effect=capture):
            self.service._enqueue_session_fact_atoms(
                "architectos", "chat-atoms", 1, [{"type": "Decision", "text": fact_text}],
                mode="review", trigger="test", **from_mcp,
            )
        self.assertEqual(len(built), 1, fact_text)
        self.assertEqual(built[0]["metadata"]["template"], template)
        return str(built[0]["label"]), str(built[0]["text"]), str(built[0]["type"])

    def _gate_outcome(
        self, stored: tuple[str, str], incoming: tuple[str, str], *, same_turn: bool = False
    ) -> tuple[str, str]:
        """``(gate verdict, auto outcome)`` of an incoming atom against a stored one.

        Both are ``(template, fact text)``. The verdict is what ``classify`` answers
        over the dedup index; ``update`` is a replacement only if
        ``replacement_allowed`` says so, otherwise both facts stay.
        """
        stored_label, stored_body, stored_type = self._atom(*stored)
        label, body, node_type = self._atom(*incoming)
        project_id = f"pair-{next(_PAIR_PROJECTS)}"
        self.repo.add_node(stored_type, stored_label, "project", stored_body, project_id, metadata={"template": stored[0]})
        action = self.service.memory_dedup.classify(label, body, node_type, "project", project_id).action
        if action == "update":
            allowed = memory_dedup.replacement_allowed(body, stored_body, same_turn=same_turn)
            return action, "replace" if allowed else "keep_both"
        return action, {"duplicate": "reject", "conflict": "reject", "enrich": "fold"}.get(action, "write")

    def _assert_pair(self, row: dict[str, Any]) -> None:
        template_pairs = [tuple(row["templates"])] if row.get("templates") else [(name, name) for name in ATOM_TEMPLATES]
        for stored_template, incoming_template in template_pairs:
            with self.subTest(pair=row["id"], stored=stored_template, incoming=incoming_template):
                got = self._gate_outcome((stored_template, row["a"]), (incoming_template, row["b"]))
                self.assertEqual(got, (row["verdict"], row["outcome"]))

    def test_pairs_that_must_fire_are_decided_from_the_whole_statement(self) -> None:
        rows = [row for row in DEDUP_PAIRS if not row["residual"] and row["outcome"] in {"reject", "fold", "replace"}]
        self.assertEqual(len(rows), 9)
        for row in rows:
            self._assert_pair(row)

    def test_pairs_that_must_stay_alive_never_fold_or_replace(self) -> None:
        rows = [row for row in DEDUP_PAIRS if not row["residual"] and row["outcome"] in {"keep_both", "write"}]
        self.assertEqual(len(rows), 12)
        for row in rows:
            self._assert_pair(row)

    def test_metamorphic_variants_of_every_must_fire_base_fact(self) -> None:
        bases = [row for row in DEDUP_PAIRS if not row["residual"] and row["outcome"] in {"reject", "fold", "replace"}]
        for row in bases:
            base, lang = row["a"], row["lang"]
            chat, mcp = "chat_session_atom", "mcp_turn_atom"
            number_10, number_20 = with_single_number(base, 10), with_single_number(base, 20)
            cases = {
                "word and number replaced is not an update": (
                    (chat, number_10), (chat, with_single_number(with_replaced_word(base, lang), 20)), None,
                ),
                "only number changed is a replacement": ((chat, number_10), (chat, number_20), ("update", "replace")),
                "inserted negation is a conflict": ((chat, base), (chat, with_negation(base, lang)), ("conflict", "reject")),
                "appended words enrich": ((chat, base), (chat, with_added_words(base, lang)), ("enrich", "fold")),
                "Ask against MCP is a duplicate": ((chat, base), (mcp, base), ("duplicate", "reject")),
                "MCP against Ask is a duplicate": ((mcp, base), (chat, base), ("duplicate", "reject")),
            }
            for name, (stored, incoming, expected) in cases.items():
                with self.subTest(base=row["id"], variant=name):
                    got = self._gate_outcome(stored, incoming)
                    if expected is None:
                        self.assertNotEqual(got[0], "update")
                        self.assertNotEqual(got[1], "replace")
                    else:
                        self.assertEqual(got, expected)

    def test_replacement_needs_equal_templates_one_changed_number_and_another_turn(self) -> None:
        def allowed(old: str, new: str, *, same_turn: bool = False) -> bool:
            return memory_dedup.replacement_allowed(
                self._atom("chat_session_atom", new)[1], self._atom("chat_session_atom", old)[1], same_turn=same_turn,
            )

        old = "The Azure Boards sync job fetches at most 200 work items per WIQL window"
        new = "The Azure Boards sync job fetches at most 1000 work items per WIQL window"
        self.assertTrue(allowed(old, new))
        self.assertFalse(allowed(old, new, same_turn=True), "facts of one turn never replace each other")
        self.assertFalse(allowed(old, old), "an unchanged number is no replacement")
        self.assertFalse(allowed(old, new.replace("sync job", "export job")), "templates differ")
        self.assertFalse(
            allowed("Service A calls service B with a 30 second timeout", "Service B calls service A with a 60 second timeout"),
            "same number count but another word order",
        )
        self.assertFalse(
            allowed("The rate limit is 100 requests per 60 seconds", "The rate limit is 200 requests per 30 seconds"),
            "several numbers are never a replacement",
        )
        self.assertFalse(
            allowed("Stage 1 of the release pipeline runs on Linux", "Stage 2 of the release pipeline runs on Linux"),
            "a number right after the first word names the item, it is not a value",
        )
        self.assertFalse(allowed("Спринт 14 закрывается в пятницу", "Спринт 15 закрывается в пятницу"))
        self.assertFalse(allowed("Limit is 5 requests", "Limit is 5 requests and 7 retries"), "number count differs")


def _residual_pair_test(row: dict[str, Any]) -> Any:
    @unittest.expectedFailure
    def test(self: GateWritePathTests) -> None:
        self._assert_pair(row)

    test.__doc__ = f"Accepted risk, kept visible: {row['id']}"
    return test


for _row in DEDUP_PAIRS:
    if _row["residual"]:
        setattr(GateWritePathTests, f"test_residual_{_row['id']}", _residual_pair_test(_row))


def caller_salt(caller: str, text: str) -> str:
    """Id salt of a fact the caller writes beside a node it does not own."""
    return f"{caller}:{content_salt(text)}"


def mcp_call(root: Path, project_id: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """One MCP tools/call on a server bound to ``project_id``; returns the tool's JSON data."""
    server = MemoryMCPServer(root, project_id=project_id)
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    stdout = io.StringIO()
    server.serve(stdin=io.StringIO(json.dumps(message) + "\n"), stdout=stdout)
    result = json.loads(stdout.getvalue().splitlines()[0])["result"]
    if result["isError"]:
        return {"error": result["content"][0]["text"]}
    return json.loads(result["content"][0]["text"].split("```json", 1)[1].rsplit("```", 1)[0])


class AuthorOwnershipTests(unittest.TestCase):
    """Shared/global nodes record the project that wrote them; only that project owns them."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.service = ArchitectOSService(self.root)
        self.repo = self.service.repository

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _shared(self, project_id: str, label: str, text: str) -> dict[str, Any]:
        return mcp_call(self.root, project_id, "memory_add", {"label": label, "text": text, "type": "Decision", "scope": "shared"})

    def test_shared_write_records_its_author_under_the_plain_id(self) -> None:
        written = self._shared("project_a", "Lint config", "Shared lint config lives in tools/lint.toml.")
        node = self.repo.get_node(written["id"])
        assert node is not None
        self.assertEqual((node.project_id, node.author_project_id), (None, "project_a"))
        self.assertEqual(node.id, self.repo.memory_node_id("Decision", "Lint config", "shared", None, None))
        self.assertEqual(MemoryNode.from_dict(node.to_dict()).author_project_id, "project_a")
        seen = mcp_call(self.root, "project_b", "memory_get", {"id": node.id})
        self.assertNotIn("error", seen)
        self.assertEqual(seen["id"], node.id)

    def test_project_nodes_belong_to_their_project_and_legacy_shared_ones_to_none(self) -> None:
        own = MemoryNode(id="n1", type="Lesson", label="l", scope="project", text="t", project_id="project_a")
        authored = MemoryNode(id="n2", type="Lesson", label="l", scope="shared", text="t", author_project_id="project_a")
        legacy = MemoryNode(id="n3", type="Lesson", label="l", scope="shared", text="t")
        self.assertEqual([owned_by(node, "project_a") for node in (own, authored, legacy)], [True, True, False])
        self.assertEqual([owned_by(node, "project_b") for node in (own, authored, legacy)], [False, False, False])
        self.assertEqual([owned_by(node, None) for node in (own, authored, legacy)], [True, True, True])

    def test_only_the_author_may_supersede_a_shared_node(self) -> None:
        old = self._shared("project_a", "Lint config", "Shared lint config lives in tools/lint.toml.")
        newer = self._shared("project_a", "Lint config v2", "Shared lint config moved to tools/lint/ruff.toml.")
        legacy = self.service.add_memory({"label": "Legacy rule", "text": "Legacy shared rule without an author.", "type": "Decision", "scope": "shared"})
        legacy_node = self.repo.get_node(legacy["id"])
        assert legacy_node is not None
        self.assertIsNone(legacy_node.author_project_id)

        refused = mcp_call(self.root, "project_b", "memory_link", {"source": newer["id"], "target": old["id"], "type": "SUPERSEDES"})
        self.assertIn("target must belong to project project_b", refused["error"])
        for project_id in ("project_a", "project_b"):
            with self.subTest(legacy_for=project_id):
                refused = mcp_call(self.root, project_id, "memory_link", {"source": newer["id"], "target": legacy["id"], "type": "SUPERSEDES"})
                self.assertIn("target must belong to project", refused["error"])
        stored = self.repo.get_node(old["id"])
        assert stored is not None
        self.assertNotIn("superseded_by", stored.metadata)

        linked = mcp_call(self.root, "project_a", "memory_link", {"source": newer["id"], "target": old["id"], "type": "SUPERSEDES"})
        self.assertNotIn("error", linked)
        stored = self.repo.get_node(old["id"])
        assert stored is not None
        self.assertEqual(stored.metadata.get("superseded_by"), newer["id"])
        # Without a caller project (UI) the legacy node may still be superseded as before.
        self.service.create_graph_edge({"source": newer["id"], "target": legacy["id"], "type": "SUPERSEDES"})
        self.assertTrue(self.repo.get_node(legacy["id"]).metadata.get("invalid_at"))

    def test_ownership_is_compared_only_through_owned_by(self) -> None:
        modules = ("graph_service", "ingestion_service", "ingestion_candidates", "chat_session_service")
        pattern = re.compile(r"\b(?:project_id|author_project_id)\s*(?:==|!=)\s*\w*(?:caller|project)")
        backend = Path(__file__).resolve().parents[1] / "backend" / "architectos"
        for module in modules:
            with self.subTest(module=module):
                self.assertEqual(pattern.findall((backend / f"{module}.py").read_text(encoding="utf-8")), [])



def http_handler(service: ArchitectOSService) -> tuple[ArchitectOSHandler, list[dict[str, Any]]]:
    """A request handler without a socket: route methods run as-is, replies are collected."""

    class TestHandler(ArchitectOSHandler):
        pass

    TestHandler.service = service
    handler = TestHandler.__new__(TestHandler)
    replies: list[dict[str, Any]] = []
    handler._json = lambda payload, status=HTTPStatus.OK: replies.append(payload)
    return handler, replies


class CallerProjectGateTests(unittest.TestCase):
    """With a caller project the gate changes only nodes that project owns."""

    # action -> (type, label, stored text, incoming text)
    FACTS = {
        "update": ("Constraint", "Лимит API", "Лимит запросов 5000 в час", "Лимит запросов 9000 в час"),
        "enrich": ("Decision", "Деплой", "Деплой через GitHub Actions", "Деплой через GitHub Actions на каждый push в main ветку"),
        "duplicate": ("Decision", "Кэш", "Мы используем Redis для кэша", "Для кэша мы используем Redis"),
    }

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.service = ArchitectOSService(self.root)
        self.repo = self.service.repository

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _mcp_add(self, project_id: str, node_type: str, label: str, text: str, scope: str = "shared") -> dict[str, Any]:
        written = mcp_call(self.root, project_id, "memory_add", {"label": label, "text": text, "type": node_type, "scope": scope})
        self.assertNotIn("error", written)
        return written

    def _add(self, caller: str | None, label: str, text: str, node_type: str = "Decision", **extra: Any) -> dict[str, Any]:
        payload = {"label": label, "text": text, "type": node_type, "scope": "shared", "project_id": caller or "architectos", **extra}
        return self.service.add_memory(payload, caller_project_id=caller)

    def _snapshot(self, node_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        node = self.repo.get_node(node_id)
        assert node is not None
        return node.to_dict(), self.repo.list_memory_events(node_id)

    def _set_status(self, node_id: str, status: str) -> None:
        node = self.repo.get_node(node_id)
        assert node is not None
        node.status = status
        self.repo.upsert_node(node)

    def _edge_types(self, source: str, target: str) -> set[str]:
        return {edge.type for edge in self.repo.list_edges_touching([source]) if edge.source == source and edge.target == target}

    def _shared_id(self, node_type: str, label: str, salt: str = "") -> str:
        return self.repo.memory_node_id(node_type, label, "shared", None, None, salt)

    def _assert_beside(self, old_id: str, text: str, written: dict[str, Any], caller: str = "project_a") -> MemoryNode:
        """The new fact is the caller's own active node; the node it does not own is untouched."""
        self.assertFalse(written.get("dedup_held"))
        self.assertNotEqual(written["id"], old_id)
        node = self.repo.get_node(written["id"])
        assert node is not None
        self.assertEqual((node.status, node.text, node.author_project_id), ("active", text, caller))
        return node

    def test_shared_fact_of_another_project_is_not_rewritten(self) -> None:
        for action, (node_type, label, stored, incoming) in self.FACTS.items():
            with self.subTest(action=action):
                old = self._mcp_add("project_b", node_type, label, stored)
                before = self._snapshot(old["id"])
                new = self._mcp_add("project_a", node_type, label, incoming)

                self.assertEqual(self._snapshot(old["id"]), before)
                self.assertEqual(new["dedup"]["action"], "new")
                self.assertEqual(new["dedup"]["details"]["suggested_action"], action)
                created = self._assert_beside(old["id"], incoming, new)
                self.assertEqual(created.id, self._shared_id(node_type, label, caller_salt("project_a", incoming)))
                self.assertEqual(created.metadata["possible_duplicate_of"], old["id"])
                self.assertEqual(created.metadata["dedup_suggestion"]["target_id"], old["id"])
                self.assertEqual(self._edge_types(new["id"], old["id"]), set())

    def test_legacy_shared_node_without_an_author_belongs_to_no_project(self) -> None:
        for caller in ("project_a", "architectos"):
            for action, (node_type, label, stored, incoming) in self.FACTS.items():
                with self.subTest(caller=caller, action=action):
                    label = f"{label} {caller}"
                    old = self._add(None, label, stored, node_type)
                    before = self._snapshot(old["id"])
                    new = self._mcp_add(caller, node_type, label, incoming)
                    self.assertEqual(new["dedup"]["details"]["suggested_action"], action)
                    self._assert_beside(old["id"], incoming, new, caller)
                    self.assertEqual(self._snapshot(old["id"]), before)

    def test_own_project_nodes_are_still_superseded_and_enriched(self) -> None:
        for scope in ("project", "shared"):
            for action, (node_type, label, stored, incoming) in self.FACTS.items():
                with self.subTest(scope=scope, action=action):
                    label = f"{label} {scope}"
                    old = self._mcp_add("project_a", node_type, label, stored, scope)
                    new = self._mcp_add("project_a", node_type, label, incoming, scope)
                    kept = self.repo.get_node(old["id"])
                    assert kept is not None
                    self.assertEqual(new["dedup"]["action"], action)
                    if action == "update":
                        self.assertEqual(kept.metadata.get("superseded_by"), new["id"])
                        self.assertTrue(kept.metadata.get("invalid_at"))
                        self.assertIn("SUPERSEDES", self._edge_types(new["id"], old["id"]))
                    else:
                        self.assertEqual(new["id"], old["id"])
                        self.assertEqual(kept.metadata.get("dedup_hits"), 1)
                        self.assertEqual(kept.text, incoming if action == "enrich" else stored)

    def test_without_caller_project_the_gate_rewrites_shared_nodes_as_before(self) -> None:
        for action, (node_type, label, stored, incoming) in self.FACTS.items():
            with self.subTest(action=action):
                old = self._mcp_add("project_b", node_type, label, stored)
                new = self.service.add_memory({"label": label, "text": incoming, "type": node_type, "scope": "shared", "project_id": "project_a"})
                kept = self.repo.get_node(old["id"])
                assert kept is not None
                self.assertEqual(new["dedup"]["action"], action)
                if action == "update":
                    self.assertEqual(kept.metadata.get("superseded_by"), new["id"])
                else:
                    self.assertTrue(new["dedup_merged"])
                    self.assertEqual(kept.text, incoming if action == "enrich" else stored)

    def test_inactive_node_of_another_project_never_blocks_or_changes(self) -> None:
        text = "Deploys always run through GitHub Actions."
        for status in ("archived", "deleted"):
            for repeat in ("same", "other"):
                with self.subTest(status=status, repeat=repeat):
                    label = f"Deploy rule {status} {repeat}"
                    old = self._mcp_add("project_b", "Decision", label, text)
                    self._set_status(old["id"], status)
                    before = self._snapshot(old["id"])
                    incoming = text if repeat == "same" else "Deploys now run through ArgoCD on every merge."
                    written = self._mcp_add("project_a", "Decision", label, incoming)
                    self.assertEqual(written["dedup"]["action"], "new")
                    self._assert_beside(old["id"], incoming, written)
                    self.assertEqual(self._snapshot(old["id"]), before)

    def test_repeats_after_a_foreign_near_duplicate_fold_into_one_own_node(self) -> None:
        node_type, label, stored, incoming = self.FACTS["duplicate"]
        old = self._mcp_add("project_b", node_type, label, stored)
        before = self._snapshot(old["id"])
        written = [self._mcp_add("project_a", node_type, label, incoming) for _ in range(3)]
        self.assertEqual({item["id"] for item in written}, {written[0]["id"]})
        own = self.repo.get_node(written[0]["id"])
        assert own is not None
        self.assertEqual((own.author_project_id, own.metadata.get("dedup_hits")), ("project_a", 2))
        self.assertEqual(self._snapshot(old["id"]), before)

    def test_archive_and_readd_cycles_reuse_one_id(self) -> None:
        text = "Shared lint config lives in tools/lint.toml."
        for slot in ("free", "foreign_active", "foreign_archived"):
            with self.subTest(slot=slot):
                label = f"Lint config {slot}"
                if slot != "free":
                    old = self._mcp_add("project_b", "Decision", label, "Lint runs on every pull request.")
                    if slot == "foreign_archived":
                        self._set_status(old["id"], "archived")
                ids = set()
                for _ in range(12):
                    written = self._add("project_a", label, text)
                    ids.add(written["id"])
                    self._set_status(written["id"], "archived")
                expected = self._shared_id("Decision", label, "" if slot == "free" else caller_salt("project_a", text))
                self.assertEqual(ids, {expected})

    def test_secret_never_reaches_the_id_and_repeats_find_one_node(self) -> None:
        raw = "CI deploy uses token: ghs_deploykey and rotates it monthly."
        redacted = "CI deploy uses [REDACTED] and rotates it monthly."
        for extras in ({}, {"dedup": False}):
            with self.subTest(dedup=extras.get("dedup", True)):
                label = f"CI token {len(extras)}"
                self._mcp_add("project_b", "Decision", label, "CI runs on every push to main.")
                ids = {self._add("project_a", label, text, **extras)["id"] for text in (raw, raw, redacted)}
                self.assertEqual(len(ids), 1)
                stored = self.repo.get_node(ids.pop())
                assert stored is not None
                self.assertEqual(stored.text, redacted)
                self.assertEqual(stored.id, self._shared_id("Decision", label, caller_salt("project_a", stored.text)))
        # Without a caller the label-collision salt hashes the redacted text too.
        self._add(None, "CI token ui", "CI runs on every push to main.")
        written = self._add(None, "CI token ui", raw)
        self.assertEqual(written["id"], self._shared_id("Decision", "CI token ui", content_salt(redacted)))

    def test_inactive_or_different_fact_under_the_label_never_blocks_a_new_fact(self) -> None:
        cases = {
            # (stored text, status of the stored node, incoming text, payload extras)
            "archived_same_fact": ("Deploys always run through GitHub Actions.", "archived", "Deploys always run through GitHub Actions.", {}),
            "archived_other_fact": ("Deploys run through Jenkins nightly.", "archived", "Deploys now run through ArgoCD on every merge.", {}),
            "active_other_fact_dedup_off": ("Releases ship every second Tuesday.", "active", "Hotfixes may ship any weekday after QA sign-off.", {"dedup": False}),
        }
        for name, (stored, status, incoming, extras) in cases.items():
            for path in ("add_memory", "bulk"):
                with self.subTest(case=name, path=path):
                    label = f"Rule {name} {path}"
                    old = self._mcp_add("project_b", "Decision", label, stored)
                    self._set_status(old["id"], status)
                    before = self._snapshot(old["id"])
                    item = {"label": label, "text": incoming, "type": "Decision", **extras}
                    written_ids = []
                    for _ in range(2):
                        if path == "add_memory":
                            written = self._add("project_a", label, incoming, **extras)
                        else:
                            bulk = self.service.add_memory_bulk({"project_id": "project_a", "scope": "shared", "items": [item]}, caller_project_id="project_a")
                            self.assertEqual(bulk["stats"]["errors"], 0)
                            written = self.repo.get_node(bulk["results"][0]["id"]).to_dict()
                        self._assert_beside(old["id"], incoming, written)
                        written_ids.append(written["id"])
                    self.assertEqual(written_ids[0], written_ids[1])
                    self.assertEqual(self._snapshot(old["id"]), before)

    def test_bulk_and_split_writes_keep_the_callers_project(self) -> None:
        node_type, label, stored, incoming = self.FACTS["update"]
        old = self._add(None, label, stored, node_type, project_id="project_b")
        before = self._snapshot(old["id"])
        bulk = mcp_call(self.root, "project_a", "memory_add_bulk", {"scope": "shared", "items": [{"label": label, "text": incoming, "type": node_type}]})
        self.assertEqual(bulk["stats"]["superseded"], 0)
        self.assertEqual(self._snapshot(old["id"]), before)
        # Two durable lines: add_memory_from_agent splits them into one write each.
        limit = self._mcp_add("project_b", "Constraint", "API limit", "The API rate limit must stay at 5000 requests per hour.")
        limit_before = self._snapshot(limit["id"])
        split = self._mcp_add(
            "project_a", "Constraint", "API limit",
            "- The API rate limit must stay at 9000 requests per hour.\n- Deploys must always run through GitHub Actions on every push.",
        )
        self.assertTrue(split["also_created"])
        self.assertEqual(split["dedup"]["details"]["suggested_action"], "update")
        self.assertEqual(self._snapshot(limit["id"]), limit_before)

    def test_http_and_sdk_writes_with_a_project_keep_it(self) -> None:
        node_type, label, stored, incoming = self.FACTS["update"]
        old = self._mcp_add("project_b", node_type, label, stored)
        before = self._snapshot(old["id"])
        handler, replies = http_handler(self.service)
        handler._post_memory(None, {}, {"label": label, "text": incoming, "type": node_type, "scope": "shared", "project_id": "project_a"})
        self.assertEqual(replies[0]["dedup"]["details"]["suggested_action"], "update")
        self.assertEqual(replies[0]["author_project_id"], "project_a")
        added = ArchitectOS(self.root).add("Лимит запросов 7000 в час", label=label, type=node_type, project_id="project_c", scope="shared")
        self.assertEqual(added["author_project_id"], "project_c")
        self.assertNotEqual(added["id"], old["id"])
        self.assertEqual(self._snapshot(old["id"]), before)

    def _candidate(self, cid: str, project_id: str, label: str, text: str, node_type: str = "Decision") -> None:
        self.repo.upsert_memory_candidate({
            "id": cid, "project_id": project_id, "source_type": "mcp", "source_ref": f"{cid}.md",
            "label": label, "type": node_type, "scope": "shared", "text": text, "status": "candidate", "metadata": {},
        })

    def test_promote_does_not_fold_into_another_projects_node(self) -> None:
        existing = self._mcp_add("project_b", "Decision", "Архитектура", "Сервер и UI работают локально")
        before = self._snapshot(existing["id"])
        self._candidate("cand-a", "project_a", "Архитектура", "Локально работают сервер и UI")
        self._candidate("cand-none", "project_a", "Архитектура", "сервер и UI работают локально.")
        beside = self.service.promote_memory_candidate("cand-a", caller_project_id="project_a")
        self.assertEqual(self._snapshot(existing["id"]), before)
        self.assertEqual(beside["candidate"]["status"], "promoted")
        node = self._assert_beside(existing["id"], "Локально работают сервер и UI", beside["memory"])
        self.assertEqual(node.metadata["possible_duplicate_of"], existing["id"])
        # Without a caller project (internal promote) the fold works as before.
        folded = self.service.promote_memory_candidate("cand-none")
        self.assertEqual(folded["memory"]["id"], existing["id"])
        self.assertTrue(folded["memory"]["dedup_merged"])

    def test_promote_beside_an_archived_node_of_another_project(self) -> None:
        old = self._mcp_add("project_b", "Decision", "Архитектура", "Сервер и UI работают локально")
        self._set_status(old["id"], "archived")
        before = self._snapshot(old["id"])
        self._candidate("cand-a", "project_a", "Архитектура", "Сервер и UI работают локально")
        promoted = self.service.promote_memory_candidate("cand-a", caller_project_id="project_a")
        self.assertEqual(promoted["candidate"]["status"], "promoted")
        self._assert_beside(old["id"], "Сервер и UI работают локально", promoted["memory"])
        self.assertEqual(self._snapshot(old["id"]), before)

    def test_promote_folds_into_the_callers_own_shared_node(self) -> None:
        own = self._mcp_add("project_a", "Decision", "Архитектура", "Сервер и UI работают локально")
        self._candidate("cand-a", "project_a", "Архитектура", "Локально работают сервер и UI")
        folded = self.service.promote_memory_candidate("cand-a", caller_project_id="project_a")
        self.assertEqual(folded["memory"]["id"], own["id"])
        self.assertEqual(folded["candidate"]["promoted_node_id"], own["id"])


    def test_exact_restatement_of_another_projects_fact_is_held(self) -> None:
        cases = {
            # (stored label, stored text, incoming label, incoming text)
            "case_and_punctuation": ("Deploy.", "Deploys always run through GitHub Actions.", "deploy", "deploys ALWAYS run through github actions"),
            "secret_redacted": ("CI token", "CI deploy uses token: ghs_deploykey and rotates it monthly.", "CI token", "CI deploy uses token: ghs_deploykey and rotates it monthly."),
        }
        for name, (stored_label, stored, label, incoming) in cases.items():
            with self.subTest(case=name):
                old = self._mcp_add("project_b", "Decision", stored_label, stored)
                before = self._snapshot(old["id"])
                count = len(self.repo.list_nodes())
                for _ in range(3):
                    held = self._mcp_add("project_a", "Decision", label, incoming)
                    self.assertTrue(held["dedup_held"])
                    self.assertEqual((held["id"], held["dedup"]["target_id"]), (old["id"], old["id"]))
                self.assertEqual(len(self.repo.list_nodes()), count)
                self.assertEqual(self._snapshot(old["id"]), before)

    def test_a_held_fact_under_a_salted_id_is_found_through_the_index(self) -> None:
        self._mcp_add("project_b", "Decision", "Lint config", "Lint runs on every pull request.")
        text = "Shared lint config lives in tools/lint.toml."
        salted = self._mcp_add("project_b", "Decision", "Lint config", text)
        self.assertNotEqual(salted["id"], self._shared_id("Decision", "Lint config"))
        before = self._snapshot(salted["id"])
        held = self._mcp_add("project_a", "Decision", "Lint config", text)
        self.assertTrue(held["dedup_held"])
        self.assertEqual(held["id"], salted["id"])
        bulk = mcp_call(self.root, "project_a", "memory_add_bulk", {"scope": "shared", "items": [{"label": "Lint config", "text": text, "type": "Decision"}]})
        self.assertEqual((bulk["stats"]["held"], bulk["stats"]["created"], bulk["results"][0]["status"]), (1, 0, "held"))
        self.assertEqual(bulk["results"][0]["id"], salted["id"])
        self.assertEqual(self._snapshot(salted["id"]), before)

    def test_own_exact_copy_wins_over_a_held_one(self) -> None:
        text = "Deploys always run through GitHub Actions."
        orders = set()
        # The index meets the copies in node-id order: these labels put either copy first.
        for label in ("Deploy", "Lint"):
            with self.subTest(label=label):
                foreign = self._mcp_add("project_b", "Decision", label, text)
                own = self._add("project_a", label, text, dedup=False)
                orders.add(foreign["id"] < own["id"])
                before = self._snapshot(foreign["id"])
                again = self._mcp_add("project_a", "Decision", label, text)
                self.assertFalse(again["dedup_held"])
                self.assertEqual(again["id"], own["id"])
                self.assertEqual(self.repo.get_node(own["id"]).metadata.get("dedup_hits"), 1)
                self.assertEqual(self._snapshot(foreign["id"]), before)
        self.assertEqual(orders, {True, False})

    def test_dedup_off_with_a_caller_stores_an_own_copy(self) -> None:
        text = "Deploys always run through GitHub Actions."
        foreign = self._mcp_add("project_b", "Decision", "Deploy", text)
        before = self._snapshot(foreign["id"])
        self.repo.set_setting("memory_dedup", {"mode": "off"})
        copies = {self._add("project_a", "Deploy", text)["id"] for _ in range(2)}
        copies |= {self._add("project_a", "Deploy", text, dedup=False)["id"]}
        self.assertEqual(copies, {self._shared_id("Decision", "Deploy", caller_salt("project_a", text))})
        self.assertEqual(self._snapshot(foreign["id"]), before)

    def test_promoting_a_held_fact_never_writes_or_overwrites(self) -> None:
        existing = self._mcp_add("project_b", "Decision", "Архитектура", "Сервер и UI работают локально")
        before = self._snapshot(existing["id"])
        count = len(self.repo.list_nodes())
        for cid, project_id in (("cand-1", "project_a"), ("cand-2", "project_a"), ("cand-3", "project_c")):
            self._candidate(cid, project_id, "Архитектура", "сервер и UI работают локально.")
            with self.subTest(candidate=cid):
                held = self.service.promote_memory_candidate(cid, caller_project_id=project_id)
                self.assertTrue(held["memory"]["dedup_held"])
                self.assertEqual(held["memory"]["id"], existing["id"])
                self.assertEqual(held["candidate"]["status"], "rejected")
                self.assertEqual(held["candidate"]["metadata"]["dedup_held_by"], existing["id"])
                self.assertFalse(held["candidate"].get("promoted_node_id"))
        self.assertEqual(len(self.repo.list_nodes()), count)
        self.assertEqual(self._snapshot(existing["id"]), before)

    def test_review_accept_and_batch_of_a_held_candidate(self) -> None:
        existing = self._mcp_add("project_b", "Decision", "Архитектура", "Сервер и UI работают локально")
        before = self._snapshot(existing["id"])
        for cid in ("cand-1", "cand-2"):
            self._candidate(cid, "project_c", "Архитектура", "сервер и UI работают локально.")
        handler, replies = http_handler(self.service)
        with self.assertRaisesRegex(ValueError, "belongs to project project_c"):
            handler._post_memory_candidate_promote(re.match(r"(?P<candidate_id>.+)", "cand-1"), {}, {"project_id": "project_b"})
        # What Review sends: Accept with an empty body, and Accept-all for the project.
        handler._post_memory_candidate_promote(re.match(r"(?P<candidate_id>.+)", "cand-1"), {}, {})
        with mock.patch.object(type(self.service), "_refresh_candidate_duplicate_flags", autospec=True) as refresh:
            handler._post_memory_candidates_batch(None, {}, {"project_id": "project_c", "action": "accept", "status": "candidate"})
        refresh.assert_called_once()
        self.assertEqual(replies[0]["candidate"]["status"], "rejected")
        self.assertEqual(replies[0]["memory"]["id"], existing["id"])
        self.assertEqual((replies[1]["held"], replies[1]["promoted"]), (1, 0))
        self.assertEqual([item["status"] for item in replies[1]["results"]], ["held"])
        self.assertEqual(self._snapshot(existing["id"]), before)

    def _race_project_b(self, write_a: Any) -> None:
        """Run project B's write (another MCP process: its own service) right after A chose its id.

        A waits until B either wrote or reached its own id transaction: without A's
        write lock B lands first (and A would overwrite it), with it B waits for A.
        """
        other = ArchitectOSService(self.root)
        # A holds its lock for up to 3 s below: give B's busy wait a wide margin under load.
        real_prepare = other.repository._prepare_connection

        def patient_connection(conn: Any) -> None:
            real_prepare(conn)
            conn.execute("PRAGMA busy_timeout=15000")

        other.repository._prepare_connection = patient_connection
        reached, written = threading.Event(), threading.Event()
        failures: list[BaseException] = []
        real_transaction = other.repository.transaction
        real_other_add_node = other.repository.add_node

        @contextmanager
        def announced_transaction() -> Iterator[Any]:
            reached.set()
            with real_transaction() as conn:
                yield conn

        def add_node_then_announce(*args: Any, **kwargs: Any) -> MemoryNode:
            node = real_other_add_node(*args, **kwargs)
            written.set()
            return node

        other.repository.transaction = announced_transaction
        other.repository.add_node = add_node_then_announce

        def write_b() -> None:
            try:
                other.add_memory({"label": "Lint config", "text": "Lint runs on every pull request.", "type": "Decision",
                                  "scope": "shared", "project_id": "project_b"}, caller_project_id="project_b")
            except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
                failures.append(exc)
            finally:
                reached.set()

        writer = threading.Thread(target=write_b)
        real_add_node = self.repo.add_node

        def add_node_while_b_writes(*args: Any, **kwargs: Any) -> MemoryNode:
            self.repo.add_node = real_add_node
            writer.start()
            self.assertTrue(reached.wait(timeout=60))
            # Without the lock B's insert follows at once; with it B is blocked until A commits.
            written.wait(timeout=3)
            return real_add_node(*args, **kwargs)

        self.repo.add_node = add_node_while_b_writes
        write_a()
        writer.join(timeout=60)
        self.assertEqual(failures, [])
        nodes = {node.author_project_id: node for node in self.repo.list_nodes() if node.label == "Lint config"}
        self.assertEqual(set(nodes), {"project_a", "project_b"})
        self.assertEqual(nodes["project_a"].text, "Shared lint config lives in tools/lint.toml.")
        self.assertEqual(nodes["project_b"].text, "Lint runs on every pull request.")

    def test_a_concurrent_writer_never_takes_the_chosen_id(self) -> None:
        self._race_project_b(lambda: self._add("project_a", "Lint config", "Shared lint config lives in tools/lint.toml."))

    def test_a_concurrent_writer_never_takes_the_id_a_promote_chose(self) -> None:
        self._candidate("cand-a", "project_a", "Lint config", "Shared lint config lives in tools/lint.toml.")
        self._race_project_b(lambda: self.service.promote_memory_candidate("cand-a", caller_project_id="project_a"))

    def test_embedding_runs_after_the_write_lock_is_released(self) -> None:
        # No background worker (an MCP stdio process): the upsert listener embeds inline.
        other = ArchitectOSService(self.root)
        self.assertFalse(self.service._embedding_worker_started)
        provider = self.service.memory_embeddings.provider
        real_embed = provider.embed
        entered, release = threading.Event(), threading.Event()

        def slow_embed(*args: Any, **kwargs: Any) -> Any:
            entered.set()
            release.wait(timeout=60)  # a slow remote provider
            return real_embed(*args, **kwargs)

        provider.embed = slow_embed
        writer = threading.Thread(target=lambda: self._add("project_a", "Lint config", "Lint runs on every pull request."))
        writer.start()
        try:
            self.assertTrue(entered.wait(timeout=60))
            written = other.add_memory({"label": "Deploy", "text": "Deploys run through GitHub Actions.", "type": "Decision",
                                        "scope": "shared", "project_id": "project_b"}, caller_project_id="project_b")
        finally:
            release.set()
            writer.join(timeout=60)
        self.assertEqual(self.repo.get_node(written["id"]).author_project_id, "project_b")

    def test_a_write_outside_a_transaction_notifies_at_once(self) -> None:
        seen: list[str] = []
        self.repo.register_node_upsert_listener(lambda node: seen.append(node.id))
        node = self.repo.add_node("Decision", "Direct", "shared", "This write runs without a transaction.", None)
        self.assertEqual(seen, [node.id])
        node.status = "archived"
        self.repo.upsert_node(node)
        self.assertEqual(seen, [node.id, node.id])

    def test_a_rolled_back_transaction_notifies_no_listener(self) -> None:
        seen: list[str] = []
        self.repo.register_node_upsert_listener(lambda node: seen.append(node.id))
        with self.assertRaises(RuntimeError), self.repo.transaction():
            self.repo.add_node("Decision", "Rolled back", "shared", "This write is rolled back.", None)
            self.assertEqual(seen, [])
            raise RuntimeError("abort")
        self.assertEqual(seen, [])
        with self.repo.transaction():
            with self.repo.transaction():
                node = self.repo.add_node("Decision", "Committed", "shared", "This write is committed.", None)
            self.assertEqual(seen, [])
        self.assertEqual(seen, [node.id])

    def test_promote_with_an_interface_id_never_overwrites_another_projects_node(self) -> None:
        old = self.service.add_memory({"type": "Decision", "scope": "shared", "label": "Lint config", "interface_id": "iface_lint",
                                       "text": "Lint runs on every pull request.", "project_id": "project_b"}, caller_project_id="project_b")
        before = self._snapshot(old["id"])
        self._candidate("cand-a", "project_a", "Lint config", "Lint is optional for drafts.")
        promoted = self.service.promote_memory_candidate("cand-a", {"interface_id": "iface_lint"}, caller_project_id="project_a")
        self._assert_beside(old["id"], "Lint is optional for drafts.", promoted["memory"])
        self.assertEqual(self._snapshot(old["id"]), before)

    def test_interface_id_accepts_rewrite_the_callers_own_node_in_place(self) -> None:
        def accept(cid: str, text: str, payload: dict[str, Any] | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
            self._candidate(cid, "project_a", "Lint config", text)
            if metadata:
                candidate = self.repo.get_memory_candidate(cid)
                self.repo.update_memory_candidate({**candidate, "metadata": metadata})
            return self.service.promote_memory_candidate(cid, payload, caller_project_id="project_a")["memory"]

        def active() -> list[MemoryNode]:
            return [node for node in self.repo.list_nodes() if node.label == "Lint config" and node.status == "active"]

        first = accept("c1", "Lint runs on every pull request.", {"interface_id": "iface_lint"})
        again = accept("c2", "Lint runs on every pull request.", {"interface_id": "iface_lint"})
        self.assertEqual((again["id"], len(active())), (first["id"], 1))
        changed = accept("c3", "Lint runs nightly, not on pull requests.", {"interface_id": "iface_lint"})
        self.assertEqual(changed["id"], first["id"])
        self.assertEqual([(node.id, node.text) for node in active()], [(first["id"], "Lint runs nightly, not on pull requests.")])
        for node in active():
            node.status = "archived"
            self.repo.upsert_node(node)
        revisions = [accept(cid, "Lint config moved to tools/lint.toml.", metadata={"revision": True, "content_hash": "h1"}) for cid in ("r1", "r2")]
        self.assertEqual(revisions[0]["id"], revisions[1]["id"])
        self.assertEqual([(node.interface_id, node.author_project_id) for node in active()], [("rev:h1", "project_a")])

    def test_undo_of_a_review_accept_fold_restores_the_fact_under_its_author(self) -> None:
        _, label, stored, incoming = self.FACTS["enrich"]
        own = self._add("project_a", label, stored)
        self._candidate("cand-a", "project_a", label, incoming)
        handler, replies = http_handler(self.service)
        handler._post_memory_candidate_promote(re.match(r"(?P<candidate_id>.+)", "cand-a"), {}, {})
        folded = replies[0]["memory"]
        self.assertEqual((folded["id"], folded["dedup"]["action"]), (own["id"], "enrich"))
        undone = self.service.undo_memory_dedup({"event_id": folded["dedup"]["event_id"]})
        restored = self.repo.get_node(undone["restored"]["id"])
        self.assertEqual((restored.text, restored.author_project_id), (incoming, "project_a"))

    def test_another_projects_text_cannot_take_the_callers_salted_id(self) -> None:
        text = "Shared lint config lives in tools/lint.toml."
        plain = self._add("project_b", "Lint config", "Lint runs on every pull request.")
        squat = self._add("project_b", "Lint config", f"project_a {text}")
        before = [self._snapshot(plain["id"]), self._snapshot(squat["id"])]
        written = self._add("project_a", "Lint config", text)
        self.assertEqual(written["id"], self._shared_id("Decision", "Lint config", caller_salt("project_a", text)))
        self.assertNotIn(written["id"], {plain["id"], squat["id"]})
        self.assertEqual([self._snapshot(plain["id"]), self._snapshot(squat["id"])], before)

    def test_a_pipe_in_a_label_or_interface_id_cannot_take_the_callers_salted_id(self) -> None:
        text = "Shared lint config lives in tools/lint.toml."
        salt = caller_salt("project_a", text)
        plain = self._add("project_b", "Lint config", "Lint runs on every pull request.")
        # Joined with "|", each of these used to hash exactly like "Lint config" salted for project_a.
        squats = [
            self._add("project_b", f"Lint config|{salt}", "Lint is optional for drafts."),
            self._add("project_b", salt, "Lint is optional for spikes.", interface_id="|Lint config"),
        ]
        before = [self._snapshot(node["id"]) for node in (plain, *squats)]
        written = self._add("project_a", "Lint config", text)
        self.assertEqual(written["id"], self._shared_id("Decision", "Lint config", salt))
        self.assertNotIn(written["id"], {node["id"] for node in (plain, *squats)})
        self._assert_beside(plain["id"], text, written)
        self.assertEqual([self._snapshot(node["id"]) for node in (plain, *squats)], before)

    def test_ids_of_labels_without_a_pipe_are_unchanged(self) -> None:
        for salt in ("", content_salt("Lint runs on every pull request."), caller_salt("project_a", "Lint runs nightly.")):
            with self.subTest(salt=salt):
                parts = ("Decision", "shared", "", "iface_lint", "Lint config", salt) if salt else ("Decision", "shared", "", "iface_lint", "Lint config")
                self.assertEqual(
                    self.repo.memory_node_id("Decision", "Lint config", "shared", None, "iface_lint", salt),
                    stable_id("node", *parts),
                )

    def _revision_candidate(self, cid: str, project_id: str, revision_of: str, label: str, text: str, scope: str = "shared") -> None:
        self.repo.upsert_memory_candidate({
            "id": cid, "project_id": project_id, "source_type": "docs", "source_ref": f"{cid}.md",
            "label": label, "type": "Doc", "scope": scope, "text": text, "status": "candidate",
            "metadata": {"revision": True, "revision_of": revision_of, "content_hash": f"hash-{cid}"},
        })

    def test_a_revision_never_supersedes_a_node_the_caller_does_not_own(self) -> None:
        stored, revised = "Deploy guide: deploys run through Jenkins nightly.", "Deploy guide: deploys run through ArgoCD on every merge."
        for owner in ("project_b", None):
            with self.subTest(owner=owner):
                label = f"Deploy guide {owner}"
                old = self._mcp_add(owner, "Doc", label, stored) if owner else self._add(None, label, stored, "Doc")
                before = self._snapshot(old["id"])
                self._revision_candidate(f"rev-a-{owner}", "project_a", old["id"], label, revised)
                promoted = self.service.promote_memory_candidate(f"rev-a-{owner}", caller_project_id="project_a")
                node = self._assert_beside(old["id"], revised, promoted["memory"])
                self.assertEqual(self._snapshot(old["id"]), before)
                self.assertEqual(self._edge_types(node.id, old["id"]), set())
                self.assertEqual(node.metadata["possible_duplicate_of"], old["id"])
                self.assertEqual(node.metadata["dedup_suggestion"]["action"], "update")
                self.assertEqual(promoted["memory"]["metadata"]["possible_duplicate_of"], old["id"])
                # Without a caller project (internal ingestion) the revision supersedes as before.
                self._revision_candidate(f"rev-none-{owner}", "project_a", old["id"], label, f"{revised} Rollbacks are manual.")
                internal = self.service.promote_memory_candidate(f"rev-none-{owner}")
                self.assertEqual(self.repo.get_node(old["id"]).metadata.get("superseded_by"), internal["memory"]["id"])

    def test_own_revisions_beside_a_foreign_predecessor_keep_one_live_chain(self) -> None:
        origins = {
            "file": {"source_type": "docs", "source_ref": "docs/deploy_guide.md"},
            "fact_subject": {"source_type": "chat", "source_ref": "chat-1", "template": "chat_session_atom", "fact_subject": "deploy pipeline"},
        }
        for name, origin in origins.items():
            with self.subTest(origin=name):
                label = f"Deploy guide {name}"
                foreign = self.repo.add_node(
                    "Doc", label, "shared", "Deploy guide v0: Jenkins nightly.", None,
                    metadata={**origin, "fact_key": "jenkins nightly"}, author_project_id="project_b",
                )
                with self.repo._connect() as conn:
                    # The foreign node is the oldest: last in list_nodes' updated_at DESC order.
                    conn.execute("UPDATE memory_nodes SET updated_at = '2020-01-01T00:00:00Z' WHERE id = ?", (foreign.id,))
                before = self._snapshot(foreign.id)
                revisions = []
                for step, text in enumerate(("Deploy guide v1: ArgoCD on merge.", "Deploy guide v2: ArgoCD with canary.")):
                    _, _, live = self.service.ingestion_engine._origin_indexes("project_a")
                    key = origin_key_from_memory_node(foreign)
                    # A second revision of one origin gets a fresh candidate id from the repository.
                    candidate = self.repo.upsert_memory_candidate({
                        "id": f"rev-{name}-{step}", "project_id": "project_a", "source_type": origin["source_type"], "source_ref": origin["source_ref"],
                        "label": label, "type": "Doc", "scope": "shared", "text": text, "status": "candidate",
                        "metadata": {**origin, "fact_key": f"step {step}", "revision": True, "revision_of": live[key]["id"], "content_hash": f"hash-{name}-{step}"},
                    })
                    revisions.append(self.service.promote_memory_candidate(candidate["id"], caller_project_id="project_a")["memory"]["id"])
                first = self.repo.get_node(revisions[0])
                self.assertEqual(first.metadata.get("possible_duplicate_of"), foreign.id)
                self.assertEqual(first.metadata.get("superseded_by"), revisions[1])
                self.assertIn("SUPERSEDES", self._edge_types(revisions[1], revisions[0]))
                live_own = [
                    node.id for node in self.repo.list_nodes()
                    if node.label == label and node.author_project_id == "project_a"
                    and node.status == "active" and not node.metadata.get("invalid_at")
                ]
                self.assertEqual(live_own, [revisions[1]])
                self.assertEqual(self._snapshot(foreign.id), before)

    def _origin_node(self, label: str, author: str, age: str) -> MemoryNode:
        """A live shared Doc of ``docs/deploy_guide.md`` written by ``author``; ``age`` orders updated_at."""
        node = self.repo.add_node(
            "Doc", label, "shared", f"Deploy guide by {author}: Jenkins nightly.", None,
            metadata={"source_type": "docs", "source_ref": "docs/deploy_guide.md"}, author_project_id=author,
        )
        with self.repo._connect() as conn:
            conn.execute("UPDATE memory_nodes SET updated_at = ? WHERE id = ?", (age, node.id))
        return node

    def test_without_a_caller_a_revision_replaces_the_own_origin_not_the_foreign_one(self) -> None:
        foreign = self._origin_node("Deploy guide b", "project_b", "2020-01-01T00:00:00Z")
        own = self._origin_node("Deploy guide a", "project_a", "2021-01-01T00:00:00Z")
        before = self._snapshot(foreign.id)
        _, _, live = self.service.ingestion_engine._origin_indexes("project_a")
        key = origin_key_from_memory_node(own)
        self.assertEqual(live[key]["id"], own.id)
        self.repo.upsert_memory_candidate({
            "id": "rev-internal", "project_id": "project_a", "source_type": "docs", "source_ref": "docs/deploy_guide.md",
            "label": "Deploy guide a", "type": "Doc", "scope": "shared", "text": "Deploy guide v2: ArgoCD on merge.", "status": "candidate",
            "metadata": {"revision": True, "revision_of": live[key]["id"], "content_hash": "hash-internal"},
        })
        promoted = self.service.promote_memory_candidate("rev-internal")
        self.assertEqual(self.repo.get_node(own.id).metadata.get("superseded_by"), promoted["memory"]["id"])
        self.assertEqual(self._snapshot(foreign.id), before)
        self.assertEqual(self.repo.get_node(foreign.id).status, "active")

    def test_without_foreign_nodes_the_live_origin_is_the_oldest_as_before(self) -> None:
        oldest = self._origin_node("Deploy guide old", "project_a", "2020-01-01T00:00:00Z")
        self._origin_node("Deploy guide new", "project_a", "2021-01-01T00:00:00Z")
        _, _, live = self.service.ingestion_engine._origin_indexes("project_a")
        self.assertEqual(live[origin_key_from_memory_node(oldest)]["id"], oldest.id)

    def test_a_revision_of_an_own_node_marks_only_own_facts_stale(self) -> None:
        own = self.repo.add_node(
            "Doc", "Deploy guide", "project", "Deploy guide v1: Jenkins nightly.", "project_a",
            metadata={"source_ref": "docs/deploy_guide.md"},
        )
        facts = {
            project_id: self.repo.add_node("Lesson", f"Release lesson {project_id}", "project", "Read docs/deploy_guide.md before a release.", project_id)
            for project_id in ("project_a", "project_b")
        }
        foreign_before = self._snapshot(facts["project_b"].id)
        self._revision_candidate("rev-own", "project_a", own.id, "Deploy guide", "Deploy guide v2: ArgoCD on every merge.", "project")
        promoted = self.service.promote_memory_candidate("rev-own", caller_project_id="project_a")
        self.assertEqual(self.repo.get_node(own.id).metadata.get("superseded_by"), promoted["memory"]["id"])
        self.assertIn("SUPERSEDES", self._edge_types(promoted["memory"]["id"], own.id))
        self.assertEqual(self.repo.get_node(facts["project_a"].id).metadata.get("source_stale_from"), promoted["memory"]["id"])
        self.assertEqual(self._snapshot(facts["project_b"].id), foreign_before)

    def test_dedup_preview_with_a_project_answers_like_the_write(self) -> None:
        handler, replies = http_handler(self.service)
        cases = {**self.FACTS, "held": ("Decision", "Deploy", "Deploys always run through GitHub Actions.", "deploys ALWAYS run through github actions")}
        for action, (node_type, label, stored, incoming) in cases.items():
            with self.subTest(action=action):
                old = self._mcp_add("project_b", node_type, label, stored)
                payload = {"label": label, "text": incoming, "type": node_type, "scope": "shared", "project_id": "project_a"}
                handler._post_memory_dedup_preview(None, {}, payload)
                preview = replies[-1]
                handler._post_memory(None, {}, payload)
                written = replies[-1]
                self.assertEqual(preview["dedup"], written["dedup"])
                self.assertEqual(preview.get("dedup_held", False), written.get("dedup_held", False))
                self.assertEqual(preview["dedup"].get("details", {}).get("suggested_action", "held"), action)
                self.assertIn(old["id"], {preview["dedup"].get("target_id"), preview["dedup"].get("near_id")})
        # A project-only fact of another project is invisible to the preview, as to the write.
        hidden = self._mcp_add("project_b", "Decision", "Queue", "Jobs go through the Redis queue.", "project")
        handler._post_memory_dedup_preview(None, {}, {"label": "Queue", "text": "Jobs go through the Redis queue.", "type": "Decision", "project_id": "project_a"})
        self.assertEqual(replies[-1]["dedup"], {"action": "new"})
        self.assertNotIn(hidden["id"], json.dumps(replies[-1]))

    def test_reaccepting_an_interface_id_beside_another_projects_node_rewrites_one_own_copy(self) -> None:
        old = self.service.add_memory({"type": "Decision", "scope": "shared", "label": "Lint config", "interface_id": "iface_lint",
                                       "text": "Lint runs on every pull request.", "project_id": "project_b"}, caller_project_id="project_b")
        before = self._snapshot(old["id"])
        ids = []
        for cid, text in (("cand-a1", "Lint is optional for drafts."), ("cand-a2", "Lint is optional for drafts and spikes.")):
            self._candidate(cid, "project_a", "Lint config", text)
            promoted = self.service.promote_memory_candidate(cid, {"interface_id": "iface_lint"}, caller_project_id="project_a")
            self._assert_beside(old["id"], text, promoted["memory"])
            ids.append(promoted["memory"]["id"])
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(ids[0], self.repo.memory_node_id("Decision", "Lint config", "shared", None, "iface_lint", "project_a:"))
        own = [node for node in self.repo.list_nodes() if node.author_project_id == "project_a" and node.status == "active"]
        self.assertEqual([(node.id, node.text) for node in own], [(ids[0], "Lint is optional for drafts and spikes.")])
        self.assertEqual(self._snapshot(old["id"]), before)

    def test_undo_of_a_fold_restores_the_fact_under_its_author(self) -> None:
        _, label, stored, incoming = self.FACTS["enrich"]
        first = self._add("project_a", label, stored, source="mcp")
        folded = self._add("project_a", label, incoming, source="mcp")
        self.assertEqual((folded["id"], folded["dedup"]["action"]), (first["id"], "enrich"))
        undone = self.service.undo_memory_dedup({"event_id": folded["dedup"]["event_id"]})
        restored = self.repo.get_node(undone["restored"]["id"])
        self.assertEqual((restored.text, restored.author_project_id), (incoming, "project_a"))

    def test_contradicting_another_projects_fact_links_without_changing_it(self) -> None:
        old = self._mcp_add("project_b", "Decision", "Кэш", "Используем Redis для кэша сессий")
        before = self._snapshot(old["id"])
        new = self._mcp_add("project_a", "Decision", "Кэш", "Не используем Redis для кэша сессий")
        self.assertEqual(new["dedup"]["action"], "conflict")
        self.assertEqual(self._edge_types(new["id"], old["id"]), {"CONTRADICTS"})
        self.assertEqual(self._snapshot(old["id"]), before)

    def test_suggest_mode_with_a_caller_never_holds(self) -> None:
        text = "Deploys always run through GitHub Actions."
        foreign = self._mcp_add("project_b", "Decision", "Deploy", text)
        before = self._snapshot(foreign["id"])
        self.repo.set_setting("memory_dedup", {"mode": "suggest"})
        written = self._add("project_a", "Deploy", text)
        self.assertFalse(written.get("dedup_held"))
        created = self._assert_beside(foreign["id"], text, written)
        self.assertEqual(created.metadata["dedup_suggestion"]["target_id"], foreign["id"])
        self.assertEqual(self._snapshot(foreign["id"]), before)

    def test_judged_own_match_wins_over_an_earlier_foreign_one(self) -> None:
        foreign = self._add("project_b", "Release cadence B", "Weekly releases happen on Tuesdays via the bot.")
        own = self._add("project_a", "Release cadence A", "Weekly releases happen on Tuesdays via the bot, owned by us.")
        before = self._snapshot(foreign["id"])
        self.repo.set_setting("memory_dedup", {"llm_judge": True, "llm_max_candidates": 2})
        judged: list[str] = []

        def judge(label: str, text: str, node_type: str, candidate: dict[str, Any], *args: Any) -> dict[str, Any]:
            judged.append(candidate["node_id"])
            return {"action": "duplicate", "confidence": 0.95, "reason": "same release rule", "provider": "test"}

        scores = {foreign["id"]: 0.97, own["id"]: 0.9}
        with (
            mock.patch.object(self.service, "_semantic_dedup_scores", return_value=scores),
            mock.patch.object(self.service, "_judge_dedup_pair", side_effect=judge),
        ):
            written = self._add("project_a", "Release schedule", "Releases are cut every Tuesday by the release bot.")
        self.assertEqual(judged[0], foreign["id"])
        self.assertTrue(written.get("dedup_merged"))
        self.assertEqual(written["id"], own["id"])
        self.assertEqual(self._snapshot(foreign["id"]), before)
        # Only the foreign match is judged a duplicate: stored beside it, flagged, never folded.
        with (
            mock.patch.object(self.service, "_semantic_dedup_scores", return_value={foreign["id"]: 0.97}),
            mock.patch.object(self.service, "_judge_dedup_pair", side_effect=judge),
        ):
            beside = self._add("project_a", "Release plan", "Release trains leave on Tuesday with the bot.")
        self.assertFalse(beside.get("dedup_merged"))
        self.assertEqual(beside["dedup"]["details"]["suggested_action"], "duplicate")
        self.assertEqual(self.repo.get_node(beside["id"]).metadata["possible_duplicate_of"], foreign["id"])
        self.assertEqual(self._snapshot(foreign["id"]), before)

    def test_the_gate_sets_aside_at_most_eight_foreign_targets(self) -> None:
        text = "Deploys always run through GitHub Actions."
        first = None
        for index in range(10):
            node = self.repo.add_node("Decision", "Deploy", "shared", text, None, id_salt=f"seed{index}", author_project_id=f"project_{index}")
            first = first or node
        real_classify = self.service.memory_dedup.classify
        with mock.patch.object(self.service.memory_dedup, "classify", side_effect=real_classify) as classify:
            held = self._add("project_a", "Deploy", text)
        self.assertEqual(classify.call_count, 9)
        self.assertTrue(held["dedup_held"])
        self.assertEqual(len([node for node in self.repo.list_nodes() if node.label == "Deploy"]), 10)

class CallerProjectMatrixTests(unittest.TestCase):
    """Every write entry under a caller project × every kind of node already holding the fact."""

    TEXT = "Deploys always run through GitHub Actions on every push."
    # Same content words, not the same wording: a near duplicate, never held.
    OTHER = "On every push deploys always run through GitHub Actions."
    ENTRIES = ("mcp_add", "mcp_bulk", "http", "sdk", "promote", "batch", "auto_accept")
    CASES = ("own_active", "own_archived", "foreign_exact", "foreign_other", "foreign_archived", "legacy_exact")

    def _fresh_store(self) -> None:
        """One store per cell: candidates or nodes of another cell must not take part."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.service = ArchitectOSService(self.root)
        self.repo = self.service.repository

    def _view(self, node_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        node = self.repo.get_node(node_id)
        assert node is not None
        return node.to_dict(), self.repo.list_memory_events(node_id)

    def _write(self, entry: str, label: str, text: str) -> dict[str, Any]:
        """Write as project_a through ``entry``; returns {"id", "held", "candidate"} (id None when nothing landed)."""
        item = {"label": label, "text": text, "type": "Decision"}
        if entry == "mcp_add":
            written = mcp_call(self.root, "project_a", "memory_add", {**item, "scope": "shared"})
            return {"id": written["id"], "held": written["dedup_held"]}
        if entry == "mcp_bulk":
            bulk = mcp_call(self.root, "project_a", "memory_add_bulk", {"scope": "shared", "items": [item]})
            return {"id": bulk["results"][0]["id"], "held": bulk["results"][0]["status"] == "held"}
        if entry == "http":
            handler, replies = http_handler(self.service)
            handler._post_memory(None, {}, {**item, "scope": "shared", "project_id": "project_a"})
            return {"id": replies[0]["id"], "held": bool(replies[0].get("dedup_held"))}
        if entry == "sdk":
            added = ArchitectOS(self.root).add(text, label=label, type="Decision", project_id="project_a", scope="shared")
            return {"id": added["id"], "held": bool(added.get("dedup_held"))}
        cid = f"cand-{entry}-{label}"
        self.repo.upsert_memory_candidate({
            "id": cid, "project_id": "project_a", "source_type": "mcp", "source_ref": "turn-1", "label": label,
            "type": "Decision", "scope": "shared", "text": text, "status": "candidate", "confidence": 0.95,
            "metadata": {"template": "mcp_turn_atom", "kind": "project"},
        })
        if entry == "promote":
            handler, replies = http_handler(self.service)
            handler._post_memory_candidate_promote(re.match(r"(?P<candidate_id>.+)", cid), {}, {})
        elif entry == "batch":
            handler, replies = http_handler(self.service)
            handler._post_memory_candidates_batch(None, {}, {"project_id": "project_a", "action": "accept", "status": "candidate"})
        else:
            self.service._maybe_auto_accept_chat_candidate(self.repo.get_memory_candidate(cid), caller_project_id="project_a")
        candidate = self.repo.get_memory_candidate(cid)
        held = bool(candidate["metadata"].get("dedup_held_by"))
        node_id = candidate.get("promoted_node_id") or candidate["metadata"].get("dedup_held_by") or None
        return {"id": node_id, "held": held, "candidate": candidate}

    def _seed(self, case: str, label: str) -> str:
        text = self.OTHER if case == "foreign_other" else self.TEXT
        if case == "legacy_exact":
            return self.service.add_memory({"label": label, "text": text, "type": "Decision", "scope": "shared"})["id"]
        owner = "project_a" if case.startswith("own") else "project_b"
        node_id = mcp_call(self.root, owner, "memory_add", {"label": label, "text": text, "type": "Decision", "scope": "shared"})["id"]
        if case.endswith("archived"):
            node = self.repo.get_node(node_id)
            node.status = "archived"
            self.repo.upsert_node(node)
        return node_id

    def test_matrix(self) -> None:
        for case in self.CASES:
            for entry in self.ENTRIES:
                with self.subTest(case=case, entry=entry):
                    self._fresh_store()
                    label = "Deploy pipeline"
                    seeded = self._seed(case, label)
                    before = self._view(seeded)
                    nodes_before = len(self.repo.list_nodes())
                    result = self._write(entry, label, self.TEXT)
                    stored = self.repo.get_node(seeded)
                    if case.startswith("own"):
                        self.assertFalse(result["held"])
                        if case == "own_active" and entry == "auto_accept":
                            # A repeat of a stored fact stays in review rather than auto-writing.
                            self.assertEqual(result["candidate"]["status"], "candidate")
                            self.assertEqual(self._view(seeded), before)
                            continue
                        self.assertEqual(result["id"], seeded)
                        self.assertEqual(stored.status, "active")
                        if case == "own_active":
                            self.assertEqual(stored.metadata.get("dedup_hits"), 1)
                        continue
                    # Never changed by a project that does not own it.
                    self.assertEqual(self._view(seeded), before)
                    if case in {"foreign_exact", "legacy_exact"}:
                        self.assertTrue(result["held"])
                        self.assertEqual(result["id"], seeded)
                        self.assertEqual(len(self.repo.list_nodes()), nodes_before)
                        if "candidate" in result:
                            self.assertEqual(result["candidate"]["status"], "rejected")
                            self.assertFalse(result["candidate"].get("promoted_node_id"))
                            self.assertNotIn("auto_accepted", result["candidate"]["metadata"])
                        continue
                    self.assertFalse(result["held"])
                    if case == "foreign_other" and entry == "auto_accept":
                        # A near duplicate (even of another project's fact) stays in review.
                        self.assertEqual(result["candidate"]["status"], "candidate")
                        continue
                    self.assertNotEqual(result["id"], seeded)
                    created = self.repo.get_node(result["id"])
                    self.assertEqual((created.status, created.author_project_id), ("active", "project_a"))
                    self.assertEqual(created.id, self.repo.memory_node_id("Decision", label, "shared", None, None, caller_salt("project_a", self.TEXT)))
                    if case == "foreign_other":
                        self.assertEqual(created.metadata.get("possible_duplicate_of"), seeded)
                        self.assertEqual(created.metadata["dedup_suggestion"]["target_id"], seeded)

if __name__ == "__main__":
    unittest.main()
