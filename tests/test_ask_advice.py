"""Unit tests for Ask memory advice helpers and skip rules."""

from __future__ import annotations

import unittest

from backend.architectos.ask_advice import (
    ask_memory_advice_enabled,
    build_advice_judge_prompt,
    collect_advice_candidates,
    format_advice_context_block,
    merge_advice_into_structured,
    parse_advice_verdict,
    postprocess_advice_verdict,
    reconcile_advice_kinds,
    should_skip_advice_turn,
)
from backend.architectos.embeddings import QUERY_SYNONYMS, expand_query_terms, content_tokens


def _hit(
    node_id: str,
    *,
    node_type: str = "Decision",
    source: str = "adr",
    label: str = "",
    text: str = "",
    score: float = 10.0,
    template: str = "",
    source_type: str = "",
    chat_id: str | None = None,
) -> dict:
    metadata = {"source": source}
    if template:
        metadata["template"] = template
    if source_type:
        metadata["source_type"] = source_type
    if chat_id is not None:
        metadata["chat_id"] = chat_id
    return {
        "score": score,
        "node": {
            "id": node_id,
            "type": node_type,
            "label": label or node_id,
            "text": text or f"body for {node_id}",
            "metadata": metadata,
        },
    }


class AskAdviceHelperTests(unittest.TestCase):
    def test_setting_default_on(self) -> None:
        self.assertTrue(ask_memory_advice_enabled(None))
        self.assertTrue(ask_memory_advice_enabled({}))
        self.assertTrue(ask_memory_advice_enabled({"ask_memory_advice": True}))
        self.assertFalse(ask_memory_advice_enabled({"ask_memory_advice": False}))

    def test_skip_when_setting_off(self) -> None:
        self.assertTrue(should_skip_advice_turn(message="should we change the API?", enabled=False))

    def test_skip_identifier_only(self) -> None:
        self.assertTrue(should_skip_advice_turn(message="AB#1234", enabled=True))
        self.assertTrue(should_skip_advice_turn(message="node_abcdef", enabled=True))
        self.assertFalse(should_skip_advice_turn(message="Does AB#1234 conflict with our auth decision?", enabled=True))

    def test_skip_council_and_local_memory(self) -> None:
        self.assertTrue(should_skip_advice_turn(message="plan a change", role="council", enabled=True))
        self.assertTrue(should_skip_advice_turn(message="plan a change", role="review", enabled=True))
        self.assertTrue(should_skip_advice_turn(message="plan a change", provider_id="local-memory", enabled=True))

    def test_skip_when_precomputed(self) -> None:
        self.assertTrue(should_skip_advice_turn(message="plan a change", precomputed=[], enabled=True))
        self.assertTrue(
            should_skip_advice_turn(
                message="plan a change",
                precomputed=[{"kind": "supports", "id": "n1"}],
                enabled=True,
            )
        )

    def test_collect_high_trust_only(self) -> None:
        hits = [
            _hit("n1", node_type="Lesson", source="chat"),
            _hit("n2", node_type="Decision", source="adr"),
            _hit("n3", node_type="Artifact", source="azure-boards", label="AB#99"),
            _hit("n4", node_type="Meeting", source="granola"),
        ]
        rule_layer = [_hit("n5", node_type="Constraint", source="manual", label="No PII in logs")]
        got = collect_advice_candidates(hits, rule_layer_hits=rule_layer)
        ids = [item["id"] for item in got]
        self.assertEqual(ids, ["n5", "n2", "n4", "n3"])
        self.assertNotIn("n1", ids)

    def test_collect_prefers_constraint_over_boards_tickets(self) -> None:
        boards = [
            _hit(f"ab{i}", node_type="Artifact", source="azure-boards", label=f"AB#{32000 + i} Data Uploader", score=90.0)
            for i in range(8)
        ]
        rule_layer = [_hit(
            "c1",
            node_type="Constraint",
            source="manual",
            label="Data Uploader",
            text="The system supports only CSV and simple EXCEL files.",
            score=70.0,
        )]
        got = collect_advice_candidates(boards, rule_layer_hits=rule_layer)
        self.assertEqual(got[0]["id"], "c1")
        self.assertEqual(got[0]["type"], "Constraint")

    def test_parse_filters_unrelated_and_low_confidence(self) -> None:
        text = """
        {
          "items": [
            {"kind": "contradicts", "id": "n2", "label": "API", "text": "Conflicts with decided contract", "confidence": 0.9},
            {"kind": "supports", "id": "n4", "label": "Call", "text": "Matches call notes", "confidence": 0.4},
            {"kind": "unrelated", "id": "n5", "label": "Logs", "text": "Different topic", "confidence": 0.99},
            {"kind": "supports", "id": "n3", "label": "AB#99", "text": "Backed by the ticket", "confidence": 0.7}
          ]
        }
        """
        candidates = [
            {"id": "n2", "type": "Meeting", "source": "granola", "label": "API"},
            {"id": "n3", "type": "Artifact", "source": "azure-boards", "label": "AB#99"},
            {"id": "n4", "type": "Meeting", "source": "granola", "label": "Call"},
        ]
        items = parse_advice_verdict(text, candidates=candidates)
        self.assertEqual([i["kind"] for i in items], ["contradicts", "supports"])
        self.assertEqual([i["id"] for i in items], ["n2", "n3"])

    def test_parse_keeps_limits_kind(self) -> None:
        text = """
        {
          "items": [
            {"kind": "limits", "id": "c1", "label": "Uploader", "text": "Allowed only with a transform script", "confidence": 0.88}
          ]
        }
        """
        candidates = [{"id": "c1", "type": "Constraint", "source": "manual", "label": "Uploader"}]
        items = parse_advice_verdict(text, candidates=candidates)
        self.assertEqual(items[0]["kind"], "limits")

    def test_postprocess_drops_boards_support_when_rule_limits(self) -> None:
        candidates = [
            {"id": "c1", "type": "Constraint", "source": "manual", "label": "Uploader"},
            {"id": "ab1", "type": "Artifact", "source": "azure-boards", "label": "AB#123"},
        ]
        items = [
            {"kind": "limits", "id": "c1", "type": "Constraint", "label": "Uploader", "text": "Only with script", "confidence": 0.9},
            {"kind": "supports", "id": "ab1", "type": "Artifact", "label": "AB#123", "text": "Ticket mentions feature", "confidence": 0.8},
        ]
        filtered = postprocess_advice_verdict(items, candidates=candidates)
        self.assertEqual([item["id"] for item in filtered], ["c1"])

    def test_merge_server_wins_and_adds_actions(self) -> None:
        structured = {
            "actions": [],
            "links": [],
            "advice": [
                {"kind": "supports", "id": "n2", "label": "Old", "text": "model said this", "confidence": 0.6},
            ],
        }
        server = [
            {"kind": "contradicts", "id": "n2", "label": "API", "text": "Server conflict", "confidence": 0.95, "source": "adr"},
            {"kind": "supports", "id": "n3", "label": "AB#1234", "text": "Ticket agrees", "confidence": 0.8, "source": "azure-boards"},
        ]
        merged = merge_advice_into_structured(structured, server)
        self.assertEqual(len(merged["advice"]), 2)
        self.assertEqual(merged["advice"][0]["kind"], "contradicts")
        self.assertEqual(merged["advice"][0]["text"], "Server conflict")
        action_types = {(a["type"], a["id"]) for a in merged["actions"]}
        self.assertIn(("memory_get", "n2"), action_types)
        self.assertIn(("open_work_item", "1234"), action_types)

    def test_format_context_block(self) -> None:
        block = format_advice_context_block([
            {"kind": "contradicts", "id": "n1", "label": "Rule", "text": "Do not store PII"},
            {"kind": "limits", "id": "n2", "label": "Uploader", "text": "Only with transform"},
        ])
        self.assertIn("Memory advice", block)
        self.assertIn("CONTRADICTS", block)
        self.assertIn("LIMITS", block)
        # Raw node ids must never leak into the model-visible block or answer.
        self.assertNotIn("n1", block)
        self.assertNotIn("n2", block)
        # Labels and texts still carry the substance the UI shows.
        self.assertIn("Rule", block)
        self.assertIn("Uploader", block)
        self.assertIn("Do not store PII", block)
        self.assertIn("Do not declare as-is support", block)
        self.assertEqual(format_advice_context_block([]), "")

    def test_collect_excludes_chat_derived_decision_nodes(self) -> None:
        # A promoted chat session summary keeps its chat template / source_type /
        # chat_id even though the summarizer labelled it Decision — those nodes
        # must not act as governing rules in the advice judge.
        hits = [
            _hit("s1", node_type="Decision", source="adr", template="chat_session_summary"),
            _hit("s2", node_type="Decision", source="adr", source_type="chat"),
            _hit("s3", node_type="Decision", source="adr", chat_id="chat_abc"),
            _hit("d1", node_type="Decision", source="adr", label="Real ADR"),
        ]
        got = collect_advice_candidates(hits)
        ids = [item["id"] for item in got]
        self.assertEqual(ids, ["d1"])

    def test_skip_action_commands_no_advice(self) -> None:
        # Bare retrieve/view imperatives carry no permissibility question: no card.
        self.assertTrue(should_skip_advice_turn(message="загрузи коммит", enabled=True))
        self.assertTrue(should_skip_advice_turn(message="скачай файл", enabled=True))
        self.assertTrue(should_skip_advice_turn(message="подтяни данные", enabled=True))
        self.assertTrue(should_skip_advice_turn(message="download the commit", enabled=True))
        self.assertTrue(should_skip_advice_turn(message="fetch", enabled=True))

    def test_keep_judge_for_advisability_phrasing(self) -> None:
        # Real permissibility / support questions must keep the judge on.
        self.assertFalse(should_skip_advice_turn(message="можно ли загрузить файл без скриптов?", enabled=True))
        self.assertFalse(should_skip_advice_turn(message="скачать можно только через трансформер?", enabled=True))
        self.assertFalse(should_skip_advice_turn(message="should we download this dependency?", enabled=True))
        self.assertFalse(should_skip_advice_turn(message="Does AB#1234 allow us to fetch the payload?", enabled=True))

    def test_judge_prompt_is_domain_agnostic(self) -> None:
        prompt = build_advice_judge_prompt(
            "can I do this directly?",
            [{"id": "c1", "type": "Constraint", "source": "manual", "label": "Policy", "text": "Only with approval"}],
        )
        self.assertIn('"limits"', prompt)
        self.assertIn("unrestricted_request", prompt)
        self.assertIn("capability_inquiry", prompt)
        self.assertIn('"framing"', prompt)
        self.assertNotIn("PDF", prompt)
        self.assertNotIn("Excel", prompt)
        self.assertIn("c1", prompt)

    def test_reconcile_upgrades_limits_when_framing_unrestricted(self) -> None:
        items = reconcile_advice_kinds([
            {
                "kind": "limits",
                "framing": "unrestricted_request",
                "id": "c1",
                "label": "Policy",
                "text": "Rule requires a prerequisite the user is not accepting",
                "confidence": 0.9,
            }
        ])
        self.assertEqual(items[0]["kind"], "contradicts")

    def test_reconcile_downgrades_contradicts_when_capability_inquiry(self) -> None:
        items = reconcile_advice_kinds([
            {
                "kind": "contradicts",
                "framing": "capability_inquiry",
                "id": "c1",
                "label": "Policy",
                "text": "Only allowed with a prerequisite",
                "confidence": 0.9,
            }
        ])
        self.assertEqual(items[0]["kind"], "limits")

    def test_parse_reconciles_misclassified_limits_from_framing(self) -> None:
        text = """
        {
          "items": [
            {
              "kind": "limits",
              "framing": "unrestricted_request",
              "id": "c1",
              "label": "Uploader",
              "text": "User seeks upload while excluding the required transform step",
              "confidence": 0.91
            }
          ]
        }
        """
        candidates = [{"id": "c1", "type": "Constraint", "source": "manual", "label": "Uploader"}]
        items = parse_advice_verdict(text, candidates=candidates)
        self.assertEqual(items[0]["kind"], "contradicts")
        self.assertEqual(items[0]["framing"], "unrestricted_request")

    def test_no_domain_upload_synonyms(self) -> None:
        self.assertNotIn("pdf", QUERY_SYNONYMS)
        self.assertNotIn("upload", QUERY_SYNONYMS)
        self.assertNotIn("excel", QUERY_SYNONYMS)
        upload_terms = expand_query_terms(content_tokens("загрузить файл PDF без скриптов"))
        self.assertNotIn("upload", upload_terms)
        self.assertNotIn("script", upload_terms)


if __name__ == "__main__":
    unittest.main()
