"""Stage 2: the dedup gate reads an index instead of scanning every node."""
from __future__ import annotations

import itertools
import json
import random
import sqlite3
import string
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from backend.architectos import memory_dedup
from backend.architectos.memory_dedup import (
    DEDUP_INDEX_VERSION,
    MemoryDedupGate,
    fact_features,
    features_to_json,
    incoming_features,
)
from backend.architectos.memory_ingestion import CHAT_FACT_BOILERPLATE_PREFIXES
from backend.architectos.service import ArchitectOSService
from backend.architectos.storage import SQLiteMemoryRepository
from backend.architectos.storage_dedup import StorageDedupMixin, dedup_index_eligible

_LABELS = ["Лимит API", "Кэш", "Деплой", "Порт", "Cache", "Queue"]
_WORDS = (
    "сервис база кэш лимит запрос порт очередь деплой релиз узел агент "
    "service cache limit request port queue deploy release node agent"
).split()


# A row written by an older release: a version below the current one.
OUTDATED_VERSION = DEDUP_INDEX_VERSION - 1
ASK_HEADER = CHAT_FACT_BOILERPLATE_PREFIXES[1]
MCP_HEADER = CHAT_FACT_BOILERPLATE_PREFIXES[2]
# Features an older release could have stored for any text: parseable, but not this text's.
STALE_FEATURES = features_to_json(fact_features("stale", "stale"))
# Statement of an atom, the stored one, the incoming one, and the gate verdict the v1 features already give.
STALE_ROW_CASES = (
    ("repeat", "Review candidates expire after 7 days unless a person accepts them",
     "Review candidates expire after 7 days unless a person accepts them.", "duplicate"),
    ("number en", "The session cache TTL is 30 minutes", "The session cache TTL is 45 minutes", "update"),
    ("number ru", "Таймаут сессии кэша составляет 30 минут", "Таймаут сессии кэша составляет 45 минут", "update"),
    ("negation en", "We use Redis for the session cache", "We do not use Redis for the session cache", None),
    ("negation ru", "Мы используем Redis для кэша сессий", "Мы не используем Redis для кэша сессий", None),
)
FEATURES_V1_REFERENCE = Path(__file__).parent / "data" / "dedup_features_v1.jsonl"


def _random_fact(rng: random.Random) -> tuple[str, str, str]:
    words = " ".join(rng.choice(_WORDS) for _ in range(rng.randint(3, 7)))
    negation = "не " if rng.random() < 0.2 else ""
    number = f" {rng.choice([10, 20, 5000])}" if rng.random() < 0.5 else ""
    return rng.choice(_LABELS), f"{negation}{words}{number}", rng.choice(["Lesson", "Decision", "Concept"])


class DedupIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.service = ArchitectOSService(Path(self._tmp.name))
        self.repo = self.service.repository
        self.gate: MemoryDedupGate = self.service.memory_dedup

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _indexed_ids(self) -> set[str]:
        with self.repo._connect() as conn:
            return {row[0] for row in conn.execute("SELECT node_id FROM memory_dedup_index")}

    @staticmethod
    def _atom(fact: str, header: str = MCP_HEADER) -> tuple[str, str]:
        """Label and body of a chat/MCP fact atom, shaped like ``_enqueue_session_fact_atoms`` builds them."""
        return f"MCP fact: {fact[:72]}", f"{header}\n\n{fact}"

    def _add_atom(
        self, fact: str, *, header: str = MCP_HEADER, template: str = "mcp_turn_atom", project_id: str = "architectos",
    ) -> str:
        label, body = self._atom(fact, header)
        metadata = {"template": template} if template else {}
        return self.repo.add_node("Lesson", label, "project", body, project_id, metadata=metadata).id

    def _add_plain(self, label: str, text: str, node_type: str = "Decision") -> str:
        return self.repo.add_node(node_type, label, "project", text, "architectos").id

    def _age(
        self, node_ids: Any, *, features: str | None = None, subject: str | None = None, drop_stems: bool = False,
    ) -> None:
        """Make the index rows look like an older release wrote them."""
        with self.repo._connect() as conn:
            for node_id in node_ids:
                conn.execute("UPDATE memory_dedup_index SET version = ? WHERE node_id = ?", (OUTDATED_VERSION, node_id))
                if features is not None:
                    conn.execute("UPDATE memory_dedup_index SET features = ? WHERE node_id = ?", (features, node_id))
                if subject is not None:
                    conn.execute("UPDATE memory_dedup_index SET subject = ? WHERE node_id = ?", (subject, node_id))
                if drop_stems:
                    conn.execute("DELETE FROM memory_dedup_stems WHERE node_id = ?", (node_id,))

    @staticmethod
    def _written_by_v1() -> Any:
        """Index rows written while active, exactly as release v1 wrote them (features of label and text)."""
        return mock.patch.multiple(memory_dedup, fact_features=memory_dedup._text_features, DEDUP_INDEX_VERSION=1)

    def _index_dump(self) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
        with self.repo._connect() as conn:
            rows = conn.execute(
                "SELECT node_id, scope, project_key, node_type, label, subject, source_hash, version, features "
                "FROM memory_dedup_index ORDER BY node_id"
            ).fetchall()
            stems = conn.execute(
                "SELECT stem, node_id, scope, project_key FROM memory_dedup_stems ORDER BY node_id, stem"
            ).fetchall()
        return [tuple(row) for row in rows], [tuple(row) for row in stems]

    def _row_versions(self) -> set[int]:
        with self.repo._connect() as conn:
            return {int(row[0]) for row in conn.execute("SELECT DISTINCT version FROM memory_dedup_index")}

    def _refresh_until_current(self) -> dict[str, int]:
        """Repeat the background pass until nothing is outdated; returns the summed counts."""
        totals = {"recomputed": 0, "stamped": 0}
        for _ in range(200):
            result = self.repo.refresh_dedup_index(budget_seconds=1.0)
            totals = {key: totals[key] + int(result[key]) for key in totals}
            if not result["remaining"]:
                return totals
        self.fail("refresh_dedup_index left outdated rows after 200 passes")

    def _assert_equals_a_build_from_scratch(self) -> None:
        after_pass = self._index_dump()
        self.repo.rebuild_dedup_index()
        self.assertEqual(after_pass, self._index_dump())

    def _brute_force(self, label: str, text: str, node_type: str) -> Any:
        """The stage 1 scan: compare against every eligible node."""
        incoming = incoming_features(label, text)
        rank = {"duplicate": 4, "enrich": 3, "update": 2, "conflict": 1}
        best_action, best_rank, best_score, best_target, best_near = "new", 0, 0.0, "", 0.0
        for node in sorted(self.repo.list_nodes(), key=lambda item: item.id):
            if not dedup_index_eligible(node) or node.scope != "project" or node.project_id != "architectos":
                continue
            if not self.gate.type_matches(node.type, node_type):
                continue
            verdict = self.gate.compare(incoming, fact_features(node.label, node.text))
            best_near = max(best_near, verdict.near_score)
            if verdict.action == "duplicate" and verdict.reason == "exact restatement":
                return "duplicate", node.id, 1.0
            r = rank.get(verdict.action, 0)
            if r and (r > best_rank or (r == best_rank and verdict.score > best_score)):
                best_action, best_rank, best_score, best_target = verdict.action, r, verdict.score, node.id
        return best_action, best_target, best_near

    def test_index_matches_full_scan_on_random_corpus(self) -> None:
        rng = random.Random(20261012)
        for i in range(250):
            label, text, node_type = _random_fact(rng)
            # Distinct ids even under equal labels; same as the gate's salting.
            self.repo.add_node(node_type, label, "project", text, "architectos", id_salt=str(i))
        for _ in range(120):
            label, text, node_type = _random_fact(rng)
            decision = self.gate.classify(label, text, node_type, "project", "architectos")
            action, target, near = self._brute_force(label, text, node_type)
            self.assertEqual(decision.action, action, (label, text))
            if action != "new":
                self.assertEqual(decision.target_id, target, (label, text))
            elif near >= self.gate.NEAR_DUPLICATE_SIMILARITY:
                self.assertAlmostEqual(decision.near_score, near, places=9, msg=(label, text))
            else:
                # Below the flag threshold the index may skip weak neighbours.
                self.assertLess(decision.near_score, self.gate.NEAR_DUPLICATE_SIMILARITY, (label, text))

    def test_near_min_shared_never_drops_a_flagged_neighbour(self) -> None:
        rng = random.Random(7)
        universe = [f"w{i}" for i in range(14)]
        for _ in range(4000):
            left = frozenset(rng.sample(universe, rng.randint(1, 10)))
            right = frozenset(rng.sample(universe, rng.randint(1, 10)))
            a = fact_features("x", "x")
            score = self.gate.near_score(
                type(a)(a.key, a.label_core, left, a.numbers, a.negated, a.identifiers),
                type(a)(a.key, a.label_core, right, a.numbers, a.negated, a.identifiers),
            )
            if score >= self.gate.NEAR_DUPLICATE_SIMILARITY:
                self.assertGreaterEqual(len(left & right), self.gate.near_min_shared(len(left)))

    def test_rows_follow_node_lifecycle(self) -> None:
        node = self.service.add_memory({"label": "Порт", "text": "Сервер слушает порт 8765", "type": "Decision"})
        self.assertIn(node["id"], self._indexed_ids())
        stored = self.repo.get_node(node["id"])
        assert stored is not None
        stored.status = "archived"
        self.repo.upsert_node(stored)
        self.assertNotIn(node["id"], self._indexed_ids())
        stored.status = "active"
        self.repo.upsert_node(stored)
        self.assertIn(node["id"], self._indexed_ids())

    def test_superseded_fact_leaves_the_index_until_undo(self) -> None:
        old = self.service.add_memory({"label": "Лимит API", "text": "Лимит запросов 5000 в час", "type": "Constraint"})
        new = self.service.add_memory({"label": "Лимит API", "text": "Лимит запросов 9000 в час", "type": "Constraint"})
        self.assertEqual(new["dedup"]["action"], "update")
        self.assertNotIn(old["id"], self._indexed_ids())
        self.service.undo_memory_dedup({"event_id": new["dedup"]["event_id"]})
        self.assertIn(old["id"], self._indexed_ids())

    def test_near_duplicate_under_another_label_is_flagged(self) -> None:
        base = self.service.add_memory({
            "label": "Хранилище", "text": "Данные проекта лежат в локальной базе SQLite", "type": "Decision",
        })
        # Same text filed under a different subject: not folded, only flagged.
        other = self.service.add_memory({
            "label": "Хранилище данных", "text": "Данные проекта лежат в локальной базе SQLite", "type": "Decision",
        })
        self.assertEqual(other["dedup"]["action"], "new")
        self.assertEqual(other["metadata"].get("possible_duplicate_of"), base["id"])

    def test_startup_builds_an_empty_index_but_leaves_outdated_rows_to_the_background_pass(self) -> None:
        plain = self._add_plain("Кэш", "Используем Redis для кэша сессий")
        atom = self._add_atom("Review candidates expire after 7 days unless a person accepts them")
        with self.repo._connect() as conn:
            conn.execute("DELETE FROM memory_dedup_stems")
            conn.execute("DELETE FROM memory_dedup_index")
        self.repo._migrate()
        self.assertEqual(self._indexed_ids(), {plain, atom})
        self.assertEqual(self._row_versions(), {DEDUP_INDEX_VERSION})
        self._age([plain])
        self._age([atom], features="{}", drop_stems=True)
        outdated = self._index_dump()
        self.repo._migrate()
        self.assertEqual(self._index_dump(), outdated, "start-up must not touch an outdated index")
        self._refresh_until_current()
        self.assertEqual(self._row_versions(), {DEDUP_INDEX_VERSION})
        self._assert_equals_a_build_from_scratch()
        again = self.service.add_memory({"label": "Кэш", "text": "Для кэша сессий используем Redis", "type": "Decision"})
        self.assertTrue(again.get("dedup_merged"))

    def test_init_leaves_outdated_rows_alone(self) -> None:
        self._age([self._add_plain("Кэш", "Используем Redis для кэша сессий"), self._add_atom("Cache entries live 30 minutes")])
        before = self._index_dump()
        with mock.patch.object(
            SQLiteMemoryRepository, "rebuild_dedup_index", side_effect=AssertionError("init must not rebuild an outdated index"),
        ):
            SQLiteMemoryRepository(self.repo.project_root)
        self.assertEqual(self._index_dump(), before)

    def test_a_repository_opens_while_another_connection_holds_the_write_lock(self) -> None:
        self._age([self._add_plain("Кэш", "Используем Redis для кэша сессий"), self._add_atom("Cache entries live 30 minutes")])
        holder = sqlite3.connect(self.repo.db_path, timeout=1)
        holder.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        try:
            SQLiteMemoryRepository(self.repo.project_root)
        finally:
            holder.rollback()
            holder.close()
        self.assertLess(time.monotonic() - started, 1.0)

    def test_gate_reads_an_outdated_atom_row_like_a_fresh_one(self) -> None:
        for number, (name, stored, incoming, expected) in enumerate(STALE_ROW_CASES):
            incoming_label, incoming_body = self._atom(incoming)
            for aged in ("features", "subject"):
                with self.subTest(case=name, aged=aged):
                    project_id = f"stale-{number}-{aged}"
                    node_id = self._add_atom(stored, project_id=project_id)
                    fresh = self.gate.classify(incoming_label, incoming_body, "Lesson", "project", project_id)
                    if expected:
                        self.assertEqual((fresh.action, fresh.target_id), (expected, node_id))
                    # "features": the row holds another text's features. "subject": the row lies under a subject
                    # the incoming fact does not hash to, so only its stems lead to it.
                    self._age(
                        [node_id],
                        features=STALE_FEATURES if aged == "features" else None,
                        subject="stale-subject" if aged == "subject" else None,
                    )
                    outdated = self.gate.classify(incoming_label, incoming_body, "Lesson", "project", project_id)
                    self.assertEqual((outdated.action, outdated.target_id), (fresh.action, fresh.target_id))

    def test_background_pass_brings_outdated_atom_rows_to_a_build_from_scratch(self) -> None:
        atoms = [
            self._add_atom("Review candidates expire after 7 days unless a person accepts them", template="chat_session_atom", header=ASK_HEADER),
            self._add_atom("Таймаут сессии кэша составляет 30 минут"),
            # An atom restored by undo has no template: the text alone makes it an atom.
            self._add_atom("The Azure Boards sync job fetches at most 200 work items per WIQL window", template=""),
        ]
        plain = [self._add_plain("Кэш", "Используем Redis для кэша сессий"), self._add_plain("Порт", "Сервер слушает порт 8765")]
        self._age(atoms, features=STALE_FEATURES, subject="stale-subject", drop_stems=True)
        self._age(plain)
        totals = self._refresh_until_current()
        self.assertEqual(totals, {"recomputed": len(atoms), "stamped": len(plain)})
        self.assertEqual(self._row_versions(), {DEDUP_INDEX_VERSION})
        self._assert_equals_a_build_from_scratch()

    def test_background_pass_only_stamps_rows_without_an_atom_header(self) -> None:
        favorite_fact = "Данные проекта хранятся в SQLite"
        plain_ids = [
            self._add_plain("Кэш", "Используем Redis для кэша сессий"),
            # Explicit saves and notes that merely mention a header are not atoms.
            self._add_plain(f"Favorite: {favorite_fact}", f"Favorited durable fact from assistant reply.\n\n{favorite_fact}", "Lesson"),
            self._add_plain("Chat note", f"Meeting notes. {ASK_HEADER} The cache TTL is 30 minutes", "Lesson"),
        ]
        atoms = [self._add_atom("Cache entries live 30 minutes"), self._add_atom("Таймаут сессии кэша составляет 30 минут")]
        self._age(plain_ids, features=STALE_FEATURES, subject="stale-subject")
        self._age(atoms, features=STALE_FEATURES, subject="stale-subject")
        before = self._index_dump()
        totals = self._refresh_until_current()
        self.assertEqual(totals, {"recomputed": len(atoms), "stamped": len(plain_ids)})
        after = self._index_dump()
        before_rows, after_rows = {row[0]: row for row in before[0]}, {row[0]: row for row in after[0]}
        for node_id in plain_ids:
            self.assertEqual(
                after_rows[node_id][:7] + after_rows[node_id][8:], before_rows[node_id][:7] + before_rows[node_id][8:],
                "features, subject and hash of a non-atom row stay byte-identical",
            )
            self.assertEqual(after_rows[node_id][7], DEDUP_INDEX_VERSION)
        for node_id in atoms:
            self.assertNotEqual(after_rows[node_id][8], STALE_FEATURES)
            self.assertEqual(after_rows[node_id][7], DEDUP_INDEX_VERSION)
        self.assertEqual(
            [row for row in after[1] if row[1] in plain_ids], [row for row in before[1] if row[1] in plain_ids],
        )

    def test_failure_on_a_row_rolls_back_the_batch_and_a_repeat_finishes_the_rest(self) -> None:
        atoms = [self._add_atom(f"Cache entry number {i} lives for {i + 10} minutes in the session store") for i in range(6)]
        self._age(atoms, features=STALE_FEATURES, subject="stale-subject", drop_stems=True)
        before = self._index_dump()
        real = StorageDedupMixin._sync_dedup_index_row
        rows_seen = itertools.count(1)

        def fail_on_fourth_row(*args: Any, **kwargs: Any) -> bool:
            if next(rows_seen) == 4:
                raise RuntimeError("injected failure on the fourth row")
            return real(*args, **kwargs)

        with mock.patch.object(StorageDedupMixin, "_sync_dedup_index_row", staticmethod(fail_on_fourth_row)):
            try:
                self.repo.refresh_dedup_index(budget_seconds=60.0)
            except (RuntimeError, sqlite3.Error):
                pass  # the pass may re-raise or report the failure; the rollback is what counts
        self.assertGreaterEqual(next(rows_seen), 5, "the pass must recompute atom rows through _sync_dedup_index_row")
        self.assertEqual(self._index_dump(), before, "a failed batch leaves no half-written rows")
        self._refresh_until_current()
        self._assert_equals_a_build_from_scratch()

    def test_add_memory_during_the_background_pass_is_not_blocked(self) -> None:
        rng = random.Random(41)
        # Long atoms with many distinct stems: one unbounded pass over them holds the write lock for seconds.
        with self.repo.transaction():
            atoms = [
                self._add_atom(" ".join("".join(rng.choices(string.ascii_lowercase, k=8)) for _ in range(45)) + f" number {i}")
                for i in range(400)
            ]
        self._age(atoms, features=STALE_FEATURES, subject="stale-subject", drop_stems=True)
        failures: list[BaseException] = []
        pass_started = threading.Event()

        def run_pass() -> None:
            try:
                pass_started.set()
                while self.repo.refresh_dedup_index(budget_seconds=0.15)["remaining"]:
                    time.sleep(0.3)  # the caller's pause between batches
            except Exception as exc:  # noqa: BLE001 - reported through the assertions below
                failures.append(exc)

        worker = threading.Thread(target=run_pass)
        worker.start()
        pass_started.wait()
        time.sleep(0.05)
        started = time.monotonic()
        try:
            self.service.add_memory({"label": "Порт", "text": "Сервер слушает порт 8765", "type": "Decision"})
        except sqlite3.OperationalError as exc:
            failures.append(exc)
        waited = time.monotonic() - started
        worker.join(timeout=60)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertLess(waited, 1.0, "a writer waits for one bounded batch, not for the whole pass")
        self.assertEqual(self._row_versions(), {DEDUP_INDEX_VERSION})

    def test_row_written_back_with_the_old_version_is_read_right_and_fixed_by_the_next_pass(self) -> None:
        fact = "Review candidates expire after 7 days unless a person accepts them"
        node_id = self._add_atom(fact)
        self._age([node_id])
        self._refresh_until_current()
        self.assertEqual(self._row_versions(), {DEDUP_INDEX_VERSION})
        # An older release still running upserts the node and writes its own row, after the pass had finished.
        self._age([node_id], features=STALE_FEATURES)
        label, body = self._atom(fact + ".")
        decision = self.gate.classify(label, body, "Lesson", "project", "architectos")
        self.assertEqual((decision.action, decision.target_id), ("duplicate", node_id))
        self.assertEqual(self._refresh_until_current(), {"recomputed": 1, "stamped": 0})
        self._assert_equals_a_build_from_scratch()

    def test_features_of_texts_without_an_atom_header_equal_the_v1_reference(self) -> None:
        reference = [json.loads(line) for line in FEATURES_V1_REFERENCE.read_text(encoding="utf-8").splitlines() if line]
        self.assertGreaterEqual(len(reference), 90)
        for entry in reference:
            with self.subTest(label=entry["label"][:40], text=entry["text"][:40]):
                self.assertEqual(features_to_json(fact_features(entry["label"], entry["text"])), entry["features"])

    def test_compaction_after_the_pass_keeps_atoms_that_differ_in_tokens_with_digits(self) -> None:
        pairs = [("p95", "p99"), ("AES128", "AES256"), ("k8s", "k3s"), ("x86", "x64"), ("ipv4", "ipv6")]
        with self._written_by_v1():
            ids = [
                self._add_atom(f"The {token} setting of the search cluster is checked on every deploy")
                for pair in pairs for token in pair
            ]
        self.assertEqual(self._row_versions(), {1})
        self.service.rebuild_memory_dedup_index()
        self.assertEqual(self._row_versions(), {DEDUP_INDEX_VERSION})
        report = self.service.compact_memory({"project_id": "architectos", "dry_run": True})
        self.assertEqual(report["would_merge"], 0)
        self.service.run_scheduled_compaction()
        self.assertEqual([self.repo.get_node(node_id).status for node_id in ids], ["active"] * len(ids))

    def test_manual_reindex_reports_indexed_nodes(self) -> None:
        node = self.service.add_memory({"label": "Порт", "text": "Сервер слушает порт 8765", "type": "Decision"})
        result = self.service.rebuild_memory_dedup_index()
        self.assertGreaterEqual(result["indexed"], 1)
        self.assertIn(node["id"], self._indexed_ids())

    def test_wipe_clears_index_rows(self) -> None:
        project = self.service.create_project({"name": "Wipe dedup", "description": "dedup index wipe"})
        node = self.service.add_memory({
            "project_id": project["id"], "label": "Порт", "type": "Decision", "scope": "project",
            "text": "Сервер слушает порт 8765",
        })
        self.assertIn(node["id"], self._indexed_ids())
        self.service.wipe_project_memory(project["id"], confirm=True)
        self.assertNotIn(node["id"], self._indexed_ids())
        with self.repo._connect() as conn:
            stems = conn.execute("SELECT count(*) FROM memory_dedup_stems WHERE node_id = ?", (node["id"],)).fetchone()[0]
        self.assertEqual(stems, 0)


if __name__ == "__main__":
    unittest.main()
