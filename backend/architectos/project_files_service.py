"""Project profile management and project-file browse/search/read helpers.

Split out of ``project_scan_service``. Composed into ``ProjectScanServiceMixin``
so agent tools and the UI share one file I/O surface via the service MRO.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .constants import (
    CODE_EXTENSIONS,
    CODE_FILE_MAX_BYTES,
    DEFAULT_EXCLUDES,
    SYSTEM_PROJECT_ID,
    TEXT_EXTENSIONS,
)
from .models import Project, stable_id
from .project_files import delete_project_file as project_files_delete
from .project_files import safe_project_path
from .project_files import save_project_file as project_files_save

_LOG = logging.getLogger("architectos.service")


class ProjectFilesMixin:
    """Create/list projects and browse/search/read files under a project root."""

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

    def projects(self) -> list[dict[str, Any]]:
        return [project.to_dict() for project in self.repository.list_projects()]

    def create_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not str(payload.get("root_path") or "").strip():
            raise ValueError("project folder path is required")
        project, created = self._upsert_project_profile(payload)
        data = project.to_dict()
        action = "created" if created else "existing"
        message = (
            f"Project {project.name} was created successfully."
            if created
            else f"Project {project.name} already exists. Switched to the existing project."
        )
        return {**data, "project_id": project.id, "project": data, "created": created, "action": action, "message": message}

    def _upsert_project_profile(self, payload: dict[str, Any]) -> tuple[Project, bool]:
        root_raw = str(payload.get("root_path") or "").strip()
        name = str(payload.get("name") or "").strip()
        description = str(payload.get("description") or "").strip()
        incoming_config = dict(payload.get("config") or {})
        root_path = ""
        if root_raw:
            root = self._validate_project_root(root_raw)
            root_path = str(root)
            name = name or root.name or "Project"
        if not name:
            raise ValueError("project name is required")
        existing = self._find_project_by_root(root_path) if root_path else None
        if existing:
            existing.name = name
            existing.description = description or existing.description
            existing.root_path = root_path
            merged_config = dict(existing.config or {})
            merged_config.update(incoming_config)
            existing.config = merged_config
            project = self.repository.upsert_project(existing)
            created = False
        else:
            project_id = stable_id("project", root_path) if root_path else stable_id("project", name, root_path)
            project = self.repository.upsert_project(Project(id=project_id, name=name, root_path=root_path, description=description, config=incoming_config))
            created = True
        self._ensure_project_memory_root(project)
        return project, created

    def _find_project_by_root(self, root_path: str) -> Project | None:
        if not root_path:
            return None
        try:
            wanted = Path(root_path).resolve()
        except OSError:
            return None
        for project in self.repository.list_projects():
            if not project.root_path:
                continue
            try:
                if Path(project.root_path).resolve() == wanted:
                    return project
            except OSError:
                continue
        return None

    def _validate_project_root(self, root_path: str) -> Path:
        root = Path(root_path).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError(f"project path does not exist: {root}")
        return root

    def _ensure_project_memory_root(self, project: Project) -> None:
        if project.id == SYSTEM_PROJECT_ID and not project.root_path and self._is_architectos_source_root():
            return
        text = f"Project profile for {project.name}. Root: {project.root_path}."
        for node in self.repository.list_nodes():
            if node.type == "Project" and node.project_id == project.id:
                node.label = project.name
                node.text = text
                node.scope = "project"
                node.metadata["project_root"] = project.root_path
                node.metadata["project_config"] = dict(project.config or {})
                self.repository.upsert_node(node)
                return
        node = self.repository.add_node(
            "Project",
            project.name,
            "project",
            text,
            project.id,
            confidence=0.9,
            metadata={"source": "project_profile", "project_root": project.root_path, "project_config": dict(project.config or {})},
        )
        self.memory_lifecycle.initialize_node(node, "project_profile")

    def _is_architectos_source_root(self) -> bool:
        return (
            (self.project_root / "backend" / "architectos" / "service.py").is_file()
            and (self.project_root / "frontend" / "app.js").is_file()
        )

    def _empty_system_project_response(self, project_id: str, collection: str, message: str) -> dict[str, Any]:
        return {"project_id": project_id, "root": "", collection: [], "count": 0, "message": message}

    def list_files(self, project_id: str | None = None) -> dict[str, Any]:
        return {"files": self.files.list_files(str(project_id or "architectos"))}

    def upload_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        meta = self.files.upload(project_id, str(payload.get("name") or "file"), str(payload.get("content") or ""))
        return {"file": meta, "files": self.files.list_files(project_id)}

    def add_files_to_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        file_ids = [str(item) for item in (payload.get("file_ids") or []) if str(item).strip()]
        if not file_ids:
            raise ValueError("at least one file is required")
        scope = str(payload.get("scope") or "project")
        node_type = str(payload.get("type") or "Doc")
        imported = []
        skipped: list[dict[str, Any]] = []
        for file_id in file_ids[:20]:
            meta, text = self.files.get_text(project_id, file_id)
            if not meta:
                skipped.append({"id": file_id, "reason": "file not found"})
                continue
            if not meta.get("text_extracted") or not text.strip():
                skipped.append({"id": file_id, "name": meta.get("name"), "reason": "text could not be extracted"})
                continue
            clean_name = str(meta.get("name") or "file")
            body = (
                f"Uploaded file: {clean_name}\n"
                f"MIME: {meta.get('mime')}\n"
                f"Size: {meta.get('size')} bytes\n\n"
                f"{text.strip()[:12000]}"
            )
            node = self.repository.add_node(
                node_type,
                f"File: {clean_name}",
                scope,
                body,
                self._memory_project_id_for_scope(scope, project_id),
                confidence=float(payload.get("confidence") or 0.7),
                metadata={
                    "source": "memory_file_upload",
                    "file_id": file_id,
                    "file_name": clean_name,
                    "mime": meta.get("mime"),
                    "size": meta.get("size"),
                },
            )
            node = self.memory_lifecycle.initialize_node(node, "memory_file_upload")
            self.graph_auto_linker.link_node(node, project_id)
            imported.append(node.to_dict())
        return {
            "project_id": project_id,
            "imported": imported,
            "skipped": skipped,
            "count": len(imported),
            "files": self.files.list_files(project_id),
        }

    def delete_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        deleted = self.files.delete(project_id, str(payload.get("id") or ""))
        return {"deleted": deleted, "files": self.files.list_files(project_id)}

    def project_files(self, project_id: str | None = None, limit: int = 80) -> dict[str, Any]:
        project_id = project_id or "architectos"
        try:
            root = self._project_root(project_id)
        except ValueError as exc:
            project = self.repository.get_project(project_id)
            configured_root = str(project.root_path).strip() if project and project.root_path else ""
            status = "missing_root" if configured_root else "no_root"
            if status == "missing_root":
                message = "This project's folder isn't available on this computer. Update its path or connect another folder."
            else:
                message = "No project folder connected yet. Connect a folder to index files and memory."
            payload = self._empty_system_project_response(project_id, "files", message)
            payload["status"] = status
            payload["configured_root"] = configured_root
            payload["detail"] = str(exc)
            return payload
        files = []
        for path in self._iter_project_tree_entries(root, limit):
            stat = path.stat()
            relative = path.relative_to(root)
            files.append({
                "path": str(relative),
                "name": path.name,
                "type": "folder" if path.is_dir() else "file",
                "extension": path.suffix.lower(),
                "size": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            })
        return {"project_id": project_id, "root": str(root), "files": files, "count": len(files), "status": "ok"}

    def project_search(
        self,
        project_id: str | None = None,
        query: str = "",
        mode: str = "name",
        limit: int = 80,
        mask: str = "",
    ) -> dict[str, Any]:
        project_id = project_id or "architectos"
        query = str(query or "").strip()
        mode = "content" if str(mode or "").strip().lower() in {"content", "text", "grep", "in_files"} else "name"
        limit = max(1, min(int(limit or 80), 200))
        masks = self._parse_file_search_masks(mask)
        if not query and not (mode == "name" and masks):
            return {
                "project_id": project_id,
                "query": "",
                "mode": mode,
                "mask": self._format_file_search_masks(masks),
                "hits": [],
                "count": 0,
                "status": "empty_query",
            }
        root = self._project_root(project_id)
        if mode == "name":
            hits = self._search_project_files_by_name(root, query, limit, masks=masks)
        else:
            hits = self._search_project_files_by_content(root, query, limit, masks=masks)
        return {
            "project_id": project_id,
            "root": str(root),
            "query": query,
            "mode": mode,
            "mask": self._format_file_search_masks(masks),
            "hits": hits,
            "count": len(hits),
            "status": "ok",
        }

    def _parse_file_search_masks(self, mask: str | None) -> list[str]:
        raw = str(mask or "").strip()
        if not raw or raw in {"*", "*.*", "**/*"}:
            return []
        parts = [part.strip() for part in re.split(r"[,;]+", raw) if part.strip()]
        cleaned: list[str] = []
        for part in parts:
            if part in {"*", "*.*", "**/*"}:
                continue
            cleaned.append(part.replace("\\", "/"))
        return cleaned

    def _format_file_search_masks(self, masks: list[str]) -> str:
        return ", ".join(masks)

    def _path_matches_file_masks(self, relative: str, masks: list[str]) -> bool:
        if not masks:
            return True
        relative = relative.replace("\\", "/")
        name = Path(relative).name
        for pattern in masks:
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relative, pattern):
                return True
            if "/" not in pattern and not pattern.startswith("*"):
                if fnmatch.fnmatch(name, f"*{pattern}*") or fnmatch.fnmatch(relative, f"*{pattern}*"):
                    return True
        return False

    def _search_project_files_by_name(
        self,
        root: Path,
        query: str,
        limit: int,
        masks: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        needle = query.lower()
        masks = masks or []
        hits: list[dict[str, Any]] = []
        for path in self._iter_project_files_for_search(root, max_files=25_000):
            rel = str(path.relative_to(root)).replace("\\", "/")
            if not self._path_matches_file_masks(rel, masks):
                continue
            name = path.name
            if needle and needle not in name.lower() and needle not in rel.lower():
                continue
            hits.append({
                "path": rel,
                "name": name,
                "match": "name",
                "score": (2 if needle and needle in name.lower() else 1) if needle else 1,
                "line": 0,
                "snippet": rel,
            })
            if len(hits) >= limit:
                break
        hits.sort(key=lambda item: (-int(item.get("score") or 0), str(item.get("path") or "")))
        return hits

    def _search_project_files_by_content(
        self,
        root: Path,
        query: str,
        limit: int,
        masks: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        masks = masks or []
        rg_hits = self._search_project_files_by_content_rg(root, query, limit, masks=masks)
        if rg_hits is not None:
            return rg_hits
        return self._search_project_files_by_content_walk(root, query, limit, masks=masks)

    def _search_project_files_by_content_rg(
        self,
        root: Path,
        query: str,
        limit: int,
        masks: list[str] | None = None,
    ) -> list[dict[str, Any]] | None:
        masks = masks or []
        command = [
            "rg",
            "--json",
            "--max-count", "3",
            "--max-filesize", "400K",
            "-i",
            "-F",
            "--glob", "!**/.git/**",
            "--glob", "!**/node_modules/**",
            "--glob", "!**/target/**",
            "--glob", "!**/dist/**",
            "--glob", "!**/build/**",
        ]
        for pattern in masks:
            command.extend(["--glob", pattern])
        command.extend([query, str(root)])
        try:
            proc = subprocess.run(command, text=True, capture_output=True, timeout=12, shell=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode not in {0, 1}:
            return None
        hits: list[dict[str, Any]] = []
        seen: set[str] = set()
        for line in (proc.stdout or "").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            abs_path = Path(str((data.get("path") or {}).get("text") or ""))
            try:
                rel = str(abs_path.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            if any(part in DEFAULT_EXCLUDES for part in Path(rel).parts):
                continue
            if not self._path_matches_file_masks(rel, masks):
                continue
            if rel in seen:
                continue
            lines = data.get("lines") or {}
            text_line = str(lines.get("text") or "").rstrip("\n")
            line_no = int(((data.get("line_number") or 0) or 0))
            snippet = text_line.strip()
            if len(snippet) > 180:
                snippet = snippet[:177] + "…"
            hits.append({
                "path": rel,
                "name": Path(rel).name,
                "match": "content",
                "score": 1,
                "line": line_no,
                "snippet": snippet,
            })
            seen.add(rel)
            if len(hits) >= limit:
                break
        return hits

    def _search_project_files_by_content_walk(
        self,
        root: Path,
        query: str,
        limit: int,
        masks: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        needle = query.lower()
        masks = masks or []
        hits: list[dict[str, Any]] = []
        scanned = 0
        for path in self._iter_project_files_for_search(root, max_files=8_000):
            scanned += 1
            rel = str(path.relative_to(root)).replace("\\", "/")
            if not self._path_matches_file_masks(rel, masks):
                continue
            suffix = path.suffix.lower()
            if suffix and suffix not in TEXT_EXTENSIONS and suffix not in CODE_EXTENSIONS:
                if not self._path_looks_text(path):
                    continue
            try:
                if path.stat().st_size > CODE_FILE_MAX_BYTES:
                    continue
            except OSError:
                continue
            preview = self._read_text_preview(path, max_bytes=120_000)
            if not preview["readable"]:
                continue
            text = str(preview["text"] or "")
            lower = text.lower()
            pos = lower.find(needle)
            if pos < 0:
                continue
            line_no = lower.count("\n", 0, pos) + 1
            line_start = text.rfind("\n", 0, pos) + 1
            line_end = text.find("\n", pos)
            if line_end < 0:
                line_end = min(len(text), pos + 160)
            snippet = text[line_start:line_end].strip()
            if len(snippet) > 180:
                snippet = snippet[:177] + "…"
            hits.append({
                "path": rel,
                "name": path.name,
                "match": "content",
                "score": 1,
                "line": line_no,
                "snippet": snippet,
            })
            if len(hits) >= limit:
                break
        return hits

    def _iter_project_files_for_search(self, root: Path, max_files: int = 10_000):
        count = 0
        for current_root, dir_names, file_names in os.walk(root):
            dir_names[:] = sorted(name for name in dir_names if name not in DEFAULT_EXCLUDES)
            current = Path(current_root)
            for name in sorted(file_names):
                if name in DEFAULT_EXCLUDES:
                    continue
                yield current / name
                count += 1
                if count >= max_files:
                    return

    def project_file(self, project_id: str | None, relative_path: str) -> dict[str, Any]:
        project_id = project_id or "architectos"
        root = self._project_root(project_id)
        path = self._safe_project_path(root, relative_path)
        if not path.is_file():
            raise ValueError("file is not readable by ArchitectOS")
        preview = self._read_text_preview(path)
        clean = str(preview["text"])
        return {
            "project_id": project_id,
            "root": str(root),
            "path": str(path.relative_to(root)),
            "text": clean,
            "readable": bool(preview["readable"]),
            "binary": bool(preview["binary"]),
            "encoding": preview["encoding"],
            "truncated": bool(preview["truncated"]),
            "message": preview["message"],
            "redacted": bool(preview["redacted"]),
            "size": path.stat().st_size,
            "lines": clean.count("\n") + (1 if clean else 0),
            "summary": self._summarize_clean_file(path, root, clean) if preview["readable"] else str(preview["message"]),
        }

    def selected_file_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        query = str(payload.get("query") or "selected file context").strip()
        paths = [str(item) for item in (payload.get("paths") or []) if str(item).strip()]
        if not paths and payload.get("path"):
            paths = [str(payload["path"])]
        if not paths:
            raise ValueError("at least one selected file path is required")
        files = [self.project_file(project_id, item) for item in paths[:6]]
        memory_context = self.context(query, project_id=project_id, limit=int(payload.get("limit") or 6))
        diff = self.project_git_diff(project_id)
        lines = [
            "ArchitectOS Selected File Context",
            f"Project: {project_id}",
            f"Query: {query}",
            "",
            "Selected Files:",
        ]
        for item in files:
            lines.append(f"- {item['path']} ({item['lines']} lines, {item['size']} bytes)")
        lines.extend(["", memory_context["context"], "", "File Contents:"])
        for item in files:
            content = item["text"][:12000] if item.get("readable", True) else item.get("message", "Preview unavailable.")
            lines.append(f"\n--- {item['path']} ---\n{content}")
        if diff.get("diff"):
            lines.append(f"\nGit Diff:\n{diff['diff'][:20000]}")
        return {"project_id": project_id, "query": query, "files": files, "git_diff": diff, "context": "\n".join(lines).strip()}

    def _project_root(self, project_id: str) -> Path:
        project = self.repository.get_project(project_id)
        if project and project.root_path:
            root = self._validate_project_root(str(project.root_path))
        elif project and self._is_architectos_source_root():
            raise ValueError(f"{project.name} has no project folder. Select a folder before indexing files.")
        else:
            root = self._validate_project_root(str(self.project_root))
        if project:
            self._ensure_project_memory_root(project)
        return root

    def _safe_project_path(self, root: Path, relative_path: str) -> Path:
        return safe_project_path(root, relative_path)
