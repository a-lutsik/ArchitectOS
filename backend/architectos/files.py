"""Per-project file attachments used as chat context.

Files are stored on disk under ``<data>/uploads/<project>/`` with a small JSON
index per project. Text is extracted best-effort so attachments can be folded
into the prompt context. Pure stdlib, no external dependencies.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import secrets
import time
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 2_000_000
MAX_TEXT_CHARS = 200_000
DEFAULT_PER_FILE_CONTEXT_CHARS = 8_000

TEXT_LIKE_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".rst", ".log", ".csv", ".tsv",
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".jsonc",
    ".yml", ".yaml", ".toml", ".ini", ".env", ".cfg", ".conf",
    ".html", ".htm", ".css", ".scss", ".xml", ".svg", ".sql", ".sh", ".bash", ".zsh",
    ".java", ".kt", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift",
    ".gradle", ".properties", ".dockerfile", ".makefile", ".gitignore",
}


def _safe_name(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_.\- ]+", "_", (name or "").strip()).strip()
    base = base.replace(" ", "_")
    return base[:120] or "file"


def _safe_project(project_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", (project_id or "architectos").strip()) or "architectos"


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


# Public alias for callers outside this module (e.g. the inbox ingestion source).
decode_text = _decode_text


class FileStore:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _project_dir(self, project_id: str) -> Path:
        directory = self.base_dir / _safe_project(project_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _index_path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "index.json"

    def _load_index(self, project_id: str) -> list[dict[str, Any]]:
        path = self._index_path(project_id)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text("utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save_index(self, project_id: str, items: list[dict[str, Any]]) -> None:
        self._index_path(project_id).write_text(json.dumps(items, indent=2), "utf-8")

    def list_files(self, project_id: str) -> list[dict[str, Any]]:
        return self._load_index(project_id)

    def upload(self, project_id: str, name: str, content_b64: str) -> dict[str, Any]:
        if not (content_b64 or "").strip():
            raise ValueError("file content is required")
        payload = content_b64.split(",", 1)[-1].strip()
        try:
            raw = base64.b64decode(payload, validate=False)
        except (ValueError, TypeError) as exc:
            raise ValueError("file content is not valid base64") from exc
        if not raw:
            raise ValueError("file is empty")
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError(f"file too large (max {MAX_FILE_BYTES // 1000} KB)")

        display_name = (name or "file").strip() or "file"
        safe_name = _safe_name(display_name)
        ext = Path(safe_name).suffix.lower()
        file_id = f"{int(time.time() * 1000):x}{secrets.token_hex(3)}"
        stored_name = f"{file_id}__{safe_name}"
        (self._project_dir(project_id) / stored_name).write_bytes(raw)

        text = ""
        if ext in TEXT_LIKE_EXTENSIONS or not ext:
            text = _decode_text(raw)[:MAX_TEXT_CHARS]

        meta = {
            "id": file_id,
            "name": display_name,
            "stored": stored_name,
            "size": len(raw),
            "ext": ext,
            "mime": mimetypes.guess_type(display_name)[0] or "application/octet-stream",
            "chars": len(text),
            "text_extracted": bool(text),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        items = self._load_index(project_id)
        items.insert(0, meta)
        self._save_index(project_id, items)
        return meta

    def get_text(self, project_id: str, file_id: str) -> tuple[dict[str, Any] | None, str]:
        meta = next((item for item in self._load_index(project_id) if item.get("id") == file_id), None)
        if not meta:
            return None, ""
        path = self._project_dir(project_id) / str(meta.get("stored") or "")
        if not path.exists():
            return meta, ""
        return meta, _decode_text(path.read_bytes())[:MAX_TEXT_CHARS]

    def delete(self, project_id: str, file_id: str) -> bool:
        items = self._load_index(project_id)
        meta = next((item for item in items if item.get("id") == file_id), None)
        if not meta:
            return False
        try:
            (self._project_dir(project_id) / str(meta.get("stored") or "")).unlink()
        except FileNotFoundError:
            pass
        self._save_index(project_id, [item for item in items if item.get("id") != file_id])
        return True

    def is_image(self, meta: dict[str, Any]) -> bool:
        return str(meta.get("mime") or "").startswith("image/")

    def image_payload(self, project_id: str, file_ids: list[str], limit: int = 6) -> list[dict[str, Any]]:
        index = {item.get("id"): item for item in self._load_index(project_id)}
        payload: list[dict[str, Any]] = []
        for file_id in file_ids or []:
            meta = index.get(str(file_id))
            if not meta or not self.is_image(meta):
                continue
            path = self._project_dir(project_id) / str(meta.get("stored") or "")
            if not path.exists():
                continue
            payload.append({
                "name": meta.get("name"),
                "mime": meta.get("mime"),
                "b64": base64.b64encode(path.read_bytes()).decode("ascii"),
            })
            if len(payload) >= limit:
                break
        return payload

    def context_for(self, project_id: str, file_ids: list[str], per_file_chars: int = DEFAULT_PER_FILE_CONTEXT_CHARS) -> str:
        blocks: list[str] = []
        for file_id in file_ids or []:
            meta, text = self.get_text(project_id, str(file_id))
            if not meta:
                continue
            body = text.strip()[:per_file_chars] if text else "(binary or non-text file; content not included)"
            blocks.append(f"### Attached file: {meta.get('name')} ({meta.get('mime')}, {meta.get('size')} bytes)\n{body}")
        return "\n\n".join(blocks)
