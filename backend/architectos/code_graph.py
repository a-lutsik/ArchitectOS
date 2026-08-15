"""Persist a lightweight code-structure graph into the memory store.

The scanner already turns each source file into a coarse ``Artifact`` node whose
text is a regex outline. This module goes one level deeper: it extracts symbols
(functions / classes / methods) and the import relationships between files, then
writes them into the same ``memory_nodes`` / ``memory_edges`` graph so the
existing graph analytics (communities, PageRank, dual-level retrieval) have a
real code substrate to work on.

Node types produced here:
    - ``Symbol``  : a function / method / class (label = ``path::qualname``)

Edge types produced here (all carry ``metadata.origin = "code_graph"``):
    - ``DEFINES``  : file ``Artifact`` -> ``Symbol``            (EXTRACTED)
    - ``CONTAINS`` : parent ``Symbol`` -> child ``Symbol``      (EXTRACTED)
    - ``IMPORTS``  : file ``Artifact`` -> imported file         (EXTRACTED)

Everything is derived offline (Python ``ast`` + regex for JS/TS) so ingestion
needs no language server and no network. Symbol embeddings are optional and
governed by the ``embed_symbols`` policy; by default only "interesting" symbols
(documented, or public top-level, or classes) are marked for embedding.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .lsp import _JS_SYMBOL_PATTERNS, _regex_symbols
from .models import MemoryNode

_PY_EXTENSIONS = {".py"}
_JS_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

_LANG_BY_EXTENSION = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
}

# JS/TS import + require specifiers, e.g. `import x from './a'` / `require('../b')`.
_JS_IMPORT_RE = re.compile(
    r"""(?:import[^'"]*?from\s*|import\s*|require\s*\(\s*|export[^'"]*?from\s*)['"](?P<spec>[^'"]+)['"]"""
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "languages": ["python", "javascript", "typescript"],
    # off | selective | all — how aggressively to embed Symbol nodes.
    "embed_symbols": "selective",
    "max_symbols_per_file": 300,
    "min_doc_chars_for_embed": 40,
}


@dataclass(slots=True)
class SymbolInfo:
    """A single extracted code symbol, language-agnostic."""

    name: str
    qualname: str
    kind: str  # Function | Method | Class
    line: int
    signature: str = ""
    doc: str = ""
    parent: str = ""  # parent qualname for CONTAINS edges
    calls: list[str] = field(default_factory=list)  # callee simple names (best-effort)


@dataclass(slots=True)
class FileParse:
    """Parsed symbols + raw import specifiers for one source file."""

    symbols: list[SymbolInfo] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)


def language_for_extension(extension: str) -> str:
    return _LANG_BY_EXTENSION.get(extension.lower(), "")


def _py_signature(node: ast.AST) -> str:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ""
    try:
        return ast.unparse(node.args)  # type: ignore[attr-defined]
    except Exception:
        args = [a.arg for a in getattr(node.args, "args", [])]
        return ", ".join(args)


def _callee_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _collect_calls(func_node: ast.AST) -> list[str]:
    """Callee names invoked directly in a function body (not nested defs)."""
    names: list[str] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # nested scope is its own symbol
            if isinstance(child, ast.Call):
                name = _callee_name(child.func)
                if name:
                    names.append(name)
            walk(child)

    walk(func_node)
    # Deduplicate while preserving order; cap to keep the graph tidy.
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered[:40]


def parse_python(text: str) -> FileParse:
    """Extract Python symbols (with qualnames, signatures, docstrings) + imports."""
    try:
        tree = ast.parse(text or "\n")
    except SyntaxError:
        return FileParse()
    symbols: list[SymbolInfo] = []
    imports: list[str] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                is_class = isinstance(child, ast.ClassDef)
                kind = "Class" if is_class else ("Method" if prefix else "Function")
                qualname = f"{prefix}.{child.name}" if prefix else child.name
                symbols.append(
                    SymbolInfo(
                        name=child.name,
                        qualname=qualname,
                        kind=kind,
                        line=int(getattr(child, "lineno", 0) or 0),
                        signature=_py_signature(child),
                        doc=(ast.get_docstring(child) or "").strip(),
                        parent=prefix,
                        calls=[] if is_class else _collect_calls(child),
                    )
                )
                visit(child, qualname)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imports.append("." * (node.level or 0) + module)

    visit(tree, "")
    return FileParse(symbols=symbols, imports=imports)


def parse_javascript(text: str) -> FileParse:
    """Extract JS/TS symbols (flat) + import/require specifiers via regex."""
    raw = _regex_symbols(text, _JS_SYMBOL_PATTERNS)
    symbols = [
        SymbolInfo(
            name=item["name"],
            qualname=item["name"],
            kind=item.get("kind") or "Function",
            line=int(item.get("line") or 0),
            signature=str(item.get("detail") or "")[:160],
        )
        for item in raw
        if item.get("name")
    ]
    imports = [match.group("spec") for match in _JS_IMPORT_RE.finditer(text or "")]
    return FileParse(symbols=symbols, imports=imports)


def parse_source(text: str, extension: str) -> FileParse:
    ext = extension.lower()
    if ext in _PY_EXTENSIONS:
        return parse_python(text)
    if ext in _JS_EXTENSIONS:
        return parse_javascript(text)
    return FileParse()


class CodeGraphIngestor:
    """Writes Symbol nodes and code edges into the memory repository."""

    def __init__(self, repository: Any, settings_getter: Callable[[], dict[str, Any]] | None = None) -> None:
        self.repository = repository
        self._settings_getter = settings_getter

    # -- settings -----------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        merged = dict(DEFAULT_SETTINGS)
        raw: dict[str, Any] = {}
        if self._settings_getter is not None:
            try:
                raw = self._settings_getter() or {}
            except Exception:
                raw = {}
        elif hasattr(self.repository, "get_setting"):
            raw = self.repository.get_setting("code_graph") or {}
        for key in merged:
            if key in raw and raw[key] is not None:
                merged[key] = raw[key]
        return merged

    def enabled(self) -> bool:
        return bool(self.settings().get("enabled", True))

    def _language_enabled(self, language: str, settings: dict[str, Any]) -> bool:
        if not language:
            return False
        allowed = settings.get("languages") or DEFAULT_SETTINGS["languages"]
        return language in set(allowed)

    # -- embedding policy ---------------------------------------------------
    def _should_embed(self, symbol: SymbolInfo, settings: dict[str, Any]) -> bool:
        policy = str(settings.get("embed_symbols") or "selective").lower()
        if policy == "off":
            return False
        if policy == "all":
            return True
        # selective: documented, or a class, or a public top-level symbol.
        min_doc = int(settings.get("min_doc_chars_for_embed") or 40)
        if len(symbol.doc) >= min_doc:
            return True
        if symbol.kind == "Class":
            return True
        return not symbol.parent and not symbol.name.startswith("_")

    # -- edge helpers -------------------------------------------------------
    def _write_edge(
        self,
        source: str,
        target: str,
        edge_type: str,
        rel_path: str,
        *,
        confidence: float = 0.9,
        inferred: bool = False,
    ) -> None:
        if not source or not target or source == target:
            return
        edge = self.repository.add_edge(source, target, edge_type, "project", confidence)
        metadata = dict(edge.metadata or {})
        metadata["origin"] = "code_graph"
        metadata["cg_file"] = rel_path
        if inferred:
            metadata["inferred"] = True
        else:
            metadata["extracted"] = True
        edge.metadata = metadata
        self.repository.upsert_edge(edge)

    def _symbol_text(self, symbol: SymbolInfo) -> str:
        head = f"{symbol.kind} {symbol.qualname}"
        if symbol.signature:
            head = f"{head}({symbol.signature})" if symbol.kind != "Class" else f"{head} {symbol.signature}".strip()
        parts = [head]
        if symbol.doc:
            parts.append(symbol.doc[:600])
        text = "\n".join(parts).strip()
        # add_node requires >= 4 non-secret chars; pad defensively.
        return text if len(text) >= 4 else f"symbol {symbol.qualname}"

    # -- main entry ---------------------------------------------------------
    def ingest_files(
        self,
        entries: Iterable[tuple[MemoryNode, str, str, str]],
        project_id: str | None,
    ) -> dict[str, Any]:
        """Ingest a batch of (file_node, relative_path, extension, text).

        Returns a summary dict. Idempotent: symbol/edge ids are content-stable,
        and symbols/imports that disappeared from a re-scanned file are pruned.
        """
        entries = list(entries)
        if not entries:
            return {"files": 0, "symbols": 0, "edges": 0, "imports": 0, "pruned": 0}
        settings = self.settings()

        # Project-wide indexes (single scan): file path -> node id, and symbol
        # simple-name -> [(symbol_id, file)], so imports and calls can resolve to
        # files/symbols outside the current (possibly limited) scan batch.
        path_to_id, name_index = self._project_indexes(project_id)
        for file_node, rel, _ext, _text in entries:
            path_to_id[_normalize_rel(rel)] = file_node.id
        py_module_index = self._python_module_index(path_to_id)

        total_symbols = 0
        total_edges = 0
        total_imports = 0
        total_calls = 0
        total_pruned = 0
        # (rel_norm, symbols, qual_to_id) records for the CALLS second pass.
        file_records: list[tuple[str, list[SymbolInfo], dict[str, str]]] = []

        for file_node, rel, ext, text in entries:
            language = language_for_extension(ext)
            if not self._language_enabled(language, settings):
                continue
            rel_norm = _normalize_rel(rel)
            parsed = parse_source(text, ext)
            symbols = parsed.symbols[: int(settings.get("max_symbols_per_file") or 300)]

            qual_to_id: dict[str, str] = {}
            fresh_symbol_ids: set[str] = set()
            for symbol in symbols:
                node = self.repository.add_node(
                    "Symbol",
                    f"{rel_norm}::{symbol.qualname}",
                    "project",
                    self._symbol_text(symbol),
                    project_id,
                    confidence=0.7,
                    metadata={
                        "source": "code_graph",
                        "cg_file": rel_norm,
                        "path": rel_norm,
                        "symbol": symbol.qualname,
                        "kind": symbol.kind,
                        "line": symbol.line,
                        "language": language,
                        "cg_embed": self._should_embed(symbol, settings),
                    },
                )
                qual_to_id[symbol.qualname] = node.id
                fresh_symbol_ids.add(node.id)
                name_index.setdefault(symbol.name, []).append((node.id, rel_norm))
                total_symbols += 1
                self._write_edge(file_node.id, node.id, "DEFINES", rel_norm)
                total_edges += 1

            for symbol in symbols:
                if symbol.parent and symbol.parent in qual_to_id and symbol.qualname in qual_to_id:
                    self._write_edge(qual_to_id[symbol.parent], qual_to_id[symbol.qualname], "CONTAINS", rel_norm)
                    total_edges += 1

            # Import edges (file -> file), resolved within the project.
            fresh_import_targets: set[str] = set()
            for spec in parsed.imports:
                target_rel = self._resolve_import(spec, rel_norm, language, path_to_id, py_module_index)
                if not target_rel:
                    continue
                target_id = path_to_id.get(target_rel)
                if not target_id or target_id == file_node.id:
                    continue
                self._write_edge(file_node.id, target_id, "IMPORTS", rel_norm)
                fresh_import_targets.add(target_id)
                total_imports += 1
                total_edges += 1

            total_pruned += self._prune_stale(file_node.id, rel_norm, fresh_symbol_ids, fresh_import_targets)
            file_records.append((rel_norm, symbols, qual_to_id))

        # Second pass: CALLS edges (INFERRED) between symbols, now that every
        # symbol id is known. Clear prior CALLS from re-ingested callers first.
        fresh_callers = {sid for _, _, qmap in file_records for sid in qmap.values()}
        if fresh_callers:
            for edge in self.repository.list_edges_touching(fresh_callers):
                if edge.type == "CALLS" and edge.source in fresh_callers:
                    self.repository.delete_edge(edge.id)
        for rel_norm, symbols, qual_to_id in file_records:
            for symbol in symbols:
                caller_id = qual_to_id.get(symbol.qualname)
                if not caller_id or not symbol.calls:
                    continue
                for callee in symbol.calls:
                    target_id = self._resolve_call(callee, rel_norm, name_index)
                    if not target_id or target_id == caller_id:
                        continue
                    self._write_edge(caller_id, target_id, "CALLS", rel_norm, confidence=0.55, inferred=True)
                    total_calls += 1
                    total_edges += 1

        return {
            "files": len(entries),
            "symbols": total_symbols,
            "edges": total_edges,
            "imports": total_imports,
            "calls": total_calls,
            "pruned": total_pruned,
        }

    @staticmethod
    def _resolve_call(name: str, file_rel: str, name_index: dict[str, list[tuple[str, str]]]) -> str:
        """Resolve a callee name to a single symbol id (precision over recall)."""
        candidates = name_index.get(name) or []
        if not candidates:
            return ""
        same_file = list({sid for (sid, rel) in candidates if rel == file_rel})
        if len(same_file) == 1:
            return same_file[0]
        if same_file:
            return ""  # ambiguous within the same file → skip
        unique = list({sid for (sid, _rel) in candidates})
        return unique[0] if len(unique) == 1 else ""

    # -- reconcile ----------------------------------------------------------
    def _prune_stale(
        self, file_id: str, rel_norm: str, fresh_symbol_ids: set[str], fresh_import_targets: set[str]
    ) -> int:
        """Archive symbols / drop import edges that no longer exist in the file."""
        touching = self.repository.list_edges_touching([file_id])
        pruned = 0
        prior_symbol_ids: set[str] = set()
        for edge in touching:
            if edge.type == "DEFINES" and edge.source == file_id:
                prior_symbol_ids.add(edge.target)
            elif edge.type == "IMPORTS" and edge.source == file_id and edge.target not in fresh_import_targets:
                self.repository.delete_edge(edge.id)
                pruned += 1
        for symbol_id in prior_symbol_ids - fresh_symbol_ids:
            node = self.repository.get_node(symbol_id)
            if node is None or node.status != "active":
                continue
            self.repository.delete_node_edges(symbol_id)
            node.status = "archived"
            metadata = dict(node.metadata or {})
            metadata["archived_reason"] = "code_graph_symbol_removed"
            node.metadata = metadata
            self.repository.upsert_node(node)
            pruned += 1
        return pruned

    # -- indexes ------------------------------------------------------------
    def _project_indexes(
        self, project_id: str | None
    ) -> tuple[dict[str, str], dict[str, list[tuple[str, str]]]]:
        """One list_nodes scan → (path->file_id, symbol_simple_name->[(id, file)])."""
        path_index: dict[str, str] = {}
        name_index: dict[str, list[tuple[str, str]]] = {}
        for node in self.repository.list_nodes():
            if node.status != "active":
                continue
            if project_id and node.project_id not in {project_id, None}:
                continue
            metadata = node.metadata or {}
            if node.type == "Artifact":
                rel = metadata.get("path")
                if rel:
                    path_index[_normalize_rel(str(rel))] = node.id
            elif node.type == "Symbol":
                qualname = str(metadata.get("symbol") or "")
                simple = qualname.rsplit(".", 1)[-1] if qualname else ""
                rel = _normalize_rel(str(metadata.get("cg_file") or ""))
                if simple:
                    name_index.setdefault(simple, []).append((node.id, rel))
        return path_index, name_index

    @staticmethod
    def _python_module_index(path_to_id: dict[str, str]) -> dict[str, str]:
        """Map dotted-module suffixes to relative .py file paths."""
        index: dict[str, str] = {}
        for rel in path_to_id:
            if not rel.endswith(".py"):
                continue
            parts = rel[:-3].split("/")
            if parts and parts[-1] == "__init__":
                parts = parts[:-1]
            if not parts:
                continue
            for start in range(len(parts)):
                suffix = ".".join(parts[start:])
                if suffix:
                    index.setdefault(suffix, rel)
        return index

    # -- import resolution --------------------------------------------------
    def _resolve_import(
        self,
        spec: str,
        current_rel: str,
        language: str,
        path_to_id: dict[str, str],
        py_module_index: dict[str, str],
    ) -> str:
        if language == "python":
            return self._resolve_python_import(spec, current_rel, path_to_id, py_module_index)
        return self._resolve_relative_import(spec, current_rel, path_to_id)

    def _resolve_python_import(
        self, spec: str, current_rel: str, path_to_id: dict[str, str], py_module_index: dict[str, str]
    ) -> str:
        if not spec:
            return ""
        if spec.startswith("."):
            level = len(spec) - len(spec.lstrip("."))
            module = spec[level:]
            base_parts = current_rel.split("/")[:-1]  # drop filename
            if level > 1:
                base_parts = base_parts[: len(base_parts) - (level - 1)]
            target_parts = base_parts + (module.split(".") if module else [])
            candidate = "/".join(part for part in target_parts if part)
            for suffix in (".py", "/__init__.py"):
                rel = f"{candidate}{suffix}"
                if rel in path_to_id:
                    return rel
            return ""
        # Absolute dotted import: match the longest known module suffix.
        parts = spec.split(".")
        for start in range(len(parts)):
            suffix = ".".join(parts[start:])
            if suffix in py_module_index:
                return py_module_index[suffix]
        return ""

    @staticmethod
    def _resolve_relative_import(spec: str, current_rel: str, path_to_id: dict[str, str]) -> str:
        if not spec.startswith("."):
            return ""  # bare specifier -> external package, skip
        base_dir = current_rel.split("/")[:-1]
        segments = spec.split("/")
        parts = list(base_dir)
        for segment in segments:
            if segment in ("", "."):
                continue
            if segment == "..":
                if parts:
                    parts.pop()
            else:
                parts.append(segment)
        stem = "/".join(parts)
        candidates = [
            stem,
            f"{stem}.ts",
            f"{stem}.tsx",
            f"{stem}.js",
            f"{stem}.jsx",
            f"{stem}/index.ts",
            f"{stem}/index.tsx",
            f"{stem}/index.js",
            f"{stem}/index.jsx",
        ]
        for candidate in candidates:
            if candidate in path_to_id:
                return candidate
        return ""


def _normalize_rel(rel: str) -> str:
    return str(rel or "").replace("\\", "/").strip("/")
