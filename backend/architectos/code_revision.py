"""Detect significant code/docs content changes for memory revisions.

Distinguishes same-truth duplicates from same-entity updates:
- same normalized content → skip (noise / identical)
- same entity, significant delta (signatures or body logic) → revision
- otherwise leave duplicate detection to token similarity
"""
from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .code_graph import _JS_EXTENSIONS, _PY_EXTENSIONS, parse_source

# Lines that are pure noise for significance (imports, blanks, comments).
_IMPORT_LINE_RE = re.compile(
    r"^\s*(?:from\s+\S+\s+import\s+|import\s+|require\s*\(|export\s+.*from\s+)",
    re.IGNORECASE,
)
_COMMENT_LINE_RE = re.compile(r"^\s*(#|//|/\*|\*|'''|\"\"\")")
_BLANK_RE = re.compile(r"^\s*$")


def normalize_source(text: str) -> str:
    """Strip comments, blank lines, and collapse whitespace for stable hashing."""
    lines: list[str] = []
    in_block = False
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if in_block:
            if "*/" in stripped or stripped.endswith('"""') or stripped.endswith("'''"):
                in_block = False
            continue
        if stripped.startswith("/*"):
            in_block = True
            if "*/" in stripped[2:]:
                in_block = False
            continue
        if stripped.startswith('"""') or stripped.startswith("'''"):
            quote = stripped[:3]
            if stripped.count(quote) >= 2 and len(stripped) > 3:
                continue
            in_block = True
            continue
        if _COMMENT_LINE_RE.match(stripped) or _BLANK_RE.match(stripped):
            continue
        lines.append(" ".join(stripped.split()))
    return "\n".join(lines)


def content_hash(text: str) -> str:
    """SHA1 of normalized source (comments/whitespace-insensitive)."""
    normalized = normalize_source(text)
    return hashlib.sha1(normalized.encode("utf-8", errors="replace")).hexdigest()[:16]


def _hash_body(text: str) -> str:
    return hashlib.sha1(normalize_source(text).encode("utf-8", errors="replace")).hexdigest()[:12]


def _py_function_bodies(text: str) -> dict[str, dict[str, str]]:
    """Map qualname → {signature, body_hash} for Python functions/methods/classes."""
    try:
        tree = ast.parse(text or "\n")
    except SyntaxError:
        return {}
    lines = (text or "").splitlines()
    result: dict[str, dict[str, str]] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = f"{prefix}.{child.name}" if prefix else child.name
                start = int(getattr(child, "lineno", 1) or 1) - 1
                end = int(getattr(child, "end_lineno", start + 1) or start + 1)
                body_src = "\n".join(lines[start:end])
                sig = ""
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    try:
                        sig = ast.unparse(child.args)  # type: ignore[attr-defined]
                    except Exception:
                        sig = ", ".join(a.arg for a in getattr(child.args, "args", []))
                result[qualname] = {
                    "signature": sig,
                    "body_hash": _hash_body(body_src),
                    "kind": "Class" if isinstance(child, ast.ClassDef) else "Function",
                }
                visit(child, qualname)

    visit(tree, "")
    return result


def _js_symbol_bodies(text: str, extension: str) -> dict[str, dict[str, str]]:
    """Best-effort symbol map for JS/TS via parse_source (signature + name hash)."""
    parsed = parse_source(text, extension)
    result: dict[str, dict[str, str]] = {}
    for symbol in parsed.symbols:
        # Without a full AST, hash signature + surrounding snippet by line.
        lines = (text or "").splitlines()
        line_idx = max(0, int(symbol.line or 1) - 1)
        snippet = "\n".join(lines[line_idx : min(len(lines), line_idx + 12)])
        result[symbol.qualname] = {
            "signature": str(symbol.signature or ""),
            "body_hash": _hash_body(snippet),
            "kind": symbol.kind,
        }
    return result


