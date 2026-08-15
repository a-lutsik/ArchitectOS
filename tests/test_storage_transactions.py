from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from backend.architectos.models import MemoryNode
from backend.architectos.service import ArchitectOSService
from backend.architectos.storage import SCHEMA_VERSION, SQLiteMemoryRepository


class StorageTransactionTests(unittest.TestCase):
    def test_graph_hard_delete_rolls_back_when_second_step_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            repository = service.repository
            alpha = repository.add_node("Concept", "Tx Alpha", "project", "Alpha transactional body text", project_id="architectos")
            beta = repository.add_node("Concept", "Tx Beta", "project", "Beta transactional body text", project_id="architectos")
            edge = repository.add_edge(alpha.id, beta.id, "RELATED_TO", "project")

            # Crash on the SECOND step of the wrapped hard-delete flow.
            with mock.patch.object(repository, "upsert_node", side_effect=RuntimeError("simulated crash after edge delete")):
                with self.assertRaises(RuntimeError):
                    service.apply_graph_command({"action": "delete", "node_id": alpha.id, "hard": True})

            # Rollback worked: the edges deleted in step one are still present.
            self.assertIn(edge.id, repository.list_edge_ids())
            surviving = repository.get_node(alpha.id)
            self.assertIsNotNone(surviving)
            self.assertEqual(surviving.status, "active")

    def test_graph_merge_rolls_back_when_second_step_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            repository = service.repository
            alpha = repository.add_node("Concept", "Tx Merge Alpha", "project", "Alpha merge body text", project_id="architectos")
            beta = repository.add_node("Concept", "Tx Merge Beta", "project", "Beta merge body text", project_id="architectos")
            gamma = repository.add_node("Concept", "Tx Merge Gamma", "project", "Gamma merge body text", project_id="architectos")
            # alpha -> gamma gets relinked to beta -> gamma during the merge.
            repository.add_edge(alpha.id, gamma.id, "RELATED_TO", "project")
            edge_ids_before = repository.list_edge_ids()
            beta_text_before = repository.get_node(beta.id).text

            original_upsert = repository.upsert_node

            def fail_on_source_upsert(node: MemoryNode, *, notify: bool = True) -> MemoryNode:
                if node.id == alpha.id:
                    raise RuntimeError("simulated crash mid-merge")
                return original_upsert(node, notify=notify)

            # Crash on the source upsert, AFTER edges were relinked and target saved.
            with mock.patch.object(repository, "upsert_node", side_effect=fail_on_source_upsert):
                with self.assertRaises(RuntimeError):
                    service.merge_graph_nodes(alpha.id, {"target_id": beta.id})

            # Nothing half-applied: relinked edge gone, target text and source status unchanged.
            self.assertEqual(repository.list_edge_ids(), edge_ids_before)
            self.assertEqual(repository.get_node(alpha.id).status, "active")
            self.assertEqual(repository.get_node(beta.id).text, beta_text_before)

    def test_nested_transaction_reuses_active_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteMemoryRepository(Path(tmp))
            with repository.transaction() as outer:
                with repository.transaction() as inner:
                    self.assertIs(outer, inner)

    def test_connect_inside_transaction_participates_without_early_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteMemoryRepository(Path(tmp))
            node = repository.add_node("Concept", "Tx Node", "project", "Node body text for transaction test")
            original_label = node.label
            with self.assertRaises(RuntimeError):
                with repository.transaction():
                    node.label = "Changed inside transaction"
                    # upsert_node uses _connect, which must join the active transaction.
                    repository.upsert_node(node)
                    peek = sqlite3.connect(repository.db_path)
                    try:
                        seen_label = peek.execute("SELECT label FROM memory_nodes WHERE id = ?", (node.id,)).fetchone()[0]
                    finally:
                        peek.close()
                    # No early commit: other connections still see the pre-transaction row.
                    self.assertEqual(seen_label, original_label)
                    raise RuntimeError("force rollback")
            self.assertEqual(repository.get_node(node.id).label, original_label)

    def test_transaction_commits_on_clean_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteMemoryRepository(Path(tmp))
            node = repository.add_node("Concept", "Tx Commit", "project", "Commit body text for transaction test")
            with repository.transaction():
                node.label = "Committed label"
                repository.upsert_node(node)
            peek = sqlite3.connect(repository.db_path)
            try:
                seen_label = peek.execute("SELECT label FROM memory_nodes WHERE id = ?", (node.id,)).fetchone()[0]
            finally:
                peek.close()
            self.assertEqual(seen_label, "Committed label")

    def test_transaction_connection_stays_on_creating_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteMemoryRepository(Path(tmp))
            seen: list[bool] = []

            def worker() -> None:
                seen.append(getattr(repository._local, "transaction_connection", None) is None)

            with repository.transaction():
                thread = threading.Thread(target=worker)
                thread.start()
                thread.join()
            self.assertEqual(seen, [True])

    def test_plain_repository_method_works_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteMemoryRepository(Path(tmp))
            node = repository.add_node("Concept", "Plain Node", "project", "Plain body text without transaction")
            fetched = repository.get_node(node.id)
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched.label, "Plain Node")
            # Committed immediately: visible from a separate connection.
            peek = sqlite3.connect(repository.db_path)
            try:
                count = peek.execute("SELECT count(*) FROM memory_nodes WHERE id = ?", (node.id,)).fetchone()[0]
            finally:
                peek.close()
            self.assertEqual(count, 1)


