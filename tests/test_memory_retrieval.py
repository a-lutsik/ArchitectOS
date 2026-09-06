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

    def test_list_edges_touching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            repo = service.repository
            a = repo.add_node("Lesson", "Alpha topic", "project", "alpha body text", "architectos", None, 0.7, {"source": "test"})
            b = repo.add_node("Lesson", "Beta topic", "project", "beta body text", "architectos", None, 0.7, {"source": "test"})
            c = repo.add_node("Lesson", "Gamma topic", "project", "gamma body text", "architectos", None, 0.7, {"source": "test"})
            service.create_graph_edge({"source": a.id, "target": b.id, "type": "RELATED_TO", "confidence": 0.8})
            self.assertEqual(len(repo.list_edges_touching([a.id])), 1)
            self.assertEqual(len(repo.list_edges_touching([b.id])), 1)
            self.assertEqual(repo.list_edges_touching([c.id]), [])
            self.assertEqual(repo.list_edges_touching([]), [])

    def test_context_expands_graph_neighbors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            matched = service.add_memory({
                "project_id": "architectos",
                "label": "Discount applied before tax",
                "type": "Lesson",
                "scope": "project",
                "text": "Discount was computed on the gross amount before removing tax.",
                "source": "chat",
            })
            neighbor = service.add_memory({
                "project_id": "architectos",
                "label": "Invoice rounding module",
                "type": "Decision",
                "scope": "project",
                "text": "Rounding rules live in the invoice totals pipeline.",
                "source": "adr",
            })
            service.create_graph_edge({"source": matched["id"], "target": neighbor["id"], "type": "RELATED_TO", "confidence": 0.8})
            plain = service.search_memory("discount before tax", project_id="architectos", limit=8)
            plain_labels = [hit["node"]["label"] for hit in plain["hits"]]
            self.assertIn("Discount applied before tax", plain_labels)
            self.assertNotIn("Invoice rounding module", plain_labels)
            ctx = service.context("discount before tax", project_id="architectos", limit=8)
            ctx_labels = [hit["node"]["label"] for hit in ctx["hits"]]
            self.assertIn("Invoice rounding module", ctx_labels)
            self.assertGreaterEqual(int(ctx["retrieval"].get("graph_neighbors") or 0), 1)

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
        self.assertIn("Retrieved for this query:", packed)
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
            # Interactive search now bumps lifecycle access stats inline (cheap,
            # no re-embedding) so promote_after_hits/decay see real activity.
            self.assertEqual(int(stored.metadata.get("access_count") or 0), 1)

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


