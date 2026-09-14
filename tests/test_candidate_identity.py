from __future__ import annotations

import unittest
from types import SimpleNamespace

from backend.architectos.candidate_identity import candidate_origin_key, origin_key_from_memory_node
from backend.architectos.memory_ingestion import candidate_fingerprint, fingerprint_from_tokens


class CandidateOriginKeyTests(unittest.TestCase):
    def test_granola_uses_meeting_id_not_text(self) -> None:
        left = candidate_origin_key({
            "source_type": "granola",
            "source_ref": "https://granola.test/m/m1",
            "metadata": {"meeting_id": "m1"},
        })
        right = candidate_origin_key({
            "source_type": "granola",
            "source_ref": "https://granola.test/m/m1",
            "metadata": {"meeting_id": "m1", "summary": "rewritten"},
        })
        self.assertEqual(left, "granola:m1")
        self.assertEqual(left, right)

    def test_azure_boards_and_git_cluster_keys(self) -> None:
        self.assertEqual(
            candidate_origin_key({
                "source_type": "azure-boards",
                "source_ref": "99",
                "metadata": {"work_item_id": "99"},
            }),
            "azure-boards:99",
        )
        self.assertEqual(
            candidate_origin_key({
                "source_type": "git_cluster",
                "source_ref": "/repo#feature",
                "metadata": {"root": "/repo", "cluster": "feature"},
            }),
            "git_cluster:/repo#feature",
        )

    def test_chat_candidates_are_not_sticky(self) -> None:
        self.assertEqual(
            candidate_origin_key({"source_type": "chat", "source_ref": "c1", "metadata": {}}),
            "",
        )
        self.assertEqual(
            candidate_origin_key({
                "source_type": "chat",
                "source_ref": "c1",
                "metadata": {"template": "chat_session_summary", "chat_id": "c1"},
            }),
            "",
        )

    def test_chat_dumps_are_sticky_by_chat_id(self) -> None:
        self.assertEqual(
            candidate_origin_key({
                "source_type": "chat",
                "source_ref": "c1",
                "metadata": {"template": "chat", "chat_id": "c1"},
            }),
            "chat:c1",
        )

    def test_fact_atoms_share_origin_across_ask_and_mcp(self) -> None:
        fact = "Never commit API keys or secrets to the git repository."
        ask = candidate_origin_key({
            "source_type": "chat",
            "source_ref": "ask-1",
            "metadata": {"template": "chat_session_atom", "fact_key": fact},
        })
        mcp = candidate_origin_key({
            "source_type": "mcp",
            "source_ref": "mcp-turn-1",
            "metadata": {"template": "mcp_turn_atom", "fact_key": fact},
        })
        self.assertEqual(ask, mcp)
        self.assertTrue(ask.startswith("fact:"))

    def test_memory_node_keeps_meeting_identity(self) -> None:
        node = SimpleNamespace(
            metadata={"source_type": "granola", "meeting_id": "m1", "source_ref": "m1"},
        )
        self.assertEqual(origin_key_from_memory_node(node), "granola:m1")


class CandidateFingerprintTests(unittest.TestCase):
    LABEL = "Billing pipeline rollout"
    BODY = "The billing pipeline rollout covers metering and invoicing for Q3."

    def test_fingerprint_token_order_is_sorted_not_set_order(self) -> None:
        # Insertion order must not leak into the value: set iteration order
        # depends on PYTHONHASHSEED, so the same text would otherwise
        # fingerprint differently in every process.
        self.assertEqual(fingerprint_from_tokens({"gamma", "alpha", "beta"}), "alpha|beta|gamma")
        self.assertEqual(fingerprint_from_tokens({"beta", "gamma", "alpha"}), "alpha|beta|gamma")

    def test_fingerprint_keeps_at_most_eighteen_tokens(self) -> None:
        tokens = {f"token{index:03d}" for index in range(40)}
        self.assertEqual(len(fingerprint_from_tokens(tokens).split("|")), 18)

    def test_identical_text_fingerprints_identically(self) -> None:
        self.assertEqual(
            candidate_fingerprint(self.LABEL, self.BODY, {}),
            candidate_fingerprint(self.LABEL, self.BODY, None),
        )

    def test_meeting_and_work_item_ids_prefix_the_fingerprint(self) -> None:
        plain = candidate_fingerprint(self.LABEL, self.BODY, {})
        self.assertEqual(
            candidate_fingerprint(self.LABEL, self.BODY, {"meeting_id": "mtg-1"}),
            f"meeting:mtg-1|{plain}",
        )
        self.assertEqual(
            candidate_fingerprint(self.LABEL, self.BODY, {"work_item_id": "15710"}),
            f"ado:15710|{plain}",
        )
        self.assertEqual(
            candidate_fingerprint(self.LABEL, self.BODY, {"meeting_id": "mtg-1", "work_item_id": "15710"}),
            f"ado:15710|meeting:mtg-1|{plain}",
        )

    def test_same_wording_in_different_meetings_does_not_collide(self) -> None:
        first = candidate_fingerprint(self.LABEL, self.BODY, {"meeting_id": "mtg-1"})
        second = candidate_fingerprint(self.LABEL, self.BODY, {"meeting_id": "mtg-2"})
        self.assertNotEqual(first, second)
