from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.architectos.chunking import chunk_text, work_item_comment_chunks, work_item_main_parts
from backend.architectos.search import pack_memory_context
from backend.architectos.service import ArchitectOSService


class MemoryRetrievalTests(unittest.TestCase):
    def test_fts_indexes_and_finds_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.add_memory({
                "project_id": "architectos",
                "label": "Auth token rotation",
                "type": "Decision",
                "scope": "project",
                "text": "Rotate API tokens every 30 days for production services.",
                "source": "adr",
            })
            service.add_memory({
                "project_id": "architectos",
                "label": "Random chat note",
                "type": "Lesson",
                "scope": "project",
                "text": "Someone mentioned lunch plans in the hallway.",
                "source": "chat",
            })
            fts_ids = service.repository.search_memory_fts("token rotation", project_id="architectos", limit=20)
            self.assertTrue(fts_ids)
            search = service.search_memory("token rotation", project_id="architectos", limit=5)
            self.assertIn(search["retrieval"]["mode"], {"fts-prefilter", "hybrid"})
            labels = [hit["node"]["label"] for hit in search["hits"]]
            self.assertIn("Auth token rotation", labels)
            self.assertLessEqual(labels.index("Auth token rotation"), 1)

    def test_context_budget_citations_and_dedupe(self) -> None:
        hits = []
        for index in range(12):
            hits.append({
                "score": 100 - index,
                "node": {
                    "id": f"node_{index}",
                    "type": "Lesson",
                    "scope": "project",
                    "label": f"Note {index}",
                    "text": ("Same almost duplicate memory body about caching. " * 20) if index < 3 else f"Unique topic {index}: " + ("x" * 400),
                    "evidence": ["memory.db"],
                    "metadata": {"source": "chat" if index else "adr"},
                },
            })
        packed = pack_memory_context(hits, project_id="architectos", char_budget=1800, node_text_chars=220)
        self.assertIn("ArchitectOS Memory Context", packed)
        self.assertIn("id=node_0", packed)
        self.assertIn("Prefer Decision/Constraint", packed)
        # Near-duplicates of node_0 should be skipped (keep at most one of 0/1/2).
        dup_ids = sum(1 for node_id in ("id=node_0", "id=node_1", "id=node_2") if node_id in packed)
        self.assertEqual(dup_ids, 1)
        self.assertLessEqual(len(packed), 1900)

    def test_trust_prefers_decision_over_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.add_memory({
                "project_id": "architectos",
                "label": "Caching strategy",
                "type": "Lesson",
                "scope": "project",
                "text": "Caching strategy was mentioned casually in chat about redis maybe.",
                "source": "chat",
            })
            service.add_memory({
                "project_id": "architectos",
                "label": "Caching strategy ADR",
                "type": "Decision",
                "scope": "project",
                "text": "Caching strategy: use Redis with 15 minute TTL for session data.",
                "source": "adr",
            })
            search = service.search_memory("caching strategy", project_id="architectos", limit=3)
            top = search["hits"][0]["node"]
            self.assertEqual(top["type"], "Decision")
            context = service.context("caching strategy", project_id="architectos")
            self.assertIn("id=", context["context"])
            self.assertIn("Caching strategy ADR", context["context"])

    def test_embeddings_indexed_on_add_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            created = service.add_memory({
                "project_id": "architectos",
                "label": "Rate limit policy",
                "type": "Constraint",
                "scope": "project",
                "text": "Public APIs must enforce 100 requests per minute per API key.",
                "source": "adr",
            })
            node_id = created["id"]
            missing = service.repository.list_nodes_missing_embeddings()
            self.assertNotIn(node_id, [node.id for node in missing])
            vector_ids = service.memory_embeddings.search(
                "rate limit API requests per minute",
                project_id="architectos",
                limit=10,
            )
            self.assertIn(node_id, vector_ids)

    def test_hybrid_retrieval_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.add_memory({
                "project_id": "architectos",
                "label": "Session timeout",
                "type": "Decision",
                "scope": "project",
                "text": "Idle web sessions expire after 30 minutes.",
                "source": "adr",
            })
            search = service.search_memory("session idle expiry", project_id="architectos", limit=5)
            retrieval = search["retrieval"]
            self.assertIn(retrieval["mode"], {"hybrid", "fts-prefilter", "vector-prefilter"})
            self.assertIn("embeddings", retrieval)
            self.assertTrue(retrieval["embeddings"]["enabled"])
            self.assertGreaterEqual(retrieval["vector_hits"] + retrieval["fts_hits"], 1)

    def test_embedding_engine_rebuild_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.update_settings({
                "memory_retrieval": {
                    "embeddings_enabled": True,
                    "embedding_provider": "hash",
                    "reindex_on_startup": False,
                }
            })
            created = service.add_memory({
                "project_id": "architectos",
                "label": "Backup window",
                "type": "Requirement",
                "scope": "project",
                "text": "Nightly backups run between 02:00 and 04:00 UTC.",
                "source": "docs",
            })
            node_id = created["id"]
            service.repository.rebuild_memory_embeddings()
            self.assertIn(node_id, [node.id for node in service.repository.list_nodes_missing_embeddings()])
            stats = service.memory_embeddings.rebuild_all()
            self.assertGreaterEqual(stats["indexed"], 1)
            self.assertNotIn(node_id, [node.id for node in service.repository.list_nodes_missing_embeddings()])

    def test_memory_get_tool_returns_full_node_and_neighbors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            parent = service.add_memory({
                "project_id": "architectos",
                "label": "WI main",
                "type": "Requirement",
                "scope": "project",
                "text": "Main work item body " + ("x" * 900),
                "source": "azure_boards",
            })
            child = service.add_memory({
                "project_id": "architectos",
                "label": "WI comment",
                "type": "Doc",
                "scope": "project",
                "text": "Comment body with FixVersion detail",
                "source": "azure_boards",
            })
            service.repository.add_edge(parent["id"], child["id"], "HAS_COMMENT", "project", 0.9)
            result = service.tool_gateway.execute("memory_get", {"id": parent["id"], "include_neighbors": True})
            self.assertTrue(result["ok"])
            payload = result["result"]
            self.assertEqual(payload["id"], parent["id"])
            self.assertIn("Main work item body", payload["text"])
            self.assertTrue(any(item["id"] == child["id"] for item in payload.get("neighbors") or []))

    def test_retrieval_feedback_boosts_helpful_hits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            helpful = service.add_memory({
                "project_id": "architectos",
                "label": "Helpful auth decision",
                "type": "Decision",
                "scope": "project",
                "text": "Auth tokens expire after 12 hours for admin APIs.",
                "source": "adr",
            })
            other = service.add_memory({
                "project_id": "architectos",
                "label": "Other auth note",
                "type": "Lesson",
                "scope": "project",
                "text": "Auth tokens expire after 12 hours for admin APIs mentioned in chat.",
                "source": "chat",
            })
            service.record_retrieval_feedback({
                "project_id": "architectos",
                "rating": 1,
                "hit_ids": [helpful["id"]],
                "query": "auth token expiry",
            })
            search = service.search_memory("auth token expiry", project_id="architectos", limit=3)
            top_id = search["hits"][0]["node"]["id"]
            self.assertEqual(top_id, helpful["id"])
            listed = service.list_retrieval_feedback("architectos")
            self.assertGreaterEqual(listed["count"], 1)

    def test_cyrillic_query_hits_fts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.add_memory({
                "project_id": "architectos",
                "label": "Решение по токенам",
                "type": "Decision",
                "scope": "project",
                "text": "Ротировать API токены каждые 30 дней в production.",
                "source": "adr",
            })
            service.add_memory({
                "project_id": "architectos",
                "label": "English only note",
                "type": "Lesson",
                "scope": "project",
                "text": "Unrelated lunch plans.",
                "source": "chat",
            })
            fts_ids = service.repository.search_memory_fts("токены", project_id="architectos", limit=20)
            self.assertTrue(fts_ids)
            search = service.search_memory("токены", project_id="architectos", limit=5)
            self.assertIn(search["retrieval"]["mode"], {"fts-prefilter", "hybrid"})
            labels = [hit["node"]["label"] for hit in search["hits"]]
            self.assertIn("Решение по токенам", labels)

    def test_vector_prefilter_recovers_when_fts_misses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.update_settings({
                "memory_retrieval": {
                    "embeddings_enabled": True,
                    "embedding_provider": "hash",
                    "vector_min_score": 0.01,
                    "vector_pool": 16,
                    "vector_query_timeout_ms": 2000,
                    "reindex_on_startup": False,
                }
            })
            service.add_memory({
                "project_id": "architectos",
                "label": "Token rotation policy",
                "type": "Decision",
                "scope": "project",
                "text": "Rotate API credentials every thirty days in production environments.",
                "source": "adr",
            })
            original_fts = service.repository.search_memory_fts
            service.repository.search_memory_fts = lambda *args, **kwargs: []  # type: ignore[method-assign]
            try:
                search = service.search_memory("rotate credentials thirty production", project_id="architectos", limit=5)
            finally:
                service.repository.search_memory_fts = original_fts  # type: ignore[method-assign]
            retrieval = search["retrieval"]
            self.assertEqual(retrieval["mode"], "vector-prefilter")
            self.assertGreaterEqual(retrieval["vector_hits"], 1)
            self.assertEqual(retrieval["fts_hits"], 0)
            self.assertIn("vector_ms", retrieval)
            self.assertFalse(retrieval.get("vector_timed_out"))
            labels = [hit["node"]["label"] for hit in search["hits"]]
            self.assertIn("Token rotation policy", labels)
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.update_settings({
                "memory_lifecycle": {
                    "enabled": True,
                    "refresh_on_access": True,
                    "short_term_ttl_days": 1,
                    "archive_after_days": 2,
                    "delete_after_days": 3,
                    "promote_after_hits": 2,
                }
            })
            node = service.add_memory({
                "project_id": "architectos",
                "label": "Scratch note",
                "type": "Lesson",
                "scope": "project",
                "text": "Temporary scratch note for interactive search.",
            })
            service.search_memory("scratch note", "architectos")
            stored = service.repository.get_node(node["id"])
            self.assertEqual(int(stored.metadata.get("access_count") or 0), 0)

    def test_chunk_helpers_split_work_item_comments(self) -> None:
        main = work_item_main_parts(
            wi_type="Task",
            work_item_id=1,
            title="Demo",
            description="short",
            comment_count=2,
        )
        self.assertIn("Comments: 2 linked", main)
        self.assertNotIn("Debbie said", main)
        chunks = work_item_comment_chunks([
            {"id": "9", "author": "Debbie", "created_date": "2026-07-01", "text": "Need FixVersion filter"},
        ])
        self.assertEqual(len(chunks), 1)
        self.assertIn("FixVersion", chunks[0]["text"])
        long = "para one.\n\n" + ("word " * 400)
        parts = chunk_text(long, size=200, overlap=40)
        self.assertGreater(len(parts), 1)

    def test_gemini_embedding_provider_and_catalog(self) -> None:
        import json
        import os
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from backend.architectos.embeddings import (
            GeminiEmbeddingProvider,
            build_embedding_provider,
            embedding_provider_catalog,
            l2_normalize,
        )

        catalog = embedding_provider_catalog()
        gemini = next(item for item in catalog if item["id"] == "gemini")
        model_ids = {item["id"] for item in gemini["models"]}
        self.assertIn("gemini-embedding-001", model_ids)
        self.assertIn("gemini-embedding-2", model_ids)
        self.assertTrue(gemini["free_tier"])
        self.assertTrue(gemini["multilingual"])

        seen: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                seen.append({"path": self.path, "key": self.headers.get("x-goog-api-key"), "body": body})
                dims = int(body.get("outputDimensionality") or 8)
                payload = {"embedding": {"values": [0.5] * dims}}
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format, *args):  # noqa: A003
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            provider = GeminiEmbeddingProvider(
                api_key="test-gemini-key",
                model="gemini-embedding-001",
                dimensions=128,
                base_url=f"http://127.0.0.1:{server.server_port}/v1beta",
            )
            vector = provider.embed("Hello мир", purpose="document")
            self.assertEqual(len(vector), 128)
            self.assertAlmostEqual(sum(v * v for v in vector), 1.0, places=5)
            self.assertEqual(seen[-1]["key"], "test-gemini-key")
            self.assertEqual(seen[-1]["body"].get("taskType"), "RETRIEVAL_DOCUMENT")
            query_vec = provider.embed("lookup pipeline", purpose="query")
            self.assertEqual(len(query_vec), 128)
            self.assertEqual(seen[-1]["body"].get("taskType"), "RETRIEVAL_QUERY")

            provider2 = GeminiEmbeddingProvider(
                api_key="test-gemini-key",
                model="gemini-embedding-2",
                dimensions=128,
                base_url=f"http://127.0.0.1:{server.server_port}/v1beta",
            )
            provider2.embed("Doc body", purpose="document")
            self.assertIn("title:", seen[-1]["body"]["content"]["parts"][0]["text"])
            self.assertNotIn("taskType", seen[-1]["body"])

            old = os.environ.get("GEMINI_API_KEY")
            old_provider = os.environ.get("MEMORY_EMBEDDING_PROVIDER")
            os.environ["GEMINI_API_KEY"] = "env-key"
            os.environ["MEMORY_EMBEDDING_PROVIDER"] = "gemini"
            os.environ["MEMORY_EMBEDDING_MODEL"] = "gemini-embedding-001"
            os.environ["GEMINI_API_BASE"] = f"http://127.0.0.1:{server.server_port}/v1beta"
            try:
                built = build_embedding_provider({
                    "embedding_provider": "gemini",
                    "embedding_model": "gemini-embedding-001",
                    "embedding_dimensions": 128,
                })
                self.assertEqual(built.provider_id, "gemini")
                built.embed("wired")
                self.assertTrue(seen)
            finally:
                if old is None:
                    os.environ.pop("GEMINI_API_KEY", None)
                else:
                    os.environ["GEMINI_API_KEY"] = old
                if old_provider is None:
                    os.environ.pop("MEMORY_EMBEDDING_PROVIDER", None)
                else:
                    os.environ["MEMORY_EMBEDDING_PROVIDER"] = old_provider
                os.environ.pop("MEMORY_EMBEDDING_MODEL", None)
                os.environ.pop("GEMINI_API_BASE", None)
        finally:
            server.shutdown()

        normalized = l2_normalize([3.0, 4.0])
        self.assertAlmostEqual(normalized[0], 0.6, places=5)
        self.assertAlmostEqual(normalized[1], 0.8, places=5)


if __name__ == "__main__":
    unittest.main()