def symbol_body_hashes(text: str, extension: str = ".py") -> dict[str, dict[str, str]]:
    """Per-symbol signature + body hash used for significant-delta detection."""
    ext = (extension or ".py").lower()
    if ext in _PY_EXTENSIONS:
        return _py_function_bodies(text)
    if ext in _JS_EXTENSIONS:
        return _js_symbol_bodies(text, ext)
    # Generic: treat whole normalized file as one synthetic symbol.
    return {
        "__file__": {
            "signature": "",
            "body_hash": _hash_body(text),
            "kind": "File",
        }
    }


def structure_fingerprint(text: str, extension: str = ".py") -> str:
    """Stable fingerprint of symbol names + signatures (ignores bodies)."""
    symbols = symbol_body_hashes(text, extension)
    parts = []
    for name in sorted(symbols):
        sig = symbols[name].get("signature") or ""
        parts.append(f"{name}({sig})")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def import_normalized_hash(text: str) -> str:
    """Hash of non-import substantive lines — detects pure import shuffles as noise."""
    lines: list[str] = []
    for raw in normalize_source(text).splitlines():
        if _IMPORT_LINE_RE.match(raw):
            continue
        lines.append(raw)
    return hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class RevisionDelta:
    significant: bool
    change_kind: str = ""  # signatures | logic | both | noise | identical
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed_bodies: list[str] = field(default_factory=list)
    changed_signatures: list[str] = field(default_factory=list)
    content_hash: str = ""
    symbol_body_hashes: dict[str, dict[str, str]] = field(default_factory=dict)
    summary: str = ""

    def to_metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "content_hash": self.content_hash,
            "symbol_body_hashes": {
                name: {"signature": info.get("signature", ""), "body_hash": info.get("body_hash", "")}
                for name, info in self.symbol_body_hashes.items()
            },
        }
        if self.significant:
            payload["revision"] = True
            payload["change_kind"] = self.change_kind
            payload["change_summary"] = {
                "added": self.added,
                "removed": self.removed,
                "changed_bodies": self.changed_bodies,
                "changed_signatures": self.changed_signatures,
                "text": self.summary,
            }
        return payload


def build_content_metadata(text: str, extension: str = ".py") -> dict[str, Any]:
    """Metadata fields to attach on every code/docs candidate."""
    symbols = symbol_body_hashes(text, extension)
    return {
        "content_hash": content_hash(text),
        "structure_fingerprint": structure_fingerprint(text, extension),
        "import_normalized_hash": import_normalized_hash(text),
        "symbol_body_hashes": {
            name: {"signature": info.get("signature", ""), "body_hash": info.get("body_hash", "")}
            for name, info in symbols.items()
        },
    }


