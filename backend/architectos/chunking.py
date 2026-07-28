from __future__ import annotations

from typing import Any

# Industry-ish defaults: retrieveable units stay under ~1–1.5k chars with light overlap.
DEFAULT_CHUNK_CHARS = 1400
DEFAULT_CHUNK_OVERLAP = 180
MAIN_DESCRIPTION_CHARS = 1600
MAIN_ACCEPTANCE_CHARS = 800
COMMENT_CHUNK_CHARS = 1200


def chunk_text(
    text: str,
    *,
    size: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    """Split long text into overlapping windows; prefer paragraph/sentence breaks."""
    clean = str(text or "").strip()
    if not clean:
        return []
    size = max(200, int(size or DEFAULT_CHUNK_CHARS))
    overlap = max(0, min(int(overlap or 0), size // 2))
    if len(clean) <= size:
        return [clean]

    chunks: list[str] = []
    start = 0
    length = len(clean)
    while start < length:
        end = min(length, start + size)
        if end < length:
            window = clean[start:end]
            break_at = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(". "), window.rfind("; "))
            if break_at >= size // 3:
                end = start + break_at + 1
        piece = clean[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= length:
            break
        start = max(end - overlap, start + 1)
    return chunks


def work_item_main_parts(
    *,
    wi_type: str,
    work_item_id: int | str,
    title: str,
    state: str = "",
    assigned: str = "",
    iteration: str = "",
    area: str = "",
    tags: str = "",
    parent_id: Any = None,
    description: str = "",
    acceptance: str = "",
    relations: list[dict[str, Any]] | None = None,
    comment_count: int = 0,
    description_chunk_count: int = 0,
) -> str:
    """Primary work-item card: metadata + truncated description/AC + link stubs (no full comments)."""
    parts = [f"Azure Boards {wi_type} #{work_item_id}: {title}"]
    if state:
        parts.append(f"State: {state}")
    if assigned:
        parts.append(f"Assigned To: {assigned}")
    if iteration:
        parts.append(f"Iteration: {iteration}")
    if area:
        parts.append(f"Area: {area}")
    if tags:
        parts.append(f"Tags: {tags}")
    if parent_id:
        parts.append(f"Parent: #{parent_id}")
    if description:
        body = description[:MAIN_DESCRIPTION_CHARS]
        if len(description) > MAIN_DESCRIPTION_CHARS:
            body = body.rstrip() + "…"
        parts.append(f"Description:\n{body}")
    if acceptance:
        body = acceptance[:MAIN_ACCEPTANCE_CHARS]
        if len(acceptance) > MAIN_ACCEPTANCE_CHARS:
            body = body.rstrip() + "…"
        parts.append(f"Acceptance Criteria:\n{body}")
    if relations:
        link_lines = [
            f"- {item.get('link_type')}: #{item.get('work_item_id')}"
            + (f" ({item.get('name')})" if item.get("name") else "")
            for item in relations[:20]
            if isinstance(item, dict)
        ]
        if link_lines:
            parts.append("Links:\n" + "\n".join(link_lines))
    if description_chunk_count:
        parts.append(
            f"Description chunks: {description_chunk_count} linked (use memory_get / graph neighbors)."
        )
    if comment_count:
        parts.append(
            f"Comments: {comment_count} linked comment chunk(s). "
            "Use memory_get on related comment nodes or boards_list_comments."
        )
    return "\n\n".join(parts).strip()


def work_item_description_chunks(description: str) -> list[str]:
    """Overflow description beyond the main card — separate retrieveable chunks."""
    clean = str(description or "").strip()
    if len(clean) <= MAIN_DESCRIPTION_CHARS:
        return []
    overflow = clean[MAIN_DESCRIPTION_CHARS:].strip()
    if not overflow:
        return []
    # Include a short bridge so each chunk is self-contained.
    bridged = f"(continued description)\n{overflow}"
    return chunk_text(bridged, size=DEFAULT_CHUNK_CHARS, overlap=DEFAULT_CHUNK_OVERLAP)


def work_item_comment_chunks(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One retrieveable unit per comment; split very long comments further."""
    out: list[dict[str, Any]] = []
    for index, comment in enumerate(comments or []):
        if not isinstance(comment, dict):
            continue
        text = str(comment.get("text") or "").strip()
        if not text:
            continue
        author = str(comment.get("author") or "unknown").strip() or "unknown"
        created = str(comment.get("created_date") or "").strip()
        comment_id = str(comment.get("id") or index + 1).strip() or str(index + 1)
        pieces = chunk_text(text, size=COMMENT_CHUNK_CHARS, overlap=80) or [text]
        for part_index, piece in enumerate(pieces):
            stamp = author + (f" @ {created}" if created else "")
            label_suffix = f" ({part_index + 1}/{len(pieces)})" if len(pieces) > 1 else ""
            out.append({
                "comment_id": comment_id,
                "part_index": part_index,
                "part_count": len(pieces),
                "author": author,
                "created_date": created,
                "label": f"ADO comment #{comment_id}{label_suffix}: {author}"[:180],
                "text": f"Comment by {stamp}:\n{piece}".strip(),
            })
    return out
