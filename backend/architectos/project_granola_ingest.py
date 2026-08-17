"""Granola meeting candidate ingest for project memory.

Split out of ``project_scan_service``. Composed into ``ProjectScanServiceMixin``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .mcp import MCPError
from .models import stable_id

_LOG = logging.getLogger("architectos.service")


class ProjectGranolaIngestMixin:
    """Granola MCP meeting listing, detail fetch, and candidate building."""

    def _ingest_granola_candidates(self, project_id: str, limit: int, payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        payload = payload or {}
        if "_ingest_source_deadline" not in payload:
            payload = self._with_ingest_timeouts("granola", payload)
        item_timeout = self._ingest_item_timeout(payload, 30)
        listed = self.mcp_manager.call_tool("granola", "list_meetings", {}, timeout=item_timeout)
        meetings = self._granola_meetings_from_tool_result(listed.get("result") or listed)
        if not meetings:
            fallback = self._granola_text_from_tool_result(listed.get("result") or listed)
            if not fallback:
                return []
            return [self._granola_text_candidate(project_id, "recent-meetings", "Recent Granola meetings", fallback)]
        meetings = meetings[:limit]
        if self._ingest_deadline_expired(payload, "granola"):
            return []
        detailed = self._granola_meeting_details(meetings, item_timeout=item_timeout, payload=payload)
        if detailed:
            by_id = {self._granola_meeting_id(item): item for item in detailed if self._granola_meeting_id(item)}
            meetings = [self._merge_granola_meeting(item, by_id.get(self._granola_meeting_id(item))) for item in meetings]
        if not self._ingest_deadline_expired(payload, "granola"):
            self._granola_attach_transcripts(meetings, item_timeout=item_timeout, payload=payload)
        candidates = []
        thin = 0
        for index, meeting in enumerate(meetings):
            if self._ingest_deadline_expired(payload, "granola"):
                break
            candidate = self._granola_meeting_candidate(project_id, meeting, index)
            if candidate:
                if len(str(candidate.get("text") or "")) < 220:
                    thin += 1
                candidates.append(candidate)
        if thin and candidates:
            self._log_ingest(
                f"Granola: {thin}/{len(candidates)} meeting(s) still thin after get_meetings — notes/summary may be empty in Granola MCP",
                level="warn",
                source="granola",
            )
        return candidates

    def _merge_granola_meeting(self, base: dict[str, Any], detailed: dict[str, Any] | None) -> dict[str, Any]:
        if not detailed:
            return dict(base)
        merged = dict(base)
        for key, value in detailed.items():
            if value in (None, "", [], {}):
                continue
            if key in merged and merged.get(key) not in (None, "", [], {}) and key in {"id", "title", "date", "start_time"}:
                continue
            merged[key] = value
        return merged

    def _granola_meeting_details(
        self,
        meetings: list[dict[str, Any]],
        *,
        item_timeout: float = 30.0,
        payload: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        ids = [meeting_id for meeting_id in (self._granola_meeting_id(item) for item in meetings) if meeting_id]
        if not ids:
            return []
        # Official Granola MCP: get_meetings.meeting_ids maxItems = 10.
        detailed: list[dict[str, Any]] = []
        chunk_size = 10
        for offset in range(0, len(ids), chunk_size):
            if self._ingest_deadline_expired(payload, "granola"):
                break
            chunk = ids[offset : offset + chunk_size]
            page = self._granola_fetch_meeting_details_chunk(chunk, item_timeout=item_timeout)
            if page:
                detailed.extend(page)
            else:
                # Fall back to one-by-one when batch chunk fails.
                for meeting_id in chunk:
                    if self._ingest_deadline_expired(payload, "granola"):
                        break
                    page = self._granola_fetch_meeting_details_chunk([meeting_id], item_timeout=item_timeout)
                    if page:
                        detailed.extend(page)
        return detailed

    def _granola_fetch_meeting_details_chunk(
        self,
        meeting_ids: list[str],
        *,
        item_timeout: float = 30.0,
    ) -> list[dict[str, Any]]:
        for arguments in (
            {"meeting_ids": meeting_ids},
            {"ids": meeting_ids},
            {"meeting_id": meeting_ids[0]} if len(meeting_ids) == 1 else None,
            {"id": meeting_ids[0]} if len(meeting_ids) == 1 else None,
        ):
            if not arguments:
                continue
            try:
                result = self.mcp_manager.call_tool("granola", "get_meetings", arguments, timeout=item_timeout)
            except MCPError:
                continue
            detailed = self._granola_meetings_from_tool_result(result.get("result") or result)
            if detailed:
                return detailed
        return []

    def _granola_attach_transcripts(
        self,
        meetings: list[dict[str, Any]],
        *,
        item_timeout: float = 30.0,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Paid-plan enrichment: pull transcripts when notes/summary are still thin."""
        for meeting in meetings:
            if self._ingest_deadline_expired(payload, "granola"):
                return
            meeting_id = self._granola_meeting_id(meeting)
            if not meeting_id:
                continue
            if self._granola_meeting_body_richness(meeting) >= 280:
                continue
            if str(meeting.get("transcript") or "").strip():
                continue
            for arguments in ({"meeting_id": meeting_id}, {"id": meeting_id}, {"meeting_ids": [meeting_id]}):
                try:
                    result = self.mcp_manager.call_tool(
                        "granola",
                        "get_meeting_transcript",
                        arguments,
                        timeout=item_timeout,
                    )
                except MCPError:
                    continue
                text = self._granola_text_from_tool_result(result.get("result") or result)
                transcript = self._first_granola_value(
                    {"transcript": text, **(result.get("result") if isinstance(result.get("result"), dict) else {})},
                    ["transcript", "text", "content"],
                ) or text
                if transcript.strip():
                    meeting["transcript"] = transcript.strip()
                    break

    def _granola_meeting_body_richness(self, meeting: dict[str, Any]) -> int:
        chunks = [
            self._first_granola_value(meeting, ["summary", "ai_summary", "overview", "description", "summarized_notes", "enhanced_notes", "notes_markdown"]),
            self._first_granola_value(meeting, ["private_notes", "notes", "note", "content", "text"]),
            self._granola_joined_value(meeting.get("decisions") or meeting.get("decision")),
            self._granola_joined_value(meeting.get("action_items") or meeting.get("actions") or meeting.get("next_steps") or meeting.get("todos")),
        ]
        return sum(len(chunk) for chunk in chunks if chunk)

    def _granola_meeting_candidate(self, project_id: str, meeting: dict[str, Any], index: int) -> dict[str, Any] | None:
        meeting_id = self._granola_meeting_id(meeting) or f"meeting-{index}"
        title = self._first_granola_value(meeting, ["title", "name", "summary_title"], max_len=300) or f"Granola meeting {index + 1}"
        started_at = self._first_granola_value(meeting, ["start_time", "started_at", "created_at", "date", "updated_at"], max_len=120)
        attendees = self._granola_joined_value(meeting.get("attendees") or meeting.get("participants") or meeting.get("people") or meeting.get("known_participants"))
        summary = self._first_granola_value(
            meeting,
            ["summary", "ai_summary", "overview", "description", "summarized_notes", "enhanced_notes", "meeting_summary", "notes_markdown"],
            max_len=10000,
        )
        private_notes = self._first_granola_value(meeting, ["private_notes", "notes", "note", "content", "text", "body"], max_len=8000)
        decisions = self._granola_joined_value(meeting.get("decisions") or meeting.get("decision"))
        actions = self._granola_joined_value(meeting.get("action_items") or meeting.get("actions") or meeting.get("next_steps") or meeting.get("todos"))
        transcript = self._first_granola_value(meeting, ["transcript", "raw_transcript"], max_len=12000)
        parts = [f"Granola meeting: {title}"]
        if started_at:
            parts.append(f"Date: {started_at}")
        if attendees:
            parts.append(f"Attendees: {attendees}")
        if summary:
            parts.append(f"Summary:\n{summary}")
        if private_notes and private_notes.strip() != summary.strip():
            parts.append(f"Notes:\n{private_notes}")
        if decisions:
            parts.append(f"Decisions:\n{decisions}")
        if actions:
            parts.append(f"Action Items:\n{actions}")
        if transcript:
            # Keep a useful excerpt even when summary exists — often more concrete than the AI stub.
            excerpt_limit = 3500 if self._granola_meeting_body_richness(meeting) < 280 else 1600
            parts.append(f"Transcript excerpt:\n{transcript[:excerpt_limit]}")
        text = "\n\n".join(parts).strip()
        if len(text) < 4:
            return None
        source_ref = str(meeting.get("url") or meeting.get("href") or meeting_id)
        richness = self._granola_meeting_body_richness(meeting) + min(len(transcript), 800)
        confidence = 0.86 if richness >= 400 else 0.74 if richness >= 180 else 0.58
        return {
            "id": stable_id("candidate", project_id, "granola", meeting_id, text[:500]),
            "project_id": project_id,
            "source_type": "granola",
            "source_ref": source_ref,
            "label": f"Granola: {title}"[:180],
            "type": "Meeting",
            "scope": "project",
            "text": text[:12000],
            "confidence": confidence,
            "metadata": {
                "template": "granola_meeting",
                "meeting_id": meeting_id,
                "meeting_title": title,
                "meeting_date": started_at,
                "granola_url": str(meeting.get("url") or meeting.get("href") or ""),
                "attendees": attendees,
                "has_summary": bool(summary),
                "has_notes": bool(private_notes),
                "has_transcript": bool(transcript),
                "richness": richness,
                "source": "granola_mcp",
            },
        }

    def _granola_text_candidate(self, project_id: str, source_ref: str, title: str, text: str) -> dict[str, Any]:
        body = f"Granola meeting import: {title}\n\n{text}"
        return {
            "id": stable_id("candidate", project_id, "granola", source_ref, body[:500]),
            "project_id": project_id,
            "source_type": "granola",
            "source_ref": source_ref,
            "label": f"Granola: {title}"[:180],
            "type": "Meeting",
            "scope": "project",
            "text": body[:3000],
            "confidence": 0.58,
            "metadata": {"template": "granola_text", "source": "granola_mcp"},
        }

    def _granola_meetings_from_tool_result(self, result: Any) -> list[dict[str, Any]]:
        meetings: list[dict[str, Any]] = []
        for payload in self._granola_payloads(result):
            meetings.extend(self._granola_meetings_from_payload(payload))
        return meetings

    def _granola_meetings_from_payload(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [dict(item) for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("meetings", "items", "results", "data", "notes"):
            value = payload.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = self._granola_meetings_from_payload(value)
                if nested:
                    return nested
        if any(key in payload for key in ("id", "meeting_id", "title", "name", "summary", "notes")):
            return [payload]
        return []

    def _granola_payloads(self, result: Any) -> list[Any]:
        payloads: list[Any] = []
        if isinstance(result, dict):
            if "structuredContent" in result:
                payloads.append(result["structuredContent"])
            content = result.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        payloads.extend(self._granola_payloads_from_text(str(item.get("text") or "")))
                    elif isinstance(item, dict):
                        payloads.append(item)
            payloads.append(result)
        elif isinstance(result, list):
            payloads.extend(result)
        elif isinstance(result, str):
            payloads.extend(self._granola_payloads_from_text(result))
        return payloads

    def _granola_payloads_from_text(self, text: str) -> list[Any]:
        stripped = text.strip()
        if not stripped:
            return []
        xml_meetings = self._granola_meetings_from_xml(stripped)
        if xml_meetings:
            return [{"meetings": xml_meetings}]
        try:
            return [json.loads(stripped)]
        except json.JSONDecodeError:
            return [{"text": stripped}]

    def _granola_meetings_from_xml(self, text: str) -> list[dict[str, Any]]:
        """Parse Granola MCP list/get_meetings XML payload into meeting dicts."""
        if "<meeting" not in text:
            return []
        meetings: list[dict[str, Any]] = []
        pattern = re.compile(
            r'<meeting\b([^>]*)>(.*?)</meeting>',
            re.IGNORECASE | re.DOTALL,
        )
        attr_pattern = re.compile(r'([A-Za-z_:][\w:.-]*)\s*=\s*"([^"]*)"')
        for match in pattern.finditer(text):
            attrs = {key.lower(): value for key, value in attr_pattern.findall(match.group(1) or "")}
            body = match.group(2) or ""
            meeting: dict[str, Any] = {
                "id": attrs.get("id") or "",
                "title": attrs.get("title") or attrs.get("name") or "",
                "date": attrs.get("date") or attrs.get("start_time") or "",
                "start_time": attrs.get("date") or attrs.get("start_time") or "",
                "url": attrs.get("url") or attrs.get("href") or "",
            }
            participants = self._granola_xml_tag_text(body, "known_participants") or self._granola_xml_tag_text(body, "participants")
            if participants:
                meeting["attendees"] = [line.strip(" -•\t") for line in participants.splitlines() if line.strip()]
            summary = self._granola_xml_tag_text(body, "summary") or self._granola_xml_tag_text(body, "summarized_notes") or self._granola_xml_tag_text(body, "enhanced_notes")
            if summary:
                meeting["summary"] = summary
            notes = (
                self._granola_xml_tag_text(body, "private_notes")
                or self._granola_xml_tag_text(body, "notes")
                or self._granola_xml_tag_text(body, "notes_markdown")
            )
            if notes:
                meeting["private_notes"] = notes
            decisions = self._granola_xml_tag_text(body, "decisions")
            if decisions:
                meeting["decisions"] = [line.strip(" -•\t") for line in decisions.splitlines() if line.strip()]
            actions = self._granola_xml_tag_text(body, "action_items") or self._granola_xml_tag_text(body, "actions")
            if actions:
                meeting["action_items"] = [line.strip(" -•\t") for line in actions.splitlines() if line.strip()]
            transcript = self._granola_xml_tag_text(body, "transcript") or self._granola_xml_tag_text(body, "raw_transcript")
            if transcript:
                meeting["transcript"] = transcript
            if meeting["id"] or meeting["title"]:
                meetings.append(meeting)
        return meetings

    def _granola_xml_tag_text(self, body: str, tag: str) -> str:
        match = re.search(
            rf"<{tag}\b[^>]*>(.*?)</{tag}>",
            body,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return ""
        return re.sub(r"\s+\n", "\n", match.group(1)).strip()

    def _granola_text_from_tool_result(self, result: Any) -> str:
        chunks: list[str] = []
        if isinstance(result, dict):
            for item in result.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "text" and str(item.get("text") or "").strip():
                    chunks.append(str(item.get("text")).strip())
            for key in ("text", "summary", "notes"):
                if str(result.get(key) or "").strip():
                    chunks.append(str(result.get(key)).strip())
        elif isinstance(result, str):
            chunks.append(result.strip())
        return "\n\n".join(dict.fromkeys(chunk for chunk in chunks if chunk))

    def _granola_meeting_id(self, meeting: dict[str, Any]) -> str:
        return str(meeting.get("id") or meeting.get("meeting_id") or meeting.get("note_id") or meeting.get("document_id") or "").strip()

    def _first_granola_value(self, payload: dict[str, Any], keys: list[str], *, max_len: int = 12000) -> str:
        # Keep body fields (summary/notes/transcript) usable for memory; callers may pass a smaller max_len for titles.
        for key in keys:
            value = payload.get(key)
            if isinstance(value, (dict, list)):
                value = self._granola_joined_value(value)
            if str(value or "").strip():
                text = str(value).strip()
                return text[:max_len] if max_len > 0 else text
        return ""

    def _granola_joined_value(self, value: Any) -> str:
        if isinstance(value, list):
            parts = []
            for item in value:
                if isinstance(item, dict):
                    parts.append(str(item.get("name") or item.get("email") or item.get("text") or item.get("title") or item))
                else:
                    parts.append(str(item))
            return "; ".join(part.strip() for part in parts if part.strip())[:1400]
        if isinstance(value, dict):
            return "; ".join(f"{key}: {item}" for key, item in value.items() if str(item).strip())[:1400]
        return str(value or "").strip()[:1400]