class MemorySearchQualityTests(unittest.TestCase):
    def test_recency_bonus_prefers_recently_accessed_node(self) -> None:
        from datetime import datetime, timedelta, timezone

        from backend.architectos.models import MemoryNode
        from backend.architectos.search import HybridSearchStrategy

        now = datetime.now(timezone.utc)
        recent_ts = now.isoformat()
        old_ts = (now - timedelta(days=240)).isoformat()
        strategy = HybridSearchStrategy()
        nodes = [
            MemoryNode(id="recent", type="Lesson", label="alpha", scope="project", text="alpha", project_id="p",
                       metadata={"last_accessed_at": recent_ts}),
            MemoryNode(id="ancient", type="Lesson", label="alpha", scope="project", text="alpha", project_id="p",
                       metadata={"last_accessed_at": old_ts}),
        ]
        hits = strategy.search("alpha", nodes, [], "p", None, 5, diversify=False)
        by_id = {hit.node.id: hit.score for hit in hits}
        self.assertGreater(by_id["recent"], by_id["ancient"])
        # 240 days = 4 half-lives of 60 days → the old node keeps only ~0.5 of 8.0.
        self.assertGreaterEqual(by_id["recent"] - by_id["ancient"], 6.0)

    def test_pack_context_dominant_hit_gets_full_text(self) -> None:
        long_text = "Full meeting notes body. " * 60 + "ENDMARKER"
        hits = [
            {"score": 100.0, "node": {"id": "n1", "type": "Lesson", "scope": "project", "label": "Top", "text": long_text, "metadata": {}}},
            {"score": 40.0, "node": {"id": "n2", "type": "Lesson", "scope": "project", "label": "Second", "text": "short", "metadata": {}}},
        ]
        packed = pack_memory_context(hits, project_id="p", node_text_chars=200)
        self.assertIn("ENDMARKER", packed)
        self.assertIn("Retrieved for this query:", packed)
        contested = [
            {"score": 100.0, "node": dict(hits[0]["node"])},
            {"score": 90.0, "node": dict(hits[1]["node"])},
        ]
        packed_contested = pack_memory_context(contested, project_id="p", node_text_chars=200)
        self.assertNotIn("ENDMARKER", packed_contested)

    def test_pack_context_hides_tool_hint_when_tools_unavailable(self) -> None:
        hits = [{"score": 10.0, "node": {"id": "n1", "type": "Lesson", "scope": "project", "label": "Note", "text": "body", "metadata": {}}}]
        with_tools = pack_memory_context(hits, project_id="p", tools_available=True)
        without_tools = pack_memory_context(hits, project_id="p", tools_available=False)
        self.assertIn("use id=…", with_tools)
        self.assertNotIn("use id=…", without_tools)
        self.assertIn("id=n1", without_tools)

    def test_uom_bug_query_prefers_boards_bug_over_chat_noise(self) -> None:
        from backend.architectos.embeddings import content_tokens, expand_query_terms
        from backend.architectos.models import MemoryNode
        from backend.architectos.search import HybridSearchStrategy

        self.assertEqual(content_tokens("в памяти есть баг по UOM"), ["баг", "uom"])
        expanded = expand_query_terms(content_tokens("в памяти есть баг по UOM"))
        self.assertIn("power", expanded)
        self.assertIn("converted", expanded)

        strategy = HybridSearchStrategy()
        nodes = [
            MemoryNode(
                id="chat-noise",
                type="Decision",
                label="Chat fact: создай requirement для DataUploader",
                scope="project",
                text="Durable fact from chat about DataUploader pre-processing.",
                project_id="p",
                confidence=0.9,
                metadata={
                    "source": "chat",
                    "template": "chat_fact_keeper",
                    "memory_score": 100.0,
                    "memory_tier": "long_term",
                },
            ),
            MemoryNode(
                id="uom-bug",
                type="Constraint",
                label="ADO Bug #70372: Meter Accuracy table is not converted to Power",
                scope="project",
                text="Azure Boards Bug #70372: Meter Accuracy table is not converted to Power when system setting is set to Power.",
                project_id="p",
                confidence=0.9,
                metadata={
                    "source": "azure_boards",
                    "work_item_id": "70372",
                    "work_item_type": "Bug",
                    "memory_score": 80.0,
                    "memory_tier": "long_term",
                },
            ),
            MemoryNode(
                id="granola-rule",
                type="Rule",
                label="Rule: какие данные можно запрашивать у Granola",
                scope="project",
                text="Rule candidate from chat about Granola meeting summaries.",
                project_id="p",
                confidence=0.9,
                metadata={"source": "chat", "memory_score": 100.0, "memory_tier": "long_term"},
            ),
        ]
        hits = strategy.search("в памяти есть баг по UOM", nodes, [], "p", None, 5, diversify=False)
        self.assertTrue(hits)
        self.assertEqual(hits[0].node.id, "uom-bug")
        self.assertNotEqual(hits[0].node.id, "chat-noise")

    def test_parse_memory_identifiers_from_ab_query(self) -> None:
        from backend.architectos.search import parse_memory_identifiers, search_query_terms

        tagged = parse_memory_identifiers("AB#79670")
        self.assertEqual(tagged.work_item_ids, ("79670",))
        self.assertTrue(tagged.identifier_only)
        self.assertEqual(search_query_terms("AB#79670"), ["79670"])
        self.assertNotIn("ab", search_query_terms("AB#79670"))

        mixed = parse_memory_identifiers("AB#79670 validation gaps")
        self.assertEqual(mixed.work_item_ids, ("79670",))
        self.assertFalse(mixed.identifier_only)

        node_query = parse_memory_identifiers("node_abc123def456")
        self.assertEqual(node_query.node_ids, ("node_abc123def456",))
        self.assertTrue(node_query.identifier_only)

    def test_ab_id_query_ranks_work_item_ahead_of_other_ab_chat_facts(self) -> None:
        from backend.architectos.models import MemoryNode
        from backend.architectos.search import HybridSearchStrategy

        strategy = HybridSearchStrategy()
        nodes = [
            MemoryNode(
                id="chat-other-1",
                type="Requirement",
                label='Chat fact: Есть упоминание "site context actions" в Requirement AB#35928',
                scope="project",
                text="Durable fact extracted from a finished chat (not a full transcript).",
                project_id="p",
                confidence=0.9,
                metadata={"source": "chat", "template": "chat_fact_keeper", "memory_score": 90.0},
            ),
            MemoryNode(
                id="chat-other-2",
                type="Requirement",
                label="Chat fact: Create this Requirement in Azure Boards under parent AB#80702",
                scope="project",
                text="Durable fact extracted from chat.",
                project_id="p",
                confidence=0.9,
                metadata={"source": "chat", "template": "chat_fact_keeper", "memory_score": 90.0},
            ),
            MemoryNode(
                id="boards-79670",
                type="Requirement",
                label="ADO Bug #79670: Validation gaps for negative values",
                scope="project",
                text="Azure Boards Bug #79670: Validation gaps existed before, so validations are a key part of flow quality.",
                project_id="p",
                confidence=0.9,
                metadata={
                    "source": "azure-boards",
                    "work_item_id": "79670",
                    "work_item_type": "Bug",
                    "memory_score": 70.0,
                },
            ),
        ]
        hits = strategy.search("AB#79670", nodes, [], "p", None, 5, diversify=True)
        self.assertTrue(hits)
        self.assertEqual(hits[0].node.id, "boards-79670")
        self.assertIn("exact-work-item", hits[0].reasons)

    def test_ab_id_search_finds_metadata_id_and_skips_vector(self) -> None:
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
            target = service.repository.add_node(
                "Requirement",
                "Uploader validation rules",
                "project",
                "Negative values must be rejected during the validation stage of file upload.",
                "architectos",
                None,
                0.9,
                {"source": "azure-boards", "work_item_id": "79670", "work_item_type": "Bug"},
            )
            service.repository.add_node(
                "Requirement",
                "Chat fact: Есть упоминание site context actions в Requirement AB#35928",
                "project",
                "Durable fact extracted from a finished chat about a different work item.",
                "architectos",
                None,
                0.9,
                {"source": "chat", "template": "chat_fact_keeper"},
            )
            search = service.search_memory("AB#79670", project_id="architectos", limit=8)
            self.assertTrue(search["hits"])
            self.assertEqual(search["hits"][0]["node"]["id"], target.id)
            self.assertIn("exact-work-item", search["hits"][0].get("reasons") or [])
            retrieval = search["retrieval"]
            self.assertTrue(retrieval.get("vector_skipped"))
            self.assertEqual(retrieval.get("vector_hits"), 0)
            self.assertGreaterEqual(int(retrieval.get("identifier_hits") or 0), 1)
            other_ids = [hit["node"]["id"] for hit in search["hits"][1:]]
            self.assertNotIn(target.id, other_ids)

    def test_node_id_search_returns_that_node_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            node = service.add_memory({
                "project_id": "architectos",
                "label": "Invoice rounding module",
                "type": "Decision",
                "scope": "project",
                "text": "Rounding rules live in the invoice totals pipeline.",
                "source": "adr",
            })
            service.add_memory({
                "project_id": "architectos",
                "label": "Chat fact: node mentions from another thread",
                "type": "Lesson",
                "scope": "project",
                "text": "Someone mentioned a node in passing during standup.",
                "source": "chat",
            })
            search = service.search_memory(node["id"], project_id="architectos", limit=8)
            self.assertTrue(search["hits"])
            self.assertEqual(search["hits"][0]["node"]["id"], node["id"])
            self.assertIn("exact-node-id", search["hits"][0].get("reasons") or [])
            self.assertTrue(search["retrieval"].get("vector_skipped"))

    def test_thin_followup_blends_prior_user_topic(self) -> None:
        self.assertTrue(ArchitectOSService._is_thin_followup_message("подтяни данные"))
        self.assertFalse(ArchitectOSService._is_thin_followup_message("в памяти есть баг по UOM"))
        svc = ArchitectOSService.__new__(ArchitectOSService)
        blended = svc._retrieval_query_for_message(
            "подтяни данные",
            [
                {"role": "user", "text": "в памяти есть баг по UOM"},
                {"role": "assistant", "text": "есть упоминание"},
            ],
        )
        self.assertIn("UOM", blended)
        self.assertIn("подтяни данные", blended)

    def test_pack_splits_stable_layer_from_query_hits(self) -> None:
        hits = [
            {"score": 90.0, "node": {"id": "q1", "type": "Lesson", "scope": "project", "label": "Chat note", "text": "Someone mentioned redis in passing.", "metadata": {"source": "chat"}}},
            {"score": 80.0, "node": {"id": "s1", "type": "Constraint", "scope": "project", "label": "No secrets in DB", "text": "Never persist API keys in SQLite.", "metadata": {"source": "adr", "pinned": True}}},
        ]
        packed = pack_memory_context(hits, project_id="p", node_text_chars=200)
        stable_at = packed.find("Stable memory")
        query_at = packed.find("Retrieved for this query:")
        self.assertGreaterEqual(stable_at, 0)
        self.assertGreater(query_at, stable_at)
        self.assertLess(packed.find("id=s1"), packed.find("id=q1"))

    def test_rule_layer_keeps_old_constraint_among_recent_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            repo = service.repository
            constraint = repo.add_node(
                "Constraint",
                "Data Uploader",
                "project",
                "The system supports only CSV and simple EXCEL files. Other formats require a Python script.",
                "architectos",
                None,
                0.9,
                {"source": "manual", "memory_tier": "long_term", "memory_score": 92.5},
            )
            for index in range(90):
                repo.add_node(
                    "Artifact",
                    f"ADO Task #{20000 + index} parse old PDF",
                    "project",
                    f"Backend job to parse old PDF number {index}",
                    "architectos",
                    None,
                    0.8,
                    {"source": "azure-boards"},
                )
            rule_layer = service.list_rule_layer_memory(project_id="architectos", limit=6)
            ids = [hit["node"]["id"] for hit in rule_layer]
            self.assertIn(constraint.id, ids)

    def test_rule_layer_surfaces_in_ask_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.add_memory({
                "project_id": "architectos",
                "label": "Data Uploader",
                "type": "Constraint",
                "scope": "project",
                "text": "The system supports only CSV and simple EXCEL files. If you need to upload other file formats you have to create and use a Python script.",
                "source": "manual",
            })
            for index in range(8):
                service.add_memory({
                    "project_id": "architectos",
                    "label": f"Unrelated long-term decision {index}",
                    "type": "Decision",
                    "scope": "project",
                    "text": f"Keep auth tokens in the vault for service {index}.",
                    "source": "adr",
                })
            ctx = service.context(
                "я хочу без преобразования и без скриптов загрузить файл PDF",
                project_id="architectos",
                limit=8,
            )
            packed = str(ctx.get("context") or "")
            self.assertIn("CSV", packed)
            self.assertIn("EXCEL", packed)
            rule_ids = [hit["node"]["id"] for hit in ctx.get("rule_layer_hits") or []]
            self.assertTrue(rule_ids)

    def test_rrf_prefers_consensus_over_single_list(self) -> None:
        from backend.architectos.models import MemoryNode
        from backend.architectos.search import HybridSearchStrategy

        strategy = HybridSearchStrategy()
        nodes = [
            MemoryNode(id="both", type="Lesson", label="gamma", scope="project", text="gamma", project_id="p"),
            MemoryNode(id="fts_only", type="Lesson", label="gamma", scope="project", text="gamma", project_id="p"),
            MemoryNode(id="vec_only", type="Lesson", label="gamma", scope="project", text="gamma", project_id="p"),
        ]
        hits = strategy.search(
            "zzz",
            nodes,
            [],
            "p",
            None,
            5,
            diversify=False,
            fts_ranks={"both": 1, "fts_only": 0},
            vector_scores={"both": 0.9, "vec_only": 0.99},
        )
        self.assertEqual(hits[0].node.id, "both")
        self.assertTrue(any(reason.startswith("rrf:") for reason in hits[0].reasons))

    def test_fts_rank_bonus_decays_with_rank(self) -> None:
        from backend.architectos.models import MemoryNode
        from backend.architectos.search import HybridSearchStrategy

        strategy = HybridSearchStrategy()
        nodes = [
            MemoryNode(id="top", type="Lesson", label="alpha", scope="project", text="alpha", project_id="p"),
            MemoryNode(id="deep", type="Lesson", label="beta", scope="project", text="beta", project_id="p"),
        ]
        hits = strategy.search(
            "gamma",  # no lexical overlap: only FTS rank separates the two
            nodes,
            [],
            "p",
            None,
            5,
            diversify=False,
            fts_ranks={"top": 0, "deep": 12},
        )
        by_id = {hit.node.id: hit.score for hit in hits}
        self.assertGreater(by_id["top"], by_id["deep"])
        top_reasons = next(hit.reasons for hit in hits if hit.node.id == "top")
        deep_reasons = next(hit.reasons for hit in hits if hit.node.id == "deep")
        self.assertTrue(any(reason.startswith("rrf:") for reason in top_reasons))
        self.assertTrue(any(reason.startswith("rrf:") for reason in deep_reasons))
        top_rrf = next(float(reason.split(":")[1]) for reason in top_reasons if reason.startswith("rrf:"))
        deep_rrf = next(float(reason.split(":")[1]) for reason in deep_reasons if reason.startswith("rrf:"))
        self.assertGreater(top_rrf, deep_rrf)

    def test_vector_cache_incremental_update_matches_full_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.add_memory({
                "project_id": "architectos",
                "label": "Auth token rotation",
                "type": "Decision",
                "scope": "project",
                "text": "Rotate API tokens every 30 days for production services.",
            })
            engine = service.memory_embeddings
            engine._load_vector_cache()  # noqa: SLF001 - build the in-RAM index
            added = service.add_memory({
                "project_id": "architectos",
                "label": "Session idle expiry",
                "type": "Constraint",
                "scope": "project",
                "text": "Idle sessions expire after 20 minutes and require re-authentication.",
            })
            cache = engine._vector_cache  # noqa: SLF001
            self.assertIsNotNone(cache, "single add must patch the cache, not drop it")
            self.assertIn(added["id"], cache.node_ids)
            incremental = engine.search_scored("session expiry", project_id="architectos", limit=10)
            engine.invalidate_vector_cache()
            reloaded = engine.search_scored("session expiry", project_id="architectos", limit=10)
            self.assertEqual(
                [(node_id, round(score, 6)) for node_id, score in incremental],
                [(node_id, round(score, 6)) for node_id, score in reloaded],
            )


