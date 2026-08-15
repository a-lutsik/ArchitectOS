from __future__ import annotations

import json
import os
import unittest
import urllib.error
from typing import Any
from unittest import mock

from backend.architectos.teams_graph import (
    GRAPH_BETA,
    TeamsGraphClient,
    TeamsGraphError,
    _format_ai_insight,
    _vtt_to_plain,
    teams_graph_configured,
)

STATIC_TOKEN_ENV = {"MS_GRAPH_ACCESS_TOKEN": "static-token", "MS_GRAPH_USER_ID": "user-1"}
APP_ENV = {
    "MS_GRAPH_TENANT_ID": "tenant-1",
    "MS_GRAPH_CLIENT_ID": "client-1",
    "MS_GRAPH_CLIENT_SECRET": "secret-1",
    "MS_GRAPH_USER_ID": "user-1",
}


class FakeGraph:
    """Stands in for Microsoft Graph via the client's request_json/request_text seams.

    Routes on substrings of the request URL so tests can describe a tenant's data
    instead of wiring up HTTP. Routes are tried in insertion order, so callers must
    list the more specific URL fragment first ("/aiInsights/<id>" before
    "/aiInsights"). A value may be a callable to vary the response per URL.
    """

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[str] = []
        self.missing: list[str] = []

    def _match(self, url: str) -> Any:
        for needle, value in self.routes.items():
            if needle in url:
                return value
        self.missing.append(url)
        raise TeamsGraphError(f"no stub for {url}")

    def json(self, url: str, *, method: str = "GET", headers: Any = None, data: Any = None) -> dict[str, Any]:
        self.calls.append(url)
        value = self._match(url)
        return value(url) if callable(value) else value

    def text(self, url: str, *, method: str = "GET", headers: Any = None, data: Any = None) -> str:
        self.calls.append(url)
        value = self._match(url)
        return value(url) if callable(value) else value


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


class TeamsGraphReadinessTests(unittest.TestCase):
    def test_missing_user_id_is_reported_first(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(TeamsGraphError) as ctx:
                TeamsGraphClient().ensure_ready()
        self.assertIn("MS_GRAPH_USER_ID", str(ctx.exception))

    def test_user_without_credentials_is_rejected(self) -> None:
        with mock.patch.dict(os.environ, {"MS_GRAPH_USER_ID": "user-1"}, clear=True):
            with self.assertRaises(TeamsGraphError) as ctx:
                TeamsGraphClient().ensure_ready()
        self.assertIn("MS_GRAPH_CLIENT_ID", str(ctx.exception))

    def test_static_token_is_enough(self) -> None:
        with mock.patch.dict(os.environ, STATIC_TOKEN_ENV, clear=True):
            TeamsGraphClient().ensure_ready()


class TeamsGraphTokenTests(unittest.TestCase):
    def test_static_token_skips_the_token_endpoint(self) -> None:
        graph = FakeGraph({"login.microsoftonline.com": {"access_token": "should-not-be-used"}})
        with mock.patch.dict(os.environ, STATIC_TOKEN_ENV, clear=True):
            client = TeamsGraphClient(request_json=graph.json, request_text=graph.text)
            self.assertEqual(client._auth_headers()["Authorization"], "Bearer static-token")
        self.assertEqual(graph.calls, [])

    def test_client_credentials_token_is_fetched_and_cached(self) -> None:
        graph = FakeGraph({"login.microsoftonline.com": {"access_token": "app-token", "expires_in": 3600}})
        with mock.patch.dict(os.environ, APP_ENV, clear=True):
            client = TeamsGraphClient(request_json=graph.json, request_text=graph.text)
            first = client._auth_headers()["Authorization"]
            second = client._auth_headers()["Authorization"]
        self.assertEqual(first, "Bearer app-token")
        self.assertEqual(second, "Bearer app-token")
        self.assertEqual(len(graph.calls), 1, "the token must be reused until it nears expiry")

    def test_expired_token_is_refetched(self) -> None:
        graph = FakeGraph({"login.microsoftonline.com": {"access_token": "app-token", "expires_in": 3600}})
        with mock.patch.dict(os.environ, APP_ENV, clear=True):
            client = TeamsGraphClient(request_json=graph.json, request_text=graph.text)
            client._auth_headers()
            client._token_expires_at = 0.0
            client._auth_headers()
        self.assertEqual(len(graph.calls), 2)

    def test_token_response_without_access_token_raises(self) -> None:
        graph = FakeGraph({"login.microsoftonline.com": {"error": "invalid_client"}})
        with mock.patch.dict(os.environ, APP_ENV, clear=True):
            client = TeamsGraphClient(request_json=graph.json, request_text=graph.text)
            with self.assertRaises(TeamsGraphError):
                client._auth_headers()


def _calendar_payload() -> dict[str, Any]:
    return {
        "value": [
            {
                "id": "event-1",
                "subject": "Sprint review",
                "isOnlineMeeting": True,
                "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/it's-here"},
                "organizer": {"emailAddress": {"name": "Anton"}},
                "attendees": [{"emailAddress": {"name": "Debbie"}}, {"emailAddress": {"address": "x@y.z"}}],
                "start": {"dateTime": "2026-08-01T10:00:00"},
                "end": {"dateTime": "2026-08-01T11:00:00"},
                "webLink": "https://outlook.office.com/event-1",
            },
            {"id": "event-2", "subject": "Offline 1:1"},
        ]
    }


