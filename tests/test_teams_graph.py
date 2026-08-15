from __future__ import annotations

import os
import unittest
from unittest import mock

from backend.architectos.teams_graph import (
    _format_ai_insight,
    _vtt_to_plain,
    teams_graph_configured,
)


class TeamsGraphHelperTests(unittest.TestCase):
    def test_vtt_to_plain_strips_cues(self) -> None:
        raw = """WEBVTT

1
00:00:01.000 --> 00:00:03.000
Hello <b>team</b>

2
00:00:03.000 --> 00:00:05.000
Hello <b>team</b>

3
00:00:05.000 --> 00:00:07.000
Ship sprint 7.7
"""
        text = _vtt_to_plain(raw)
        self.assertEqual(text, "Hello team\nShip sprint 7.7")

    def test_format_ai_insight_extracts_actions(self) -> None:
        text, actions = _format_ai_insight({
            "meetingNotes": [{"title": "Scope", "text": "Lock Dev v7.7"}],
            "actionItems": [{"text": "Publish Docker image", "ownerDisplayName": "Anton"}],
            "mentionEvents": [{"speaker": "Debbie", "eventDescription": "Asked about FixVersion"}],
        })
        self.assertIn("Lock Dev v7.7", text)
        self.assertIn("Publish Docker image", text)
        self.assertEqual(actions, ["Publish Docker image (Anton)"])
        self.assertIn("Debbie: Asked about FixVersion", text)

    def test_configured_requires_user_and_token_or_app(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(teams_graph_configured())
        with mock.patch.dict(
            os.environ,
            {
                "MS_GRAPH_ACCESS_TOKEN": "tok",
                "MS_GRAPH_USER_ID": "user-1",
            },
            clear=True,
        ):
            self.assertTrue(teams_graph_configured())
        with mock.patch.dict(
            os.environ,
            {
                "MS_GRAPH_TENANT_ID": "t",
                "MS_GRAPH_CLIENT_ID": "c",
                "MS_GRAPH_CLIENT_SECRET": "s",
                "MS_GRAPH_USER_ID": "user-1",
            },
            clear=True,
        ):
            self.assertTrue(teams_graph_configured())
        with mock.patch.dict(
            os.environ,
            {
                "MS_GRAPH_TENANT_ID": "t",
                "MS_GRAPH_CLIENT_ID": "c",
                "MS_GRAPH_CLIENT_SECRET": "s",
            },
            clear=True,
        ):
            self.assertFalse(teams_graph_configured())


if __name__ == "__main__":
    unittest.main()
