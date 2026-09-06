"""Microsoft Teams meeting import via Graph and memory upserts."""

from __future__ import annotations

import logging
from typing import Any

from .models import stable_id, utc_now
from .teams_graph import TeamsGraphClient

_LOG = logging.getLogger("architectos.service")


class TeamsMeetingSyncMixin:
    """Microsoft Teams meeting ingest via Graph."""

    def _import_teams_meetings_to_memory(self, project_id: str, limit: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        meetings = self._collect_teams_meetings(limit, payload)
        imported: list[dict[str, Any]] = []
        updated = 0
        for meeting in meetings[:limit]:
            item = self._teams_meeting_item(project_id, meeting)
            if not item:
                continue
            node, was_update = self._upsert_teams_meeting_memory_node(project_id, item)
            if was_update:
                updated += 1
            imported.append(node.to_dict())
        return {"imported": imported, "updated": updated, "count": len(imported)}

    def _ingest_teams_meeting_candidates(self, project_id: str, limit: int, payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for meeting in self._collect_teams_meetings(limit, payload or {})[:limit]:
            item = self._teams_meeting_item(project_id, meeting)
            if item:
                candidates.append(item)
        return candidates

    def _collect_teams_meetings(self, limit: int, payload: dict[str, Any]) -> list[dict[str, Any]]:
        client = payload.get("_teams_graph_client")
        if client is None:
            client = TeamsGraphClient()
        lookback = payload.get("lookback_days") or payload.get("teams_lookback_days")
        include_transcripts = payload.get("include_transcripts")
        include_ai_insights = payload.get("include_ai_insights")
        if include_transcripts is None and "teams_include_transcripts" in payload:
            include_transcripts = payload.get("teams_include_transcripts")
        if include_ai_insights is None and "teams_include_ai_insights" in payload:
            include_ai_insights = payload.get("teams_include_ai_insights")
        return client.collect_meetings(
            limit=limit,
            lookback_days=int(lookback) if lookback not in (None, "") else None,
            include_transcripts=None if include_transcripts is None else bool(include_transcripts),
            include_ai_insights=None if include_ai_insights is None else bool(include_ai_insights),
        )

    def _teams_meeting_item(self, project_id: str, meeting: dict[str, Any]) -> dict[str, Any] | None:
        meeting_id = str(meeting.get("meeting_id") or "").strip()
        if not meeting_id:
            return None
        subject = str(meeting.get("subject") or "Teams meeting").strip() or "Teams meeting"
        start = str(meeting.get("start") or "").strip()
        end = str(meeting.get("end") or "").strip()
        organizer = str(meeting.get("organizer") or "").strip()
        attendees = [str(item).strip() for item in list(meeting.get("attendees") or []) if str(item).strip()]
        insights = str(meeting.get("insights_text") or "").strip()
        transcript = str(meeting.get("transcript_text") or "").strip()
        actions = [str(item).strip() for item in list(meeting.get("action_items") or []) if str(item).strip()]
        if not insights and not transcript:
            return None

        header = [
            f"Teams meeting: {subject}",
            f"When: {start} — {end}" if start or end else "",
            f"Organizer: {organizer}" if organizer else "",
            f"Attendees: {', '.join(attendees[:20])}" if attendees else "",
            f"Join: {meeting.get('join_url')}" if meeting.get("join_url") else "",
        ]
        body_parts = [line for line in header if line]
        if insights:
            body_parts.append("AI Insights / Facilitator notes:\n" + insights[:3500])
        if actions and not insights:
            body_parts.append("Action items:\n" + "\n".join(f"- {item}" for item in actions[:30]))
        if transcript:
            body_parts.append("Transcript:\n" + transcript[:3500])
        text = "\n\n".join(body_parts)
        return {
            "id": stable_id("teams", project_id, meeting_id),
            "project_id": project_id,
            "source_type": "teams-meetings",
            "source_ref": meeting_id,
            "label": f"Teams: {subject}"[:180],
            "type": "Meeting",
            "scope": "project",
            "text": text[:5000],
            "confidence": 0.82 if insights else 0.74,
            "metadata": {
                "template": "teams_meeting",
                "source": "teams_graph",
                "source_type": "teams-meetings",
                "teams_meeting_id": meeting_id,
                "teams_event_id": meeting.get("event_id") or "",
                "meeting_title": subject,
                "meeting_start": start,
                "meeting_end": end,
                "organizer": organizer,
                "attendees": attendees,
                "join_url": meeting.get("join_url") or "",
                "web_link": meeting.get("web_link") or "",
                "action_items": actions,
                "has_insights": bool(meeting.get("has_insights")),
                "has_transcript": bool(meeting.get("has_transcript")),
                "insight_ids": list(meeting.get("insight_ids") or []),
                "transcript_ids": list(meeting.get("transcript_ids") or []),
            },
        }

    def _upsert_teams_meeting_memory_node(self, project_id: str, item: dict[str, Any]) -> tuple[Any, bool]:
        metadata = dict(item.get("metadata") or {})
        meeting_id = str(metadata.get("teams_meeting_id") or item.get("source_ref") or "").strip()
        existing = self._find_memory_node_by_teams_meeting_id(project_id, meeting_id) if meeting_id else None
        if existing:
            existing.type = str(item.get("type") or existing.type or "Meeting")
            existing.label = str(item.get("label") or existing.label)
            existing.text = str(item.get("text") or existing.text)
            existing.confidence = float(item.get("confidence") or existing.confidence or 0.8)
            merged = dict(existing.metadata or {})
            merged.update(metadata)
            existing.metadata = merged
            existing.updated_at = utc_now()
            node = self.repository.upsert_node(existing)
            node = self.memory_lifecycle.initialize_node(node, "teams_meeting_refresh")
            self.graph_auto_linker.link_node(node, project_id)
            return node, True
        node = self.repository.add_node(
            str(item.get("type") or "Meeting"),
            str(item.get("label") or "Teams meeting"),
            str(item.get("scope") or "project"),
            str(item.get("text") or ""),
            self._memory_project_id_for_scope(str(item.get("scope") or "project"), project_id),
            None,
            float(item.get("confidence") or 0.8),
            metadata,
        )
        node = self.memory_lifecycle.initialize_node(node, "teams_meeting")
        self.graph_auto_linker.link_node(node, project_id)
        return node, False

    def _find_memory_node_by_teams_meeting_id(self, project_id: str, meeting_id: str):
        for node in self.repository.list_nodes():
            if node.status != "active":
                continue
            if node.project_id not in {project_id, None}:
                continue
            if str(dict(node.metadata or {}).get("teams_meeting_id") or "").strip() == meeting_id:
                return node
        return None
