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
        self.assertIn("fts:55", next(hit.reasons for hit in hits if hit.node.id == "top"))
        self.assertIn("fts:15", next(hit.reasons for hit in hits if hit.node.id == "deep"))

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
            self.assertEqual(float(prepared[1]["metadata"].get("duplicate_score") or 0), 1.0)


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
            "label": "Chat fact: rounding decided",
            "type": "Decision",
            "scope": "project",
            "text": "Durable fact extracted from chat: discount rounds half up.",
            "confidence": 0.9,
            "metadata": {"template": "chat_fact_keeper"},
        }
        payload.update(overrides)
        return service.repository.upsert_memory_candidate(payload)

    def test_auto_accept_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            candidate = self._candidate(service)
            result = service._maybe_auto_accept_chat_candidate(candidate, service.memory_lifecycle.settings())  # noqa: SLF001
            self.assertIsNone(result)
            stored = service.repository.get_memory_candidate("cand_auto_1")
            self.assertEqual(stored.get("status"), "candidate")

    def test_auto_accept_promotes_durable_fact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.repository.set_setting("memory_lifecycle", {"chat_auto_accept": True})
            candidate = self._candidate(service)
            result = service._maybe_auto_accept_chat_candidate(candidate, service.memory_lifecycle.settings())  # noqa: SLF001
            self.assertIsNotNone(result)
            memory = result.get("memory") or {}
            self.assertTrue(memory.get("id"))
            node = service.repository.get_node(memory["id"])
            metadata = dict(node.metadata or {})
            self.assertTrue(metadata.get("auto_accepted"))
            self.assertEqual(metadata.get("source_ref"), "chat_1")
            stored = service.repository.get_memory_candidate("cand_auto_1")
            self.assertEqual(stored.get("status"), "promoted")

    def test_auto_accept_skips_weak_or_non_durable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.repository.set_setting("memory_lifecycle", {"chat_auto_accept": True})
            settings = service.memory_lifecycle.settings()
            weak = self._candidate(service, id="cand_auto_2", confidence=0.6)
            self.assertIsNone(service._maybe_auto_accept_chat_candidate(weak, settings))  # noqa: SLF001
            lesson = self._candidate(service, id="cand_auto_3", type="Lesson", confidence=0.95)
            self.assertIsNone(service._maybe_auto_accept_chat_candidate(lesson, settings))  # noqa: SLF001
            orphan = self._candidate(service, id="cand_auto_4", source_ref="")
            self.assertIsNone(service._maybe_auto_accept_chat_candidate(orphan, settings))  # noqa: SLF001


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


if __name__ == "__main__":
    unittest.main()
