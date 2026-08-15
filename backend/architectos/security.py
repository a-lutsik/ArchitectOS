from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

SECRET_PATTERNS = [
    ("bearer", re.compile(r"(?i)(authorization\s*[:=]\s*)?bearer\s+[a-z0-9._\-]{12,}")),
    ("assignment", re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*['\"]?[^\s'\",;]+")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_\-]{12,}")),
    ("github_token", re.compile(r"github_pat_[A-Za-z0-9_]{12,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("basic_auth_url", re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@")),
]


@dataclass(slots=True)
class SecurityFinding:
    kind: str
    count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SecurityPolicy:
    """Single policy for prompt, result, audit, and persistence redaction."""

    replacement = "[REDACTED]"

    def inspect_text(self, value: str) -> dict[str, Any]:
        original = value or ""
        redacted = original
        findings: list[SecurityFinding] = []
        for kind, pattern in SECRET_PATTERNS:
            redacted, count = pattern.subn(self.replacement, redacted)
            if count:
                findings.append(SecurityFinding(kind, count))
        return {
            "redacted": bool(findings),
            "text": redacted.strip(),
            "findings": [finding.to_dict() for finding in findings],
            "finding_count": sum(finding.count for finding in findings),
        }

    def redact_text(self, value: str) -> tuple[str, bool]:
        inspected = self.inspect_text(value)
        return str(inspected["text"]), bool(inspected["redacted"])

    def redact_payload(self, value: Any) -> tuple[Any, bool]:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, list):
            changed = False
            items = []
            for item in value:
                clean, redacted = self.redact_payload(item)
                changed = changed or redacted
                items.append(clean)
            return items, changed
        if isinstance(value, tuple):
            clean, redacted = self.redact_payload(list(value))
            return tuple(clean), redacted
        if isinstance(value, dict):
            changed = False
            payload: dict[str, Any] = {}
            for key, item in value.items():
                clean, redacted = self.redact_payload(item)
                changed = changed or redacted
                payload[str(key)] = clean
            if changed:
                payload["security_redacted"] = True
            return payload, changed
        return value, False
