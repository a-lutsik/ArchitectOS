from __future__ import annotations

import unittest

from backend.architectos.rich_response import RESPONSE_FORMAT_POLICY, split_rich_response


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

    def test_response_format_policy_mentions_mermaid_and_actions(self) -> None:
        self.assertIn("mermaid", RESPONSE_FORMAT_POLICY)
        self.assertIn("architectos", RESPONSE_FORMAT_POLICY)
        self.assertIn("open_work_item", RESPONSE_FORMAT_POLICY)


if __name__ == "__main__":
    unittest.main()