class MemoryAsyncIndexingTests(unittest.TestCase):
    def test_queued_indexing_drains_and_covers_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            # Simulate the HTTP runtime: worker started, writes only enqueue.
            service._embedding_worker_started = True  # noqa: SLF001
            node = service.add_memory({
                "project_id": "architectos",
                "label": "Queued embedding note",
                "type": "Lesson",
                "scope": "project",
                "text": "This node is indexed by the background embedding queue.",
            })
            missing = service.repository.list_nodes_missing_embeddings()
            self.assertIn(node["id"], [item.id for item in missing])
            processed = service._drain_embedding_index_queue()  # noqa: SLF001
            self.assertGreaterEqual(processed, 1)
            missing_after = service.repository.list_nodes_missing_embeddings()
            self.assertNotIn(node["id"], [item.id for item in missing_after])

    def test_inline_indexing_without_worker_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            node = service.add_memory({
                "project_id": "architectos",
                "label": "Inline embedding note",
                "type": "Lesson",
                "scope": "project",
                "text": "Without a worker, indexing happens inline on the write path.",
            })
            missing = service.repository.list_nodes_missing_embeddings()
            self.assertNotIn(node["id"], [item.id for item in missing])

    def test_query_embedding_cache_avoids_repeat_calls(self) -> None:
        from backend.architectos.embeddings import HashEmbeddingProvider, MemoryEmbeddingEngine

        class CountingProvider(HashEmbeddingProvider):
            def __init__(self) -> None:
                super().__init__()
                self.query_calls = 0

            def embed(self, text: str, *, purpose: str = "document") -> list[float]:
                if purpose == "query":
                    self.query_calls += 1
                return super().embed(text, purpose=purpose)

        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            provider = CountingProvider()
            engine = MemoryEmbeddingEngine(service.repository, provider)
            engine.search_scored("auth token rotation", project_id="architectos", limit=5)
            engine.search_scored("auth token rotation", project_id="architectos", limit=5)
            self.assertEqual(provider.query_calls, 1)
            engine.search_scored("different query entirely", project_id="architectos", limit=5)
            self.assertEqual(provider.query_calls, 2)

    def test_ingestion_dedup_marks_identical_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            candidates = [
                {"label": "Deploy freeze", "text": "Deploys freeze during the release week.", "source_type": "manual"},
                {"label": "Deploy freeze", "text": "Deploys freeze during the release week.", "source_type": "manual"},
            ]
            prepared = service.ingestion_engine.prepare_candidates("architectos", candidates, 10)
            self.assertEqual(len(prepared), 2)
            self.assertFalse(bool(prepared[0]["metadata"].get("duplicate")))
            self.assertTrue(bool(prepared[1]["metadata"].get("duplicate")))
            self.assertEqual(prepared[1]["metadata"].get("duplicate_kind"), "candidate")
            self.assertEqual(float(prepared[1]["metadata"].get("duplicate_score") or 0), 1.0)

    def test_pending_refresh_is_not_marked_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            payload = {
                "project_id": "architectos",
                "source_type": "docs",
                "source_ref": "docs/deploy-freeze.md",
                "label": "Deploy freeze",
                "type": "Doc",
                "scope": "project",
                "text": "Deploys freeze during the release week. Coordinate with SRE before shipping hotfixes.",
                "status": "candidate",
                "confidence": 0.7,
                "metadata": {},
            }
            stored = service.repository.upsert_memory_candidate(payload)
            prepared = service.ingestion_engine.prepare_candidates("architectos", [{
                "label": payload["label"],
                "text": payload["text"],
                "source_type": "docs",
                "source_ref": payload["source_ref"],
            }], 10)
            self.assertEqual(len(prepared), 1)
            self.assertEqual(prepared[0].get("id") or stored["id"], stored["id"])
            self.assertFalse(bool(prepared[0]["metadata"].get("duplicate")))

    def test_pending_lookalike_is_duplicate_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            text = "Deploys freeze during the release week. Coordinate with SRE before shipping hotfixes."
            service.repository.upsert_memory_candidate({
                "project_id": "architectos",
                "source_type": "docs",
                "source_ref": "docs/deploy-freeze.md",
                "label": "Deploy freeze",
                "type": "Doc",
                "scope": "project",
                "text": text,
                "status": "candidate",
                "confidence": 0.7,
                "metadata": {},
            })
            prepared = service.ingestion_engine.prepare_candidates("architectos", [{
                "label": "Release freeze note",
                "text": text,
                "source_type": "docs",
                "source_ref": "notes/release-freeze.md",
            }], 10)
            self.assertEqual(len(prepared), 1)
            self.assertTrue(bool(prepared[0]["metadata"].get("duplicate")))
            self.assertEqual(prepared[0]["metadata"].get("duplicate_kind"), "candidate")

    def test_rejecting_one_pending_duplicate_clears_survivor_badge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            text = "Deploys freeze during the release week. Coordinate with SRE before shipping hotfixes."
            original = service.repository.upsert_memory_candidate({
                "id": "cand_orig",
                "project_id": "architectos",
                "source_type": "docs",
                "source_ref": "docs/deploy-freeze.md",
                "label": "Deploy freeze",
                "type": "Doc",
                "scope": "project",
                "text": text,
                "status": "candidate",
                "confidence": 0.7,
                "metadata": {},
            })
            prepared = service.ingestion_engine.prepare_candidates("architectos", [{
                "id": "cand_dup",
                "label": "Release freeze note",
                "text": text,
                "source_type": "docs",
                "source_ref": "notes/release-freeze.md",
            }], 10)
            duplicate = service.repository.upsert_memory_candidate(prepared[0])
            self.assertTrue(bool(duplicate["metadata"].get("duplicate")))
            self.assertEqual(duplicate["metadata"].get("duplicate_kind"), "candidate")
            service.reject_memory_candidate(original["id"], {"reason": "keep the other copy"})
            survivor = service.repository.get_memory_candidate(duplicate["id"]) or {}
            self.assertFalse(bool((survivor.get("metadata") or {}).get("duplicate")))
            self.assertNotEqual(str((survivor.get("metadata") or {}).get("duplicate_kind") or ""), "candidate")

    def test_refresh_canonicalizes_mutual_duplicate_cluster(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            text = "Deploys freeze during the release week. Coordinate with SRE before shipping hotfixes."
            fingerprint = service.ingestion_engine._fingerprint(text)
            for index, cid in enumerate(("cand_x", "cand_y", "cand_z")):
                service.repository.upsert_memory_candidate({
                    "id": cid,
                    "project_id": "architectos",
                    "source_type": "docs",
                    "source_ref": f"docs/{cid}.md",
                    "label": f"Freeze note {index}",
                    "type": "Doc",
                    "scope": "project",
                    "text": text,
                    "status": "candidate",
                    "confidence": 0.45,
                    "created_at": f"2026-03-0{index + 1}T00:00:00Z",
                    "metadata": {
                        "fingerprint": fingerprint,
                        "tokens": sorted(service.ingestion_engine._tokens(text)),
                        "duplicate": True,
                        "duplicate_kind": "candidate",
                        "duplicate_of": "cand_y" if cid != "cand_y" else "cand_x",
                        "duplicate_label": "peer",
                        "duplicate_score": 1.0,
                    },
                })
            updated = service.ingestion_engine.refresh_candidate_duplicate_flags("architectos")
            self.assertGreaterEqual(updated, 1)
            pending = service.repository.list_memory_candidates("architectos", "candidate", None)
            unmarked = [item for item in pending if not bool((item.get("metadata") or {}).get("duplicate"))]
            marked = [item for item in pending if bool((item.get("metadata") or {}).get("duplicate"))]
            self.assertEqual(len(unmarked), 1)
            self.assertEqual(len(marked), 2)
            self.assertEqual(unmarked[0]["id"], "cand_x")
            for item in marked:
                self.assertEqual((item.get("metadata") or {}).get("duplicate_kind"), "candidate")
                self.assertEqual((item.get("metadata") or {}).get("duplicate_of"), "cand_x")

    def test_autoscan_skips_chat_dump_when_session_summary_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            result = service.post_chat_message({
                "project_id": "architectos",
                "message": "Architecture decision: useful chat turns should become reviewable memory candidates.",
                "provider_id": "local-memory",
            })
            chat_id = result["chat"]["id"]
            service.finalize_chat_session(chat_id, force=True, trigger="manual")
            ingested = service.ingest_memory({"project_id": "architectos", "sources": ["chat"], "limit": 10})
            dumps = [
                item for item in ingested["candidates"]
                if str((item.get("metadata") or {}).get("template") or "") == "chat"
            ]
            self.assertEqual(dumps, [])
            pending = service.list_memory_candidates("architectos")["candidates"]
            self.assertFalse(
                any(str((item.get("metadata") or {}).get("template") or "") == "chat" for item in pending),
            )
            self.assertTrue(
                any(str((item.get("metadata") or {}).get("template") or "") == "chat_session_atom" for item in pending),
            )

    def test_autoscan_chat_dump_keeps_stable_id_as_chat_grows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.repository.upsert_chat({
                "id": "chat-stable",
                "project_id": "architectos",
                "title": "Growing chat",
                "messages": [{"role": "user", "text": "Remember the review queue."}],
                "favorite": False,
                "created_at": "now",
            })
            first = service.ingest_memory({"project_id": "architectos", "sources": ["chat"], "limit": 10})
            dumps = [item for item in first["candidates"] if item.get("source_type") == "chat"]
            self.assertEqual(len(dumps), 1)
            dump_id = dumps[0]["id"]
            service.repository.upsert_chat({
                "id": "chat-stable",
                "project_id": "architectos",
                "title": "Growing chat",
                "messages": [
                    {"role": "user", "text": "Remember the review queue."},
                    {"role": "assistant", "text": "Use candidates before durable memory."},
                    {"role": "user", "text": "Also remember the session keeper owns the thread."},
                ],
                "favorite": False,
                "created_at": "now",
            })
            second = service.ingest_memory({"project_id": "architectos", "sources": ["chat"], "limit": 10})
            dumps = [item for item in second["candidates"] if item.get("source_type") == "chat"]
            self.assertEqual(len(dumps), 1)
            self.assertEqual(dumps[0]["id"], dump_id)
            pending = [
                item for item in service.list_memory_candidates("architectos")["candidates"]
                if item.get("source_type") == "chat"
            ]
            self.assertEqual(len(pending), 1)

    def test_finalize_retires_autoscan_chat_dump(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            posted = service.post_chat_message({
                "project_id": "architectos",
                "message": "Architecture decision: useful chat turns should become reviewable memory candidates.",
                "provider_id": "local-memory",
            })
            chat_id = posted["chat"]["id"]
            ingested = service.ingest_memory({"project_id": "architectos", "sources": ["chat"], "limit": 10})
            self.assertTrue(any(str((item.get("metadata") or {}).get("template") or "") == "chat" for item in ingested["candidates"]))
            service.finalize_chat_session(chat_id, force=True, trigger="manual")
            pending = service.list_memory_candidates("architectos")["candidates"]
            dumps = [item for item in pending if str((item.get("metadata") or {}).get("template") or "") == "chat"]
            atoms = [item for item in pending if str((item.get("metadata") or {}).get("template") or "") == "chat_session_atom"]
            self.assertEqual(dumps, [])
            self.assertGreaterEqual(len(atoms), 1)
            self.assertFalse(bool((atoms[0].get("metadata") or {}).get("duplicate")))

    def test_memory_lookalike_is_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.add_memory({
                "project_id": "architectos",
                "label": "Deploy freeze",
                "type": "Decision",
                "scope": "project",
                "text": "Deploys freeze during the release week. Coordinate with SRE before shipping hotfixes.",
                "source": "manual",
            })
            prepared = service.ingestion_engine.prepare_candidates("architectos", [{
                "label": "Deploy freeze",
                "text": "Deploys freeze during the release week. Coordinate with SRE before shipping hotfixes.",
                "source_type": "docs",
                "source_ref": "docs/deploy-freeze.md",
            }], 10)
            self.assertEqual(len(prepared), 1)
            self.assertTrue(bool(prepared[0]["metadata"].get("duplicate")))
            self.assertEqual(prepared[0]["metadata"].get("duplicate_kind"), "memory")

    def test_refresh_memory_cluster_marks_one_duplicate_and_rest_duplicate_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            text = "Deploys freeze during the release week. Coordinate with SRE before shipping hotfixes."
            service.add_memory({
                "project_id": "architectos",
                "label": "Deploy freeze",
                "type": "Decision",
                "scope": "project",
                "text": text,
                "source": "manual",
            })
            for index, cid in enumerate(("cand_mem_a", "cand_mem_b")):
                service.repository.upsert_memory_candidate({
                    "id": cid,
                    "project_id": "architectos",
                    "source_type": "docs",
                    "source_ref": f"docs/{cid}.md",
                    "label": f"Deploy freeze copy {index}",
                    "type": "Doc",
                    "scope": "project",
                    "text": text,
                    "status": "candidate",
                    "confidence": 0.45,
                    "created_at": f"2026-03-0{index + 1}T00:00:00Z",
                    "metadata": {},
                })
            service.ingestion_engine.refresh_candidate_duplicate_flags("architectos")
            pending = service.repository.list_memory_candidates("architectos", "candidate", None)
            memory_dups = [
                item for item in pending
                if str((item.get("metadata") or {}).get("duplicate_kind") or "") == "memory"
            ]
            candidate_dups = [
                item for item in pending
                if str((item.get("metadata") or {}).get("duplicate_kind") or "") == "candidate"
            ]
            unmarked = [
                item for item in pending
                if not bool((item.get("metadata") or {}).get("duplicate"))
            ]
            self.assertEqual(len(memory_dups), 1)
            self.assertEqual(len(candidate_dups), 1)
            self.assertEqual(len(unmarked), 0)
            self.assertEqual(memory_dups[0]["id"], "cand_mem_a")
            self.assertEqual(candidate_dups[0]["metadata"].get("duplicate_of"), "cand_mem_a")

    def test_reject_candidate_in_three_way_cluster_promotes_survivor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            text = "Deploys freeze during the release week. Coordinate with SRE before shipping hotfixes."
            fingerprint = service.ingestion_engine._fingerprint(text)
            for index, cid in enumerate(("cand_x", "cand_y", "cand_z")):
                service.repository.upsert_memory_candidate({
                    "id": cid,
                    "project_id": "architectos",
                    "source_type": "docs",
                    "source_ref": f"docs/{cid}.md",
                    "label": f"Freeze note {index}",
                    "type": "Doc",
                    "scope": "project",
                    "text": text,
                    "status": "candidate",
                    "confidence": 0.45,
                    "created_at": f"2026-03-0{index + 1}T00:00:00Z",
                    "metadata": {
                        "fingerprint": fingerprint,
                        "tokens": sorted(service.ingestion_engine._tokens(text)),
                    },
                })
            service.ingestion_engine.refresh_candidate_duplicate_flags("architectos")
            service.reject_memory_candidate("cand_x", {"reason": "drop keeper"})
            pending = service.repository.list_memory_candidates("architectos", "candidate", None)
            unmarked = [item for item in pending if not bool((item.get("metadata") or {}).get("duplicate"))]
            marked = [item for item in pending if bool((item.get("metadata") or {}).get("duplicate"))]
            self.assertEqual(len(unmarked), 1)
            self.assertEqual(len(marked), 1)
            self.assertEqual(unmarked[0]["id"], "cand_y")
            self.assertEqual(marked[0]["metadata"].get("duplicate_kind"), "candidate")
            self.assertEqual(marked[0]["metadata"].get("duplicate_of"), "cand_y")

    def test_reject_memory_duplicate_leaves_survivor_as_memory_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            text = "Deploys freeze during the release week. Coordinate with SRE before shipping hotfixes."
            service.add_memory({
                "project_id": "architectos",
                "label": "Deploy freeze",
                "type": "Decision",
                "scope": "project",
                "text": text,
                "source": "manual",
            })
            for index, cid in enumerate(("cand_mem_a", "cand_mem_b")):
                service.repository.upsert_memory_candidate({
                    "id": cid,
                    "project_id": "architectos",
                    "source_type": "docs",
                    "source_ref": f"docs/{cid}.md",
                    "label": f"Deploy freeze copy {index}",
                    "type": "Doc",
                    "scope": "project",
                    "text": text,
                    "status": "candidate",
                    "confidence": 0.45,
                    "created_at": f"2026-03-0{index + 1}T00:00:00Z",
                    "metadata": {},
                })
            service.ingestion_engine.refresh_candidate_duplicate_flags("architectos")
            service.reject_memory_candidate("cand_mem_a", {"reason": "drop memory duplicate"})
            survivor = service.repository.get_memory_candidate("cand_mem_b") or {}
            meta = survivor.get("metadata") or {}
            self.assertTrue(bool(meta.get("duplicate")))
            self.assertEqual(meta.get("duplicate_kind"), "memory")

    def test_chat_fact_atoms_do_not_false_positive_on_shared_boilerplate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.add_memory({
                "project_id": "architectos",
                "label": "Chat fact: Перепроверить root cause Azure Boards bug #85090: New Predict Expert Mod",
                "type": "Lesson",
                "scope": "project",
                "text": (
                    "Durable fact extracted from a finished chat (not a full transcript).\n\n"
                    "Azure Boards bug #85090 needs a root-cause review for the Predict Expert Mod rollout."
                ),
                "source": "chat",
            })
            chat_id = "chat_10223fdcfe340d66"
            for fact_key in ("SiteLineageEvent", "SiteLineageDbRequest"):
                service.repository.upsert_memory_candidate({
                    "id": f"cand_{fact_key.lower()}",
                    "project_id": "architectos",
                    "source_type": "chat",
                    "source_ref": chat_id,
                    "label": f"Chat fact: {fact_key}",
                    "type": "Lesson",
                    "scope": "project",
                    "text": (
                        "Durable fact extracted from a finished chat (not a full transcript).\n\n"
                        f"{fact_key}"
                    ),
                    "status": "candidate",
                    "confidence": 0.74,
                    "metadata": {
                        "chat_id": chat_id,
                        "template": "chat_session_atom",
                        "fact_key": fact_key,
                    },
                })
            service.ingestion_engine.refresh_candidate_duplicate_flags("architectos")
            pending = service.repository.list_memory_candidates("architectos", "candidate", None)
            self.assertEqual(len(pending), 2)
            for item in pending:
                meta = item.get("metadata") or {}
                self.assertNotEqual(str(meta.get("duplicate_kind") or ""), "memory")
            unmarked = [item for item in pending if not bool((item.get("metadata") or {}).get("duplicate"))]
            self.assertEqual(len(unmarked), 2)


class MemoryLifecycleApplicationTests(unittest.TestCase):
    def _service_with_memory(self, tmp: str) -> ArchitectOSService:
        service = ArchitectOSService(Path(tmp))
        service.add_memory({
            "project_id": "architectos",
            "label": "Zephyr quixotic runbook",
            "type": "Lesson",
            "scope": "project",
            "text": "The zephyr quixotic pipeline needs a manual restart after deploy.",
            "source": "manual",
        })
        return service

    def test_search_sync_refresh_updates_access_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service_with_memory(tmp)
            search = service.search_memory("zephyr quixotic", project_id="architectos", limit=5, refresh=True)
            self.assertTrue(search["hits"])
            node_id = search["hits"][0]["node"]["id"]
            node = service.repository.get_node(node_id)
            metadata = dict(node.metadata or {})
            self.assertGreaterEqual(int(metadata.get("access_count") or 0), 1)
            self.assertEqual(metadata.get("last_refresh_reason"), "search")

    def test_search_updates_access_count_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service_with_memory(tmp)
            search = service.search_memory("zephyr quixotic", project_id="architectos", limit=5)
            self.assertTrue(search["hits"])
            node_id = search["hits"][0]["node"]["id"]
            node = service.repository.get_node(node_id)
            metadata = dict(node.metadata or {})
            self.assertGreaterEqual(int(metadata.get("access_count") or 0), 1)
            self.assertEqual(metadata.get("last_refresh_reason"), "search")

    def test_search_access_bump_does_not_reindex_embeddings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service_with_memory(tmp)
            spy: list[str] = []
            service.repository._node_upsert_listeners = [lambda node: spy.append(node.id)]  # noqa: SLF001
            search = service.search_memory("zephyr quixotic", project_id="architectos", limit=5)
            self.assertTrue(search["hits"])
            self.assertEqual(spy, [])

    def test_decay_loop_runs_decay_once_then_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            calls: list[tuple[object, bool]] = []

            def fake_run_decay(project_id=None, dry_run=False):
                calls.append((project_id, dry_run))
                service._decay_stop.set()  # noqa: SLF001 - test stops the loop
                return {"changed": 0}

            service.memory_lifecycle.run_decay = fake_run_decay  # type: ignore[method-assign]
            service._memory_decay_loop(initial_delay_s=0.0, interval_s=3600.0)  # noqa: SLF001
            self.assertEqual(len(calls), 1)
            self.assertIsNone(calls[0][0])
            self.assertFalse(calls[0][1])

    def test_decay_loop_skips_when_lifecycle_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.repository.set_setting("memory_lifecycle", {"enabled": False})
            calls = []
            service.memory_lifecycle.run_decay = lambda *a, **k: calls.append(1)  # type: ignore[method-assign]
            service._decay_stop.set()  # noqa: SLF001 - pre-set: wait(0) returns True, loop exits
            service._memory_decay_loop(initial_delay_s=0.0, interval_s=3600.0)  # noqa: SLF001
            self.assertEqual(calls, [])


class MemoryChatAutoAcceptTests(unittest.TestCase):
    def _candidate(self, service, **overrides):
        payload = {
            "id": "cand_auto_1",
            "project_id": "architectos",
            "source_type": "chat",
            "source_ref": "chat_1",
            "status": "candidate",
            "label": "Chat fact: team prefers pytest",
            "type": "Lesson",
            "scope": "project",
            "text": "Durable fact extracted from chat: Remember the team prefers pytest for unit tests.",
            "confidence": 0.9,
            "metadata": {"template": "chat_fact_keeper"},
        }
        payload.update(overrides)
        return service.repository.upsert_memory_candidate(payload)

    def test_auto_accept_skips_decisions_even_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            candidate = self._candidate(service, type="Decision")
            result = service._maybe_auto_accept_chat_candidate(candidate, service.memory_lifecycle.settings())  # noqa: SLF001
            self.assertIsNone(result)
            stored = service.repository.get_memory_candidate("cand_auto_1")
            self.assertEqual(stored.get("status"), "candidate")

    def test_auto_accept_promotes_low_risk_lesson_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            candidate = self._candidate(service)
            result = service._maybe_auto_accept_chat_candidate(candidate, service.memory_lifecycle.settings())  # noqa: SLF001
            self.assertIsNotNone(result)
            memory = result.get("memory") or {}
            self.assertTrue(memory.get("id"))
            node = service.repository.get_node(memory["id"])
            metadata = dict(node.metadata or {})
            self.assertTrue(metadata.get("auto_accepted"))
            self.assertEqual(metadata.get("source_ref"), "chat_1")
            self.assertEqual(str(metadata.get("memory_tier") or ""), "short_term")
            stored = service.repository.get_memory_candidate("cand_auto_1")
            self.assertEqual(stored.get("status"), "promoted")

    def test_auto_accept_skips_weak_summary_or_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            settings = service.memory_lifecycle.settings()
            weak = self._candidate(service, id="cand_auto_2", confidence=0.6)
            self.assertIsNone(service._maybe_auto_accept_chat_candidate(weak, settings))  # noqa: SLF001
            summary = self._candidate(service, id="cand_auto_3", metadata={"template": "chat_session_summary"})
            self.assertIsNone(service._maybe_auto_accept_chat_candidate(summary, settings))  # noqa: SLF001
            orphan = self._candidate(service, id="cand_auto_4", source_ref="")
            self.assertIsNone(service._maybe_auto_accept_chat_candidate(orphan, settings))  # noqa: SLF001
            disabled = ArchitectOSService(Path(tmp))
            disabled.repository.set_setting("memory_lifecycle", {"chat_auto_accept": False})
            blocked = self._candidate(disabled, id="cand_auto_5")
            self.assertIsNone(disabled._maybe_auto_accept_chat_candidate(blocked, disabled.memory_lifecycle.settings()))  # noqa: SLF001


class MemorySearchFilterTests(unittest.TestCase):
    def _service(self, tmp: str) -> ArchitectOSService:
        service = ArchitectOSService(Path(tmp))
        service.add_memory({
            "project_id": "architectos", "label": "Req open", "type": "Requirement",
            "scope": "project", "text": "Discount rules must round half up.", "source": "azure-boards",
        })
        service.add_memory({
            "project_id": "architectos", "label": "Req done", "type": "Requirement",
            "scope": "project", "text": "Legacy discount rounding note.", "source": "azure-boards",
        })
        service.add_memory({
            "project_id": "architectos", "label": "Casual lesson", "type": "Lesson",
            "scope": "project", "text": "Discount chat chatter.", "source": "chat",
        })
        # Mark one requirement as Done via metadata.
        nodes = {node.label: node for node in service.repository.list_nodes()}
        done = nodes["Req done"]
        meta = dict(done.metadata or {})
        meta["work_item_state"] = "Done"
        done.metadata = meta
        service.repository.upsert_node(done, notify=False)
        open_node = nodes["Req open"]
        meta = dict(open_node.metadata or {})
        meta["work_item_state"] = "Active"
        open_node.metadata = meta
        service.repository.upsert_node(open_node, notify=False)
        return service

    def test_list_mode_filters_by_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp)
            result = service.search_memory("", project_id="architectos", mode="list",
                                           filters={"type": "Requirement", "work_item_state": "!=Done"})
            labels = [hit["node"]["label"] for hit in result["hits"]]
            self.assertEqual(labels, ["Req open"])
            self.assertEqual(result["retrieval"]["mode"], "list")

    def test_filter_only_call_acts_as_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp)
            result = service.search_memory("", project_id="architectos", filters={"type": "Lesson"})
            labels = [hit["node"]["label"] for hit in result["hits"]]
            self.assertEqual(labels, ["Casual lesson"])

    def test_list_mode_does_not_bump_access_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp)
            service.search_memory("", project_id="architectos", mode="list", filters={"type": "Lesson"})
            node = next(n for n in service.repository.list_nodes() if n.label == "Casual lesson")
            self.assertEqual(int((node.metadata or {}).get("access_count") or 0), 0)

    def test_filters_apply_in_search_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp)
            result = service.search_memory("discount", project_id="architectos", limit=8,
                                           filters={"work_item_state": "!=Done"})
            labels = [hit["node"]["label"] for hit in result["hits"]]
            self.assertIn("Req open", labels)
            self.assertNotIn("Req done", labels)

    def test_filter_alternatives_and_bool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(tmp)
            result = service.search_memory("", project_id="architectos", mode="list",
                                           filters={"type": "Lesson|Decision"})
            labels = [hit["node"]["label"] for hit in result["hits"]]
            self.assertEqual(labels, ["Casual lesson"])