class TeamsGraphCollectionTests(unittest.TestCase):
    def _client(self, graph: FakeGraph) -> TeamsGraphClient:
        return TeamsGraphClient(request_json=graph.json, request_text=graph.text)

    def _full_graph(self, overrides: dict[str, Any] | None = None) -> FakeGraph:
        # Most specific fragment first: the content URL also contains
        # "/transcripts", and the insight detail URL also contains "/aiInsights".
        routes: dict[str, Any] = {
            "/content": "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nWe ship on Friday\n",
            "/transcripts": {"value": [{"id": "transcript-1"}]},
            "/aiInsights/": {
                "id": "insight-1",
                "createdDateTime": "2026-08-01T11:05:00Z",
                "meetingNotes": [{"title": "Scope", "text": "Cut the sync feature"}],
                "actionItems": [{"text": "Write the ADR", "ownerDisplayName": "Anton"}],
            },
            "/aiInsights": {"value": [{"id": "insight-1"}]},
            "onlineMeetings?$filter": {"value": [{"id": "meeting-1"}]},
            "calendarView": _calendar_payload(),
        }
        routes.update(overrides or {})
        return FakeGraph(routes)

    def test_collect_meetings_assembles_transcript_and_insights(self) -> None:
        graph = self._full_graph()
        with mock.patch.dict(os.environ, STATIC_TOKEN_ENV, clear=True):
            meetings = self._client(graph).collect_meetings(limit=5)

        self.assertEqual(len(meetings), 1, "the non-online calendar entry must be skipped")
        meeting = meetings[0]
        self.assertEqual(meeting["meeting_id"], "meeting-1")
        self.assertEqual(meeting["subject"], "Sprint review")
        self.assertEqual(meeting["organizer"], "Anton")
        self.assertEqual(meeting["attendees"], ["Debbie", "x@y.z"])
        self.assertEqual(meeting["start"], "2026-08-01T10:00:00")
        self.assertTrue(meeting["has_transcript"])
        self.assertTrue(meeting["has_insights"])
        self.assertIn("We ship on Friday", meeting["transcript_text"])
        self.assertIn("Cut the sync feature", meeting["insights_text"])
        self.assertEqual(meeting["action_items"], ["Write the ADR (Anton)"])
        self.assertEqual(meeting["transcript_ids"], ["transcript-1"])

    def test_join_url_quotes_are_escaped_in_the_filter(self) -> None:
        # A raw apostrophe would terminate the OData string literal.
        captured: list[str] = []

        def resolve(url: str) -> dict[str, Any]:
            captured.append(url)
            return {"value": [{"id": "meeting-1"}]}

        graph = self._full_graph({"onlineMeetings?$filter": resolve})
        with mock.patch.dict(os.environ, STATIC_TOKEN_ENV, clear=True):
            self._client(graph).collect_meetings(limit=5)
        self.assertTrue(captured)
        self.assertIn("it%27%27s-here", captured[0])

    def test_meeting_without_content_is_dropped(self) -> None:
        graph = self._full_graph({"/transcripts": {"value": []}, "/aiInsights": {"value": []}})
        with mock.patch.dict(os.environ, STATIC_TOKEN_ENV, clear=True):
            self.assertEqual(self._client(graph).collect_meetings(limit=5), [])

    def test_unresolvable_meeting_is_skipped(self) -> None:
        graph = self._full_graph({"onlineMeetings?$filter": {"value": []}})
        with mock.patch.dict(os.environ, STATIC_TOKEN_ENV, clear=True):
            self.assertEqual(self._client(graph).collect_meetings(limit=5), [])

    def test_transcript_errors_do_not_abort_the_meeting(self) -> None:
        def failing(url: str) -> Any:
            raise TeamsGraphError("Microsoft Graph HTTP 404")

        graph = self._full_graph({"/transcripts": failing})
        with mock.patch.dict(os.environ, STATIC_TOKEN_ENV, clear=True):
            meetings = self._client(graph).collect_meetings(limit=5)
        self.assertEqual(len(meetings), 1)
        self.assertFalse(meetings[0]["has_transcript"])
        self.assertIn("Cut the sync feature", meetings[0]["insights_text"])

    def test_ai_insights_fall_back_to_the_beta_endpoint(self) -> None:
        seen: list[str] = []

        def only_beta(payload: dict[str, Any]) -> Any:
            def route(url: str) -> dict[str, Any]:
                seen.append(url)
                if url.startswith(GRAPH_BETA):
                    return payload
                raise TeamsGraphError("Microsoft Graph HTTP 404")

            return route

        graph = self._full_graph({
            "/aiInsights/": only_beta({"summary": "From beta"}),
            "/aiInsights": only_beta({"value": [{"id": "insight-1"}]}),
        })
        with mock.patch.dict(os.environ, STATIC_TOKEN_ENV, clear=True):
            meetings = self._client(graph).collect_meetings(limit=5)
        self.assertIn("From beta", meetings[0]["insights_text"])
        self.assertTrue(any(url.startswith(GRAPH_BETA) for url in seen))

    def test_limit_caps_the_number_of_meetings(self) -> None:
        payload = _calendar_payload()
        payload["value"] = [dict(payload["value"][0], id=f"event-{index}") for index in range(5)]
        graph = self._full_graph({"calendarView": payload})
        with mock.patch.dict(os.environ, STATIC_TOKEN_ENV, clear=True):
            meetings = self._client(graph).collect_meetings(limit=2)
        self.assertEqual(len(meetings), 2)


