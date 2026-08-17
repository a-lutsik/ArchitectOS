"""Regex/heuristic LSP fallbacks when a language server is unavailable."""
from __future__ import annotations

import ast
import re
from typing import Any


def fallback_symbols(relative_path: str, text: str, extension: str) -> dict[str, Any] | None:
    if extension == ".py":
        return {"language": "python", "symbols": _python_symbols(text)}
    if extension in {".js", ".jsx", ".ts", ".tsx"}:
        return {"language": "javascript" if extension in {".js", ".jsx"} else "typescript", "symbols": _regex_symbols(text, _JS_SYMBOL_PATTERNS)}
    if extension == ".java":
        return {"language": "java", "symbols": _regex_symbols(text, _JAVA_SYMBOL_PATTERNS)}
    return None


def fallback_hover(relative_path: str, text: str, extension: str, line: int, character: int) -> dict[str, Any] | None:
    symbols = fallback_symbols(relative_path, text, extension)
    if symbols is None:
        return None
    word = _word_at(text, line, character)
    line_text = _line_at(text, line).strip()
    hover = word or line_text
    if word and line_text:
        hover = f"{word}\n{line_text}"
    return {"language": symbols["language"], "path": relative_path, "line": line, "character": character, "hover": hover}


def fallback_diagnostics(relative_path: str, text: str, extension: str) -> dict[str, Any] | None:
    language = _language_for_extension(extension)
    if not language:
        return None
    diagnostics: list[dict[str, Any]] = []
    if extension == ".py":
        try:
            ast.parse(text or "\n", filename=relative_path)
        except SyntaxError as exc:
            diagnostics.append({
                "severity": "error",
                "message": exc.msg,
                "line": int(exc.lineno or 1),
                "character": int(exc.offset or 1) - 1,
            })
    return {"language": language, "server": "fallback", "path": relative_path, "diagnostics": diagnostics, "count": len(diagnostics), "source": "fallback"}


def fallback_references(relative_path: str, text: str, extension: str, line: int, character: int, query: str = "") -> dict[str, Any] | None:
    language = _language_for_extension(extension)
    if not language:
        return None
    needle = (query or _word_at(text, line, character)).strip()
    if not needle:
        return {"language": language, "path": relative_path, "query": "", "references": [], "count": 0}
    pattern = re.compile(rf"\b{re.escape(needle)}\b")
    references = []
    for index, value in enumerate(text.splitlines(), start=1):
        for match in pattern.finditer(value):
            references.append({"path": relative_path, "line": index, "character": match.start(), "preview": value.strip()[:240]})
            if len(references) >= 80:
                break
        if len(references) >= 80:
            break
    return {"language": language, "path": relative_path, "query": needle, "references": references, "count": len(references)}


def _python_symbols(text: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(text or "\n")
    except SyntaxError:
        return []
    symbols: list[dict[str, Any]] = []

    def visit(node: ast.AST, depth: int = 0) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "Class" if isinstance(child, ast.ClassDef) else "Function"
                symbols.append({"name": child.name, "kind": kind, "detail": "", "line": child.lineno, "depth": depth, "container": ""})
                visit(child, depth + 1)

    visit(tree)
    return symbols


def _regex_symbols(text: str, patterns: list[tuple[re.Pattern[str], str]]) -> list[dict[str, Any]]:
    symbols: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines(), start=1):
        for pattern, kind in patterns:
            match = pattern.search(line)
            if match:
                name = match.group("name")
                symbols.append({"name": name, "kind": kind, "detail": line.strip()[:120], "line": index, "depth": 0, "container": ""})
                break
    return symbols


_JS_SYMBOL_PATTERNS = [
    (re.compile(r"\bclass\s+(?P<name>[A-Za-z_$][\w$]*)"), "Class"),
    (re.compile(r"\bfunction\s+(?P<name>[A-Za-z_$][\w$]*)\s*\("), "Function"),
    (re.compile(r"\b(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"), "Function"),
    (re.compile(r"\bexport\s+(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\s*\("), "Function"),
]

_JAVA_SYMBOL_PATTERNS = [
    (re.compile(r"\b(?:class|interface|enum|record)\s+(?P<name>[A-Za-z_]\w*)"), "Class"),
    (re.compile(r"\b(?:public|private|protected)?\s*(?:static\s+)?(?:final\s+)?[\w<>\[\], ?]+\s+(?P<name>[A-Za-z_]\w*)\s*\([^;]*\)\s*(?:throws\s+[\w.,\s]+)?\{?"), "Method"),
]


def _language_for_extension(extension: str) -> str:
    return {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".java": "java",
    }.get(extension.lower(), "")


def _line_at(text: str, line: int) -> str:
    lines = text.splitlines()
    if 0 <= line < len(lines):
        return lines[line]
    return ""


def _word_at(text: str, line: int, character: int) -> str:
    value = _line_at(text, line)
    if not value:
        return ""
    character = max(0, min(character, len(value)))
    left = character
    right = character
    while left > 0 and re.match(r"[\w$]", value[left - 1]):
        left -= 1
    while right < len(value) and re.match(r"[\w$]", value[right]):
        right += 1
    return value[left:right]
