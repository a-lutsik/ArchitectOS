"""Memory revisions when code or fact claims change."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.architectos.code_revision import (
    build_content_metadata,
    code_revision_delta,
    content_hash,
)
from backend.architectos.candidate_identity import candidate_origin_key
from backend.architectos.chat_memory import infer_fact_subject
from backend.architectos.service import ArchitectOSService


OLD_AUTH = '''\
def authenticate(token):
    if not token:
        return False
    return token.startswith("Bearer ")
'''

NEW_AUTH_LOGIC = '''\
def authenticate(token):
    if not token:
        return False
    # bugfix: also reject expired JWTs
    return token.startswith("Bearer ") and len(token) > 20
'''

NEW_AUTH_SIGNATURE = '''\
def authenticate(token, *, allow_expired=False):
    if not token:
        return False
    return token.startswith("Bearer ")
'''

NOISE_COMMENT = '''\
def authenticate(token):
    # just a comment
    if not token:
        return False
    return token.startswith("Bearer ")
'''

IMPORT_SHUFFLE = '''\
import os
import sys

def authenticate(token):
    if not token:
        return False
    return token.startswith("Bearer ")
'''

IMPORT_SHUFFLE_B = '''\
import sys
import os

def authenticate(token):
    if not token:
        return False
    return token.startswith("Bearer ")
'''


class CodeRevisionDeltaTests(unittest.TestCase):
    def test_identical_hash_is_not_significant(self) -> None:
        meta = build_content_metadata(OLD_AUTH, ".py")
        delta = code_revision_delta(meta, OLD_AUTH, extension=".py")
        self.assertFalse(delta.significant)
        self.assertEqual(delta.change_kind, "identical")

    def test_comment_only_is_noise(self) -> None:
        meta = build_content_metadata(OLD_AUTH, ".py")
        delta = code_revision_delta(meta, NOISE_COMMENT, extension=".py")
        self.assertFalse(delta.significant)
        # Comments are stripped from the content hash, so this may report
        # identical rather than noise — both mean "do not revise".
        self.assertIn(delta.change_kind, {"noise", "identical"})

    def test_import_shuffle_is_noise(self) -> None:
        meta = build_content_metadata(IMPORT_SHUFFLE, ".py")
        delta = code_revision_delta(meta, IMPORT_SHUFFLE_B, extension=".py")
        self.assertFalse(delta.significant)

    def test_signature_change_is_significant(self) -> None:
        meta = build_content_metadata(OLD_AUTH, ".py")
        delta = code_revision_delta(meta, NEW_AUTH_SIGNATURE, extension=".py")
        self.assertTrue(delta.significant)
        self.assertIn(delta.change_kind, {"signatures", "both"})
        self.assertTrue(delta.changed_signatures or delta.added or delta.removed)

    def test_body_logic_change_is_significant(self) -> None:
        meta = build_content_metadata(OLD_AUTH, ".py")
        delta = code_revision_delta(meta, NEW_AUTH_LOGIC, extension=".py")
        self.assertTrue(delta.significant)
        self.assertEqual(delta.change_kind, "logic")
        self.assertIn("authenticate", delta.changed_bodies)

    def test_content_hash_stable_across_whitespace(self) -> None:
        a = content_hash("def foo():\n  return 1\n")
        b = content_hash("def foo():\n\n  return 1\n")
        self.assertEqual(a, b)


class FactSubjectTests(unittest.TestCase):
    def test_infer_backtick_path(self) -> None:
        self.assertEqual(
            infer_fact_subject("`auth/service.py` validates JWT expiry"),
            "auth/service.py",
        )

    def test_fact_subject_origin_key(self) -> None:
        key = candidate_origin_key({
            "source_type": "chat",
            "metadata": {
                "template": "chat_session_atom",
                "fact_key": "auth uses cookies now",
                "fact_subject": "auth.authenticate",
            },
        })
        self.assertEqual(key, "fact_subject:auth.authenticate")


class CodeRevisionIngestTests(unittest.TestCase):
    def _promote_code(self, service: ArchitectOSService, text: str, path: str = "auth.py") -> dict:
        meta = build_content_metadata(text, ".py")
        candidate = {
            "id": f"cand-{path}-{meta['content_hash']}",
            "project_id": "architectos",
            "source_type": "code",
            "source_ref": path,
            "label": f"Code: {path}",
            "type": "Artifact",
            "scope": "project",
            "text": f"Code file {path} structure:\n\ndef authenticate",
            "confidence": 0.7,
            "metadata": {
                "path": path,
                "extension": ".py",
                "template": "generic",
                "source_text": text,
                **meta,
            },
        }
        prepared = service.ingestion_engine.prepare_candidates("architectos", [candidate], 1)
        self.assertEqual(len(prepared), 1, f"expected prepared candidate, got {prepared!r} skipped={service.ingestion_engine.skipped_reviewed}")
        saved = service.repository.upsert_memory_candidate(prepared[0])
        result = service.promote_memory_candidate(str(saved["id"]))
        return result["memory"]

    def test_same_hash_skips_requeue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            first = self._promote_code(service, OLD_AUTH)
            self.assertTrue(first.get("id"))
            meta = build_content_metadata(OLD_AUTH, ".py")
            candidate = {
                "id": "cand-rescan-same",
                "project_id": "architectos",
                "source_type": "code",
                "source_ref": "auth.py",
                "label": "Code: auth.py",
                "type": "Artifact",
                "scope": "project",
                "text": "Code file auth.py structure:\n\ndef authenticate",
                "confidence": 0.7,
                "metadata": {
                    "path": "auth.py",
                    "extension": ".py",
                    "template": "generic",
                    "source_text": OLD_AUTH,
                    **meta,
                },
            }
            prepared = service.ingestion_engine.prepare_candidates("architectos", [candidate], 1)
            self.assertEqual(prepared, [])
            self.assertEqual(service.ingestion_engine.skipped_reviewed, 1)

    def test_noise_skips_requeue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            self._promote_code(service, OLD_AUTH)
            meta = build_content_metadata(NOISE_COMMENT, ".py")
            candidate = {
                "id": "cand-noise",
                "project_id": "architectos",
                "source_type": "code",
                "source_ref": "auth.py",
                "label": "Code: auth.py",
                "type": "Artifact",
                "scope": "project",
                "text": "Code file auth.py structure:\n\ndef authenticate",
                "confidence": 0.7,
                "metadata": {
                    "path": "auth.py",
                    "extension": ".py",
                    "template": "generic",
                    "source_text": NOISE_COMMENT,
                    **meta,
                },
            }
            prepared = service.ingestion_engine.prepare_candidates("architectos", [candidate], 1)
            self.assertEqual(prepared, [])
            self.assertEqual(service.ingestion_engine.skipped_reviewed, 1)

    def test_logic_change_is_revision_not_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            old = self._promote_code(service, OLD_AUTH)
            meta = build_content_metadata(NEW_AUTH_LOGIC, ".py")
            candidate = {
                "id": "cand-logic",
                "project_id": "architectos",
                "source_type": "code",
                "source_ref": "auth.py",
                "label": "Code: auth.py",
                "type": "Artifact",
                "scope": "project",
                "text": "Code file auth.py structure:\n\ndef authenticate",
                "confidence": 0.7,
                "metadata": {
                    "path": "auth.py",
                    "extension": ".py",
                    "template": "generic",
                    "source_text": NEW_AUTH_LOGIC,
                    **meta,
                },
            }
            prepared = service.ingestion_engine.prepare_candidates("architectos", [candidate], 1)
            self.assertEqual(len(prepared), 1)
            prep_meta = prepared[0]["metadata"]
            self.assertTrue(prep_meta.get("revision"))
            self.assertFalse(prep_meta.get("duplicate"))
            self.assertEqual(prep_meta.get("revision_of"), old["id"])
            self.assertEqual(prep_meta.get("change_kind"), "logic")

            saved = service.repository.upsert_memory_candidate(prepared[0])
            promoted = service.promote_memory_candidate(str(saved["id"]))["memory"]
            self.assertNotEqual(promoted["id"], old["id"])
            old_node = service.repository.get_node(old["id"])
            self.assertTrue(old_node.metadata.get("invalid_at"))
            self.assertEqual(old_node.metadata.get("superseded_by"), promoted["id"])
            edges = [
                e for e in service.repository.list_edges()
                if e.type == "SUPERSEDES" and e.source == promoted["id"] and e.target == old["id"]
            ]
            self.assertTrue(edges)

            live = service.search_memory("authenticate", project_id="architectos", limit=20)
            live_ids = {hit["node"]["id"] for hit in live["hits"]}
            self.assertIn(promoted["id"], live_ids)
            self.assertNotIn(old["id"], live_ids)

    def test_signature_change_revision_and_supersede(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            old = self._promote_code(service, OLD_AUTH)
            meta = build_content_metadata(NEW_AUTH_SIGNATURE, ".py")
            candidate = {
                "id": "cand-sig",
                "project_id": "architectos",
                "source_type": "code",
                "source_ref": "auth.py",
                "label": "Code: auth.py",
                "type": "Artifact",
                "scope": "project",
                "text": "Code file auth.py",
                "confidence": 0.7,
                "metadata": {
                    "path": "auth.py",
                    "extension": ".py",
                    "template": "generic",
                    "source_text": NEW_AUTH_SIGNATURE,
                    **meta,
                },
            }
            prepared = service.ingestion_engine.prepare_candidates("architectos", [candidate], 1)
            self.assertEqual(len(prepared), 1)
            self.assertTrue(prepared[0]["metadata"].get("revision"))
            self.assertIn(prepared[0]["metadata"].get("change_kind"), {"signatures", "both"})
            saved = service.repository.upsert_memory_candidate(prepared[0])
            new = service.promote_memory_candidate(str(saved["id"]))["memory"]
            self.assertNotEqual(new["id"], old["id"])
            self.assertTrue(service.repository.get_node(old["id"]).metadata.get("invalid_at"))

    def test_fact_same_subject_different_claim_is_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            first = {
                "id": "fact-1",
                "project_id": "architectos",
                "source_type": "chat",
                "source_ref": "c1",
                "label": "Chat fact: auth validates JWT",
                "type": "Lesson",
                "scope": "project",
                "text": "Durable fact extracted from Ask (not a full transcript).\n\n`auth.authenticate` validates JWT expiry.",
                "confidence": 0.8,
                "metadata": {
                    "template": "chat_session_atom",
                    "fact_key": "`auth.authenticate` validates JWT expiry.",
                    "fact_subject": "auth.authenticate",
                    "chat_id": "c1",
                },
            }
            prepared = service.ingestion_engine.prepare_candidates("architectos", [first], 1)
            self.assertEqual(len(prepared), 1)
            saved = service.repository.upsert_memory_candidate(prepared[0])
            old = service.promote_memory_candidate(str(saved["id"]))["memory"]

            second = {
                "id": "fact-2",
                "project_id": "architectos",
                "source_type": "chat",
                "source_ref": "c2",
                "label": "Chat fact: auth uses cookies",
                "type": "Lesson",
                "scope": "project",
                "text": "Durable fact extracted from Ask (not a full transcript).\n\n`auth.authenticate` uses session cookies instead of JWT.",
                "confidence": 0.8,
                "metadata": {
                    "template": "chat_session_atom",
                    "fact_key": "`auth.authenticate` uses session cookies instead of JWT.",
                    "fact_subject": "auth.authenticate",
                    "chat_id": "c2",
                },
            }
            prepared2 = service.ingestion_engine.prepare_candidates("architectos", [second], 1)
            self.assertEqual(len(prepared2), 1)
            self.assertTrue(prepared2[0]["metadata"].get("revision"))
            self.assertFalse(prepared2[0]["metadata"].get("duplicate"))
            self.assertEqual(prepared2[0]["metadata"].get("revision_of"), old["id"])

    def test_fact_paraphrase_other_subject_still_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = ArchitectOSService(Path(tmp))
            first = {
                "id": "fact-a",
                "project_id": "architectos",
                "source_type": "chat",
                "source_ref": "c1",
                "label": "Chat fact: never commit secrets",
                "type": "Lesson",
                "scope": "project",
                "text": "Durable fact extracted from Ask (not a full transcript).\n\nNever commit API keys or secrets to the git repository.",
                "confidence": 0.8,
                "metadata": {
                    "template": "chat_session_atom",
                    "fact_key": "Never commit API keys or secrets to the git repository.",
                    "chat_id": "c1",
                },
            }
            prepared = service.ingestion_engine.prepare_candidates("architectos", [first], 1)
            saved = service.repository.upsert_memory_candidate(prepared[0])
            service.promote_memory_candidate(str(saved["id"]))

            paraphrase = {
                "id": "fact-b",
                "project_id": "architectos",
                "source_type": "chat",
                "source_ref": "c2",
                "label": "Chat fact: never commit secrets again",
                "type": "Lesson",
                "scope": "project",
                "text": "Durable fact extracted from Ask (not a full transcript).\n\nNever commit API keys or secrets into the git repository.",
                "confidence": 0.8,
                "metadata": {
                    "template": "chat_session_atom",
                    "fact_key": "Never commit API keys or secrets into the git repository.",
                    "chat_id": "c2",
                },
            }
            prepared2 = service.ingestion_engine.prepare_candidates("architectos", [paraphrase], 1)
            # Near-duplicate fact atoms without shared subject are skipped.
            self.assertEqual(prepared2, [])
            self.assertGreaterEqual(service.ingestion_engine.skipped_reviewed, 1)


if __name__ == "__main__":
    unittest.main()
