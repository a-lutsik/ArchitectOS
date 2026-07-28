from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha1
from typing import Any

SCHEMA_VERSION = "0.1"
VALID_SCOPES = {"interface", "project", "shared", "global"}
VALID_NODE_TYPES = {
    "Project",
    "Doc",
    "Decision",
    "Lesson",
    "Constraint",
    "Artifact",
    "Provider",
    "Task",
    "Meeting",
    "Requirement",
    "Feature",
    "Rule",
    "Concept",
}
VALID_EDGE_TYPES = {
    "HAS_MEMORY",
    "DOCUMENTED_IN",
    "SUPPORTS",
    "DEPENDS_ON",
    "RELATED_TO",
    "IMPLEMENTS",
    "DECIDED_IN",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_id(prefix: str, *parts: str) -> str:
    raw = "|".join(str(part or "").strip().lower() for part in parts)
    return f"{prefix}_{sha1(raw.encode('utf-8')).hexdigest()[:16]}"


@dataclass(slots=True)
class Project:
    id: str
    name: str
    root_path: str = ""
    description: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MemoryNode:
    id: str
    type: str
    label: str
    scope: str
    text: str
    project_id: str | None = None
    interface_id: str | None = None
    status: str = "active"
    confidence: float = 0.8
    metadata: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryNode":
        return cls(
            id=str(data.get("id") or ""),
            type=str(data.get("type") or "Concept"),
            label=str(data.get("label") or ""),
            scope=str(data.get("scope") or "project"),
            text=str(data.get("text") or ""),
            project_id=data.get("project_id"),
            interface_id=data.get("interface_id"),
            status=str(data.get("status") or "active"),
            confidence=float(data.get("confidence") or 0.0),
            metadata=dict(data.get("metadata") or {}),
            evidence=list(data.get("evidence") or []),
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
            schema_version=str(data.get("schema_version") or SCHEMA_VERSION),
        )


@dataclass(slots=True)
class MemoryEdge:
    id: str
    source: str
    target: str
    type: str
    scope: str
    status: str = "active"
    confidence: float = 0.8
    metadata: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryEdge":
        return cls(
            id=str(data.get("id") or ""),
            source=str(data.get("source") or ""),
            target=str(data.get("target") or ""),
            type=str(data.get("type") or "RELATED_TO"),
            scope=str(data.get("scope") or "project"),
            status=str(data.get("status") or "active"),
            confidence=float(data.get("confidence") or 0.0),
            metadata=dict(data.get("metadata") or {}),
            evidence=list(data.get("evidence") or []),
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
            schema_version=str(data.get("schema_version") or SCHEMA_VERSION),
        )


@dataclass(slots=True)
class SearchHit:
    node: MemoryNode
    score: float
    matched_terms: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node.to_dict(),
            "score": round(self.score, 4),
            "matched_terms": self.matched_terms,
            "reasons": self.reasons,
        }
