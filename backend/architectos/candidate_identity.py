"""Stable origin keys for memory candidates that should not re-enter review.

Once a Granola meeting, git cluster, Azure work item, or similar entity is
accepted or rejected, later AutoScans must not enqueue a new card just because
the text, title, or commit list changed.
"""
from __future__ import annotations

from typing import Any

STICKY_SOURCE_TYPES = {
    "granola",
    "git",
    "git_cluster",
    "azure-boards",
    "azure-git",
    "teams-meetings",
    "inbox",
    "docs",
    "code",
    "adr",
    "issue",
    "pr",
    "meeting",
}

# AutoScan transcript dump of a chat. Session-keeper cards use other templates.
CHAT_DUMP_TEMPLATE = "chat"
CHAT_KEEPER_TEMPLATES = frozenset({
    "chat_session_summary",
    "chat_session_atom",
    "chat_fact_keeper",
    "chat_turn_keeper",
    "chat_favorite",
})
FACT_ATOM_TEMPLATES = frozenset({
    "chat_session_atom",
    "chat_fact_keeper",
    "chat_turn_keeper",
    "mcp_turn_atom",
})


def _normalize_fact_ident(value: str) -> str:
    return " ".join(str(value or "").split()).strip().lower()[:240]


def candidate_origin_key(candidate: dict[str, Any] | None) -> str:
    """Return a durable identity for an ingest entity, or empty if content-hashed."""
    if not candidate:
        return ""
    meta = dict(candidate.get("metadata") or {})
    source_type = str(candidate.get("source_type") or meta.get("source_type") or "").strip().lower()
    meeting_id = str(meta.get("meeting_id") or "").strip()
    work_item_id = str(meta.get("work_item_id") or "").strip()
    pull_request_id = str(meta.get("pull_request_id") or "").strip()
    repository_id = str(meta.get("repository_id") or "").strip()
    teams_meeting_id = str(meta.get("teams_meeting_id") or "").strip()
    if not source_type:
        if meeting_id:
            source_type = "granola"
        elif work_item_id:
            source_type = "azure-boards"
        elif pull_request_id or repository_id:
            source_type = "azure-git"
        elif teams_meeting_id:
            source_type = "teams-meetings"
    template = str(meta.get("template") or "").strip().lower()
    if template in FACT_ATOM_TEMPLATES:
        subject = _normalize_fact_ident(str(meta.get("fact_subject") or ""))
        if subject:
            return f"fact_subject:{subject}"
        ident = _normalize_fact_ident(str(meta.get("fact_key") or ""))
        if ident:
            return f"fact:{ident}"
    if source_type == "chat" and template == CHAT_DUMP_TEMPLATE:
        ident = str(meta.get("chat_id") or candidate.get("source_ref") or "").strip()
        return f"chat:{ident.lower()}" if ident else ""
    if source_type not in STICKY_SOURCE_TYPES:
        return ""
    ident = ""
    if source_type == "granola":
        ident = meeting_id or str(candidate.get("source_ref") or "").strip()
    elif source_type == "azure-boards":
        ident = work_item_id or str(candidate.get("source_ref") or "").strip()
    elif source_type == "azure-git":
        if pull_request_id:
            ident = f"pr:{pull_request_id}"
        elif repository_id:
            ident = f"repo:{repository_id}"
        else:
            ident = str(candidate.get("source_ref") or "").strip()
    elif source_type == "teams-meetings":
        ident = teams_meeting_id or str(candidate.get("source_ref") or "").strip()
    elif source_type == "git_cluster":
        root = str(meta.get("root") or "").strip()
        cluster = str(meta.get("cluster") or "").strip()
        ident = f"{root}#{cluster}" if root or cluster else str(candidate.get("source_ref") or "").strip()
    elif source_type == "git":
        ident = str(meta.get("root") or candidate.get("source_ref") or "").strip()
    else:
        ident = str(candidate.get("source_ref") or meta.get("path") or "").strip()
    if not ident:
        return ""
    return f"{source_type}:{ident.lower()}"


def origin_key_from_memory_node(node: Any) -> str:
    meta = dict(getattr(node, "metadata", None) or {})
    return candidate_origin_key(
        {
            "source_type": meta.get("source_type") or "",
            "source_ref": meta.get("source_ref") or "",
            "metadata": meta,
        }
    )