def code_revision_delta(
    old_meta: dict[str, Any] | None,
    new_text: str,
    *,
    extension: str = ".py",
    new_meta: dict[str, Any] | None = None,
) -> RevisionDelta:
    """Compare prior node/candidate metadata against new source text.

    Returns a RevisionDelta. ``significant`` is False for identical content or
    noise-only changes (comments, whitespace, import order).
    """
    fresh = dict(new_meta or {}) if new_meta else build_content_metadata(new_text, extension)
    new_hash = str(fresh.get("content_hash") or content_hash(new_text))
    new_symbols = dict(fresh.get("symbol_body_hashes") or symbol_body_hashes(new_text, extension))
    old = dict(old_meta or {})
    old_hash = str(old.get("content_hash") or "").strip()

    if old_hash and old_hash == new_hash:
        return RevisionDelta(
            significant=False,
            change_kind="identical",
            content_hash=new_hash,
            symbol_body_hashes=new_symbols,
            summary="identical content",
        )

    old_symbols = dict(old.get("symbol_body_hashes") or {})
    # Fallback: if no prior symbol map, treat any content_hash change as logic
    # when structure fingerprint also differs, else as significant logic.
    if not old_symbols and old_hash:
        old_struct = str(old.get("structure_fingerprint") or "")
        new_struct = str(fresh.get("structure_fingerprint") or structure_fingerprint(new_text, extension))
        old_imports = str(old.get("import_normalized_hash") or "")
        new_imports = str(fresh.get("import_normalized_hash") or import_normalized_hash(new_text))
        if old_imports and old_imports == new_imports and old_struct and old_struct == new_struct:
            return RevisionDelta(
                significant=False,
                change_kind="noise",
                content_hash=new_hash,
                symbol_body_hashes=new_symbols,
                summary="formatting or comments only",
            )
        kind = "signatures" if old_struct and old_struct != new_struct else "logic"
        return RevisionDelta(
            significant=True,
            change_kind=kind,
            content_hash=new_hash,
            symbol_body_hashes=new_symbols,
            summary=f"content changed ({kind})",
        )

    old_names = set(old_symbols)
    new_names = set(new_symbols)
    added = sorted(new_names - old_names)
    removed = sorted(old_names - new_names)
    changed_sigs: list[str] = []
    changed_bodies: list[str] = []
    for name in sorted(old_names & new_names):
        prev = old_symbols[name] if isinstance(old_symbols[name], dict) else {}
        curr = new_symbols[name] if isinstance(new_symbols[name], dict) else {}
        if str(prev.get("signature") or "") != str(curr.get("signature") or ""):
            changed_sigs.append(name)
        if str(prev.get("body_hash") or "") != str(curr.get("body_hash") or ""):
            changed_bodies.append(name)

    # Pure import / comment / whitespace: content_hash differs but symbols and
    # non-import body are unchanged.
    if not added and not removed and not changed_sigs and not changed_bodies:
        old_imports = str(old.get("import_normalized_hash") or "")
        new_imports = str(fresh.get("import_normalized_hash") or import_normalized_hash(new_text))
        if old_imports and old_imports == new_imports:
            return RevisionDelta(
                significant=False,
                change_kind="noise",
                content_hash=new_hash,
                symbol_body_hashes=new_symbols,
                summary="formatting or comments only",
            )
        # Import order / set changed only.
        if old_imports != new_imports or not old_imports:
            # If only imports differ from normalized view, still noise when
            # symbol bodies are unchanged (we already checked).
            return RevisionDelta(
                significant=False,
                change_kind="noise",
                content_hash=new_hash,
                symbol_body_hashes=new_symbols,
                summary="import or formatting only",
            )

    has_sig = bool(added or removed or changed_sigs)
    # Body-only changes among shared symbols (bugfix / logic).
    body_only = [n for n in changed_bodies if n not in changed_sigs and n not in added]
    has_logic = bool(body_only) or (bool(changed_bodies) and not has_sig)
    if has_sig and (body_only or (changed_bodies and not set(changed_bodies) <= set(changed_sigs))):
        kind = "both"
    elif has_sig:
        kind = "signatures"
    elif has_logic or changed_bodies:
        kind = "logic"
    else:
        kind = "noise"
        return RevisionDelta(
            significant=False,
            change_kind=kind,
            content_hash=new_hash,
            symbol_body_hashes=new_symbols,
            summary="no significant symbol changes",
        )

    parts: list[str] = []
    if added:
        parts.append(f"added {', '.join(added[:6])}")
    if removed:
        parts.append(f"removed {', '.join(removed[:6])}")
    if changed_sigs:
        parts.append(f"signature changed: {', '.join(changed_sigs[:6])}")
    if body_only or (kind in {"logic", "both"} and changed_bodies):
        show = body_only or changed_bodies
        parts.append(f"logic changed: {', '.join(show[:6])}")
    return RevisionDelta(
        significant=True,
        change_kind=kind,
        added=added,
        removed=removed,
        changed_bodies=body_only or changed_bodies,
        changed_signatures=changed_sigs,
        content_hash=new_hash,
        symbol_body_hashes=new_symbols,
        summary="; ".join(parts) or f"content changed ({kind})",
    )


def live_origin_node(nodes: list[Any], origin_key: str) -> Any | None:
    """Return the active, non-superseded memory node for an origin key."""
    from .candidate_identity import origin_key_from_memory_node

    best = None
    for node in nodes:
        if getattr(node, "status", "") != "active":
            continue
        meta = dict(getattr(node, "metadata", None) or {})
        if meta.get("invalid_at"):
            continue
        if origin_key_from_memory_node(node) != origin_key:
            continue
        created = str(getattr(node, "created_at", "") or "")
        if best is None or created >= str(getattr(best, "created_at", "") or ""):
            best = node
    return best
