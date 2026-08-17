"""Azure Boards payload parsing helpers.

Split out of ``azure_boards_sync`` so HTML/id/relation/comment normalization
stays together without bloating work-item import/upsert logic.
``AzureBoardsSyncMixin`` inherits ``AzureBoardsParseMixin``; payload unwrap
and identity formatting remain on ``AzureSyncCommonMixin`` via the service MRO.
"""
from __future__ import annotations

import re
from typing import Any

from .constants import ADO_MAX_RELATIONS


class AzureBoardsParseMixin:
    def _azure_boards_relations(self, work_item: dict[str, Any]) -> list[dict[str, Any]]:
        relations: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for relation in list(work_item.get("relations") or []):
            if not isinstance(relation, dict):
                continue
            rel_type = str(relation.get("rel") or relation.get("type") or "related")
            url = str(relation.get("url") or "")
            related_id = self._azure_boards_id_from_url(url)
            if not related_id:
                continue
            related_id_str = str(related_id)
            if related_id_str in seen_ids:
                continue
            seen_ids.add(related_id_str)
            attributes = dict(relation.get("attributes") or {})
            name = str(attributes.get("name") or attributes.get("comment") or "")
            link_type = rel_type.split(".")[-1].replace("-Forward", "").replace("-Reverse", "")
            if "Hierarchy-Forward" in rel_type:
                link_type = "child"
            elif "Hierarchy-Reverse" in rel_type:
                link_type = "parent"
            elif "Related" in rel_type:
                link_type = "related"
            relations.append({
                "work_item_id": related_id_str,
                "link_type": link_type,
                "rel": rel_type,
                "name": name,
                "url": url,
            })
        if len(relations) > ADO_MAX_RELATIONS:
            # Keep the most meaningful links (parent/child) first so hub items
            # (Epics/Features with hundreds of relations) stay bounded.
            priority = {"parent": 0, "child": 1, "related": 2}
            relations.sort(key=lambda rel: priority.get(str(rel.get("link_type")), 3))
            relations = relations[:ADO_MAX_RELATIONS]
        return relations

    def _azure_boards_comments_from_payload(self, payload: Any) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        for item in self._azure_boards_payloads(payload):
            if isinstance(item, dict):
                raw_list = item.get("comments") or item.get("value") or item.get("items")
                if isinstance(raw_list, list):
                    for comment in raw_list:
                        parsed = self._azure_boards_normalize_comment(comment)
                        if parsed:
                            comments.append(parsed)
                else:
                    parsed = self._azure_boards_normalize_comment(item)
                    if parsed:
                        comments.append(parsed)
            elif isinstance(item, list):
                for comment in item:
                    parsed = self._azure_boards_normalize_comment(comment)
                    if parsed:
                        comments.append(parsed)
        return comments

    def _azure_boards_normalize_comment(self, comment: Any) -> dict[str, Any] | None:
        if not isinstance(comment, dict):
            return None
        # Comment text is always a string field. An MCP envelope dict carries
        # "content" as a list of {"type": "text"} parts — that is a transport
        # wrapper, not a comment, and must not be stringified into one.
        raw_text = ""
        for key in ("text", "body", "content"):
            value = comment.get(key)
            if isinstance(value, str) and value.strip():
                raw_text = value
                break
        text = self._azure_boards_html_to_text(raw_text)
        if not text:
            return None
        author_raw = comment.get("createdBy") or comment.get("author") or comment.get("user") or {}
        author = self._azure_boards_identity(author_raw) if isinstance(author_raw, (dict, str)) else "unknown"
        return {
            "id": str(comment.get("id") or ""),
            "author": author,
            "created_date": str(comment.get("createdDate") or comment.get("created_date") or ""),
            "text": text[:800],
        }

    def _azure_boards_first_work_item(self, payload: Any) -> dict[str, Any] | None:
        for item in self._azure_boards_payloads(payload):
            if isinstance(item, dict) and (item.get("id") or item.get("fields")):
                return item
            if isinstance(item, list):
                for nested in item:
                    if isinstance(nested, dict) and (nested.get("id") or nested.get("fields")):
                        return nested
            if isinstance(item, dict):
                for key in ("value", "workItems", "results", "data"):
                    value = item.get(key)
                    if isinstance(value, list):
                        for nested in value:
                            if isinstance(nested, dict) and (nested.get("id") or nested.get("fields")):
                                return nested
        return None

    def _azure_boards_ids_from_payload(self, payload: Any) -> list[int]:
        ids: list[int] = []
        for item in self._azure_boards_payloads(payload):
            ids.extend(self._azure_boards_extract_ids(item))
        deduped: list[int] = []
        seen: set[int] = set()
        for value in ids:
            if value not in seen:
                seen.add(value)
                deduped.append(value)
        return deduped

    def _azure_boards_extract_ids(self, payload: Any) -> list[int]:
        found: list[int] = []
        if isinstance(payload, dict):
            for key in ("id", "workItemId", "System.Id"):
                try:
                    value = int(payload.get(key))  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    value = 0
                if value > 0:
                    found.append(value)
            for key in ("ids", "workItemIds", "targetIds"):
                list_value = payload.get(key)
                if isinstance(list_value, list):
                    for item in list_value:
                        try:
                            number = int(item)
                        except (TypeError, ValueError):
                            continue
                        if number > 0:
                            found.append(number)
            for key in ("value", "workItems", "results", "data", "fields"):
                nested = payload.get(key)
                if nested is not None:
                    found.extend(self._azure_boards_extract_ids(nested))
            url = payload.get("url")
            if isinstance(url, str):
                related = self._azure_boards_id_from_url(url)
                if related:
                    found.append(related)
        elif isinstance(payload, list):
            for item in payload:
                found.extend(self._azure_boards_extract_ids(item))
        elif isinstance(payload, (int, float)):
            number = int(payload)
            if number > 0:
                found.append(number)
        elif isinstance(payload, str):
            related = self._azure_boards_id_from_url(payload)
            if related:
                found.append(related)
            else:
                for match in re.findall(r"\b(\d{4,7})\b", payload):
                    found.append(int(match))
        return found

    @staticmethod
    def _azure_boards_id_from_url(url: str) -> int | None:
        match = re.search(r"/work[Ii]tems/(\d+)", url or "")
        if not match:
            return None
        return int(match.group(1))

    @staticmethod
    def _azure_boards_html_to_text(value: Any) -> str:
        text = str(value or "")
        if not text:
            return ""
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</p>", "\n", text)
        text = re.sub(r"(?i)<li[^>]*>", "- ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()
