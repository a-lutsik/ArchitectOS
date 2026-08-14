from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from backend.architectos.launcher import build_app_window_command, clear_runtime_state, find_available_port, read_runtime_state, write_runtime_state
from backend.architectos.mcp import MCPError
from backend.architectos.service import ArchitectOSService
from backend.architectos.storage import sanitize_text


class ArchitectOSMemoryTests(unittest.TestCase):
    def test_seed_search_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            project = service.repository.get_project("architectos")
            self.assertIsNotNone(project)
            self.assertEqual(project.name, "System Workspace")
            self.assertEqual(project.root_path, "")
            service.add_memory({
                "project_id": "architectos",
                "label": "Memory scopes",
                "type": "Decision",
                "scope": "project",
                "text": "Memory uses interface, project, shared, and global scopes.",
            })
            service.add_memory({
                "project_id": "architectos",
                "label": "Context Builder",
                "type": "Feature",
                "scope": "project",
                "text": "Context builder prepares project memory for prompts.",
            })
            search = service.search_memory("memory scopes", project_id="architectos", limit=5)
            self.assertGreaterEqual(len(search["hits"]), 1)
            labels = [hit["node"]["label"] for hit in search["hits"]]
            self.assertIn("Memory scopes", labels)
            context = service.context("context builder", project_id="architectos")
            self.assertIn("ArchitectOS Memory Context", context["context"])
            self.assertIn("Context Builder", context["context"])

    def test_add_memory_redacts_secret_like_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            node = service.add_memory({"project_id": "architectos", "label": "Provider token rule", "type": "Constraint", "scope": "project", "text": "Do not store token=abc123456789xyz in memory."})
            self.assertIn("[REDACTED]", node["text"])
            self.assertTrue(node["metadata"].get("redacted"))

    def test_tasks_workflows_and_local_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            task = service.create_task({"project_id": "architectos", "title": "Test task", "priority": "high"})
            self.assertEqual(task["status"], "todo")
            updated = service.update_task(task["id"], {"status": "doing"})
            self.assertEqual(updated["status"], "doing")
            workflow = service.run_workflow("help", {"project_id": "architectos", "query": "memory", "provider_id": "local-memory", "scan_limit": 0})
            self.assertEqual(workflow["status"], "ok")
            self.assertIn("ArchitectOS Memory Context", workflow["context"])
            self.assertTrue(workflow["task"]["id"])
            self.assertEqual(workflow["task"]["status"], "done")
            self.assertTrue(workflow["review_task"]["id"])
            self.assertTrue(workflow["memory"]["id"])
            self.assertEqual([step["id"] for step in workflow["steps"]], ["scan", "context", "task", "provider", "memory", "review"])
            self.assertEqual(workflow["provider"]["id"], "local-memory")
            chat = service.post_chat_message({"project_id": "architectos", "message": "What is the memory model?"})
            self.assertIn("Strongest memory matches", chat["response"])
            self.assertEqual(len(chat["chat"]["messages"]), 2)

    def test_chat_turn_does_not_create_candidate_until_session_ends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            result = service.post_chat_message({
                "project_id": "architectos",
                "message": "Architecture decision: useful chat turns should become reviewable memory candidates.",
                "provider_id": "local-memory",
            })
            self.assertEqual(service.list_memory_candidates("architectos")["candidates"], [])
            chat_id = result["chat"]["id"]
            finalized = service.finalize_chat_session(chat_id, force=True, trigger="manual")
            candidates = service.list_memory_candidates("architectos")["candidates"]
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["source_type"], "chat")
            self.assertEqual(candidates[0]["metadata"]["template"], "chat_session_summary")
            self.assertEqual(finalized["chat"]["session_status"], "complete")
            self.assertIn("Architecture decision", candidates[0]["text"])

    def test_chitchat_session_skips_candidate_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            chat_id = None
            for message in ("привет!", "как дела?", "hello", "thanks"):
                result = service.post_chat_message({
                    "project_id": "architectos",
                    "message": message,
                    "provider_id": "local-memory",
                    "chat_id": chat_id,
                })
                chat_id = result["chat"]["id"]
            finalized = service.finalize_chat_session(chat_id, force=True, trigger="manual")
            self.assertTrue(finalized.get("skipped"))
            self.assertEqual(service.list_memory_candidates("architectos")["candidates"], [])

    def test_chat_memory_mode_off_skips_auto_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.update_settings({"memory_lifecycle": {"chat_memory_mode": "off"}})
            result = service.post_chat_message({
                "project_id": "architectos",
                "message": "Architecture decision: this should not auto-save when mode is off.",
                "provider_id": "local-memory",
            })
            idle = service._finalize_idle_chat_sessions()
            self.assertTrue(idle.get("disabled"))
            self.assertEqual(service.list_memory_candidates("architectos")["candidates"], [])
            # Manual finalize still allowed with force.
            finalized = service.finalize_chat_session(result["chat"]["id"], force=True, trigger="manual")
            self.assertEqual(finalized["chat"]["session_status"], "complete")

    def test_stale_chat_candidates_expire_by_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.update_settings({"memory_lifecycle": {"chat_candidate_ttl_days": 1}})
            result = service.post_chat_message({
                "project_id": "architectos",
                "message": "Architecture decision: expire stale chat candidates after TTL.",
                "provider_id": "local-memory",
            })
            service.finalize_chat_session(result["chat"]["id"], force=True, trigger="manual")
            candidates = service.list_memory_candidates("architectos")["candidates"]
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            candidate["created_at"] = "2020-01-01T00:00:00Z"
            candidate["updated_at"] = "2020-01-01T00:00:00Z"
            service.repository.update_memory_candidate(candidate)
            result = service.memory_lifecycle.expire_stale_chat_candidates("architectos")
            self.assertGreaterEqual(result["chat_candidates_expired"], 1)
            self.assertEqual(service.list_memory_candidates("architectos")["candidates"], [])

    def test_idle_finalize_creates_session_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.update_settings({"memory_lifecycle": {"chat_session_idle_minutes": 1}})
            created = service.post_chat_message({
                "project_id": "architectos",
                "message": "Architecture decision: idle sessions should summarize into candidates.",
                "provider_id": "local-memory",
            })
            chat = service.repository.get_chat(created["chat"]["id"])
            chat["last_activity_at"] = "2020-01-01T00:00:00Z"
            chat["updated_at"] = "2020-01-01T00:00:00Z"
            service.repository.upsert_chat(chat)
            sweep = service._finalize_idle_chat_sessions()
            self.assertGreaterEqual(sweep["finalized"], 1)
            candidates = service.list_memory_candidates("architectos")["candidates"]
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["metadata"]["template"], "chat_session_summary")
            closed = service.repository.get_chat(created["chat"]["id"])
            self.assertEqual(closed["session_status"], "complete")

            # Continuing the chat reopens the session and bumps revision.
            continued = service.post_chat_message({
                "project_id": "architectos",
                "chat_id": created["chat"]["id"],
                "message": "Architecture decision: reopen after idle finalize.",
                "provider_id": "local-memory",
            })
            self.assertEqual(continued["chat"]["session_status"], "active")
            self.assertGreaterEqual(int(continued["chat"]["session_revision"] or 1), 2)
            self.assertEqual(service.list_memory_candidates("architectos")["candidates"], candidates)

    def test_continued_chat_summarizes_only_delta_and_links_promoted_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            summarized_segments: list[list[str]] = []

            def fake_summary(chat, *, session_messages=None):
                texts = [str(item.get("text") or "") for item in (session_messages or [])]
                summarized_segments.append(texts)
                revision = int(chat.get("session_revision") or 1)
                return {
                    "text": f"Session {revision} standalone fact: {texts[0]}",
                    "has_facts": True,
                    "facts": [{"text": texts[0], "type": "Decision"}],
                    "type": "Decision",
                    "confidence": 0.8,
                }

            service._summarize_chat_session = fake_summary
            first = service.post_chat_message({
                "project_id": "architectos",
                "message": "Decision: first session fact.",
                "provider_id": "local-memory",
            })
            chat_id = first["chat"]["id"]
            service.finalize_chat_session(chat_id, force=True, trigger="manual")

            second = service.post_chat_message({
                "project_id": "architectos",
                "chat_id": chat_id,
                "message": "Decision: second session fact.",
                "provider_id": "local-memory",
            })
            service.finalize_chat_session(chat_id, force=True, trigger="manual")

            self.assertEqual(len(summarized_segments), 2)
            self.assertEqual(len(summarized_segments[0]), 2)
            self.assertEqual(len(summarized_segments[1]), 2)
            self.assertIn("first session fact", summarized_segments[0][0])
            self.assertIn("second session fact", summarized_segments[1][0])
            self.assertNotIn("first session fact", " ".join(summarized_segments[1]))

            candidates = service.list_memory_candidates("architectos", "all")["candidates"]
            session_candidates = sorted(
                [item for item in candidates if (item.get("metadata") or {}).get("template") == "chat_session_summary"],
                key=lambda item: int((item.get("metadata") or {}).get("session_revision") or 0),
            )
            self.assertEqual([int(item["metadata"]["session_revision"]) for item in session_candidates], [1, 2])
            self.assertTrue(all(item["source_ref"] == chat_id for item in session_candidates))
            self.assertEqual(session_candidates[1]["metadata"]["session_start_message_index"], 2)
            self.assertEqual(session_candidates[1]["metadata"]["session_end_message_index"], 4)

            first_promoted = service.promote_memory_candidate(session_candidates[0]["id"])
            second_promoted = service.promote_memory_candidate(session_candidates[1]["id"])
            edges = [edge for edge in service.repository.list_edges() if edge.type == "NEXT_SESSION"]
            self.assertTrue(any(
                edge.source == first_promoted["memory"]["id"] and edge.target == second_promoted["memory"]["id"]
                for edge in edges
            ))
            second_node = service.repository.get_node(second_promoted["memory"]["id"])
            self.assertEqual(second_node.metadata.get("chat_id"), chat_id)
            self.assertEqual(int(second_node.metadata.get("session_revision") or 0), 2)

    def test_chat_list_is_lightweight_and_single_chat_has_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            created = service.post_chat_message({
                "project_id": "architectos",
                "message": "Architecture decision: chat lists should stay lightweight.",
                "provider_id": "local-memory",
            })
            listing = service.list_chats("architectos")
            self.assertTrue(listing["lightweight"])
            self.assertGreaterEqual(len(listing["chats"]), 1)
            summary = next(item for item in listing["chats"] if item["id"] == created["chat"]["id"])
            self.assertNotIn("messages", summary)
            self.assertEqual(summary["message_count"], 2)

            full = service.get_chat(created["chat"]["id"])
            self.assertEqual(len(full["messages"]), 2)
            self.assertEqual(full["id"], summary["id"])

    def test_chat_remember_writes_direct_memory_without_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.post_chat_message({
                "project_id": "architectos",
                "message": "Architecture decision: remember this response directly.",
                "provider_id": "local-memory",
                "remember": True,
            })
            self.assertEqual(service.repository.count_memory_candidates_by_status("architectos")["pending"], 0)
            nodes = [node for node in service.repository.list_nodes() if dict(node.metadata or {}).get("source") == "chat"]
            self.assertEqual(len(nodes), 1)

    def test_council_turn_persists_chat_without_per_turn_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))

            def fake_run_ai(payload):
                return {
                    "text": f"{payload.get('provider_id')} says architecture decision should feed project memory.",
                    "provider": {"id": payload.get("provider_id"), "status": "ok"},
                    "run_id": f"run-{payload.get('provider_id')}",
                }

            service.council._run_ai = fake_run_ai
            events = list(service.stream_council({
                "project_id": "architectos",
                "message": "Architecture decision: council conversations should create memory candidates.",
                "models": ["openai"],
            }))
            done = events[-1]["result"]
            self.assertTrue(done["chat"]["id"])
            self.assertEqual(len(done["chat"]["messages"]), 2)
            self.assertEqual(service.list_memory_candidates("architectos")["candidates"], [])
            finalized = service.finalize_chat_session(done["chat"]["id"], force=True, trigger="manual")
            candidates = service.list_memory_candidates("architectos")["candidates"]
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["metadata"]["template"], "chat_session_summary")
            self.assertEqual(finalized["chat"]["session_status"], "complete")

    def test_chat_context_pack_session_summary_and_favorite_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            result = service.post_chat_message({
                "project_id": "architectos",
                "message": "Rule: agents must preserve user changes and architecture decision logs.",
                "provider_id": "local-memory",
            })
            chat_id = result["chat"]["id"]
            finalized = service.finalize_chat_session(chat_id, force=True, trigger="manual")
            self.assertEqual(finalized["chat"]["session_status"], "complete")
            pack = service.chat_context_pack(chat_id, "architecture decision")
            session = next((s for s in pack["summaries"] if s.get("summary_type") == "session_summary"), None)
            self.assertTrue(session and session.get("summary_text"))

            favorite = service.favorite_chat_message(chat_id, {"message_index": 1, "favorite": True})
            self.assertTrue(favorite["candidate"]["id"])
            self.assertEqual(favorite["candidate"]["source_type"], "chat_favorite")

            retry = service.retry_chat_keeper(chat_id)
            self.assertEqual(retry["chat"]["id"], chat_id)
            events = service.repository.list_keeper_events("architectos", chat_id, 20)
            self.assertTrue(any(event["kind"] == "session_keeper" for event in events))

    def test_graph_commands_rename_relink_delete_and_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            a = service.add_memory({"project_id": "architectos", "label": "Alpha", "type": "Decision", "scope": "project", "text": "Alpha decision text."})
            b = service.add_memory({"project_id": "architectos", "label": "Beta", "type": "Decision", "scope": "project", "text": "Beta decision text."})
            c = service.add_memory({"project_id": "architectos", "label": "Gamma", "type": "Decision", "scope": "project", "text": "Gamma decision text."})

            renamed = service.apply_graph_command({"action": "rename", "node_id": a["id"], "label": "Alpha renamed"})
            self.assertEqual(renamed["node"]["label"], "Alpha renamed")

            relinked = service.apply_graph_command({"action": "relink", "source_id": a["id"], "target_id": b["id"], "type": "DEPENDS_ON"})
            self.assertEqual(relinked["edge"]["type"], "DEPENDS_ON")

            merged = service.apply_graph_command({"action": "merge", "source_id": c["id"], "target_id": b["id"]})
            self.assertEqual(merged["source"]["status"], "merged")

            deleted = service.apply_graph_command({"action": "delete", "edge_id": relinked["edge"]["id"]})
            self.assertTrue(deleted["deleted"])

    def test_memory_ingestion_candidates_promote_and_reject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# Notes\n\nDecision: review memory before promotion.", encoding="utf-8")
            (root / "app.py").write_text("def run():\n    return 'candidate code memory'\n", encoding="utf-8")
            service = ArchitectOSService(root)
            service.repository.upsert_chat({"id": "chat-ingest", "project_id": "architectos", "title": "Ingest chat", "messages": [{"role": "user", "text": "Remember the review queue."}, {"role": "assistant", "text": "Use candidates before durable memory."}], "favorite": False, "created_at": "now"})
            result = service.ingest_memory({"project_id": "architectos", "sources": ["docs", "code", "chat", "git"], "limit": 10})
            source_types = {candidate["source_type"] for candidate in result["candidates"]}
            self.assertIn("docs", source_types)
            self.assertIn("code", source_types)
            self.assertIn("chat", source_types)
            pending = service.list_memory_candidates("architectos")["candidates"]
            self.assertGreaterEqual(len(pending), 3)
            promoted = service.promote_memory_candidate(pending[0]["id"])
            self.assertEqual(promoted["candidate"]["status"], "promoted")
            self.assertTrue(promoted["memory"]["id"])
            rejected = service.reject_memory_candidate(pending[1]["id"], {"reason": "duplicate"})
            self.assertEqual(rejected["candidate"]["status"], "rejected")
            self.assertEqual(rejected["candidate"]["reject_reason"], "duplicate")
            exported = service.export_bundle("architectos")
            self.assertIn("memory_candidates", exported)

    def test_inbox_ingestion_from_drop_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = ArchitectOSService(root)
            inbox = service.repository.data_dir / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            (inbox / "meeting-notes.md").write_text("# Sync notes\n\nDecision: discount applies after tax removal.\n", encoding="utf-8")
            (inbox / "todo.txt").write_text("Remember: review queue SLA is 2 days.", encoding="utf-8")
            (inbox / "image.png").write_bytes(b"\x89PNG\r\n")  # non-text: skipped
            result = service.ingest_memory({"project_id": "architectos", "sources": ["inbox"], "limit": 10})
            inbox_candidates = [c for c in result["candidates"] if c["source_type"] == "inbox"]
            self.assertEqual(len(inbox_candidates), 2)
            labels = {c["label"] for c in inbox_candidates}
            self.assertIn("Inbox: meeting-notes.md", labels)
            self.assertIn("Inbox: todo.txt", labels)
            pending = service.list_memory_candidates("architectos")["candidates"]
            self.assertGreaterEqual(len(pending), 2)
            status = service.memory_ingest_status()
            self.assertEqual(status["inbox_dir"], str(inbox))

    def test_inbox_dir_setting_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            drop = root / "drop"
            drop.mkdir()
            (drop / "adr.md").write_text("# ADR\n\nConstraint: stdlib only backend.\n", encoding="utf-8")
            service = ArchitectOSService(root)
            service.update_settings({"memory_ingest": {"inbox_dir": str(drop)}})
            self.assertEqual(service._inbox_dir(), drop)
            result = service.ingest_memory({"project_id": "architectos", "sources": ["folder"], "limit": 5})
            inbox_candidates = [c for c in result["candidates"] if c["source_type"] == "inbox"]
            self.assertEqual(len(inbox_candidates), 1)
            self.assertEqual(inbox_candidates[0]["label"], "Inbox: adr.md")

    def test_suggest_memory_links_llm_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            a = service.add_memory({"label": "Discount computed before tax", "text": "Discount was applied to the gross amount before removing tax, which overstated it.", "type": "Lesson", "scope": "project", "project_id": "architectos"})
            b = service.add_memory({"label": "Discount rounding bug analysis", "text": "Root cause: discount must apply to the net amount after tax removal; see tax rounding in invoice module.", "type": "Decision", "scope": "project", "project_id": "architectos"})
            service.add_memory({"label": "UI theme tokens", "text": "Color palette uses CSS custom properties for dark mode.", "type": "Concept", "scope": "project", "project_id": "architectos"})

            def fake_judge(left, right, project_id, provider_id, providers=None):
                if "discount" in (left.label + right.label).lower() and "tax" in (left.text + right.text).lower():
                    return {"related": True, "edge_type": "SUPPORTS", "reason": "both explain the discount-after-tax rule", "confidence": 0.9}
                return {"related": False, "reason": "unrelated topics", "confidence": 0.4}

            service._judge_link_with_llm = fake_judge

            dry = service.suggest_memory_links({"project_id": "architectos", "limit": 10, "pair_limit": 6, "min_similarity": 0.15})
            self.assertFalse(dry["applied"])
            self.assertGreaterEqual(dry["pool_size"], 3)
            related = [s for s in dry["suggestions"] if s["related"]]
            self.assertEqual(len(related), 1)
            self.assertEqual({related[0]["source_id"], related[0]["target_id"]}, {a["id"], b["id"]})
            self.assertEqual(related[0]["edge_type"], "SUPPORTS")
            self.assertEqual(dry["created"], [])

            applied = service.suggest_memory_links({"project_id": "architectos", "limit": 10, "pair_limit": 6, "min_similarity": 0.15, "apply": True})
            self.assertTrue(applied["applied"])
            self.assertEqual(len(applied["created"]), 1)
            self.assertEqual(applied["created"][0]["type"], "SUPPORTS")

            # Already-linked pair is not suggested again.
            again = service.suggest_memory_links({"project_id": "architectos", "limit": 10, "pair_limit": 6, "min_similarity": 0.15})
            self.assertFalse(any(s["related"] for s in again["suggestions"]))

    def test_parse_link_verdict(self) -> None:
        verdict = ArchitectOSService._parse_link_verdict('Sure! {"related": true, "edge_type": "depends_on", "reason": "A explains B", "confidence": 0.83}')
        self.assertTrue(verdict["related"])
        self.assertEqual(verdict["edge_type"], "DEPENDS_ON")
        self.assertAlmostEqual(verdict["confidence"], 0.83)
        bad = ArchitectOSService._parse_link_verdict("no json here")
        self.assertFalse(bad["related"])
        self.assertIn("error", bad)
        weird = ArchitectOSService._parse_link_verdict('{"related": true, "edge_type": "HATES", "confidence": 5}')
        self.assertEqual(weird["edge_type"], "RELATED_TO")
        self.assertEqual(weird["confidence"], 1.0)

    def test_suggest_consolidations_merge_and_contradicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            a = service.add_memory({"label": "Discount applies after tax removal", "text": "Discount applies to the net amount after tax removal from the gross invoice total.", "type": "Decision", "scope": "project", "project_id": "architectos"})
            b = service.add_memory({"label": "Discount after tax removal rule", "text": "The discount is computed on the net amount after tax removal, per finance policy.", "type": "Decision", "scope": "project", "project_id": "architectos"})
            c = service.add_memory({"label": "Discount applies before tax removal", "text": "Discount applies to the gross amount before tax removal from the invoice total.", "type": "Decision", "scope": "project", "project_id": "architectos"})

            def fake_judge(left, right, project_id, provider_id, providers=None):
                labels = (left.label + right.label).lower()
                if "before" in labels and "after" in labels:
                    return {"action": "contradicts", "reason": "before vs after tax conflict", "confidence": 0.9}
                return {"action": "merge", "keep": "A", "reason": "same rule stated twice", "confidence": 0.95}

            service._judge_consolidation_with_llm = fake_judge

            dry = service.suggest_consolidations({"project_id": "architectos", "limit": 10, "pair_limit": 8, "min_similarity": 0.2})
            self.assertFalse(dry["applied"])
            actions = {(s["source_id"], s["target_id"]): s["action"] for s in dry["suggestions"]}
            self.assertIn("merge", actions.values())
            self.assertEqual(dry["merged"], [])

            applied = service.suggest_consolidations({"project_id": "architectos", "limit": 10, "pair_limit": 8, "min_similarity": 0.2, "apply": True})
            self.assertTrue(applied["applied"])
            # Merge: exactly one pair merged, keeper is A (source side).
            self.assertEqual(len(applied["merged"]), 1)
            merge_record = applied["merged"][0]
            kept_node = service.repository.get_node(merge_record["kept"])
            merged_node = service.repository.get_node(merge_record["merged"])
            self.assertEqual(kept_node.status, "active")
            self.assertEqual(merged_node.status, "merged")
            # Contradiction: CONTRADICTS edge created for the conflicting pair.
            self.assertEqual(len(applied["contradictions"]), 1)
            self.assertEqual(applied["contradictions"][0]["type"], "CONTRADICTS")
            # A repeated run does not re-suggest the same CONTRADICTS pair.
            again = service.suggest_consolidations({"project_id": "architectos", "limit": 10, "pair_limit": 8, "min_similarity": 0.2})
            self.assertFalse(any(s["action"] == "contradicts" for s in again["suggestions"]))

    def test_parse_consolidation_verdict(self) -> None:
        verdict = ArchitectOSService._parse_consolidation_verdict('{"action": "merge", "keep": "B", "reason": "B is fuller", "confidence": 0.9}')
        self.assertEqual(verdict["action"], "merge")
        self.assertEqual(verdict["keep"], "B")
        fallback = ArchitectOSService._parse_consolidation_verdict('{"action": "squash", "confidence": 2}')
        self.assertEqual(fallback["action"], "keep")
        self.assertEqual(fallback["confidence"], 1.0)
        merge_default_keep = ArchitectOSService._parse_consolidation_verdict('{"action": "merge"}')
        self.assertEqual(merge_default_keep["keep"], "A")
        bad = ArchitectOSService._parse_consolidation_verdict("not json")
        self.assertEqual(bad["action"], "keep")
        self.assertIn("error", bad)

    def test_search_memory_as_of_temporal_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            old = service.add_memory({"label": "Old pricing rule", "text": "Legacy pricing: flat 10 percent margin on all items.", "type": "Decision", "scope": "project", "project_id": "architectos"})
            new = service.add_memory({"label": "New pricing rule", "text": "Current pricing: tiered margin by category, replacing the flat 10 percent.", "type": "Decision", "scope": "project", "project_id": "architectos"})
            # Backdate the "old" node.
            old_node = service.repository.get_node(old["id"])
            old_node.created_at = "2025-01-01T00:00:00+00:00"
            service.repository.upsert_node(old_node)
            new_node = service.repository.get_node(new["id"])
            new_node.created_at = "2026-06-01T00:00:00+00:00"
            service.repository.upsert_node(new_node)

            all_hits = service.search_memory("pricing margin", project_id="architectos", limit=8)["hits"]
            all_ids = {h["node"]["id"] for h in all_hits}
            self.assertIn(old["id"], all_ids)
            self.assertIn(new["id"], all_ids)

            as_of_hits = service.search_memory("pricing margin", project_id="architectos", limit=8, as_of="2025-06-01")["hits"]
            as_of_ids = {h["node"]["id"] for h in as_of_hits}
            self.assertIn(old["id"], as_of_ids)
            self.assertNotIn(new["id"], as_of_ids)

    def test_rescan_memory_sources_schedules_and_ingests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# Rescan\n\nDecision: rescan should create candidates.", encoding="utf-8")
            (root / "notes.md").write_text("# More notes\n\nArchitecture constraint for rescan tests.", encoding="utf-8")
            service = ArchitectOSService(root)
            project = service.repository.add_project("Rescan Project", str(root))
            scheduled = service.schedule_memory_rescan({
                "trigger": "manual",
                "project_id": project.id,
                "all_projects": False,
                "sources": ["docs"],
                "limit": 5,
                "include_mcp": False,
            })
            self.assertTrue(scheduled["scheduled"])
            self.assertTrue(scheduled["running"])
            deadline = __import__("time").time() + 10
            status = service.memory_rescan_status()
            while status.get("running") and __import__("time").time() < deadline:
                __import__("time").sleep(0.05)
                status = service.memory_rescan_status()
            self.assertFalse(status.get("running"))
            self.assertFalse(status.get("error"))
            result = status.get("result") or {}
            self.assertTrue(result.get("ok"))
            self.assertGreaterEqual(result.get("count") or 0, 1)
            self.assertEqual(result.get("sources"), ["docs"])
            self.assertEqual(result["projects"][0]["project_id"], project.id)
            pending = service.list_memory_candidates(project.id)["candidates"]
            self.assertGreaterEqual(len(pending), 1)

    def test_batch_update_memory_candidates_by_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            # The batch promote catch-up spawns an embedding backfill thread;
            # keep it off so it cannot race tempdir cleanup.
            service.memory_embeddings.enabled = lambda: False
            service.repository.upsert_memory_candidate({
                "id": "cand-pending-1",
                "project_id": "architectos",
                "source_type": "docs",
                "source_ref": "a.md",
                "label": "Pending A",
                "type": "Doc",
                "scope": "project",
                "text": "Pending candidate A for batch promote.",
                "status": "candidate",
                "confidence": 0.7,
                "metadata": {},
            })
            service.repository.upsert_memory_candidate({
                "id": "cand-dup-1",
                "project_id": "architectos",
                "source_type": "docs",
                "source_ref": "b.md",
                "label": "Duplicate B",
                "type": "Doc",
                "scope": "project",
                "text": "Duplicate candidate B for batch reject.",
                "status": "candidate",
                "confidence": 0.7,
                "metadata": {"duplicate": True, "duplicate_score": 0.91, "duplicate_label": "Pending A"},
            })
            service.repository.upsert_memory_candidate({
                "id": "cand-rejected-1",
                "project_id": "architectos",
                "source_type": "docs",
                "source_ref": "c.md",
                "label": "Rejected C",
                "type": "Doc",
                "scope": "project",
                "text": "Rejected candidate C for batch accept.",
                "status": "rejected",
                "confidence": 0.7,
                "metadata": {},
            })
            rejected_dups = service.batch_update_memory_candidates({
                "project_id": "architectos",
                "action": "reject",
                "status": "duplicate",
            })
            self.assertEqual(rejected_dups["rejected"], 1)
            self.assertEqual(service.repository.get_memory_candidate("cand-dup-1")["status"], "rejected")

            accepted_pending = service.batch_update_memory_candidates({
                "project_id": "architectos",
                "action": "accept",
                "status": "pending",
            })
            self.assertEqual(accepted_pending["promoted"], 1)
            self.assertEqual(service.repository.get_memory_candidate("cand-pending-1")["status"], "promoted")

            accepted_rejected = service.batch_update_memory_candidates({
                "project_id": "architectos",
                "action": "promote",
                "status": "rejected",
            })
            self.assertGreaterEqual(accepted_rejected["promoted"], 1)
            self.assertEqual(service.repository.get_memory_candidate("cand-rejected-1")["status"], "promoted")
            self.assertEqual(service.repository.get_memory_candidate("cand-dup-1")["status"], "promoted")

    def test_granola_mcp_ingestion_creates_reviewable_meeting_candidates(self) -> None:
        class FakeGranolaMCP:
            def __init__(self) -> None:
                self.calls = []

            def call_tool(self, server_id, tool, arguments, timeout=None):
                self.calls.append((server_id, tool, arguments))
                if server_id != "granola":
                    raise MCPError("unexpected server")
                if tool == "list_meetings":
                    return {"result": {"content": [{"type": "text", "text": json.dumps({"meetings": [{"id": "m1", "title": "Platform Sync"}]})}]}}
                if tool == "get_meetings":
                    return {"result": {"meetings": [{
                        "id": "m1",
                        "title": "Platform Sync",
                        "created_at": "2026-07-07T10:00:00Z",
                        "attendees": [{"name": "Ari"}, {"email": "dev@example.com"}],
                        "summary": "Discussed project memory and MCP ingestion.",
                        "decisions": ["Import meetings as reviewable candidates."],
                        "action_items": ["Add Granola source to AutoScan."],
                        "url": "https://granola.test/m/m1",
                    }]}}
                if tool == "get_meeting_transcript":
                    raise MCPError("paid only")
                raise MCPError("unexpected tool")

        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            fake = FakeGranolaMCP()
            service.mcp_manager = fake

            result = service.ingest_memory({"project_id": "architectos", "sources": ["granola"], "limit": 5})

            self.assertEqual(result["warnings"], [])
            self.assertEqual(result["count"], 1)
            candidate = result["candidates"][0]
            self.assertEqual(candidate["source_type"], "granola")
            self.assertEqual(candidate["type"], "Meeting")
            self.assertEqual(candidate["metadata"]["template"], "granola_meeting")
            self.assertEqual(candidate["metadata"]["meeting_id"], "m1")
            self.assertIn("Import meetings as reviewable candidates", candidate["text"])
            self.assertIn(("granola", "get_meetings", {"meeting_ids": ["m1"]}), fake.calls)

    def test_granola_get_meetings_is_chunked_by_ten(self) -> None:
        class FakeGranolaMCP:
            def __init__(self) -> None:
                self.get_calls = []

            def call_tool(self, server_id, tool, arguments, timeout=None):
                if tool == "list_meetings":
                    meetings = [{"id": f"m{i}", "title": f"Meet {i}"} for i in range(12)]
                    return {"result": {"meetings": meetings}}
                if tool == "get_meetings":
                    ids = list(arguments.get("meeting_ids") or [])
                    self.get_calls.append(ids)
                    if len(ids) > 10:
                        raise MCPError("meeting_ids maxItems is 10")
                    return {"result": {"meetings": [
                        {"id": mid, "title": f"Meet {mid}", "summary": f"Rich notes for {mid} about architecture decisions."}
                        for mid in ids
                    ]}}
                if tool == "get_meeting_transcript":
                    raise MCPError("skip")
                raise MCPError(tool)

        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            fake = FakeGranolaMCP()
            service.mcp_manager = fake
            result = service.ingest_memory({"project_id": "architectos", "sources": ["granola"], "limit": 12})
            self.assertEqual(result["count"], 12)
            self.assertEqual(len(fake.get_calls), 2)
            self.assertEqual(len(fake.get_calls[0]), 10)
            self.assertEqual(len(fake.get_calls[1]), 2)
            self.assertTrue(all("Rich notes" in item["text"] for item in result["candidates"]))

    def test_granola_xml_list_splits_into_multiple_candidates(self) -> None:
        class FakeGranolaMCP:
            def call_tool(self, server_id, tool, arguments, timeout=None):
                if tool == "list_meetings":
                    xml = (
                        '<meetings_data count="2">'
                        '<meeting id="a1" title="First Sync" date="Jul 1, 2026">'
                        "<known_participants>Ada</known_participants>"
                        "</meeting>"
                        '<meeting id="a2" title="Second Sync" date="Jul 2, 2026">'
                        "<summary>Follow-up actions</summary>"
                        "</meeting>"
                        "</meetings_data>"
                    )
                    return {"result": {"content": [{"type": "text", "text": xml}]}}
                if tool == "get_meetings":
                    return {"result": {"content": [{"type": "text", "text": (
                        '<meetings_data count="2">'
                        '<meeting id="a1" title="First Sync" date="Jul 1, 2026"><summary>Kickoff notes</summary></meeting>'
                        '<meeting id="a2" title="Second Sync" date="Jul 2, 2026"><summary>Follow-up actions</summary></meeting>'
                        "</meetings_data>"
                    )}]}}
                if tool == "get_meeting_transcript":
                    raise MCPError("skip")
                raise MCPError(tool)

        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.mcp_manager = FakeGranolaMCP()
            result = service.ingest_memory({"project_id": "architectos", "sources": ["granola"], "limit": 10})
            self.assertEqual(result["count"], 2)
            labels = {item["label"] for item in result["candidates"]}
            self.assertIn("Granola: First Sync", labels)
            self.assertIn("Granola: Second Sync", labels)
            self.assertTrue(all(item["metadata"]["template"] == "granola_meeting" for item in result["candidates"]))

    def test_granola_distinct_meeting_ids_are_not_duplicates(self) -> None:
        engine_source = Path(__file__).resolve().parents[1] / "backend" / "architectos" / "service.py"
        self.assertTrue(engine_source.exists())
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            project_id = "architectos"
            existing = {
                "id": "candidate_powergen",
                "project_id": project_id,
                "source_type": "granola",
                "source_ref": "meeting-a",
                "label": "Granola: PowerGen Project",
                "type": "Meeting",
                "scope": "project",
                "text": "Granola meeting: PowerGen Project\n\nDate: Jun 30, 2026 1:11 PM GMT+4\n\nAttendees: Anton <lutsenko.anton@gmail.com>",
                "confidence": 0.74,
                "status": "candidate",
                "metadata": {
                    "template": "granola_meeting",
                    "meeting_id": "meeting-a",
                    "meeting_title": "PowerGen Project",
                    "source": "granola_mcp",
                },
            }
            service.repository.upsert_memory_candidate(existing)
            incoming = {
                "id": "candidate_releases",
                "project_id": project_id,
                "source_type": "granola",
                "source_ref": "meeting-b",
                "label": "Granola: Product releases — data uploader, Python reports, billing features, and dashboard updates",
                "type": "Meeting",
                "scope": "project",
                "text": "Granola meeting: Product releases — data uploader, Python reports, billing features, and dashboard updates\n\nDate: Jun 18, 2026 1:01 PM GMT+4\n\nAttendees: Anton <lutsenko.anton@gmail.com>",
                "confidence": 0.74,
                "metadata": {
                    "template": "granola_meeting",
                    "meeting_id": "meeting-b",
                    "meeting_title": "Product releases — data uploader, Python reports, billing features, and dashboard updates",
                    "source": "granola_mcp",
                },
            }
            prepared = service.ingestion_engine.prepare_candidates(project_id, [incoming], limit=5)
            self.assertEqual(len(prepared), 1)
            self.assertFalse(prepared[0]["metadata"].get("duplicate"))
            self.assertGreaterEqual(float(prepared[0].get("confidence") or 0), 0.7)

    def test_granola_ingestion_warning_does_not_block_other_candidates(self) -> None:
        class FailingGranolaMCP:
            def call_tool(self, _server_id, _tool, _arguments, timeout=None):
                raise MCPError("not authorized")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# Notes\n\nDecision: keep local candidates.", encoding="utf-8")
            service = ArchitectOSService(root)
            service.mcp_manager = FailingGranolaMCP()

            result = service.ingest_memory({"project_id": "architectos", "sources": ["docs", "granola"], "limit": 5})

            self.assertGreaterEqual(result["count"], 1)
            self.assertTrue(any(candidate["source_type"] == "docs" for candidate in result["candidates"]))
            self.assertTrue(result["warnings"])
            self.assertIn("Granola MCP skipped", result["warnings"][0])

    def test_code_ingest_includes_java_js_tsx_css(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "App.java").write_text("class App { String title = \"planning\"; }", encoding="utf-8")
            (root / "src" / "widget.tsx").write_text("export const Widget = () => <div>widget</div>;", encoding="utf-8")
            (root / "src" / "main.js").write_text("export function main() { return 1; }", encoding="utf-8")
            (root / "src" / "theme.css").write_text(".hero { color: #111; }", encoding="utf-8")
            (root / "README.md").write_text("# Docs only", encoding="utf-8")
            service = ArchitectOSService(root)
            project = service.repository.add_project("CodeScan", str(root), "")
            result = service.ingest_memory({"project_id": project.id, "sources": ["code"], "limit": 20, "root_path": str(root)})
            refs = {item["source_ref"] for item in result["candidates"] if item["source_type"] == "code"}
            self.assertIn("src/App.java", refs)
            self.assertIn("src/widget.tsx", refs)
            self.assertIn("src/main.js", refs)
            self.assertIn("src/theme.css", refs)
            self.assertNotIn("README.md", refs)

    def test_azure_boards_ingest_captures_links_and_comments(self) -> None:
        class FakeAzureBoardsMCP:
            def __init__(self) -> None:
                self.calls = []

            def get_server(self, server_id):
                if server_id != "azure-devops":
                    return None
                from backend.architectos.mcp import MCPServerConfig
                return MCPServerConfig.from_dict({
                    "id": "azure-devops",
                    "label": "Azure DevOps",
                    "command": ["npx", "-y", "@azure-devops/mcp", "Fsight1"],
                    "enabled": True,
                    "env": {"ado_mcp_project": "E-AI"},
                })

            def call_tool(self, server_id, tool, arguments, timeout=None):
                self.calls.append((server_id, tool, arguments))
                if server_id != "azure-devops":
                    raise MCPError("unexpected server")
                # Mirrors @azure-devops/mcp: wit_work_item / wit_query dispatch on "action".
                action = str(arguments.get("action") or "")
                if tool == "wit_work_item" and action == "my":
                    return {"result": {"content": [{"type": "text", "text": json.dumps({"workItems": [{"id": 78167}]})}]}}
                if tool == "wit_query" and action == "wiql":
                    return {"result": {"workItems": [{"id": 78167}]}}
                if tool == "search_workitem":
                    return {"result": {"results": []}}
                if tool == "wit_work_item" and action == "get":
                    return {"result": {"content": [{"type": "text", "text": json.dumps({
                        "id": 78167,
                        "fields": {
                            "System.Id": 78167,
                            "System.WorkItemType": "Task",
                            "System.Title": "Docker image publish for sprint 7.7",
                            "System.State": "Active",
                            "System.AssignedTo": {"displayName": "Anton", "uniqueName": "anton@example.com"},
                            "System.IterationPath": "E-AI\\Dev v7.7",
                            "System.AreaPath": "E-AI",
                            "System.Tags": "sprint-7.7; docker",
                            "System.Description": "<p>Publish image for sprint planning</p>",
                            "System.Parent": 77001,
                        },
                        "relations": [
                            {
                                "rel": "System.LinkTypes.Hierarchy-Reverse",
                                "url": "https://dev.azure.com/Fsight1/E-AI/_apis/wit/workItems/77001",
                                "attributes": {"name": "Feature 7.7"},
                            },
                            {
                                "rel": "System.LinkTypes.Related",
                                "url": "https://dev.azure.com/Fsight1/E-AI/_apis/wit/workItems/78168",
                            },
                        ],
                        "url": "https://dev.azure.com/Fsight1/E-AI/_workitems/edit/78167",
                    })}]}}
                if tool == "wit_work_item" and action == "list_comments":
                    return {"result": {"comments": [
                        {"id": 1, "text": "Need FixVersion 7.7 filter", "createdBy": {"displayName": "Debbie"}, "createdDate": "2026-07-01T10:00:00Z"},
                    ]}}
                raise MCPError(f"unexpected tool {tool} ({action or 'no action'})")

        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            fake = FakeAzureBoardsMCP()
            service.mcp_manager = fake
            result = service.ingest_memory({
                "project_id": "architectos",
                "sources": ["azure-boards"],
                "limit": 5,
                "ado_project": "E-AI",
            })
            self.assertEqual(result["warnings"], [])
            self.assertEqual(result["boards_count"], 1)
            self.assertEqual(result["count"], 0)
            self.assertEqual(result["candidates"], [])
            node = result["boards_imported"][0]
            self.assertEqual(node["metadata"]["work_item_id"], "78167")
            self.assertEqual(node["metadata"]["work_item_type"], "Task")
            self.assertEqual(node["metadata"]["iteration_path"], "E-AI\\Dev v7.7")
            self.assertEqual(node["metadata"]["source"], "azure_boards")
            self.assertTrue(any(item["work_item_id"] == "77001" for item in node["metadata"]["relations"]))
            self.assertNotIn("Need FixVersion 7.7 filter", node["text"])
            self.assertIn("linked comment", node["text"].lower())
            stored = service.repository.get_node(node["id"])
            self.assertIsNotNone(stored)
            self.assertEqual(stored.metadata["comments"][0]["text"], "Need FixVersion 7.7 filter")
            comment_nodes = [
                item for item in service.repository.list_nodes()
                if dict(item.metadata or {}).get("chunk_role") == "comment"
                and str(dict(item.metadata or {}).get("parent_work_item_id") or "") == "78167"
            ]
            self.assertEqual(len(comment_nodes), 1)
            self.assertIn("Need FixVersion 7.7 filter", comment_nodes[0].text)
            edges = service.repository.list_edges()
            self.assertTrue(any(edge.type == "HAS_COMMENT" and edge.source == node["id"] for edge in edges))
            # Second ingest updates the same work item in place.
            again = service.ingest_memory({
                "project_id": "architectos",
                "sources": ["azure-boards"],
                "limit": 5,
                "ado_project": "E-AI",
            })
            self.assertEqual(again["boards_updated"], 1)
            self.assertEqual(again["boards_count"], 1)
            self.assertEqual(again["boards_imported"][0]["id"], node["id"])

            fake.calls.clear()
            typed = service.ingest_memory({
                "project_id": "architectos",
                "sources": ["azure-boards"],
                "limit": 5,
                "ado_project": "E-AI",
                "all_items": True,
                "work_item_types": ["Epic", "Task"],
            })
            wiql_call = next(arguments for _server, tool, arguments in fake.calls if tool == "wit_query")
            self.assertIn("[System.WorkItemType] IN ('Epic', 'Task')", wiql_call["wiql"])
            search_call = next(arguments for _server, tool, arguments in fake.calls if tool == "search_workitem")
            self.assertEqual(search_call["workItemType"], ["Epic"])
            self.assertEqual(typed["boards_count"], 1)

            # Default scope is the whole project: the id sweep must use WIQL, not "assigned to me".
            fake.calls.clear()
            service.ingest_memory({
                "project_id": "architectos",
                "sources": ["azure-boards"],
                "limit": 5,
                "ado_project": "E-AI",
            })
            actions = [(tool, arguments.get("action")) for _server, tool, arguments in fake.calls]
            self.assertIn(("wit_query", "wiql"), actions)
            self.assertNotIn(("wit_work_item", "my"), actions)

            # Only an explicit opt-in narrows the scan to the current user.
            fake.calls.clear()
            service.ingest_memory({
                "project_id": "architectos",
                "sources": ["azure-boards"],
                "limit": 5,
                "ado_project": "E-AI",
                "mine_only": True,
            })
            actions = [(tool, arguments.get("action")) for _server, tool, arguments in fake.calls]
            self.assertIn(("wit_work_item", "my"), actions)
            self.assertNotIn(("wit_query", "wiql"), actions)

    def test_azure_git_ingest_writes_repos_and_pull_requests(self) -> None:
        class FakeAzureGitMCP:
            def get_server(self, server_id):
                if server_id not in {"azure-devops", "azure-devops-git"}:
                    return None
                from backend.architectos.mcp import MCPServerConfig
                return MCPServerConfig.from_dict({
                    "id": server_id,
                    "label": "Azure DevOps Git",
                    "command": ["npx", "-y", "@azure-devops/mcp", "Fsight1", "-d", "repositories"],
                    "enabled": True,
                    "env": {"ado_mcp_project": "E-AI"},
                })

            def call_tool(self, server_id, tool, arguments, timeout=None):
                if tool == "repo_repository" and arguments.get("action") == "list":
                    return {"result": {"content": [{"type": "text", "text": json.dumps([
                        {"id": "repo-1", "name": "E-AI", "webUrl": "https://dev.azure.com/Fsight1/E-AI/_git/E-AI", "isDisabled": False},
                        {"id": "repo-2", "name": "DevOps", "webUrl": "https://dev.azure.com/Fsight1/E-AI/_git/DevOps", "isDisabled": False},
                    ])}]}}
                if tool == "repo_pull_request" and arguments.get("action") == "list":
                    return {"result": {"content": [{"type": "text", "text": json.dumps([
                        {
                            "pullRequestId": 15232,
                            "title": "fix customer portal registration",
                            "statusName": "Active",
                            "repository": "E-AI",
                            "createdBy": {"displayName": "Anton", "uniqueName": "anton@example.com"},
                            "sourceRefName": "refs/heads/fix/portal",
                            "targetRefName": "refs/heads/master",
                            "creationDate": "2026-07-14T10:45:49.676Z",
                            "isDraft": False,
                        }
                    ])}]}}
                raise MCPError(f"unexpected tool {tool}")

        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.mcp_manager = FakeAzureGitMCP()
            result = service.ingest_memory({
                "project_id": "architectos",
                "sources": ["azure-git"],
                "limit": 5,
                "ado_project": "E-AI",
                "repository": "E-AI",
            })
            self.assertEqual(result["warnings"], [])
            self.assertGreaterEqual(result["azure_git_count"], 2)
            kinds = {item["metadata"].get("kind") for item in result["azure_git_imported"]}
            self.assertIn("repository", kinds)
            self.assertIn("pull_request", kinds)
            pr = next(item for item in result["azure_git_imported"] if item["metadata"].get("kind") == "pull_request")
            self.assertEqual(pr["metadata"]["pull_request_id"], "15232")
            self.assertIn("fix customer portal registration", pr["label"])
            again = service.ingest_memory({
                "project_id": "architectos",
                "sources": ["azure-git"],
                "limit": 5,
                "ado_project": "E-AI",
                "repository": "E-AI",
            })
            self.assertGreaterEqual(again["azure_git_updated"], 1)

    def test_azure_git_candidates_mode_resolves_server_id(self) -> None:
        class FakeAzureGitMCP:
            def get_server(self, server_id):
                if server_id not in {"azure-devops", "azure-devops-git"}:
                    return None
                from backend.architectos.mcp import MCPServerConfig
                return MCPServerConfig.from_dict({
                    "id": server_id,
                    "label": "Azure DevOps Git",
                    "command": ["npx", "-y", "@azure-devops/mcp", "Fsight1", "-d", "repositories"],
                    "enabled": True,
                    "env": {"ado_mcp_project": "E-AI"},
                })

            def call_tool(self, server_id, tool, arguments, timeout=None):
                if tool == "repo_repository" and arguments.get("action") == "list":
                    return {"result": {"content": [{"type": "text", "text": json.dumps([
                        {"id": "repo-1", "name": "E-AI", "webUrl": "https://dev.azure.com/Fsight1/E-AI/_git/E-AI", "isDisabled": False},
                    ])}]}}
                if tool == "repo_pull_request" and arguments.get("action") == "list":
                    return {"result": {"content": [{"type": "text", "text": json.dumps([
                        {
                            "pullRequestId": 15299,
                            "title": "candidates mode pr",
                            "statusName": "Active",
                            "repository": "E-AI",
                            "creationDate": "2026-07-22T10:00:00Z",
                        }
                    ])}]}}
                raise MCPError(f"unexpected tool {tool}")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            service = ArchitectOSService(Path(tmp))
            service.mcp_manager = FakeAzureGitMCP()
            service.memory_embeddings.enabled = lambda: False  # type: ignore[method-assign]
            result = service.ingest_memory({
                "project_id": "architectos",
                "sources": ["azure-git"],
                "limit": 5,
                "ado_project": "E-AI",
                "ingest_mode": "candidates",
            })
            self.assertEqual(result["warnings"], [])
            self.assertGreaterEqual(result["count"], 2)
            labels = [item["label"] for item in result["candidates"]]
            self.assertTrue(any(label.startswith("Azure Git:") for label in labels))
            self.assertTrue(any(label.startswith("PR #15299:") for label in labels))

    def test_azure_git_mcp_is_error_surfaces_warning(self) -> None:
        class FakeFailingGitMCP:
            def get_server(self, server_id):
                if server_id not in {"azure-devops", "azure-devops-git"}:
                    return None
                from backend.architectos.mcp import MCPServerConfig
                return MCPServerConfig.from_dict({
                    "id": server_id,
                    "label": "Azure DevOps Git",
                    "command": ["npx", "-y", "@azure-devops/mcp", "Fsight1", "-d", "repositories"],
                    "enabled": True,
                    "env": {"ado_mcp_project": "E-AI"},
                })

            def call_tool(self, server_id, tool, arguments, timeout=None):
                return {
                    "result": {
                        "content": [{"type": "text", "text": "Error listing repositories: tunneling socket could not be established, statusCode=403"}],
                        "isError": True,
                    }
                }

            def close_session(self, server_id):
                return None

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            service = ArchitectOSService(Path(tmp))
            service.mcp_manager = FakeFailingGitMCP()
            service.memory_embeddings.enabled = lambda: False  # type: ignore[method-assign]
            result = service.ingest_memory({
                "project_id": "architectos",
                "sources": ["azure-git"],
                "limit": 5,
                "ado_project": "E-AI",
                "ingest_mode": "memory",
            })
            self.assertEqual(result["azure_git_count"], 0)
            self.assertTrue(result["warnings"])
            self.assertTrue(any("tunneling socket" in warning for warning in result["warnings"]))

    def test_ingest_timeout_params_abort_hung_boards_source(self) -> None:
        class SlowBoardsMCP:
            def __init__(self):
                self.closed = []
                self._stop = threading.Event()

            def get_server(self, server_id):
                if server_id != "azure-devops":
                    return None
                from backend.architectos.mcp import MCPServerConfig
                return MCPServerConfig.from_dict({
                    "id": server_id,
                    "label": "Azure DevOps",
                    "command": ["npx", "-y", "@azure-devops/mcp", "Fsight1"],
                    "enabled": True,
                    "env": {"ado_mcp_project": "E-AI"},
                })

            def call_tool(self, server_id, tool, arguments, timeout=None):
                if tool == "wit_work_item" and str(arguments.get("action") or "") == "get":
                    # Simulate a wedged MCP that ignores item_timeout — source budget must still win
                    # without blocking on ThreadPoolExecutor shutdown(wait=True).
                    if not self._stop.wait(60.0):
                        raise TimeoutError("fake MCP still wedged")
                    raise TimeoutError("MCP session closed")
                return {"result": {"content": [{"type": "text", "text": json.dumps({
                    "id": int(arguments.get("id") or 1),
                    "fields": {
                        "System.Id": int(arguments.get("id") or 1),
                        "System.Title": "Slow item",
                        "System.WorkItemType": "Task",
                        "System.State": "Active",
                        "System.CommentCount": 0,
                    },
                    "relations": [],
                })}]}}

            def close_session(self, server_id):
                self.closed.append(server_id)
                self._stop.set()

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            service = ArchitectOSService(Path(tmp))
            fake = SlowBoardsMCP()
            service.mcp_manager = fake
            service.memory_embeddings.enabled = lambda: False  # type: ignore[method-assign]
            started = time.time()
            result = service.ingest_memory({
                "project_id": "architectos",
                "sources": ["azure-boards"],
                "limit": 3,
                "ado_project": "E-AI",
                "ingest_mode": "memory",
                "work_item_ids": [11, 12, 13],
                "item_timeout": 5,
                "source_timeout": 2,
            })
            elapsed = time.time() - started
            # Must return near source_timeout even while MCP worker is still sleeping.
            self.assertLess(elapsed, 6)
            self.assertTrue(any("timeout" in warning.lower() for warning in result["warnings"]))
            self.assertIn("azure-devops", fake.closed)

    def test_boards_source_timeout_keeps_partial_candidates(self) -> None:
        class PartialThenHangMCP:
            def __init__(self):
                self.closed = []
                self._calls = 0
                self._stop = threading.Event()

            def get_server(self, server_id):
                if server_id != "azure-devops":
                    return None
                from backend.architectos.mcp import MCPServerConfig
                return MCPServerConfig.from_dict({
                    "id": server_id,
                    "label": "Azure DevOps",
                    "command": ["npx", "-y", "@azure-devops/mcp", "Fsight1"],
                    "enabled": True,
                    "env": {"ado_mcp_project": "E-AI"},
                })

            def call_tool(self, server_id, tool, arguments, timeout=None):
                if tool != "wit_work_item" or str(arguments.get("action") or "") != "get":
                    raise MCPError(f"unexpected {tool}")
                self._calls += 1
                work_item_id = int(arguments.get("id") or 0)
                if self._calls <= 2:
                    return {"result": {"content": [{"type": "text", "text": json.dumps({
                        "id": work_item_id,
                        "fields": {
                            "System.Id": work_item_id,
                            "System.Title": f"Item {work_item_id}",
                            "System.WorkItemType": "Task",
                            "System.State": "Active",
                            "System.CommentCount": 0,
                        },
                        "relations": [],
                    })}]}}
                if not self._stop.wait(60.0):
                    raise TimeoutError("wedged")
                raise TimeoutError("closed")

            def close_session(self, server_id):
                self.closed.append(server_id)
                self._stop.set()

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            service = ArchitectOSService(Path(tmp))
            fake = PartialThenHangMCP()
            service.mcp_manager = fake
            service.memory_embeddings.enabled = lambda: False  # type: ignore[method-assign]
            result = service.ingest_memory({
                "project_id": "architectos",
                "sources": ["azure-boards"],
                "limit": 5,
                "ado_project": "E-AI",
                "ingest_mode": "candidates",
                "work_item_ids": [101, 102, 103, 104, 105],
                "item_timeout": 5,
                "source_timeout": 3,
            })
            # Already-fetched items must survive the source budget, even if later
            # items hang and the run ends via deadline/timeout.
            self.assertGreaterEqual(int(result.get("count") or 0), 2)
            self.assertIn("azure-devops", fake.closed)

    def test_azure_wiki_ingest_writes_pages_to_memory(self) -> None:
        class FakeAzureWikiMCP:
            def get_server(self, server_id):
                if server_id != "azure-devops":
                    return None
                from backend.architectos.mcp import MCPServerConfig
                return MCPServerConfig.from_dict({
                    "id": "azure-devops",
                    "label": "Azure DevOps",
                    "command": ["npx", "-y", "@azure-devops/mcp", "Fsight1"],
                    "enabled": True,
                    "env": {"ado_mcp_project": "E-AI"},
                })

            def call_tool(self, server_id, tool, arguments, timeout=None):
                if tool == "wiki" and arguments.get("action") == "list_wikis":
                    return {"result": {"content": [{"type": "text", "text": json.dumps([
                        {"id": "wiki-1", "name": "E-AI Wiki"},
                    ])}]}}
                if tool == "wiki" and arguments.get("action") == "list_pages":
                    return {"result": {"value": [
                        {"id": 11, "path": "/Architecture/Overview"},
                        {"id": 12, "path": "/Sprint/Dev-v7.7"},
                    ]}}
                if tool == "wiki" and arguments.get("action") == "get_page_content":
                    path = arguments.get("path") or "/"
                    return {"result": {"content": [{"type": "text", "text": f"# Page {path}\n\nSprint planning notes for {path}."}]}}
                raise MCPError(f"unexpected tool {tool}")

        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.mcp_manager = FakeAzureWikiMCP()
            result = service.ingest_memory({
                "project_id": "architectos",
                "sources": ["azure-wiki"],
                "limit": 5,
                "ado_project": "E-AI",
            })
            self.assertEqual(result["warnings"], [])
            self.assertEqual(result["wiki_count"], 2)
            self.assertEqual(result["count"], 0)
            labels = {item["label"] for item in result["wiki_imported"]}
            self.assertTrue(any("Overview" in label for label in labels))
            self.assertTrue(any("Dev v7.7" in label or "Dev-v7.7" in label for label in labels))
            node = result["wiki_imported"][0]
            self.assertEqual(node["metadata"]["source"], "azure_wiki")
            self.assertTrue(node["metadata"]["wiki_page_key"])
            again = service.ingest_memory({
                "project_id": "architectos",
                "sources": ["azure-wiki"],
                "limit": 5,
                "ado_project": "E-AI",
            })
            self.assertEqual(again["wiki_updated"], 2)

    def test_teams_graph_ingest_writes_meetings_to_memory(self) -> None:
        class FakeTeamsGraph:
            def collect_meetings(self, *, limit=12, lookback_days=None, include_transcripts=None, include_ai_insights=None):
                return [{
                    "meeting_id": "mtg-1",
                    "event_id": "evt-1",
                    "subject": "Sprint 7.7 planning",
                    "start": "2026-07-10T10:00:00.0000000",
                    "end": "2026-07-10T11:00:00.0000000",
                    "join_url": "https://teams.microsoft.com/l/meetup-join/xxx",
                    "web_link": "https://outlook.office.com/calendar/item/yyy",
                    "organizer": "Anton",
                    "attendees": ["Anton", "Debbie"],
                    "insights_text": "Notes:\n- Align Docker publish for Dev v7.7\n\nAction items:\n- Anton: finish image pipeline",
                    "transcript_text": "Anton: Let's lock FixVersion 7.7.\nDebbie: Agreed.",
                    "action_items": ["Anton: finish image pipeline"],
                    "insight_ids": ["ins-1"],
                    "transcript_ids": ["tr-1"],
                    "has_insights": True,
                    "has_transcript": True,
                }][:limit]

        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            result = service.ingest_memory({
                "project_id": "architectos",
                "sources": ["teams-meetings"],
                "limit": 5,
                "_teams_graph_client": FakeTeamsGraph(),
            })
            self.assertEqual(result["warnings"], [])
            self.assertEqual(result["teams_count"], 1)
            self.assertEqual(result["count"], 0)
            node = result["teams_imported"][0]
            self.assertEqual(node["metadata"]["teams_meeting_id"], "mtg-1")
            self.assertEqual(node["metadata"]["source"], "teams_graph")
            self.assertEqual(node["type"], "Meeting")
            self.assertIn("AI Insights", node["text"])
            self.assertIn("Transcript:", node["text"])
            self.assertIn("FixVersion 7.7", node["text"])
            graph = service.graph("architectos")
            hubs = [
                item for item in graph["nodes"]
                if dict(item.get("metadata") or {}).get("hub") and item["metadata"].get("source_key") == "teams-meetings"
            ]
            self.assertEqual(len(hubs), 1)
            again = service.ingest_memory({
                "project_id": "architectos",
                "sources": ["teams-meetings"],
                "limit": 5,
                "_teams_graph_client": FakeTeamsGraph(),
            })
            self.assertEqual(again["teams_updated"], 1)
            self.assertEqual(again["teams_imported"][0]["id"], node["id"])

    def test_teams_graph_missing_config_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            for key in (
                "MS_GRAPH_ACCESS_TOKEN",
                "MS_GRAPH_TENANT_ID",
                "MS_GRAPH_CLIENT_ID",
                "MS_GRAPH_CLIENT_SECRET",
                "MS_GRAPH_USER_ID",
                "TEAMS_USER_ID",
            ):
                os.environ.pop(key, None)
            result = service.ingest_memory({
                "project_id": "architectos",
                "sources": ["teams-meetings"],
                "limit": 3,
            })
            self.assertEqual(result["teams_count"], 0)
            self.assertTrue(any("Teams Graph skipped" in item for item in result["warnings"]))

    def test_memory_lifecycle_refresh_decay_and_long_term_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.update_settings({"memory_lifecycle": {"enabled": True, "refresh_on_access": True, "short_term_ttl_days": 1, "archive_after_days": 2, "delete_after_days": 3, "promote_after_hits": 2}})
            node = service.add_memory({"project_id": "architectos", "label": "Ephemeral scratch", "type": "Lesson", "scope": "project", "text": "Temporary scratch note for memory lifecycle."})
            stored = service.repository.get_node(node["id"])
            self.assertEqual(stored.metadata["memory_tier"], "short_term")
            service.search_memory("ephemeral scratch", "architectos", refresh=True)
            refreshed = service.repository.get_node(node["id"])
            self.assertEqual(refreshed.metadata["access_count"], 1)
            service.search_memory("ephemeral scratch", "architectos", refresh=True)
            promoted = service.repository.get_node(node["id"])
            self.assertEqual(promoted.metadata["memory_tier"], "long_term")
            self.assertFalse(promoted.metadata["decay_enabled"])

            stale = service.add_memory({"project_id": "architectos", "label": "Disposable artifact", "type": "Artifact", "scope": "project", "text": "Disposable cache item for old project inventory."})
            stale_node = service.repository.get_node(stale["id"])
            stale_node.metadata["memory_tier"] = "short_term"
            stale_node.metadata["decay_enabled"] = True
            stale_node.metadata["retention_policy"] = "delete"
            stale_node.metadata["last_accessed_at"] = "2000-01-01T00:00:00Z"
            service.repository.upsert_node(stale_node)
            decay = service.run_memory_decay({"project_id": "architectos"})
            self.assertTrue(any(item["id"] == stale["id"] and item["action"] == "deleted" for item in decay["items"]))
            self.assertEqual(service.repository.get_node(stale["id"]).status, "deleted")

            durable = service.add_memory({"project_id": "architectos", "label": "Architecture rule", "type": "Decision", "scope": "project", "text": "Architecture decisions stay in long term memory."})
            service.promote_memory_long_term(durable["id"], {"reason": "test"})
            long_term = service.repository.get_node(durable["id"])
            long_term.metadata["last_accessed_at"] = "2000-01-01T00:00:00Z"
            service.repository.upsert_node(long_term)
            service.run_memory_decay({"project_id": "architectos"})
            self.assertEqual(service.repository.get_node(durable["id"]).status, "active")
            self.assertEqual(service.repository.get_node(durable["id"]).metadata["memory_tier"], "long_term")

    def test_extended_memory_ingestion_templates_duplicates_and_commit_clusters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs" / "adr").mkdir(parents=True)
            (root / "issues").mkdir()
            (root / "prs").mkdir()
            (root / "meetings").mkdir()
            (root / "docs" / "adr" / "0001-local-memory.md").write_text("# Use Local Memory\n\n## Status\nAccepted\n\n## Context\nArchitectOS needs durable local knowledge.\n\n## Decision\nUse local memory for ArchitectOS ingestion.\n\n## Consequences\nFast local context and reviewable promotion.", encoding="utf-8")
            (root / "issues" / "issue-42.md").write_text("# Provider Setup Bug\n\n## Summary\nProvider checks need clearer setup hints.\n\n## Acceptance Criteria\nShow status, hint, and retry action.", encoding="utf-8")
            (root / "prs" / "pr-15.md").write_text("# Memory Ingestion PR\n\n## Summary\nAdds candidate review queue.\n\n## Testing\nUnit tests and HTTP smoke.", encoding="utf-8")
            (root / "meetings" / "2026-07-05-sync.md").write_text("# Architecture Sync\n\nAttendees: team\n\n## Notes\nReviewed memory ingestion.\n\n## Decisions\nPromote candidates manually.\n\n## Action Items\nAdd duplicate detection.", encoding="utf-8")
            service = ArchitectOSService(root)
            service.add_memory({"project_id": "architectos", "label": "Use Local Memory", "type": "Decision", "scope": "project", "text": "Decision: Use local memory for ArchitectOS ingestion."})
            result = service.ingest_memory({"project_id": "architectos", "sources": ["adr", "issues", "prs", "meetings"], "limit": 20})
            source_types = {candidate["source_type"] for candidate in result["candidates"]}
            self.assertIn("adr", source_types)
            self.assertIn("issue", source_types)
            self.assertIn("pr", source_types)
            self.assertIn("meeting", source_types)
            adr = next(candidate for candidate in result["candidates"] if candidate["source_type"] == "adr")
            self.assertEqual(adr["metadata"]["template"], "adr")
            self.assertTrue(adr["metadata"]["duplicate"])
            self.assertGreaterEqual(adr["metadata"]["duplicate_score"], 0.68)
            clusters = service._build_git_cluster_candidates("architectos", root, [("a1", "feat: add memory ingestion"), ("b2", "feat: add ADR templates"), ("c3", "fix: avoid duplicate candidates"), ("d4", "fix: preserve promoted candidates")], 10)
            cluster_names = {candidate["metadata"]["cluster"] for candidate in clusters}
            self.assertIn("feature", cluster_names)
            self.assertIn("fix", cluster_names)

    def test_memory_ingest_can_write_candidates_directly_to_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "architecture.md").write_text(
                "# Architecture Rule\n\nDecision: direct AutoScan imports should become memory immediately.",
                encoding="utf-8",
            )
            service = ArchitectOSService(root)
            result = service.ingest_memory({
                "project_id": "architectos",
                "sources": ["docs"],
                "limit": 5,
                "ingest_mode": "memory",
            })
            self.assertEqual(result["ingest_mode"], "memory")
            self.assertEqual(result["count"], 0)
            self.assertGreaterEqual(result["direct_count"], 1)
            self.assertGreaterEqual(result["memory_written"], 1)
            self.assertEqual(service.repository.count_memory_candidates_by_status("architectos")["pending"], 0)
            items = service.list_memory_items("architectos", lifecycle_state="stable", tier="long_term", limit=10)
            labels = {item["label"] for item in items["items"]}
            self.assertIn("Doc: docs/architecture.md", labels)

    def test_memory_ingest_can_explicitly_create_review_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# Review Queue\n\nDecision: default ingest should stay reviewable.", encoding="utf-8")
            service = ArchitectOSService(root)
            result = service.ingest_memory({"project_id": "architectos", "sources": ["docs"], "limit": 5, "ingest_mode": "candidates"})
            self.assertEqual(result["ingest_mode"], "candidates")
            self.assertGreaterEqual(result["count"], 1)
            self.assertEqual(result["direct_count"], 0)
            self.assertGreaterEqual(service.repository.count_memory_candidates_by_status("architectos")["pending"], 1)

    def test_project_scan_and_bundle_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# Demo\n\nImportant architecture decision.", encoding="utf-8")
            service = ArchitectOSService(root)
            result = service.scan_project({"project_id": "architectos", "root_path": str(root), "limit": 5})
            self.assertGreaterEqual(result["count"], 1)
            self.assertTrue(any(item["label"] == "README.md" for item in result["imported"]))
            bundle = service.export_bundle(result["project_id"])
            self.assertEqual(bundle["format"], "architectos.bundle")
            imported = service.import_bundle(bundle)
            self.assertGreaterEqual(imported["nodes"], 1)



    def test_project_workspace_files_diff_and_selected_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# Demo\n\nSelected file context content.", encoding="utf-8")
            (root / "app.py").write_text("def run():\n    return 'workspace'\n", encoding="utf-8")
            (root / "pyproject.toml").write_text("[tool.demo]\nname = 'workspace'\n", encoding="utf-8")
            (root / "runtime.bin").write_bytes(b"\x00\x01\x02\x03")
            service = ArchitectOSService(root)
            files = service.project_files("architectos", limit=10)
            paths = {item["path"] for item in files["files"]}
            self.assertIn("README.md", paths)
            opened = service.project_file("architectos", "README.md")
            self.assertIn("Selected file context", opened["text"])
            toml = service.project_file("architectos", "pyproject.toml")
            self.assertTrue(toml["readable"])
            self.assertIn("[tool.demo]", toml["text"])
            binary = service.project_file("architectos", "runtime.bin")
            self.assertFalse(binary["readable"])
            self.assertTrue(binary["binary"])
            self.assertIn("binary file", binary["message"])
            with self.assertRaises(ValueError):
                service.project_file("architectos", "../outside.txt")
            selected = service.selected_file_context({"project_id": "architectos", "path": "README.md", "query": "demo context"})
            self.assertIn("ArchitectOS Selected File Context", selected["context"])
            self.assertIn("--- README.md ---", selected["context"])
            diff = service.project_git_diff("architectos")
            self.assertIn(diff["status"], {"ok", "unavailable", "timeout"})

    def test_project_without_folder_does_not_fallback_to_architectos_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backend" / "architectos").mkdir(parents=True)
            (root / "backend" / "architectos" / "service.py").write_text("# source marker", encoding="utf-8")
            (root / "frontend").mkdir()
            (root / "frontend" / "app.js").write_text("// source marker", encoding="utf-8")
            (root / "foreign.md").write_text("# Foreign file", encoding="utf-8")
            service = ArchitectOSService(root)
            project = service.repository.add_project("Empty Project")

            empty = service.project_files(project.id)
            self.assertEqual(empty["count"], 0)
            self.assertEqual(empty["files"], [])
            self.assertEqual(empty["status"], "no_root")
            self.assertIn("connect a folder", empty["message"].lower())

            with self.assertRaises(ValueError):
                service.create_project({"name": "No Folder"})

    def test_project_files_returns_explorer_tree_entries_not_only_importable_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp) / "app"
            project_root = Path(tmp) / "AnyToAny"
            framework = project_root / "framework"
            app_root.mkdir()
            (framework / "scripts").mkdir(parents=True)
            (framework / "sdk").mkdir()
            (framework / "tools").mkdir()
            (framework / "__init__.py").write_text("", encoding="utf-8")
            (framework / "runtime.bin").write_bytes(b"\x00\x01")

            service = ArchitectOSService(app_root)
            project = service.create_project({"name": "AnyToAny", "root_path": str(project_root)})
            files = service.project_files(project["id"], limit=50)["files"]
            paths = {item["path"].replace("\\", "/"): item["type"] for item in files}

            self.assertEqual(paths["framework"], "folder")
            self.assertEqual(paths["framework/scripts"], "folder")
            self.assertEqual(paths["framework/sdk"], "folder")
            self.assertEqual(paths["framework/tools"], "folder")
            self.assertEqual(paths["framework/runtime.bin"], "file")

    def test_create_project_persists_wizard_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp) / "app"
            project_root = Path(tmp) / "demo"
            app_root.mkdir()
            project_root.mkdir()
            (project_root / "node_modules").mkdir()
            (project_root / "node_modules" / "pkg.js").write_text("ignore me", encoding="utf-8")
            (project_root / "src.py").write_text("print('ok')", encoding="utf-8")

            service = ArchitectOSService(app_root)
            created = service.create_project({
                "name": "Demo",
                "root_path": str(project_root),
                "config": {
                    "code_style": "standard",
                    "ignore_patterns": ["node_modules/"],
                    "auto_index": True,
                    "memory_scope": "project",
                },
            })
            stored = next(project for project in service.projects() if project["id"] == created["id"])
            self.assertEqual(stored["config"]["code_style"], "standard")
            self.assertEqual(stored["config"]["ignore_patterns"], ["node_modules/"])

            scan = service.scan_project({"project_id": created["id"], "limit": 20})
            imported_paths = {item["label"] for item in scan["imported"]}
            self.assertIn("src.py", imported_paths)
            self.assertNotIn("node_modules/pkg.js", imported_paths)

    def test_selected_project_folder_creates_isolated_project_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp) / "app"
            alpha_root = Path(tmp) / "alpha"
            beta_root = Path(tmp) / "beta"
            app_root.mkdir()
            alpha_root.mkdir()
            beta_root.mkdir()
            (alpha_root / "README.md").write_text("# Alpha\n\nAlphaOnlyDecision lives here.", encoding="utf-8")
            (beta_root / "README.md").write_text("# Beta\n\nBetaOnlyDecision lives here.", encoding="utf-8")
            service = ArchitectOSService(app_root)

            alpha = service.create_project({"name": "Alpha", "root_path": str(alpha_root)})
            beta = service.create_project({"name": "Beta", "root_path": str(beta_root)})
            self.assertNotEqual(alpha["id"], beta["id"])

            alpha_scan = service.scan_project({"project_id": alpha["id"], "root_path": str(alpha_root), "limit": 5})
            beta_scan = service.scan_project({"project_id": beta["id"], "root_path": str(beta_root), "limit": 5})
            self.assertEqual(alpha_scan["project_id"], alpha["id"])
            self.assertEqual(beta_scan["project_id"], beta["id"])
            self.assertIn("README.md", {item["path"] for item in service.project_files(alpha["id"])["files"]})
            self.assertIn("README.md", {item["path"] for item in service.project_files(beta["id"])["files"]})

            alpha_hits = service.search_memory("AlphaOnlyDecision", alpha["id"])["hits"]
            beta_hits = service.search_memory("AlphaOnlyDecision", beta["id"])["hits"]
            self.assertTrue(any(hit["node"]["project_id"] == alpha["id"] for hit in alpha_hits))
            self.assertFalse(any("AlphaOnlyDecision" in hit["node"]["text"] for hit in beta_hits if hit["node"]["project_id"] == beta["id"]))
            alpha_graph = service.graph(alpha["id"])
            beta_graph = service.graph(beta["id"])
            self.assertTrue(any(node["label"] == "Alpha" and node["type"] == "Project" for node in alpha_graph["nodes"]))
            self.assertTrue(any(node["label"] == "Beta" and node["type"] == "Project" for node in beta_graph["nodes"]))

    def test_projects_and_active_workspace_project_persist_in_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp) / "app"
            project_root = Path(tmp) / "persisted"
            app_root.mkdir()
            project_root.mkdir()
            (project_root / "README.md").write_text("# Persisted Project", encoding="utf-8")

            first = ArchitectOSService(app_root)
            project = first.create_project({"name": "Persisted", "root_path": str(project_root)})
            first.update_settings({"workspace": {"current_project_id": project["id"]}})

            second = ArchitectOSService(app_root)
            projects = {item["id"]: item for item in second.projects()}
            self.assertIn(project["id"], projects)
            self.assertEqual(projects[project["id"]]["root_path"], str(project_root.resolve()))
            self.assertEqual(second.settings()["workspace"]["current_project_id"], project["id"])

    def test_project_creation_reuses_existing_folder_without_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp) / "app"
            project_root = Path(tmp) / "ProjectA"
            app_root.mkdir()
            project_root.mkdir()

            service = ArchitectOSService(app_root)
            first = service.create_project({"name": "Project A", "root_path": str(project_root)})
            second = service.create_project({"name": "Project A", "root_path": str(project_root)})
            projects = [
                item
                for item in service.projects()
                if item["root_path"] == str(project_root.resolve())
            ]

            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(second["action"], "existing")
            self.assertIn("already exists", second["message"])
            self.assertEqual(len(projects), 1)

    def test_settings_redacts_mcp_auth_tokens_but_preserves_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            stored = service.repository.get_setting("mcp_servers") or {}
            servers = list(stored.get("servers") or [])
            servers.append({
                "id": "secure-remote",
                "label": "Secure Remote",
                "transport": "http",
                "url": "https://example.test/mcp",
                "enabled": True,
                "auth": {"status": "authorized", "access_token": "secret-token", "refresh_token": "secret-refresh", "expires_at": 123},
            })
            service.repository.set_setting("mcp_servers", {"servers": servers})

            public = service.settings()["mcp_servers"]["servers"]
            secure = next(item for item in public if item["id"] == "secure-remote")

            self.assertEqual(secure["auth"]["status"], "authorized")
            self.assertTrue(secure["auth"]["has_access_token"])
            self.assertTrue(secure["auth"]["has_refresh_token"])
            self.assertNotIn("access_token", secure["auth"])
            self.assertNotIn("refresh_token", secure["auth"])

    def test_legacy_empty_project_duplicates_are_removed_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp) / "app"
            project_root = Path(tmp) / "AnyToAny"
            app_root.mkdir()
            project_root.mkdir()

            first = ArchitectOSService(app_root)
            stale = first.repository.add_project("AnyToAny")
            real = first.create_project({"name": "AnyToAny", "root_path": str(project_root)})
            first.update_settings({"workspace": {"current_project_id": stale.id}})

            second = ArchitectOSService(app_root)
            projects = {project["id"]: project for project in second.projects()}
            self.assertNotIn(stale.id, projects)
            self.assertIn(real["id"], projects)
            self.assertEqual(second.settings()["workspace"]["current_project_id"], real["id"])

    def test_graph_actions_create_path_pin_merge_and_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            first = service.add_memory({"project_id": "architectos", "label": "Graph source", "type": "Decision", "scope": "project", "text": "Source mentions OpenAI provider."})
            second = service.add_memory({"project_id": "architectos", "label": "Graph target", "type": "Lesson", "scope": "project", "text": "Target memory."})
            task = service.create_task({"project_id": "architectos", "title": "Graph linked task", "linked_memory_ids": [first["id"]], "status": "doing"})
            edge = service.create_graph_edge({"source": first["id"], "target": second["id"], "type": "SUPPORTS"})
            self.assertEqual(edge["edge"]["type"], "SUPPORTS")
            path = service.explain_graph_path(first["id"], second["id"])
            self.assertTrue(path["found"])
            self.assertIn("SUPPORTS", path["edge_types"])
            pinned = service.pin_graph_node(first["id"])
            self.assertTrue(pinned["node"]["metadata"]["pinned"])
            pinned_graph = service.graph("architectos", pinned=True)
            self.assertEqual([node["id"] for node in pinned_graph["nodes"]], [first["id"]])
            task_graph = service.graph("architectos", task_id=task["id"])
            self.assertTrue(any(node["id"] == f"task:{task['id']}" for node in task_graph["nodes"]))
            provider_graph = service.graph("architectos", provider_id="openai")
            self.assertTrue(any(node["id"] == "provider:openai" for node in provider_graph["nodes"]))
            merged = service.merge_graph_nodes(second["id"], {"target_id": first["id"]})
            self.assertEqual(merged["source"]["status"], "merged")
            active_ids = {node["id"] for node in service.graph("architectos")["nodes"]}
            self.assertIn(first["id"], active_ids)
            self.assertNotIn(second["id"], active_ids)
            for index in range(8):
                service.add_memory({"project_id": "architectos", "label": f"Graph density {index}", "type": "Concept", "scope": "project", "text": f"Density node {index} for graph limit tests."})
            limited = service.graph("architectos", limit=4)
            self.assertLessEqual(len(limited["nodes"]), 4)
            self.assertGreater(limited["total_nodes"], len(limited["nodes"]))
            by_id = service.graph("architectos", search=first["id"], limit=4)
            self.assertTrue(any(node["id"] == first["id"] for node in by_id["nodes"]))
            self.assertEqual(by_id["filters"]["search"], first["id"].lower())
            by_label = service.graph("architectos", search="Graph source", limit=20)
            self.assertTrue(any(node["id"] == first["id"] for node in by_label["nodes"]))

    def test_graph_density_quota_spreads_across_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.memory_embeddings.enabled = lambda: False  # type: ignore[method-assign]
            # One sparse source + one dense source. With a shared budget the sparse
            # source must still appear — leftover quota then fills the dense source.
            granola = [
                service.add_memory({
                    "project_id": "architectos",
                    "label": f"Granola meeting {index}",
                    "type": "Meeting",
                    "scope": "project",
                    "text": f"Granola notes {index}",
                    "source": "granola",
                    "source_type": "granola",
                })
                for index in range(2)
            ]
            boards = [
                service.add_memory({
                    "project_id": "architectos",
                    "label": f"Boards item {index}",
                    "type": "Artifact",
                    "scope": "project",
                    "text": f"Work item {index}",
                    "source": "azure_boards",
                    "source_type": "azure_boards",
                })
                for index in range(20)
            ]
            graph = service.graph("architectos", limit=10)
            ids = {node["id"] for node in graph["nodes"]}
            granola_hits = sum(1 for item in granola if item["id"] in ids)
            boards_hits = sum(1 for item in boards if item["id"] in ids)
            self.assertEqual(granola_hits, 2)
            self.assertGreaterEqual(boards_hits, 1)
            self.assertLessEqual(len(graph["nodes"]), 10)

    def test_graph_auto_linker_connects_scanned_promoted_and_existing_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# Demo\n\nArchitectOS documents local memory graph linker.", encoding="utf-8")
            (root / "app.py").write_text("def run():\n    return 'local memory graph'\n", encoding="utf-8")
            service = ArchitectOSService(root)
            scanned = service.scan_project({"project_id": "architectos", "limit": 10})
            self.assertGreaterEqual(scanned["count"], 2)
            graph = service.graph("architectos")
            by_label = {node["label"]: node["id"] for node in graph["nodes"]}
            hubs = {
                str(node["metadata"].get("source_key")): node["id"]
                for node in graph["nodes"]
                if dict(node.get("metadata") or {}).get("hub")
            }
            readme_id = by_label["README.md"]
            app_id = by_label["app.py"]
            self.assertIn("docs", hubs)
            self.assertIn("code", hubs)
            self.assertTrue(any(edge["source"] == hubs["docs"] and edge["target"] == readme_id and edge["type"] == "DOCUMENTED_IN" for edge in graph["edges"]))
            self.assertTrue(any(edge["source"] == hubs["code"] and edge["target"] == app_id and edge["type"] == "IMPLEMENTS" for edge in graph["edges"]))
            project_id = next(node["id"] for node in graph["nodes"] if node["type"] == "Project")
            self.assertTrue(any(edge["source"] == project_id and edge["target"] == hubs["docs"] and edge["type"] == "HAS_MEMORY" for edge in graph["edges"]))

            service.repository.upsert_memory_candidate({
                "id": "candidate-autolink",
                "project_id": "architectos",
                "source_type": "adr",
                "source_ref": "docs/adr/0001.md",
                "label": "ADR auto link",
                "type": "Decision",
                "scope": "project",
                "text": "ArchitectOS documents local memory graph linker for promoted decision nodes.",
                "confidence": 0.8,
                "metadata": {"template": "adr"},
            })
            promoted = service.promote_memory_candidate("candidate-autolink")
            graph = service.graph("architectos")
            hubs = {
                str(node["metadata"].get("source_key")): node["id"]
                for node in graph["nodes"]
                if dict(node.get("metadata") or {}).get("hub")
            }
            promoted_id = promoted["memory"]["id"]
            self.assertIn("adr", hubs)
            self.assertTrue(any(edge["source"] == hubs["adr"] and edge["target"] == promoted_id and edge["type"] == "DOCUMENTED_IN" for edge in graph["edges"]))
            rebuilt = service.rebuild_graph_links({"project_id": "architectos"})
            self.assertGreaterEqual(rebuilt["linked_nodes"], 3)
            self.assertTrue(any(edge["type"] == "RELATED_TO" for edge in rebuilt["edges"]))

    def test_source_hubs_are_scoped_and_allow_cross_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            project_doc = service.add_memory({
                "project_id": "architectos",
                "scope": "project",
                "type": "Doc",
                "label": "Project requirement note",
                "text": "Shared sprint planning note for architecture.",
                "source": "docs",
                "source_type": "docs",
            })
            shared_doc = service.add_memory({
                "project_id": "architectos",
                "scope": "shared",
                "type": "Doc",
                "label": "Shared architecture note",
                "text": "Shared sprint planning note for architecture.",
                "source": "docs",
                "source_type": "docs",
            })
            wiki = service.add_memory({
                "project_id": "architectos",
                "scope": "project",
                "type": "Doc",
                "label": "Wiki overview",
                "text": "Azure wiki overview for sprint planning.",
                "source": "azure-wiki",
                "source_type": "azure-wiki",
            })
            graph = service.graph("architectos")
            hubs = [
                node for node in graph["nodes"]
                if dict(node.get("metadata") or {}).get("hub")
            ]
            docs_hubs = [node for node in hubs if node["metadata"].get("source_key") == "docs"]
            scopes = {node["scope"] for node in docs_hubs}
            self.assertIn("project", scopes)
            self.assertIn("shared", scopes)
            self.assertTrue(any(node["metadata"].get("source_key") == "azure-wiki" for node in hubs))
            # Cross-group leaf link remains possible.
            edge = service.create_graph_edge({
                "source": project_doc["id"],
                "target": wiki["id"],
                "type": "RELATED_TO",
                "scope": "project",
            })
            self.assertEqual(edge["edge"]["type"], "RELATED_TO")
            self.assertNotEqual(project_doc["id"], shared_doc["id"])

    def test_empty_source_hubs_are_not_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.add_memory({
                "project_id": "architectos",
                "scope": "project",
                "type": "Doc",
                "label": "Only docs leaf",
                "text": "Documentation leaf that creates a docs hub.",
                "source": "docs",
                "source_type": "docs",
            })
            graph = service.graph("architectos")
            hubs = [
                node for node in graph["nodes"]
                if dict(node.get("metadata") or {}).get("hub")
            ]
            self.assertEqual({node["metadata"].get("source_key") for node in hubs}, {"docs"})
            # Orphan empty hub should disappear on rebuild.
            orphan = service.repository.add_node(
                "Concept",
                "Source: Azure Wiki",
                "project",
                "Empty wiki hub",
                "architectos",
                confidence=0.95,
                metadata={
                    "hub": True,
                    "source_key": "azure-wiki",
                    "source": "source_hub",
                    "source_type": "azure-wiki",
                    "structural": True,
                },
            )
            rebuilt = service.rebuild_graph_links({"project_id": "architectos"})
            self.assertEqual(rebuilt["pruned_hubs"]["hubs"], 1)
            graph = service.graph("architectos")
            hub_keys = {
                node["metadata"].get("source_key")
                for node in graph["nodes"]
                if dict(node.get("metadata") or {}).get("hub") and node["status"] == "active"
            }
            self.assertEqual(hub_keys, {"docs"})
            orphan_after = next(node for node in service.repository.list_nodes() if node.id == orphan.id)
            self.assertEqual(orphan_after.status, "deleted")

    def test_provider_and_settings_updates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            self.assertEqual(service.settings()["ui"]["language"], "en")
            updated_settings = service.update_settings({"ui": {"theme": "dark", "density": "compact", "memory_enabled": True, "language": "he"}})
            self.assertEqual(updated_settings["ui"]["language"], "he")
            provider = service.update_provider("openai", {"enabled": True, "model": "gpt-test"})
            self.assertTrue(provider["enabled"])
            self.assertEqual(provider["status"], "configured")
            settings = service.update_settings({"ui": {"theme": "dark", "density": "compact", "memory_enabled": True}})
            self.assertEqual(settings["ui"]["theme"], "dark")

    def test_ai_router_uses_local_fallback_and_records_chat_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            result = service.run_ai({"project_id": "architectos", "message": "memory scopes", "provider_id": "auto"})
            self.assertEqual(result["provider"]["id"], "local-memory")
            self.assertIn("Local context", result["text"])
            chat = service.post_chat_message({"project_id": "architectos", "message": "memory scopes", "provider_id": "local-memory"})
            self.assertEqual(chat["chat"]["messages"][-1]["provider"]["id"], "local-memory")

    def test_streaming_chat_persists_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            events = list(service.stream_chat_message({"project_id": "architectos", "message": "memory scopes", "provider_id": "local-memory"}))
            deltas = [event["text"] for event in events if event["type"] == "delta"]
            done = events[-1]
            self.assertEqual(done["type"], "done")
            self.assertTrue("".join(deltas))
            self.assertIn("Local context", done["response"])
            self.assertEqual(done["chat"]["messages"][-1]["provider"]["id"], "local-memory")

    def test_provider_check_and_command_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            local = service.test_provider("local-memory")
            self.assertTrue(local["provider"]["ready"])
            self.assertEqual(local["provider"]["status"], "ok")
            service.update_provider("codex-cli", {"command": "architectos-missing-cli-for-test", "enabled": True})
            checked = service.test_provider("codex-cli")
            self.assertFalse(checked["provider"]["ready"])
            self.assertEqual(checked["provider"]["status"], "missing_cli")
            self.assertIn("Install", checked["actions"][0])
            provider = next(item for item in service.providers()["providers"] if item["id"] == "codex-cli")
            self.assertEqual(provider["command"], "architectos-missing-cli-for-test")
            self.assertEqual(provider["status"], "missing_cli")
            self.assertEqual(provider["last_check"]["status"], "missing_cli")
            all_checks = service.test_providers()
            self.assertGreaterEqual(len(all_checks["checks"]), 1)

    def test_connect_env_providers_uses_env_names_without_persisting_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.local").write_text(
                "OPENAI_API_KEY=test-openai-key\nANTHROPIC_API_KEY=test-anthropic-key\n",
                encoding="utf-8",
            )
            old_openai = os.environ.pop("OPENAI_API_KEY", None)
            old_anthropic = os.environ.pop("ANTHROPIC_API_KEY", None)
            old_azure = os.environ.pop("AZURE_OPENAI_API_KEY", None)
            old_azure_endpoint = os.environ.pop("AZURE_OPENAI_ENDPOINT", None)
            try:
                service = ArchitectOSService(root)
                # Isolate from the developer machine's real Gemini CLI session.
                with mock.patch("backend.architectos.adapters._gemini_session_auth_state", return_value={"ready": False}):
                    result = service.connect_env_providers()
                providers = {item["id"]: item for item in service.providers()["providers"]}

                self.assertEqual({item["id"] for item in result["connected"]}, {"openai", "anthropic"})
                self.assertTrue(providers["openai"]["enabled"])
                self.assertTrue(providers["anthropic"]["enabled"])
                self.assertEqual(providers["openai"]["api_key_env"], "OPENAI_API_KEY")
                self.assertEqual(providers["anthropic"]["api_key_env"], "ANTHROPIC_API_KEY")
                self.assertNotIn("test-openai-key", json.dumps(providers))
                self.assertNotIn("test-anthropic-key", json.dumps(providers))
            finally:
                if old_openai is not None:
                    os.environ["OPENAI_API_KEY"] = old_openai
                else:
                    os.environ.pop("OPENAI_API_KEY", None)
                if old_anthropic is not None:
                    os.environ["ANTHROPIC_API_KEY"] = old_anthropic
                else:
                    os.environ.pop("ANTHROPIC_API_KEY", None)
                if old_azure is not None:
                    os.environ["AZURE_OPENAI_API_KEY"] = old_azure
                else:
                    os.environ.pop("AZURE_OPENAI_API_KEY", None)
                if old_azure_endpoint is not None:
                    os.environ["AZURE_OPENAI_ENDPOINT"] = old_azure_endpoint
                else:
                    os.environ.pop("AZURE_OPENAI_ENDPOINT", None)

    def test_connect_env_providers_configures_azure_openai(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.local").write_text(
                "AZURE_OPENAI_API_KEY=test-azure-key\nAZURE_OPENAI_ENDPOINT=https://example.azure.com/openai/v1\nAZURE_OPENAI_DEPLOYMENT=test-deployment\n",
                encoding="utf-8",
            )
            old_openai = os.environ.pop("OPENAI_API_KEY", None)
            old_anthropic = os.environ.pop("ANTHROPIC_API_KEY", None)
            old_azure = os.environ.pop("AZURE_OPENAI_API_KEY", None)
            old_azure_endpoint = os.environ.pop("AZURE_OPENAI_ENDPOINT", None)
            try:
                service = ArchitectOSService(root)
                # Isolate from the developer machine's real Gemini CLI session.
                with mock.patch("backend.architectos.adapters._gemini_session_auth_state", return_value={"ready": False}):
                    result = service.connect_env_providers()
                providers = {item["id"]: item for item in service.providers()["providers"]}

                self.assertEqual({item["id"] for item in result["connected"]}, {"azure-openai"})
                self.assertTrue(providers["azure-openai"]["enabled"])
                self.assertEqual(providers["azure-openai"]["api_key_env"], "AZURE_OPENAI_API_KEY")
                self.assertEqual(providers["azure-openai"]["base_url"], "https://example.azure.com/openai/v1")
                self.assertEqual(providers["azure-openai"]["model"], "test-deployment")
                self.assertNotIn("test-azure-key", json.dumps(providers))
            finally:
                if old_openai is not None:
                    os.environ["OPENAI_API_KEY"] = old_openai
                else:
                    os.environ.pop("OPENAI_API_KEY", None)
                if old_anthropic is not None:
                    os.environ["ANTHROPIC_API_KEY"] = old_anthropic
                else:
                    os.environ.pop("ANTHROPIC_API_KEY", None)
                if old_azure is not None:
                    os.environ["AZURE_OPENAI_API_KEY"] = old_azure
                else:
                    os.environ.pop("AZURE_OPENAI_API_KEY", None)
                if old_azure_endpoint is not None:
                    os.environ["AZURE_OPENAI_ENDPOINT"] = old_azure_endpoint
                else:
                    os.environ.pop("AZURE_OPENAI_ENDPOINT", None)

    def test_ollama_model_discovery_updates_provider(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = json.dumps({"models": [{"name": "llama3.2:latest", "model": "llama3.2:latest", "size": 123, "digest": "sha256:test"}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                service = ArchitectOSService(Path(tmp))
                service.update_provider("ollama", {"base_url": f"http://127.0.0.1:{server.server_port}"})
                result = service.provider_models("ollama")
                self.assertTrue(result["provider"]["ready"])
                self.assertEqual(result["models"][0]["name"], "llama3.2:latest")
                provider = next(item for item in service.providers()["providers"] if item["id"] == "ollama")
                self.assertEqual(provider["available_models"][0]["name"], "llama3.2:latest")
                self.assertEqual(provider["model"], "llama3.2:latest")
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


    def test_anthropic_adapter_runs_and_streams_against_fake_server(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if payload.get("stream"):
                    body = (
                        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hello "}}\n\n'
                        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"claude"}}\n\n'
                        "data: [DONE]\n\n"
                    ).encode("utf-8")
                else:
                    body = json.dumps({"content": [{"type": "text", "text": "hello claude"}]}).encode("utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        os.environ["ARCHITECTOS_TEST_ANTHROPIC_KEY"] = "test-key"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                service = ArchitectOSService(Path(tmp))
                service.update_provider("anthropic", {"base_url": f"http://127.0.0.1:{server.server_port}", "api_key_env": "ARCHITECTOS_TEST_ANTHROPIC_KEY", "model": "claude-test", "enabled": True})
                result = service.run_ai({"project_id": "architectos", "message": "memory scopes", "provider_id": "anthropic"})
                self.assertEqual(result["provider"]["id"], "anthropic")
                self.assertEqual(result["provider"]["status"], "ok")
                self.assertEqual(result["text"], "hello claude")
                events = list(service.stream_ai({"project_id": "architectos", "message": "memory scopes", "provider_id": "anthropic"}))
                self.assertEqual("".join(event.get("text", "") for event in events if event["type"] == "delta"), "hello claude")
        finally:
            os.environ.pop("ARCHITECTOS_TEST_ANTHROPIC_KEY", None)
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_azure_openai_adapter_runs_against_fake_server(self) -> None:
        seen: list[dict[str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                seen.append({
                    "path": self.path,
                    "api_key": self.headers.get("api-key", ""),
                    "model": str(payload.get("model") or ""),
                })
                body = json.dumps({"output": [{"content": [{"type": "output_text", "text": "hello azure"}]}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        os.environ["ARCHITECTOS_TEST_AZURE_KEY"] = "test-key"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                service = ArchitectOSService(Path(tmp))
                service.update_provider("azure-openai", {
                    "base_url": f"http://127.0.0.1:{server.server_port}/openai/v1",
                    "api_key_env": "ARCHITECTOS_TEST_AZURE_KEY",
                    "model": "azure-deployment-test",
                    "enabled": True,
                })
                result = service.run_ai({"project_id": "architectos", "message": "memory scopes", "provider_id": "azure-openai"})
                self.assertEqual(result["provider"]["id"], "azure-openai")
                self.assertEqual(result["provider"]["status"], "ok")
                self.assertEqual(result["text"], "hello azure")
                self.assertEqual(seen, [{"path": "/openai/v1/responses", "api_key": "test-key", "model": "azure-deployment-test"}])
        finally:
            os.environ.pop("ARCHITECTOS_TEST_AZURE_KEY", None)
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_azure_openai_adapter_explains_missing_deployment(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.dumps({"error": {"code": "DeploymentNotFound", "message": "missing"}}).encode("utf-8")
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        os.environ["ARCHITECTOS_TEST_AZURE_KEY"] = "test-key"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                service = ArchitectOSService(Path(tmp))
                service.update_provider("azure-openai", {
                    "base_url": f"http://127.0.0.1:{server.server_port}/openai/v1",
                    "api_key_env": "ARCHITECTOS_TEST_AZURE_KEY",
                    "model": "wrong-deployment",
                    "enabled": True,
                })
                result = service.run_ai({"project_id": "architectos", "message": "memory scopes", "provider_id": "azure-openai"})
                self.assertEqual(result["provider"]["status"], "error")
                self.assertIn("deployment was not found", result["text"])
                self.assertIn("AZURE_OPENAI_DEPLOYMENT", result["text"])
        finally:
            os.environ.pop("ARCHITECTOS_TEST_AZURE_KEY", None)
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_openrouter_adapter_discovers_models_and_runs_against_fake_server(self) -> None:
        seen_models = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = json.dumps({"data": [{"id": "openai/gpt-test", "name": "GPT Test", "context_length": 8192}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                seen_models.append(payload["model"])
                body = json.dumps({"choices": [{"message": {"content": "hello router"}}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        os.environ["ARCHITECTOS_TEST_OPENROUTER_KEY"] = "test-key"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                service = ArchitectOSService(Path(tmp))
                service.update_provider("openrouter", {"base_url": f"http://127.0.0.1:{server.server_port}", "api_key_env": "ARCHITECTOS_TEST_OPENROUTER_KEY", "enabled": True})
                models = service.provider_models("openrouter")
                self.assertTrue(models["provider"]["ready"])
                self.assertEqual(models["models"][0]["name"], "openai/gpt-test")
                provider = next(item for item in service.providers()["providers"] if item["id"] == "openrouter")
                self.assertEqual(provider["model"], "openai/gpt-test")
                result = service.run_ai({"project_id": "architectos", "message": "memory scopes", "provider_id": "openrouter"})
                self.assertEqual(result["provider"]["id"], "openrouter")
                self.assertEqual(result["provider"]["status"], "ok")
                self.assertEqual(result["text"], "hello router")
                self.assertEqual(seen_models, ["openai/gpt-test"])
        finally:
            os.environ.pop("ARCHITECTOS_TEST_OPENROUTER_KEY", None)
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_cli_requires_approval_and_writes_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.update_provider("codex-cli", {"enabled": True, "command": "architectos-missing-cli-for-test", "approval_required": True})
            result = service.run_ai({"project_id": "architectos", "message": "memory scopes", "provider_id": "codex-cli"})
            self.assertEqual(result["provider"]["status"], "approval_required")
            runs = service.provider_runs("architectos")["runs"]
            self.assertEqual(runs[0]["status"], "approval_required")
            approved = service.run_ai({"project_id": "architectos", "message": "memory scopes", "provider_id": "codex-cli", "allow_cli": True})
            self.assertEqual(approved["provider"]["status"], "error")
            self.assertTrue(approved["run_id"])

    def test_cancel_run_marks_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.repository.upsert_provider_run({"id": "run-test", "project_id": "architectos", "provider_id": "codex-cli", "status": "running", "started_at": "now"})
            cancelled = service.cancel_run("run-test")
            self.assertTrue(cancelled["cancel_requested"])
            self.assertEqual(service.provider_runs("architectos")["runs"][0]["status"], "cancel_requested")

    def test_sanitize_text_handles_bearer_tokens(self) -> None:
        text, redacted = sanitize_text("authorization: Bearer abcdefghijklmnopqrstuvwxyz")
        self.assertTrue(redacted)
        self.assertIn("[REDACTED]", text)



    def test_security_preview_and_storage_redact_sensitive_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            preview = service.security_preview({"text": "token=abc123456789xyz and authorization: Bearer abcdefghijklmnopqrstuvwxyz"})
            self.assertTrue(preview["redacted"])
            self.assertIn("[REDACTED]", preview["text"])
            task = service.create_task({"project_id": "architectos", "title": "Deploy token=abc123456789xyz", "detail": "password=supersecret123456"})
            self.assertIn("[REDACTED]", task["title"])
            self.assertIn("[REDACTED]", task["detail"])
            chat = service.post_chat_message({"project_id": "architectos", "message": "please use token=abc123456789xyz", "provider_id": "local-memory", "remember": True})
            serialized = json.dumps(chat)
            self.assertNotIn("abc123456789xyz", serialized)
            self.assertIn("[REDACTED]", serialized)

    def test_provider_result_audit_and_bundle_are_redacted(self) -> None:
        class FakeRouter:
            def route(self, _providers, _request, provider_id=None):
                return {
                    "provider_id": provider_id or "fake",
                    "requested_provider_id": provider_id or "fake",
                    "selected_provider": {"id": "fake", "label": "Fake", "provider_type": "api"},
                    "status": "ok",
                    "text": "result secret=abcdef1234567890",
                    "raw": {"stderr": "authorization: Bearer abcdefghijklmnopqrstuvwxyz", "body": "token=abc123456789xyz"},
                }

        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            service.provider_router = FakeRouter()
            result = service.run_ai({"project_id": "architectos", "message": "run with password=supersecret123456", "provider_id": "fake"})
            serialized = json.dumps(result)
            self.assertNotIn("supersecret123456", serialized)
            self.assertNotIn("abcdef1234567890", serialized)
            self.assertNotIn("abcdefghijklmnopqrstuvwxyz", serialized)
            self.assertTrue(result["security"]["redacted"])
            runs = service.provider_runs("architectos")["runs"]
            self.assertNotIn("supersecret123456", json.dumps(runs))
            self.assertNotIn("abcdefghijklmnopqrstuvwxyz", json.dumps(runs))
            bundle = service.export_bundle("architectos")
            self.assertNotIn("supersecret123456", json.dumps(bundle))
            self.assertNotIn("abcdefghijklmnopqrstuvwxyz", json.dumps(bundle))


    def test_launcher_runtime_state_and_port_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = write_runtime_state(root, {"host": "127.0.0.1", "port": 8765, "url": "http://127.0.0.1:8765"})
            self.assertEqual(state["app"], "ArchitectOS")
            self.assertEqual(read_runtime_state(root)["port"], 8765)
            clear_runtime_state(root)
            self.assertIsNone(read_runtime_state(root))

        self.assertEqual(
            build_app_window_command("msedge", "http://127.0.0.1:8765"),
            ["msedge", "--app=http://127.0.0.1:8765"],
        )

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        try:
            busy_port = int(sock.getsockname()[1])
            selected = find_available_port("127.0.0.1", busy_port, attempts=5)
            self.assertNotEqual(selected, busy_port)
            self.assertGreater(selected, busy_port)
        finally:
            sock.close()


if __name__ == "__main__":
    unittest.main()
