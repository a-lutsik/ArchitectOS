"""Project scanning, file-ingestion candidate building, and file I/O helpers.

Extracted from ``service.py``. Covers ``scan_project``, per-source file/inbox/chat/
granola/git candidate builders, filesystem summarisation/preview helpers, and small
project-file/bundle I/O methods. Depends only on shared constants and leaf modules,
never on ``service`` itself, so it introduces no import cycle. Repository access and
MCP managers are reached through ``self`` via the MRO on :class:`ArchitectOSService`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections import deque
from pathlib import Path
from typing import Any

from .constants import (
    CODE_EXTENSIONS,
    CODE_FILE_MAX_BYTES,
    DEFAULT_EXCLUDES,
    DOC_EXTENSIONS,
    GENERIC_FILE_MAX_BYTES,
    INGESTION_SOURCE_ALIASES,
    NOISE_FILE_NAMES,
    NOISE_FILE_SUFFIXES,
    SYSTEM_PROJECT_ID,
    TEXT_EXTENSIONS,
    TEXT_PREVIEW_BYTES,
    TEXT_SAMPLE_BYTES,
)
from .files import TEXT_LIKE_EXTENSIONS, decode_text
from .mcp import MCPError
from .models import stable_id, utc_now
from .project_files import delete_project_file as project_files_delete, save_project_file as project_files_save
from .storage import sanitize_text

_LOG = logging.getLogger("architectos.service")


class ProjectScanServiceMixin:
    """Project scan, file-ingestion candidates and filesystem helpers."""

    def scan_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        reindex = bool(payload.get("reindex") or payload.get("reset") or payload.get("rebuild"))
        project = self.repository.get_project(project_id)
        if str(payload.get("root_path") or "").strip():
            project, _created = self._upsert_project_profile({
                "name": payload.get("name") or (project.name if project else ""),
                "root_path": payload.get("root_path"),
                "description": payload.get("description") or (project.description if project else ""),
            })
            project_id = project.id
        elif project_id == SYSTEM_PROJECT_ID and self._is_architectos_source_root():
            raise ValueError("Choose or create a project folder before scanning memory.")
        root = self._project_root(project_id)
        project = self.repository.get_project(project_id)
        exclude_patterns = list((project.config or {}).get("ignore_patterns") or []) if project else []
        imported = []
        imported_ids: set[str] = set()
        code_entries: list[tuple[Any, str, str, str]] = []
        code_graph_on = self.code_graph.enabled()
        for path in self._iter_importable_files(root, int(payload.get("limit") or 25), exclude_patterns):
            relative = str(path.relative_to(root))
            node = self.repository.add_node("Doc" if path.suffix.lower() in {".md", ".txt", ".rst"} else "Artifact", relative, "project", self._summarize_file(path, root), project_id, confidence=0.72, metadata={"source": "project_scan", "path": relative, "project_root": str(root)})
            node = self.memory_lifecycle.initialize_node(node, "project_scan")
            self.graph_auto_linker.link_node(node, project_id)
            imported.append(node.to_dict())
            imported_ids.add(node.id)
            if code_graph_on and node.type == "Artifact":
                text = self._safe_code_text(path)
                if text is not None:
                    code_entries.append((node, relative.replace("\\", "/"), path.suffix.lower(), text))
        code_graph_result = self.code_graph.ingest_files(code_entries, project_id) if code_entries else None
        archived = 0
        if reindex:
            archived = self._archive_stale_scan_nodes(project_id, imported_ids)
        links = self.graph_auto_linker.rebuild(project_id) if (reindex or bool(payload.get("rebuild_links"))) else {"created": 0, "linked_nodes": 0, "edges": []}
        return {
            "project_id": project_id,
            "project": self.repository.get_project(project_id).to_dict(),
            "root": str(root),
            "imported": imported,
            "count": len(imported),
            "archived": archived,
            "links": links,
            "code_graph": code_graph_result,
            "mode": "reindex" if reindex else "scan",
        }

    def _safe_code_text(self, path: Path) -> str | None:
        """Read decoded source text for code-graph ingest, or None if unusable."""
        try:
            preview = self._read_text_preview(path)
        except OSError:
            return None
        if not preview.get("readable"):
            return None
        return str(preview.get("text") or "")

    def _archive_stale_scan_nodes(self, project_id: str, imported_ids: set[str]) -> int:
        archived = 0
        for node in self.repository.list_nodes():
            metadata = dict(node.metadata or {})
            if node.project_id != project_id or metadata.get("source") != "project_scan" or node.id in imported_ids or node.status != "active":
                continue
            node.status = "archived"
            metadata["archived_at"] = utc_now()
            metadata["archive_reason"] = "project_reindex_missing_source"
            node.metadata = metadata
            self.repository.upsert_node(node)
            archived += 1
        return archived

    def export_bundle(self, project_id: str) -> dict[str, Any]:
        return self.repository.export_bundle(project_id or "architectos")

    def import_bundle(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.repository.import_bundle(payload)

    def _normalize_ingestion_sources(self, sources: Any) -> list[str]:
        normalized: list[str] = []
        for source in sources:
            item = INGESTION_SOURCE_ALIASES.get(str(source).strip().lower(), str(source).strip().lower())
            if item and item not in normalized:
                normalized.append(item)
        return normalized or ["docs", "code", "chat", "git"]

    def _inbox_dir(self) -> Path:
        """Folder where users drop files for ingestion into memory.

        Configurable via the ``memory_ingest.inbox_dir`` setting; defaults to
        ``<root>/data/inbox`` and is created on first scan.
        """
        configured = str((self.repository.get_setting("memory_ingest") or {}).get("inbox_dir") or "").strip()
        if configured:
            path = Path(configured).expanduser()
            if not path.is_absolute():
                path = self.project_root / path
        else:
            path = self.repository.data_dir / "inbox"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _ingest_inbox_candidates(self, project_id: str, limit: int) -> list[dict[str, Any]]:
        inbox = self._inbox_dir()
        self._log_ingest(f"Inbox folder: {inbox}", source="inbox")
        candidates: list[dict[str, Any]] = []
        paths = sorted(
            (p for p in inbox.rglob("*") if p.is_file() and not any(part.startswith(".") for part in p.parts)),
            key=lambda p: p.stat().st_mtime,
        )
        for path in paths:
            if path.suffix.lower() not in TEXT_LIKE_EXTENSIONS:
                continue
            try:
                text = decode_text(path.read_bytes())[:200_000].strip()
            except OSError:
                continue
            if len(text) < 4:
                continue
            relative = str(path.relative_to(inbox))
            candidates.append({
                "id": stable_id("candidate", project_id, "inbox", relative, str(path.stat().st_mtime_ns)),
                "project_id": project_id,
                "source_type": "inbox",
                "source_ref": relative,
                "label": f"Inbox: {relative}",
                "type": "Doc",
                "scope": "project",
                "text": f"Dropped file: {relative}\n\n{text[:12000]}",
                "confidence": 0.7,
                "metadata": {
                    "path": str(path),
                    "size": path.stat().st_size,
                    "extension": path.suffix.lower(),
                    "template": "inbox",
                },
            })
            if len(candidates) >= limit:
                break
        return candidates

    def _ingest_file_candidates(self, project_id: str, root: Path, sources: list[str], limit: int) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        walk_limit = max(limit * 12, 120) if "code" in sources else max(limit * 3, 24)
        for path in self._iter_importable_files(root, walk_limit):
            preview = self._read_text_preview(path)
            if not preview["readable"]:
                continue
            clean = str(preview["text"])
            relative = str(path.relative_to(root))
            specialized = self._specialized_file_candidate(project_id, root, path, clean, sources)
            if specialized:
                candidates.append(specialized)
            else:
                source_type = self._generic_file_source(path)
                if source_type not in sources:
                    continue
                candidates.append({
                    "id": stable_id("candidate", project_id, source_type, relative, str(path.stat().st_mtime_ns)),
                    "project_id": project_id,
                    "source_type": source_type,
                    "source_ref": relative,
                    "label": f"{'Doc' if source_type == 'docs' else 'Code'}: {relative}",
                    "type": "Doc" if source_type == "docs" else "Artifact",
                    "scope": "project",
                    "text": self._summarize_clean_file(path, root, clean),
                    "confidence": 0.68 if source_type == "docs" else 0.6,
                    "metadata": {
                        "path": str(path),
                        "size": path.stat().st_size,
                        "extension": path.suffix.lower(),
                        "template": "generic",
                    },
                })
            if len(candidates) >= limit:
                break
        return candidates

    def _specialized_file_candidate(self, project_id: str, root: Path, path: Path, clean: str, sources: list[str]) -> dict[str, Any] | None:
        relative = str(path.relative_to(root))
        detector_order = [
            ("adr", self._looks_like_adr, self._build_adr_candidate),
            ("issues", self._looks_like_issue, self._build_issue_candidate),
            ("prs", self._looks_like_pr, self._build_pr_candidate),
            ("meetings", self._looks_like_meeting, self._build_meeting_candidate),
        ]
        for source, detector, builder in detector_order:
            if source in sources and detector(path, relative, clean):
                return builder(project_id, root, path, clean)
        return None

    def _generic_file_source(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in DOC_EXTENSIONS:
            return "docs"
        if suffix in CODE_EXTENSIONS:
            return "code"
        # Unknown but readable text still counts as code when scanning code sources.
        return "code"

    def _build_adr_candidate(self, project_id: str, root: Path, path: Path, clean: str) -> dict[str, Any]:
        relative = str(path.relative_to(root))
        title = self._markdown_title(clean, path)
        status = self._section_excerpt(clean, ["status"], 260)
        context = self._section_excerpt(clean, ["context", "problem"], 520)
        decision = self._section_excerpt(clean, ["decision", "decisions"], 700)
        consequences = self._section_excerpt(clean, ["consequences", "tradeoffs", "trade-offs"], 520)
        parts = [f"ADR: {title}"]
        if status:
            parts.append(f"Status: {status}")
        if context:
            parts.append(f"Context: {context}")
        if decision:
            parts.append(f"Decision: {decision}")
        if consequences:
            parts.append(f"Consequences: {consequences}")
        return self._candidate(project_id, "adr", relative, f"ADR: {title}", "Decision", "\n\n".join(parts), 0.82, path, {"template": "adr", "sections": ["status", "context", "decision", "consequences"]})

    def _build_issue_candidate(self, project_id: str, root: Path, path: Path, clean: str) -> dict[str, Any]:
        relative = str(path.relative_to(root))
        title = self._markdown_title(clean, path)
        summary = self._section_excerpt(clean, ["summary", "description", "problem"], 650) or self._first_lines(clean, 18)
        acceptance = self._section_excerpt(clean, ["acceptance criteria", "expected behavior", "expected"], 520)
        text = f"Issue tracker item: {title}\n\nSummary: {summary}"
        if acceptance:
            text += f"\n\nAcceptance/Expected: {acceptance}"
        return self._candidate(project_id, "issue", relative, f"Issue: {title}", "Requirement", text, 0.72, path, {"template": "issue"})

    def _build_pr_candidate(self, project_id: str, root: Path, path: Path, clean: str) -> dict[str, Any]:
        relative = str(path.relative_to(root))
        title = self._markdown_title(clean, path)
        summary = self._section_excerpt(clean, ["summary", "what changed", "changes"], 680) or self._first_lines(clean, 18)
        testing = self._section_excerpt(clean, ["testing", "test plan", "validation"], 460)
        text = f"Pull request memory candidate: {title}\n\nSummary: {summary}"
        if testing:
            text += f"\n\nTesting: {testing}"
        return self._candidate(project_id, "pr", relative, f"PR: {title}", "Artifact", text, 0.68, path, {"template": "pr"})

    def _build_meeting_candidate(self, project_id: str, root: Path, path: Path, clean: str) -> dict[str, Any]:
        relative = str(path.relative_to(root))
        title = self._markdown_title(clean, path)
        decisions = self._section_excerpt(clean, ["decisions", "decision"], 520)
        actions = self._section_excerpt(clean, ["action items", "actions", "next steps"], 640)
        notes = self._section_excerpt(clean, ["notes", "discussion"], 640) or self._first_lines(clean, 20)
        text = f"Meeting notes: {title}\n\nNotes: {notes}"
        if decisions:
            text += f"\n\nDecisions: {decisions}"
        if actions:
            text += f"\n\nAction Items: {actions}"
        return self._candidate(project_id, "meeting", relative, f"Meeting: {title}", "Meeting", text, 0.7, path, {"template": "meeting"})

    def _candidate(self, project_id: str, source_type: str, source_ref: str, label: str, node_type: str, text: str, confidence: float, path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "id": stable_id("candidate", project_id, source_type, source_ref, text[:500]),
            "project_id": project_id,
            "source_type": source_type,
            "source_ref": source_ref,
            "label": label,
            "type": node_type,
            "scope": "project",
            "text": text[:3000],
            "confidence": confidence,
            "metadata": {"path": str(path), "size": path.stat().st_size, **metadata},
        }
        return payload

    def _looks_like_adr(self, path: Path, relative: str, clean: str) -> bool:
        needle = relative.replace("\\", "/").lower()
        return (
            "/adr/" in f"/{needle}"
            or "/adrs/" in f"/{needle}"
            or "architecture/decision" in needle
            or path.name.lower().startswith(("adr-", "adr_"))
            or ("## status" in clean.lower() and "## decision" in clean.lower() and "## context" in clean.lower())
        )

    def _looks_like_issue(self, path: Path, relative: str, clean: str) -> bool:
        needle = relative.replace("\\", "/").lower()
        body = clean.lower()
        return (
            "/issues/" in f"/{needle}"
            or ".github/issue_template" in needle
            or path.name.lower().startswith(("issue-", "bug-", "ticket-"))
            or "acceptance criteria" in body
            or ("expected behavior" in body and "actual behavior" in body)
        )

    def _looks_like_pr(self, path: Path, relative: str, clean: str) -> bool:
        needle = relative.replace("\\", "/").lower()
        body = clean.lower()
        return (
            "pull_request_template" in needle
            or "/prs/" in f"/{needle}"
            or "/pull-requests/" in f"/{needle}"
            or path.name.lower().startswith(("pr-", "pull-request-"))
            or ("## summary" in body and ("## testing" in body or "test plan" in body))
        )

    def _looks_like_meeting(self, path: Path, relative: str, clean: str) -> bool:
        needle = relative.replace("\\", "/").lower()
        body = clean.lower()
        return (
            "/meetings/" in f"/{needle}"
            or "/minutes/" in f"/{needle}"
            or path.stem.lower().startswith(("meeting-", "minutes-", "standup-", "retro-"))
            or ("attendees" in body and ("action items" in body or "next steps" in body))
        )

    def _markdown_title(self, clean: str, path: Path) -> str:
        for line in clean.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip()[:140] or path.stem
        return path.stem.replace("_", " ").replace("-", " ").strip().title()

    def _section_excerpt(self, clean: str, names: list[str], limit: int) -> str:
        lines = clean.splitlines()
        collected: list[str] = []
        in_section = False
        wanted = {name.lower() for name in names}
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                heading = stripped.lstrip("#").strip().lower().rstrip(":")
                if in_section and heading not in wanted:
                    break
                in_section = heading in wanted
                continue
            if in_section and stripped:
                collected.append(stripped)
        return " ".join(collected)[:limit]

    def _first_lines(self, clean: str, limit: int) -> str:
        return "\n".join(line.strip() for line in clean.splitlines() if line.strip())[:limit * 120]

    def _ingest_chat_candidates(self, project_id: str, limit: int) -> list[dict[str, Any]]:
        candidates = []
        for chat in self.repository.list_chats(project_id)[:limit]:
            messages = [message for message in chat.get("messages", []) if str(message.get("text") or "").strip()]
            if not messages:
                continue
            text = "\n".join(f"{message.get('role', 'message')}: {str(message.get('text') or '').strip()}" for message in messages[-6:])
            candidates.append({
                "id": stable_id("candidate", project_id, "chat", chat["id"], text[:500]),
                "project_id": project_id,
                "source_type": "chat",
                "source_ref": chat["id"],
                "label": f"Chat: {chat.get('title') or chat['id']}",
                "type": "Lesson",
                "scope": "project",
                "text": f"Chat memory candidate from {chat.get('title') or chat['id']}.\n\n{text}",
                "confidence": 0.58,
                "metadata": {"chat_id": chat["id"], "messages": len(messages), "template": "chat"},
            })
        return candidates

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

    def _ingest_git_candidates(self, project_id: str, root: Path, limit: int) -> list[dict[str, Any]]:
        try:
            proc = subprocess.run(["git", "-C", str(root), "log", "--pretty=format:%h%x09%s", "-n", str(min(max(limit * 4, 20), 80))], text=True, capture_output=True, timeout=5, shell=False)
        except OSError:
            return []
        except subprocess.TimeoutExpired:
            return []
        output = (proc.stdout or "").strip()
        if proc.returncode != 0 or not output:
            return []
        commits = []
        for line in output.splitlines():
            if "\t" in line:
                commit_hash, subject = line.split("\t", 1)
            else:
                parts = line.split(" ", 1)
                commit_hash, subject = parts[0], parts[1] if len(parts) > 1 else line
            commits.append((commit_hash.strip(), subject.strip()))
        candidates = [{
            "id": stable_id("candidate", project_id, "git", root.as_posix(), output[:1000]),
            "project_id": project_id,
            "source_type": "git",
            "source_ref": str(root),
            "label": "Git: recent commit history",
            "type": "Decision",
            "scope": "project",
            "text": "Recent git history worth reviewing for durable memory:\n\n" + "\n".join(f"{item[0]} {item[1]}" for item in commits)[:2500],
            "confidence": 0.55,
            "metadata": {"root": str(root), "commits": len(commits), "template": "git_history"},
        }]
        candidates.extend(self._build_git_cluster_candidates(project_id, root, commits, limit))
        return candidates[:limit]

    def _build_git_cluster_candidates(self, project_id: str, root: Path, commits: list[tuple[str, str]], limit: int) -> list[dict[str, Any]]:
        clusters: dict[str, list[tuple[str, str]]] = {}
        for commit_hash, subject in commits:
            cluster = self._commit_cluster(subject)
            clusters.setdefault(cluster, []).append((commit_hash, subject))
        candidates: list[dict[str, Any]] = []
        for cluster, items in sorted(clusters.items(), key=lambda item: (-len(item[1]), item[0])):
            if len(items) < 2 and len(clusters) > 1:
                continue
            lines = [f"- {commit_hash} {subject}" for commit_hash, subject in items[:12]]
            candidates.append({
                "id": stable_id("candidate", project_id, "git_cluster", root.as_posix(), cluster, "|".join(subject for _commit_hash, subject in items[:12])),
                "project_id": project_id,
                "source_type": "git_cluster",
                "source_ref": f"{root}#{cluster}",
                "label": f"Git cluster: {cluster}",
                "type": "Decision",
                "scope": "project",
                "text": f"Semantic commit cluster '{cluster}' suggests durable project memory.\n\n" + "\n".join(lines),
                "confidence": min(0.78, 0.54 + 0.04 * len(items)),
                "metadata": {"root": str(root), "cluster": cluster, "commits": len(items), "template": "commit_cluster"},
            })
            if len(candidates) >= limit:
                break
        return candidates

    def _commit_cluster(self, subject: str) -> str:
        lower = subject.lower()
        prefix_match = re.match(r"^([a-z]+)(\([^)]+\))?!?:", lower)
        if prefix_match:
            prefix = prefix_match.group(1)
            if prefix in {"feat", "feature"}:
                return "feature"
            if prefix in {"fix", "bugfix", "hotfix"}:
                return "fix"
            if prefix in {"docs", "doc"}:
                return "docs"
            if prefix in {"test", "tests"}:
                return "tests"
            if prefix in {"refactor", "perf", "chore", "build", "ci"}:
                return prefix
        keyword_clusters = [
            ("security", ("security", "secret", "token", "auth", "approval")),
            ("memory", ("memory", "candidate", "ingest", "context")),
            ("provider", ("provider", "openai", "ollama", "claude", "codex", "router")),
            ("graph", ("graph", "node", "edge", "canvas")),
            ("workflow", ("workflow", "task", "review")),
            ("release", ("release", "version", "backup", "readiness", "package")),
        ]
        for cluster, keywords in keyword_clusters:
            if any(keyword in lower for keyword in keywords):
                return cluster
        return "general"

    def _path_matches_exclude(self, relative: str, pattern: str) -> bool:
        normalized = str(pattern or "").strip().replace("\\", "/")
        if not normalized:
            return False
        rel = relative.replace("\\", "/").strip("/")
        if normalized.endswith("/"):
            prefix = normalized.strip("/")
            return rel == prefix or rel.startswith(f"{prefix}/") or f"/{prefix}/" in f"/{rel}/"
        return rel == normalized or rel.endswith(f"/{normalized}") or normalized in rel.split("/")

    def _iter_importable_files(self, root: Path, limit: int, exclude_patterns: list[str] | None = None) -> list[Path]:
        patterns = [str(item).strip() for item in (exclude_patterns or []) if str(item).strip()]
        files: list[Path] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in DEFAULT_EXCLUDES for part in path.parts):
                continue
            relative = str(path.relative_to(root)).replace("\\", "/")
            if any(self._path_matches_exclude(relative, pattern) for pattern in patterns):
                continue
            if self._is_noise_file_name(path.name):
                continue
            suffix = path.suffix.lower()
            max_size = CODE_FILE_MAX_BYTES if suffix in CODE_EXTENSIONS else GENERIC_FILE_MAX_BYTES
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > max_size:
                continue
            if suffix not in TEXT_EXTENSIONS and suffix not in CODE_EXTENSIONS and suffix not in DOC_EXTENSIONS and not self._path_looks_text(path):
                continue
            files.append(path)
        files.sort(
            key=lambda item: (
                0 if item.suffix.lower() in CODE_EXTENSIONS else 1 if item.suffix.lower() in DOC_EXTENSIONS else 2,
                str(item).lower(),
            )
        )
        return files[:limit]

    def _iter_project_tree_entries(self, root: Path, limit: int) -> list[Path]:
        entries: list[Path] = []
        queue: deque[Path] = deque([root])
        while queue:
            current = queue.popleft()
            try:
                child_dirs = sorted(path for path in current.iterdir() if path.is_dir() and path.name not in DEFAULT_EXCLUDES)
            except OSError:
                continue
            for child in child_dirs:
                entries.append(child)
                queue.append(child)
                if len(entries) >= limit:
                    return entries
        for current_root, dir_names, file_names in os.walk(root):
            dir_names[:] = sorted(name for name in dir_names if name not in DEFAULT_EXCLUDES)
            file_names = sorted(file_names)
            current = Path(current_root)
            for name in file_names:
                entries.append(current / name)
                if len(entries) >= limit:
                    return entries
        return entries

    def _summarize_file(self, path: Path, root: Path) -> str:
        preview = self._read_text_preview(path)
        if not preview["readable"]:
            return str(preview["message"])
        clean = str(preview["text"])
        return self._summarize_clean_file(path, root, clean)

    def _summarize_clean_file(self, path: Path, root: Path, clean: str) -> str:
        suffix = path.suffix.lower()
        if suffix in CODE_EXTENSIONS:
            structure = self._extract_code_structure(clean, suffix)
            return f"Code file {path.relative_to(root)} structure:\n\n{structure}"
        lines = [line.strip() for line in clean.splitlines() if line.strip()]
        return f"File {path.relative_to(root)} imported into ArchitectOS memory.\n\n" + "\n".join(lines[:30])[:1800]

    def _extract_code_structure(self, clean: str, extension: str) -> str:
        lines = clean.splitlines()
        structural_lines = []
        in_multiline_comment = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("/*") or stripped.startswith('"""') or stripped.startswith("'''"):
                in_multiline_comment = True
            if in_multiline_comment:
                if stripped.endswith("*/") or stripped.endswith('"""') or stripped.endswith("'''"):
                    in_multiline_comment = False
                continue
            if stripped.startswith("//") or stripped.startswith("#") or stripped.startswith("*"):
                continue
            if extension == ".py":
                if stripped.startswith(("def ", "class ", "import ", "from ")):
                    structural_lines.append(line)
            elif extension in {".js", ".jsx", ".ts", ".tsx"}:
                if (
                    stripped.startswith(("import ", "export ", "class ", "interface ", "type ", "enum ")) or
                    "function " in stripped or
                    "constructor" in stripped or
                    (stripped.endswith("{") and any(k in stripped for k in ["public", "private", "protected", "static", "readonly", "async"]))
                ):
                    structural_lines.append(stripped)
            elif extension in {".java", ".kt", ".kts", ".scala"}:
                if (
                    stripped.startswith(("package ", "import ", "public class ", "class ", "interface ", "enum ", "public interface ", "public enum ")) or
                    ("class " in stripped and "{" in stripped) or
                    any(k in stripped for k in ["public ", "private ", "protected ", "static ", "final ", "abstract "])
                ):
                    if "=" not in stripped and (";" in stripped or "{" in stripped):
                        structural_lines.append(stripped)
            else:
                if any(k in stripped for k in ["class ", "def ", "function ", "import "]):
                    structural_lines.append(stripped)
        if not structural_lines:
            return "No high-level code structure detected."
        return "\n".join(structural_lines[:50])[:1700]

    def _read_text_preview(self, path: Path, max_bytes: int = TEXT_PREVIEW_BYTES) -> dict[str, Any]:
        size = path.stat().st_size
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
        truncated = len(raw) > max_bytes
        data = raw[:max_bytes]
        if self._looks_binary_bytes(data[:TEXT_SAMPLE_BYTES]):
            return {
                "readable": False,
                "binary": True,
                "encoding": None,
                "text": "",
                "redacted": False,
                "truncated": truncated,
                "message": f"Preview unavailable: {path.name} appears to be a binary file ({size} bytes).",
            }
        text, encoding = self._decode_text_bytes(data)
        clean, redacted = sanitize_text(text)
        return {
            "readable": True,
            "binary": False,
            "encoding": encoding,
            "text": clean,
            "redacted": redacted,
            "truncated": truncated,
            "message": "File preview truncated." if truncated else "",
        }

    def _path_looks_text(self, path: Path) -> bool:
        try:
            with path.open("rb") as handle:
                return not self._looks_binary_bytes(handle.read(TEXT_SAMPLE_BYTES))
        except OSError:
            return False

    @staticmethod
    def _is_noise_file_name(name: str) -> bool:
        """True for build/runtime artifacts that must never be ingested as memory."""
        lowered = str(name or "").strip().lower()
        if not lowered:
            return False
        if lowered in NOISE_FILE_NAMES:
            return True
        if lowered.endswith((".min.js", ".min.css")):
            return True
        suffix = ("." + lowered.rsplit(".", 1)[1]) if "." in lowered else ""
        return suffix in NOISE_FILE_SUFFIXES

    @staticmethod
    def _looks_binary_bytes(data: bytes) -> bool:
        if not data:
            return False
        if b"\x00" in data:
            return True
        text_control = {7, 8, 9, 10, 12, 13, 27}
        control_count = sum(1 for byte in data if byte < 32 and byte not in text_control)
        return control_count / max(len(data), 1) > 0.30

    @staticmethod
    def _decode_text_bytes(data: bytes) -> tuple[str, str]:
        for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
            try:
                return data.decode(encoding), encoding
            except UnicodeError:
                continue
        return data.decode("utf-8", errors="replace"), "utf-8-replace"


    def save_project_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Save or create a project file."""
        project_id = payload.get("project_id") or "architectos"
        root = self._project_root(project_id)
        return project_files_save(root, project_id, payload.get("path", ""), payload.get("text", ""))

    def delete_project_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Delete a project file."""
        project_id = payload.get("project_id") or "architectos"
        root = self._project_root(project_id)
        return project_files_delete(root, project_id, payload.get("path", ""))