class SchemaMigrationTests(unittest.TestCase):
    @staticmethod
    def _user_version(repository: SQLiteMemoryRepository) -> int:
        with repository._connect() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    @staticmethod
    def _memory_node_indexes(repository: SQLiteMemoryRepository) -> set[str]:
        with repository._connect() as conn:
            return {str(row["name"]) for row in conn.execute("PRAGMA index_list(memory_nodes)")}

    def test_fresh_database_is_stamped_with_current_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteMemoryRepository(Path(tmp))
            self.assertEqual(self._user_version(repository), SCHEMA_VERSION)
            self.assertIn("idx_memory_nodes_updated_at", self._memory_node_indexes(repository))

    def test_migrate_from_version_zero_applies_migrations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteMemoryRepository(Path(tmp))
            # Simulate a pre-versioning database: no version stamp, no new index,
            # and settings seeded by an older release (missing later defaults).
            with repository._connect() as conn:
                conn.execute("DROP INDEX IF EXISTS idx_memory_nodes_updated_at")
                conn.execute("PRAGMA user_version=0")
            repository.set_setting("memory_lifecycle", {"enabled": True})
            repository.set_setting("mcp_servers", {"servers": [{"id": "granola", "label": "Granola", "command": ["npx", "-y", "mcp-granola"], "enabled": False, "status": "planned"}]})

            repository._migrate()

            self.assertEqual(self._user_version(repository), SCHEMA_VERSION)
            self.assertIn("idx_memory_nodes_updated_at", self._memory_node_indexes(repository))
            lifecycle = repository.get_setting("memory_lifecycle") or {}
            self.assertTrue(lifecycle["enabled"])  # pre-existing key preserved
            self.assertEqual(lifecycle["auto_rescan_limit"], 24)  # backfilled default
            self.assertEqual(lifecycle["chat_memory_mode"], "strict")
            servers = (repository.get_setting("mcp_servers") or {}).get("servers") or []
            granola = next(server for server in servers if server.get("id") == "granola")
            self.assertEqual(granola.get("url"), "https://mcp.granola.ai/mcp")  # npx -> remote http
            self.assertIn("azure-devops-git", {server.get("id") for server in servers})

    def test_migrate_is_idempotent_on_current_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteMemoryRepository(Path(tmp))
            repository._migrate()
            repository._migrate()
            self.assertEqual(self._user_version(repository), SCHEMA_VERSION)
            self.assertIn("idx_memory_nodes_updated_at", self._memory_node_indexes(repository))


if __name__ == "__main__":
    unittest.main()