class MemoryCorpusHygieneTests(unittest.TestCase):
    def _add(self, service, **overrides):
        payload = {
            "project_id": "architectos",
            "label": "Plain note",
            "type": "Lesson",
            "scope": "project",
            "text": "Ordinary body text.",
            "source": "manual",
        }
        payload.update(overrides)
        return service.add_memory(payload)

    def test_keyword_in_text_does_not_promote_to_long_term(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            node = self._add(service, text="We discussed architecture and routing constraints over lunch.")
            metadata = dict(service.repository.get_node(node["id"]).metadata or {})
            self.assertEqual(metadata.get("memory_tier"), "short_term")

    def test_keyword_in_label_still_promotes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            node = self._add(service, label="Architecture decision: routing policy")
            metadata = dict(service.repository.get_node(node["id"]).metadata or {})
            self.assertEqual(metadata.get("memory_tier"), "long_term")

    def test_artifact_never_keyword_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            node = self._add(service, type="Artifact", label="architecture decision record", text="architecture adr decision")
            metadata = dict(service.repository.get_node(node["id"]).metadata or {})
            self.assertEqual(metadata.get("memory_tier"), "short_term")

    def test_reclassify_demotes_inflated_long_term(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            node = self._add(service, text="architecture mentioned deep in the body")
            engine = service.memory_lifecycle
            # Simulate the legacy inflation: node stored as long_term although current rules disagree.
            stored = service.repository.get_node(node["id"])
            metadata = dict(stored.metadata or {})
            metadata["memory_tier"] = "long_term"
            stored.metadata = metadata
            service.repository.upsert_node(stored, notify=False)

            dry = engine.reclassify_memory_tiers("architectos", dry_run=True)
            self.assertEqual(dry["changed"], 1)
            self.assertEqual(dry["items"][0]["to"], "short_term")
            still_long = dict(service.repository.get_node(node["id"]).metadata or {})
            self.assertEqual(still_long.get("memory_tier"), "long_term", "dry-run must not write")

            applied = engine.reclassify_memory_tiers("architectos", dry_run=False)
            self.assertEqual(applied["changed"], 1)
            demoted = dict(service.repository.get_node(node["id"]).metadata or {})
            self.assertEqual(demoted.get("memory_tier"), "short_term")
            self.assertTrue(demoted.get("decay_enabled"))

    def test_purge_memory_candidates_respects_windows(self) -> None:
        from datetime import datetime, timedelta, timezone

        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            repo = service.repository
            old_ts = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
            fresh_ts = datetime.now(timezone.utc).isoformat()
            repo.upsert_memory_candidate({
                "id": "cand_old", "project_id": "architectos", "source_type": "chat",
                "status": "rejected", "label": "old rejected", "text": "old rejected candidate body", "source_ref": "r1",
                "created_at": old_ts, "updated_at": old_ts, "rejected_at": old_ts,
            })
            repo.upsert_memory_candidate({
                "id": "cand_fresh", "project_id": "architectos", "source_type": "chat",
                "status": "rejected", "label": "fresh rejected", "text": "fresh rejected candidate body", "source_ref": "r2",
                "created_at": fresh_ts, "updated_at": fresh_ts, "rejected_at": fresh_ts,
            })
            result = service.memory_lifecycle.purge_memory_candidates("architectos", dry_run=False)
            self.assertEqual(result["candidates_purged"], 1)
            self.assertIsNone(repo.get_memory_candidate("cand_old"))
            self.assertIsNotNone(repo.get_memory_candidate("cand_fresh"))

            granola_ts = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
            repo.upsert_memory_candidate({
                "id": "cand_granola_old",
                "project_id": "architectos",
                "source_type": "granola",
                "status": "rejected",
                "label": "Granola: old meeting",
                "text": "Granola meeting body that was rejected",
                "source_ref": "m-old",
                "created_at": granola_ts,
                "updated_at": granola_ts,
                "rejected_at": granola_ts,
                "metadata": {"meeting_id": "m-old"},
            })
            service.memory_lifecycle.purge_memory_candidates("architectos", dry_run=False)
            self.assertIsNotNone(repo.get_memory_candidate("cand_granola_old"))

    def test_code_artifacts_skip_similarity_edges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            linker = service.graph_auto_linker
            code_a = self._add(service, type="Artifact", label="billing calc module", text="discount billing calculation pipeline",
                               source="project_scan", metadata={"path": "src/billing/calc.py"})
            code_b = self._add(service, type="Artifact", label="billing calc tests", text="discount billing calculation pipeline",
                               source="project_scan", metadata={"path": "tests/billing/test_calc.py"})
            note = self._add(service, label="billing calc decision", text="discount billing calculation pipeline rationale")
            self.assertTrue(linker._is_code_artifact(service.repository.get_node(code_a["id"])))  # noqa: SLF001
            linker.rebuild("architectos")
            edges = [edge for edge in service.repository.list_edges() if edge.type == "RELATED_TO"]
            pairs = {tuple(sorted((edge.source, edge.target))) for edge in edges}
            self.assertNotIn(tuple(sorted((code_a["id"], code_b["id"]))), pairs)

    def test_prune_code_artifact_similarity_edges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            linker = service.graph_auto_linker
            code = self._add(service, type="Artifact", label="module file", text="body",
                             source="project_scan", metadata={"path": "src/mod.py"})
            note = self._add(service, label="related note", text="body")
            edge = service.repository.add_edge(code["id"], note["id"], "RELATED_TO", "project", 0.7)
            dry = linker.prune_code_artifact_similarity_edges("architectos", dry_run=True)
            self.assertEqual(dry["pruned"], 1)
            self.assertTrue(any(item["id"] == edge.id for item in dry["items"]))
            applied = linker.prune_code_artifact_similarity_edges("architectos", dry_run=False)
            self.assertEqual(applied["pruned"], 1)
            remaining = [item for item in service.repository.list_edges() if item.id == edge.id]
            self.assertEqual(remaining, [])


class SqliteVecSearchTests(unittest.TestCase):
    def test_repository_reports_vector_backend(self) -> None:
        from backend.architectos.vecsql import sqlite_vec_available, sqlite_vec_status

        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            backend = service.repository.vector_backend()
            self.assertEqual(backend, "sqlite-vec" if sqlite_vec_available() else "python")
            self.assertEqual(service.memory_embeddings.provider_info()["vector_backend"], backend)
            status = sqlite_vec_status()
            self.assertIn(status["reason"], {"ok", "not-installed", "load-extension-disabled", "probe-failed"})
            self.assertEqual(service.memory_embeddings.provider_info()["sqlite_vec"]["loaded"], status["loaded"])

    def test_sql_knn_path_ranks_the_matching_node_first(self) -> None:
        from unittest import mock

        from backend.architectos.embeddings import cosine, unpack_vector

        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            token = service.add_memory({
                "project_id": "architectos",
                "label": "Rotate production API tokens every 30 days",
                "type": "Decision",
                "scope": "project",
                "text": "Production services rotate API tokens every thirty days.",
            })
            service.add_memory({
                "project_id": "architectos",
                "label": "Office snack budget",
                "type": "Lesson",
                "scope": "project",
                "text": "The office keeps tea and biscuits in the kitchen.",
            })

            def _fake_load(conn) -> bool:
                def distance(left: bytes, right: bytes) -> float:
                    return 1.0 - cosine(unpack_vector(left), unpack_vector(right))

                conn.create_function("vec_distance_cosine", 2, distance)
                return True

            with mock.patch("backend.architectos.vecsql.sqlite_vec_available", return_value=True), mock.patch(
                "backend.architectos.storage.sqlite_vec_available", return_value=True
            ), mock.patch("backend.architectos.storage.load_sqlite_vec", side_effect=_fake_load), mock.patch.object(
                service.repository, "vector_backend", return_value="sqlite-vec"
            ):
                hits = service.memory_embeddings.search_scored(
                    "token rotation policy for production APIs",
                    project_id="architectos",
                    limit=5,
                    min_score=0.01,
                )
            self.assertTrue(hits)
            self.assertEqual(hits[0][0], token["id"])


_EMBED_ENV_KEYS = (
    "CLOUDFLARE_API_TOKEN",
    "CF_API_TOKEN",
    "CLOUDFLARE_ACCOUNT_ID",
    "CF_ACCOUNT_ID",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_BASE",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "OLLAMA_HOST",
    "OLLAMA_BASE_URL",
    "MEMORY_EMBEDDING_PROVIDER",
    "MEMORY_EMBEDDING_MODEL",
    "MEMORY_EMBEDDING_DIMENSIONS",
)


class EmbeddingConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        import os

        self._saved_env = {key: os.environ[key] for key in _EMBED_ENV_KEYS if key in os.environ}
        for key in _EMBED_ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        import os

        for key in _EMBED_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(self._saved_env)

    def test_catalog_exposes_connection_fields(self) -> None:
        from backend.architectos.embeddings import embedding_provider_catalog

        catalog = {item["id"]: item for item in embedding_provider_catalog()}
        cf_fields = {field["id"] for field in catalog["cloudflare"]["connection_fields"]}
        self.assertEqual(cf_fields, {"account_id", "api_key"})
        gemini_fields = {field["id"] for field in catalog["gemini"]["connection_fields"]}
        self.assertIn("api_key", gemini_fields)
        self.assertIn("base_url", gemini_fields)

    def test_pinned_cloudflare_without_keys_does_not_fall_back_on_probe(self) -> None:
        from backend.architectos.embeddings import HashEmbeddingProvider, build_embedding_provider, probe_embedding_provider

        settings = {"embedding_provider": "cloudflare", "embedding_model": "@cf/baai/bge-m3"}
        runtime = build_embedding_provider(settings, allow_fallback=True)
        self.assertIsInstance(runtime, HashEmbeddingProvider)
        pinned = build_embedding_provider(settings, allow_fallback=False)
        self.assertIsNone(pinned)
        result = probe_embedding_provider(settings)
        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "missing_credentials")
        self.assertEqual(result["requested_provider"], "cloudflare")
        self.assertNotEqual(result.get("provider"), "hash")
        self.assertIn("CLOUDFLARE_API_TOKEN", result["hint"])

    def test_hash_probe_is_ready(self) -> None:
        from backend.architectos.embeddings import probe_embedding_provider

        result = probe_embedding_provider({"embedding_provider": "hash"})
        self.assertTrue(result["ready"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["provider"], "hash")
        self.assertGreater(result["dimensions"], 0)

    def test_service_test_writes_last_check_and_secrets_to_env_local(self) -> None:
        import json
        import os
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                payload = {"embedding": {"values": [0.25] * 8}}
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
            with tempfile.TemporaryDirectory() as tmp:
                service = ArchitectOSService(Path(tmp))
                missing = service.test_memory_embeddings({
                    "embedding_provider": "cloudflare",
                    "embedding_model": "@cf/baai/bge-m3",
                    "save": True,
                })
                self.assertFalse(missing["ready"])
                self.assertEqual(missing["status"], "missing_credentials")
                stored = service.repository.get_setting("memory_retrieval") or {}
                self.assertEqual(stored.get("embedding_provider"), "cloudflare")
                self.assertEqual((stored.get("embedding_last_check") or {}).get("status"), "missing_credentials")

                result = service.test_memory_embeddings({
                    "embedding_provider": "gemini",
                    "embedding_model": "gemini-embedding-001",
                    "embedding_dimensions": 8,
                    "embedding_base_url": f"http://127.0.0.1:{server.server_port}/v1beta",
                    "api_key": "probe-secret",
                    "save": True,
                })
                self.assertTrue(result["ready"], result)
                self.assertEqual(result["provider"], "gemini")
                env_local = (Path(tmp) / ".env.local").read_text(encoding="utf-8")
                self.assertIn("GEMINI_API_KEY=probe-secret", env_local)
                self.assertEqual(os.environ.get("GEMINI_API_KEY"), "probe-secret")
                persisted = service.repository.get_setting("memory_retrieval") or {}
                self.assertNotIn("api_key", persisted)
                self.assertTrue((persisted.get("embedding_last_check") or {}).get("ready"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_update_settings_strips_embedding_secrets(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.update_settings({
                "memory_retrieval": {
                    "embedding_provider": "openai",
                    "embedding_base_url": "https://api.openai.com/v1",
                    "api_key": "sk-not-in-sqlite",
                },
            })
            stored = service.repository.get_setting("memory_retrieval") or {}
            self.assertNotIn("api_key", stored)
            self.assertNotIn("embedding_api_key", stored)
            self.assertEqual(stored.get("embedding_provider"), "openai")
            env_local = (Path(tmp) / ".env.local").read_text(encoding="utf-8")
            self.assertIn("OPENAI_API_KEY=sk-not-in-sqlite", env_local)
            self.assertEqual(os.environ.get("OPENAI_API_KEY"), "sk-not-in-sqlite")

    def test_upsert_env_local_keeps_unrelated_keys(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.local").write_text("KEEP_ME=yes\nOLD_KEY=stale\n", encoding="utf-8")
            service = ArchitectOSService(root)
            service._upsert_env_local({"OLD_KEY": "fresh", "NEW_KEY": "added"})
            text = (root / ".env.local").read_text(encoding="utf-8")
            self.assertIn("KEEP_ME=yes", text)
            self.assertIn("OLD_KEY=fresh", text)
            self.assertIn("NEW_KEY=added", text)
            self.assertNotIn("OLD_KEY=stale", text)
            self.assertEqual(os.environ.get("OLD_KEY"), "fresh")
            self.assertEqual(os.environ.get("NEW_KEY"), "added")
            os.environ.pop("OLD_KEY", None)
            os.environ.pop("NEW_KEY", None)


if __name__ == "__main__":
    unittest.main()
