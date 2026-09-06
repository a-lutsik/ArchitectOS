from __future__ import annotations

import unittest
from pathlib import Path

from backend.architectos.rich_response import RESPONSE_FORMAT_POLICY, extract_clarify_question, split_rich_response

ROOT = Path(__file__).resolve().parents[1]


class RichResponseTests(unittest.TestCase):
    def test_split_extracts_architectos_actions(self) -> None:
        text = """## Findings
See AB#1234

```architectos
{"actions":[{"type":"open_work_item","id":"1234","label":"Open AB#1234"}],"links":[{"kind":"work_item","id":"1234","title":"Login"}]}
```
"""
        display, structured = split_rich_response(text)
        self.assertIn("## Findings", display)
        self.assertIn("AB#1234", display)
        self.assertNotIn("architectos", display)
        self.assertEqual(structured["actions"][0]["type"], "open_work_item")
        self.assertEqual(structured["links"][0]["id"], "1234")

    def test_split_without_block_returns_empty_structured(self) -> None:
        display, structured = split_rich_response("Just text")
        self.assertEqual(display, "Just text")
        self.assertEqual(structured["actions"], [])
        self.assertEqual(structured["links"], [])
        self.assertEqual(structured.get("advice"), [])

    def test_split_extracts_advice_array(self) -> None:
        text = """## Plan
Looks fine.

```architectos
{"actions":[{"type":"memory_get","id":"node_abc","label":"Open"}],"advice":[{"kind":"contradicts","id":"node_abc","label":"API","text":"Conflicts with decided shape","confidence":0.9}]}
```
"""
        display, structured = split_rich_response(text)
        self.assertIn("## Plan", display)
        self.assertNotIn("architectos", display)
        self.assertEqual(structured["advice"][0]["kind"], "contradicts")
        self.assertEqual(structured["advice"][0]["id"], "node_abc")
        self.assertIn("advice", RESPONSE_FORMAT_POLICY)

    def test_response_format_policy_mentions_mermaid_and_actions(self) -> None:
        self.assertIn("mermaid", RESPONSE_FORMAT_POLICY)
        self.assertIn("flowchart", RESPONSE_FORMAT_POLICY)
        self.assertIn("block-beta", RESPONSE_FORMAT_POLICY)
        self.assertIn("curve: linear", RESPONSE_FORMAT_POLICY)
        self.assertIn("architectos", RESPONSE_FORMAT_POLICY)
        self.assertIn("open_work_item", RESPONSE_FORMAT_POLICY)
        source = (ROOT / "frontend" / "rich-response.js").read_text(encoding="utf-8")
        self.assertIn('curve: "linear"', source)
        self.assertNotIn('curve: "basis"', source)

    def test_frontend_keeps_mermaid_source_out_of_attributes(self) -> None:
        source = (ROOT / "frontend" / "rich-response.js").read_text(encoding="utf-8")
        self.assertIn("rich-mermaid-src", source)
        self.assertIn("encodeMermaidLabel", source)
        self.assertIn("#40;", source)
        self.assertIn("srcKeep", source)
        self.assertIn("sweepOrphanMermaidErrors", source)
        self.assertIn("ask-question-card", source)
        self.assertIn("extractClarifyQuestion", source)
        lightbox = (ROOT / "frontend" / "media-lightbox.js").read_text(encoding="utf-8")
        self.assertIn("decorateAskMedia", lightbox)
        self.assertIn("media-lightbox", lightbox)
        self.assertIn("data-mermaid-action", lightbox)
        self.assertIn("svgToPngBlob", lightbox)
        self.assertIn("writePngToClipboard", lightbox)
        self.assertIn("downloadBlob", lightbox)
        self.assertIn("copyMermaidSource", lightbox)
        html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-media-copy="mermaid"', html)
        self.assertIn('data-media-copy="png"', html)
        self.assertNotIn('data-mermaid="${escapeHtml(code)}"', source)

    def test_clarify_question_extracts_choice_list(self) -> None:
        text = (
            "I don't have enough code/context yet to explain SiteLineage accurately.\n"
            "\n"
            "Please point me to one of these so I can give you the real flow (not guesses):\n"
            "• the SiteLineage class/file path, or\n"
            "• a related endpoint/service name, or\n"
            "• a work item / PR id that introduced it.\n"
        )
        display, question = extract_clarify_question(text)
        self.assertIn("SiteLineage accurately", display)
        self.assertNotIn("class/file path", display)
        self.assertIsNotNone(question)
        assert question is not None
        self.assertTrue(question["prompt"].startswith("Please point me"))
        self.assertEqual(
            [item["label"] for item in question["options"]],
            [
                "the SiteLineage class/file path",
                "a related endpoint/service name",
                "a work item / PR id that introduced it.",
            ],
        )
        _, structured = split_rich_response(text)
        self.assertEqual(len(structured["questions"]), 1)
        self.assertEqual(structured["questions"][0]["options"][0]["label"], "the SiteLineage class/file path")

    def test_clarify_question_ignores_finding_lists(self) -> None:
        text = (
            "Key Azure Boards bugs captured:\n"
            "- AB#1234 login timeout\n"
            "- AB#1288 export zeros\n"
        )
        display, question = extract_clarify_question(text)
        self.assertIsNone(question)
        self.assertIn("AB#1234", display)

    def test_response_format_policy_mentions_questions(self) -> None:
        self.assertIn("questions", RESPONSE_FORMAT_POLICY)


if __name__ == "__main__":
    unittest.main()
