from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .ssl_util import urlopen

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_BETA = "https://graph.microsoft.com/beta"
TOKEN_URL_TMPL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


class TeamsGraphError(ValueError):
    """Raised when Microsoft Graph Teams ingest cannot proceed."""


def teams_graph_configured() -> bool:
    if str(os.environ.get("MS_GRAPH_ACCESS_TOKEN") or "").strip():
        return bool(str(os.environ.get("MS_GRAPH_USER_ID") or os.environ.get("TEAMS_USER_ID") or "").strip())
    return bool(
        str(os.environ.get("MS_GRAPH_TENANT_ID") or "").strip()
        and str(os.environ.get("MS_GRAPH_CLIENT_ID") or "").strip()
        and str(os.environ.get("MS_GRAPH_CLIENT_SECRET") or "").strip()
        and str(os.environ.get("MS_GRAPH_USER_ID") or os.environ.get("TEAMS_USER_ID") or "").strip()
    )


def _env_flag(name: str, default: bool = True) -> bool:
    raw = str(os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


class TeamsGraphClient:
    """Fetch Teams meeting transcripts and Copilot/Facilitator-style AI Insights via Microsoft Graph."""

    def __init__(
        self,
        *,
        request_json: Callable[..., dict[str, Any]] | None = None,
        request_text: Callable[..., str] | None = None,
    ) -> None:
        self.tenant_id = str(os.environ.get("MS_GRAPH_TENANT_ID") or "").strip()
        self.client_id = str(os.environ.get("MS_GRAPH_CLIENT_ID") or "").strip()
        self.client_secret = str(os.environ.get("MS_GRAPH_CLIENT_SECRET") or "").strip()
        self.user_id = str(os.environ.get("MS_GRAPH_USER_ID") or os.environ.get("TEAMS_USER_ID") or "").strip()
        self._static_token = str(os.environ.get("MS_GRAPH_ACCESS_TOKEN") or "").strip()
        self._token: str = ""
        self._token_expires_at = 0.0
        self._request_json = request_json or self._http_json
        self._request_text = request_text or self._http_text

    def ensure_ready(self) -> None:
        if not self.user_id:
            raise TeamsGraphError(
                "Set MS_GRAPH_USER_ID (UPN or object id of the Teams user whose meetings to read) in .env."
            )
        if self._static_token:
            return
        if not (self.tenant_id and self.client_id and self.client_secret):
            raise TeamsGraphError(
                "Configure MS_GRAPH_TENANT_ID, MS_GRAPH_CLIENT_ID, MS_GRAPH_CLIENT_SECRET, "
                "and MS_GRAPH_USER_ID in .env (or set MS_GRAPH_ACCESS_TOKEN)."
            )

    def collect_meetings(
        self,
        *,
        limit: int = 12,
        lookback_days: int | None = None,
        include_transcripts: bool | None = None,
        include_ai_insights: bool | None = None,
    ) -> list[dict[str, Any]]:
        self.ensure_ready()
        if lookback_days is None:
            lookback_days = max(1, int(os.environ.get("TEAMS_LOOKBACK_DAYS") or 14))
        if include_transcripts is None:
            include_transcripts = _env_flag("TEAMS_INCLUDE_TRANSCRIPTS", True)
        if include_ai_insights is None:
            include_ai_insights = _env_flag("TEAMS_INCLUDE_AI_INSIGHTS", True)
        if not include_transcripts and not include_ai_insights:
            include_transcripts = True

        events = self.list_online_calendar_events(lookback_days=lookback_days, limit=max(limit * 3, limit))
        meetings: list[dict[str, Any]] = []
        for event in events:
            if len(meetings) >= limit:
                break
            assembled = self._assemble_meeting(
                event,
                include_transcripts=include_transcripts,
                include_ai_insights=include_ai_insights,
            )
            if assembled:
                meetings.append(assembled)
        return meetings

    def list_online_calendar_events(self, *, lookback_days: int, limit: int) -> list[dict[str, Any]]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=lookback_days)
        params = {
            "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "$select": "id,subject,start,end,isOnlineMeeting,onlineMeeting,organizer,attendees,webLink",
            "$orderby": "start/dateTime desc",
            "$top": str(min(max(limit * 2, 20), 50)),
        }
        url = f"{GRAPH_BASE}/users/{urllib.parse.quote(self.user_id)}/calendarView?{urllib.parse.urlencode(params)}"
        payload = self._get_json(url)
        events = [item for item in list(payload.get("value") or []) if isinstance(item, dict)]
        online: list[dict[str, Any]] = []
        for event in events:
            if event.get("isOnlineMeeting") or dict(event.get("onlineMeeting") or {}).get("joinUrl"):
                online.append(event)
            if len(online) >= limit:
                break
        return online

    def resolve_online_meeting(self, join_url: str) -> dict[str, Any] | None:
        join_url = str(join_url or "").strip()
        if not join_url:
            return None
        # Filter values must use single quotes; escape embedded quotes.
        safe = join_url.replace("'", "''")
        filter_value = urllib.parse.quote(f"JoinWebUrl eq '{safe}'", safe="")
        url = f"{GRAPH_BASE}/users/{urllib.parse.quote(self.user_id)}/onlineMeetings?$filter={filter_value}"
        try:
            payload = self._get_json(url)
        except TeamsGraphError:
            return None
        values = [item for item in list(payload.get("value") or []) if isinstance(item, dict)]
        return values[0] if values else None

    def list_transcripts(self, meeting_id: str) -> list[dict[str, Any]]:
        url = f"{GRAPH_BASE}/users/{urllib.parse.quote(self.user_id)}/onlineMeetings/{urllib.parse.quote(meeting_id)}/transcripts"
        try:
            payload = self._get_json(url)
        except TeamsGraphError:
            return []
        return [item for item in list(payload.get("value") or []) if isinstance(item, dict)]

    def get_transcript_content(self, meeting_id: str, transcript_id: str) -> str:
        url = (
            f"{GRAPH_BASE}/users/{urllib.parse.quote(self.user_id)}/onlineMeetings/"
            f"{urllib.parse.quote(meeting_id)}/transcripts/{urllib.parse.quote(transcript_id)}/content"
            f"?$format=text/vtt"
        )
        try:
            raw = self._get_text(url, accept="text/vtt,text/plain,*/*")
        except TeamsGraphError:
            return ""
        return _vtt_to_plain(raw)

    def list_ai_insights(self, meeting_id: str) -> list[dict[str, Any]]:
        # Meeting AI Insights live under /copilot (v1.0 and beta). Prefer v1.0, fall back to beta.
        path = f"/copilot/users/{urllib.parse.quote(self.user_id)}/onlineMeetings/{urllib.parse.quote(meeting_id)}/aiInsights"
        for base in (GRAPH_BASE, GRAPH_BETA):
            try:
                payload = self._get_json(f"{base}{path}")
                return [item for item in list(payload.get("value") or []) if isinstance(item, dict)]
            except TeamsGraphError:
                continue
        return []

    def get_ai_insight(self, meeting_id: str, insight_id: str) -> dict[str, Any] | None:
        path = (
            f"/copilot/users/{urllib.parse.quote(self.user_id)}/onlineMeetings/"
            f"{urllib.parse.quote(meeting_id)}/aiInsights/{urllib.parse.quote(insight_id)}"
        )
        for base in (GRAPH_BASE, GRAPH_BETA):
            try:
                payload = self._get_json(f"{base}{path}")
                if isinstance(payload, dict) and payload:
                    return payload
            except TeamsGraphError:
                continue
        return None

    def _assemble_meeting(
        self,
        event: dict[str, Any],
        *,
        include_transcripts: bool,
        include_ai_insights: bool,
    ) -> dict[str, Any] | None:
        subject = str(event.get("subject") or "Teams meeting").strip() or "Teams meeting"
        join_url = str(dict(event.get("onlineMeeting") or {}).get("joinUrl") or "").strip()
        meeting = self.resolve_online_meeting(join_url) if join_url else None
        meeting_id = str((meeting or {}).get("id") or "").strip()
        if not meeting_id:
            # Calendar-only online meeting without resolvable Graph onlineMeeting id — skip API artifacts.
            return None

        start = _event_dt(event.get("start"))
        end = _event_dt(event.get("end"))
        organizer = _person_name(dict(event.get("organizer") or {}).get("emailAddress"))
        attendees = [
            _person_name(dict(item.get("emailAddress") or {}))
            for item in list(event.get("attendees") or [])
            if isinstance(item, dict)
        ]
        attendees = [name for name in attendees if name][:24]

        insights_text = ""
        insights_meta: list[dict[str, Any]] = []
        action_items: list[str] = []
        if include_ai_insights:
            insights = self.list_ai_insights(meeting_id)
            for insight in insights[:3]:
                insight_id = str(insight.get("id") or "").strip()
                detail = self.get_ai_insight(meeting_id, insight_id) if insight_id else insight
                if not detail:
                    continue
                formatted, actions = _format_ai_insight(detail)
                if formatted:
                    insights_text = "\n\n".join(part for part in (insights_text, formatted) if part).strip()
                    insights_meta.append({
                        "id": insight_id,
                        "createdDateTime": detail.get("createdDateTime") or insight.get("createdDateTime"),
                    })
                action_items.extend(actions)

        transcript_text = ""
        transcript_ids: list[str] = []
        if include_transcripts:
            for transcript in self.list_transcripts(meeting_id)[:2]:
                transcript_id = str(transcript.get("id") or "").strip()
                if not transcript_id:
                    continue
                content = self.get_transcript_content(meeting_id, transcript_id)
                if content.strip():
                    transcript_ids.append(transcript_id)
                    transcript_text = "\n\n".join(part for part in (transcript_text, content.strip()) if part).strip()

        if not insights_text and not transcript_text:
            return None

        return {
            "meeting_id": meeting_id,
            "event_id": str(event.get("id") or "").strip(),
            "subject": subject,
            "start": start,
            "end": end,
            "join_url": join_url,
            "web_link": str(event.get("webLink") or "").strip(),
            "organizer": organizer,
            "attendees": attendees,
            "insights_text": insights_text[:6000],
            "transcript_text": transcript_text[:8000],
            "action_items": _unique_keep_order(action_items)[:40],
            "insight_ids": [item["id"] for item in insights_meta if item.get("id")],
            "transcript_ids": transcript_ids,
            "has_insights": bool(insights_text),
            "has_transcript": bool(transcript_text),
        }

    def _access_token(self) -> str:
        if self._static_token:
            return self._static_token
        import time

        now = time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token
        body = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }).encode("utf-8")
        url = TOKEN_URL_TMPL.format(tenant=urllib.parse.quote(self.tenant_id))
        payload = self._request_json(
            url,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data=body,
        )
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise TeamsGraphError("Microsoft Graph token response did not include access_token.")
        expires_in = int(payload.get("expires_in") or 3600)
        self._token = token
        self._token_expires_at = now + expires_in
        return token

    def _auth_headers(self, *, accept: str = "application/json") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token()}",
            "Accept": accept,
        }

    def _get_json(self, url: str) -> dict[str, Any]:
        return self._request_json(url, method="GET", headers=self._auth_headers())

    def _get_text(self, url: str, *, accept: str) -> str:
        return self._request_text(url, method="GET", headers=self._auth_headers(accept=accept))

    def _http_json(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
    ) -> dict[str, Any]:
        raw = self._http_text(url, method=method, headers=headers, data=data)
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TeamsGraphError(f"Invalid JSON from Microsoft Graph: {exc}") from exc
        if not isinstance(parsed, dict):
            raise TeamsGraphError("Unexpected Microsoft Graph JSON payload.")
        return parsed

    def _http_text(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
    ) -> str:
        request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urlopen(request, timeout=45.0) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                detail = ""
            raise TeamsGraphError(f"Microsoft Graph HTTP {exc.code} for {url}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise TeamsGraphError(f"Microsoft Graph network error: {exc.reason}") from exc


def _event_dt(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("dateTime") or "").strip()
    return str(value or "").strip()


def _person_name(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("name") or value.get("address") or "").strip()


def _unique_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _vtt_to_plain(raw: str) -> str:
    lines: list[str] = []
    for line in str(raw or "").splitlines():
        item = line.strip()
        if not item or item.upper().startswith("WEBVTT") or "-->" in item or re.fullmatch(r"\d+", item):
            continue
        item = re.sub(r"<[^>]+>", "", item).strip()
        if item:
            lines.append(item)
    # Collapse consecutive duplicates common in VTT cues.
    collapsed: list[str] = []
    for line in lines:
        if collapsed and collapsed[-1] == line:
            continue
        collapsed.append(line)
    return "\n".join(collapsed).strip()


def _format_ai_insight(detail: dict[str, Any]) -> tuple[str, list[str]]:
    parts: list[str] = []
    actions: list[str] = []

    meeting_notes = detail.get("meetingNotes") or detail.get("meetingNote") or []
    if isinstance(meeting_notes, list):
        note_lines = []
        for note in meeting_notes:
            if isinstance(note, dict):
                title = str(note.get("title") or note.get("subheading") or "").strip()
                text = str(note.get("text") or note.get("content") or "").strip()
                if title and text:
                    note_lines.append(f"- {title}: {text}")
                elif text:
                    note_lines.append(f"- {text}")
                elif title:
                    note_lines.append(f"- {title}")
            elif str(note).strip():
                note_lines.append(f"- {str(note).strip()}")
        if note_lines:
            parts.append("Notes:\n" + "\n".join(note_lines[:40]))
    elif isinstance(meeting_notes, str) and meeting_notes.strip():
        parts.append("Notes:\n" + meeting_notes.strip())

    action_items = detail.get("actionItems") or detail.get("actionItem") or []
    if isinstance(action_items, list):
        for item in action_items:
            if isinstance(item, dict):
                text = str(item.get("text") or item.get("title") or item.get("content") or "").strip()
                owner = str(item.get("ownerDisplayName") or item.get("owner") or "").strip()
                line = f"{text} ({owner})" if text and owner else text
            else:
                line = str(item or "").strip()
            if line:
                actions.append(line)
        if actions:
            parts.append("Action items:\n" + "\n".join(f"- {item}" for item in actions[:40]))

    mention_events = detail.get("mentionEvents") or []
    if isinstance(mention_events, list) and mention_events:
        mentions = []
        for item in mention_events[:20]:
            if not isinstance(item, dict):
                continue
            who = str(item.get("mentionedIdentity") or item.get("speaker") or item.get("displayName") or "").strip()
            event = str(item.get("eventDescription") or item.get("text") or "").strip()
            if who and event:
                mentions.append(f"- {who}: {event}")
            elif event:
                mentions.append(f"- {event}")
        if mentions:
            parts.append("Mentions:\n" + "\n".join(mentions))

    # Some payloads nest content under "insight" / "summary".
    for key in ("summary", "insightContent", "content"):
        value = detail.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in "\n".join(parts):
            parts.append(value.strip())
        elif isinstance(value, dict):
            nested, nested_actions = _format_ai_insight(value)
            if nested:
                parts.append(nested)
            actions.extend(nested_actions)

    return "\n\n".join(parts).strip(), _unique_keep_order(actions)
