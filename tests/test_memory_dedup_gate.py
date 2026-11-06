"""Stage 1 dedup gate: one classifier for every memory write path."""
from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from http import HTTPStatus
from typing import Any
from unittest import mock

from backend.architectos.mcp_server import MemoryMCPServer
from backend.architectos.memory_dedup import MemoryDedupGate, content_salt, fact_features, stem
from backend.architectos.models import MemoryNode, owned_by
from backend.architectos.sdk import ArchitectOS
from backend.architectos.server import ArchitectOSHandler
from backend.architectos.service import ArchitectOSService


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
                self.assertEqual(created.id, self._shared_id(node_type, label, content_salt(f"project_a\n{incoming}")))
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
                expected = self._shared_id("Decision", label, "" if slot == "free" else content_salt(f"project_a\n{text}"))
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
                self.assertEqual(stored.id, self._shared_id("Decision", label, content_salt(f"project_a\n{stored.text}")))
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
        foreign = self._mcp_add("project_b", "Decision", "Deploy", text)
        own = self._add("project_a", "Deploy", text, dedup=False)
        self.assertNotEqual(own["id"], foreign["id"])
        before = self._snapshot(foreign["id"])
        again = self._mcp_add("project_a", "Decision", "Deploy", text)
        self.assertFalse(again["dedup_held"])
        self.assertEqual(again["id"], own["id"])
        self.assertEqual(self.repo.get_node(own["id"]).metadata.get("dedup_hits"), 1)
        self.assertEqual(self._snapshot(foreign["id"]), before)

    def test_dedup_off_with_a_caller_stores_an_own_copy(self) -> None:
        text = "Deploys always run through GitHub Actions."
        foreign = self._mcp_add("project_b", "Decision", "Deploy", text)
        before = self._snapshot(foreign["id"])
        self.repo.set_setting("memory_dedup", {"mode": "off"})
        copies = {self._add("project_a", "Deploy", text)["id"] for _ in range(2)}
        copies |= {self._add("project_a", "Deploy", text, dedup=False)["id"]}
        self.assertEqual(copies, {self._shared_id("Decision", "Deploy", content_salt(f"project_a\n{text}"))})
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

if __name__ == "__main__":
    unittest.main()