class TeamsGraphTransportTests(unittest.TestCase):
    """The real _http_json/_http_text path, with only urlopen replaced."""

    def _client(self) -> TeamsGraphClient:
        with mock.patch.dict(os.environ, STATIC_TOKEN_ENV, clear=True):
            return TeamsGraphClient()

    def _response(self, body: str) -> Any:
        response = mock.MagicMock()
        response.read.return_value = body.encode("utf-8")
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        return response

    def test_json_response_is_parsed(self) -> None:
        with mock.patch("backend.architectos.teams_graph.urlopen", return_value=self._response(json.dumps({"value": [1]}))):
            self.assertEqual(self._client()._http_json("https://graph.microsoft.com/v1.0/x"), {"value": [1]})

    def test_empty_body_becomes_an_empty_dict(self) -> None:
        with mock.patch("backend.architectos.teams_graph.urlopen", return_value=self._response("   ")):
            self.assertEqual(self._client()._http_json("https://graph.microsoft.com/v1.0/x"), {})

    def test_invalid_json_raises_graph_error(self) -> None:
        with mock.patch("backend.architectos.teams_graph.urlopen", return_value=self._response("not json")):
            with self.assertRaises(TeamsGraphError):
                self._client()._http_json("https://graph.microsoft.com/v1.0/x")

    def test_non_object_json_raises_graph_error(self) -> None:
        with mock.patch("backend.architectos.teams_graph.urlopen", return_value=self._response("[1, 2]")):
            with self.assertRaises(TeamsGraphError):
                self._client()._http_json("https://graph.microsoft.com/v1.0/x")

    def test_http_error_is_wrapped_with_the_status(self) -> None:
        error = urllib.error.HTTPError("https://graph.microsoft.com/v1.0/x", 403, "Forbidden", {}, None)  # type: ignore[arg-type]
        with mock.patch("backend.architectos.teams_graph.urlopen", side_effect=error):
            with self.assertRaises(TeamsGraphError) as ctx:
                self._client()._http_text("https://graph.microsoft.com/v1.0/x")
        self.assertIn("403", str(ctx.exception))

    def test_network_error_is_wrapped(self) -> None:
        with mock.patch(
            "backend.architectos.teams_graph.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with self.assertRaises(TeamsGraphError) as ctx:
                self._client()._http_text("https://graph.microsoft.com/v1.0/x")
        self.assertIn("network error", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
