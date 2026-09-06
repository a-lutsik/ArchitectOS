"""Agent tool-execution helpers used during tool-augmented AI runs.

Extracted from ``service.py``. Covers the filesystem/memory tool callbacks
(``fs_read``/``fs_list``/``fs_search``/``fs_write``, ``memory_search``/``memory_get``),
project file-reference resolution, the interactive terminal runners
(``terminal_run``/``terminal_open`` and their shell helpers), and the small
Azure Boards id helper. Depends only on shared constants and stdlib; repository
access and other service helpers are reached through ``self`` via the MRO on
:class:`ArchitectOSService`.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .constants import (
    FS_READ_MAX_CHARS,
    FS_READ_MAX_LINES,
    SOURCE_FILE_SUFFIXES,
    SYSTEM_PROJECT_ID,
    TERMINAL_DANGEROUS_PATTERNS,
    TERMINAL_DESTRUCTIVE_CONFIRM,
    TERMINAL_SHELLS,
)
from .permissions import PermissionRequired

_LOG = logging.getLogger("architectos.service")


class ToolExecServiceMixin:
    """Filesystem/memory agent tool callbacks and file-reference resolution."""

    @staticmethod
    def _boards_ids_from_memory_hits(hits: list[dict[str, Any]]) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        for hit in hits or []:
            node = hit.get("node") if isinstance(hit, dict) else None
            if not isinstance(node, dict):
                continue
            meta = dict(node.get("metadata") or {})
            candidates = [
                meta.get("work_item_id"),
                meta.get("workItemId"),
                meta.get("ado_work_item_id"),
            ]
            source = str(meta.get("source") or node.get("source") or "").lower()
            for value in candidates:
                text = str(value or "").strip()
                if not text:
                    continue
                if text not in seen:
                    seen.add(text)
                    ids.append(text)
            if "azure" in source or "boards" in source:
                label = str(node.get("label") or "")
                match = re.search(r"#?(\d{3,})", label)
                if match and match.group(1) not in seen:
                    seen.add(match.group(1))
                    ids.append(match.group(1))
        return ids[:12]

    def _filesystem_mcp_root(self) -> Path:
        workspace = self.repository.get_setting("workspace") or {}
        project_id = str(workspace.get("current_project_id") or "").strip()
        if not project_id:
            projects = self.repository.list_projects()
            for project in projects:
                if project.id != SYSTEM_PROJECT_ID and project.root_path:
                    project_id = project.id
                    break
        if project_id:
            try:
                return self._project_root(project_id)
            except Exception:
                pass
        return self.project_root

    def _path_outside_project(self, project_id: str, reference: str) -> Path | None:
        raw = str(reference or "").strip().strip("\"'")
        if not raw:
            return None
        root = self._project_root(project_id).resolve()
        expanded = Path(raw).expanduser()
        looks_escape = expanded.is_absolute() or raw.startswith("..") or "/../" in raw.replace("\\", "/")
        try:
            candidate = expanded.resolve() if looks_escape else (root / raw).resolve()
            candidate.relative_to(root)
            return None
        except (ValueError, OSError):
            try:
                return expanded.resolve() if looks_escape else (root / raw).resolve()
            except OSError:
                return expanded if looks_escape else None

    def _require_sandbox_permission(self, path: Path, action: str) -> None:
        target = str(path)
        broker = getattr(self, "permissions", None)
        if broker is not None and broker.is_allowed("sandbox", action, target):
            return
        raise PermissionRequired(
            "sandbox",
            action,
            target,
            f"This path is outside the project folder: {target}",
        )

    def _file_window_from_text(self, text: str, args: dict[str, Any], *, path_label: str, size: int) -> dict[str, Any]:
        lines = str(text or "").splitlines()
        total = len(lines)
        start = max(1, int(args.get("start_line") or args.get("from_line") or 1))
        end_arg = args.get("end_line") or args.get("to_line")
        end = int(end_arg) if end_arg else start + FS_READ_MAX_LINES - 1
        end = max(start, min(end, total))
        window: list[str] = []
        budget = FS_READ_MAX_CHARS
        for offset, line in enumerate(lines[start - 1 : end]):
            numbered = f"{start + offset}| {line}"
            if budget - len(numbered) < 0:
                end = start + offset - 1
                break
            budget -= len(numbered) + 1
            window.append(numbered)
        end = max(start - 1, min(end, total))
        return {
            "path": path_label,
            "text": "\n".join(window),
            "start_line": start,
            "end_line": end,
            "total_lines": total,
            "next_start_line": end + 1 if end < total else 0,
            "truncated": end < total,
            "size": size,
            "readable": True,
        }

    def _tool_fs_read(self, args: dict[str, Any]) -> dict[str, Any]:
        project_id = str(args.get("project_id") or "architectos")
        reference = args.get("path") or args.get("file") or args.get("class") or args.get("class_name") or ""
        outside = self._path_outside_project(project_id, str(reference))
        if outside is not None:
            self._require_sandbox_permission(outside, "read")
            if not outside.is_file():
                raise ValueError(f"file is not readable: {outside}")
            preview = self._read_text_preview(outside)
            payload = self._file_window_from_text(
                str(preview.get("text") or ""),
                args,
                path_label=str(outside),
                size=outside.stat().st_size,
            )
            payload["readable"] = bool(preview.get("readable", True))
            payload["outside"] = True
            return payload
        path = self._resolve_project_file_ref(project_id, str(reference))
        result = self.project_file(project_id, path)
        payload = self._file_window_from_text(
            str(result.get("text") or ""),
            args,
            path_label=str(result.get("path") or path),
            size=int(result.get("size") or 0),
        )
        payload["readable"] = result.get("readable")
        return payload

    def _resolve_project_file_ref(self, project_id: str, reference: str) -> str:
        """Resolve what a model or user typed into a project-relative file path.

        Accepts project-relative paths, absolute paths inside the project folder,
        and fully-qualified class names such as com.acme.chart.FooBuilder.
        """
        raw = str(reference or "").strip().strip("\"'")
        if not raw:
            raise ValueError("fs_read requires path")
        root = self._project_root(project_id)
        candidates = [raw.replace("\\", "/")]
        as_path = Path(raw)
        if as_path.is_absolute():
            try:
                candidates.append(str(as_path.resolve().relative_to(root.resolve())))
            except (OSError, ValueError):
                pass
        for candidate in candidates:
            cleaned = candidate.strip("/")
            if not cleaned:
                continue
            try:
                resolved = self._safe_project_path(root, cleaned)
            except ValueError:
                continue
            if resolved.is_file():
                return str(resolved.relative_to(root))
        name = self._class_name_from_file_ref(raw)
        matches = self._rank_file_ref_matches(self._search_project_files_by_name(root, name, 25), name) if name else []
        if matches:
            return matches[0]
        raise ValueError(f"no file under the project folder matches {raw}; use fs_search to locate it first")

    @staticmethod
    def _class_name_from_file_ref(reference: str) -> str:
        """Reduce a path or fully-qualified class name to a searchable file name."""
        token = str(reference or "").strip().replace("\\", "/").rstrip("/").split("/")[-1].strip()
        if not token:
            return ""
        if Path(token).suffix.lower() in SOURCE_FILE_SUFFIXES:
            return token
        parts = [part for part in token.split(".") if part]
        if not parts:
            return ""
        for part in reversed(parts):
            if part[:1].isupper():
                return part
        return parts[-1]

    @staticmethod
    def _rank_file_ref_matches(hits: list[dict[str, Any]], name: str) -> list[str]:
        """Prefer an exact stem match in production sources over tests and near misses."""
        stem = Path(name).stem.lower()
        ranked: list[tuple[int, int, int, str]] = []
        for hit in hits:
            path = str(hit.get("path") or "").replace("\\", "/")
            if not path:
                continue
            hit_stem = Path(path).stem.lower()
            is_test = "/test/" in f"/{path.lower()}" or hit_stem.endswith("test")
            ranked.append((0 if hit_stem == stem else 1, 1 if is_test else 0, len(path), path))
        ranked.sort()
        return [item[3] for item in ranked]

    def _tool_memory_search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or args.get("q") or args.get("text") or "").strip()
        if not query:
            raise ValueError("memory_search requires query")
        project_id = str(args.get("project_id") or "").strip() or None
        limit = max(1, min(int(args.get("limit") or 8), 20))
        search = self.search_memory(query, project_id=project_id, limit=limit, expand_graph=True)
        hits_out: list[dict[str, Any]] = []
        for hit in search.get("hits") or []:
            node = dict(hit.get("node") or {})
            text = str(node.get("text") or "")
            hits_out.append({
                "id": node.get("id"),
                "type": node.get("type"),
                "label": node.get("label"),
                "scope": node.get("scope"),
                "score": hit.get("score"),
                "text": text[:1200],
                "metadata": {
                    key: dict(node.get("metadata") or {}).get(key)
                    for key in ("work_item_id", "work_item_type", "work_item_state", "source", "source_type")
                    if dict(node.get("metadata") or {}).get(key) is not None
                },
            })
        return {"query": query, "count": len(hits_out), "hits": hits_out}

    def _tool_memory_get(self, args: dict[str, Any]) -> dict[str, Any]:
        node_id = str(args.get("id") or args.get("node_id") or "").strip()
        if not node_id:
            raise ValueError("memory_get requires id")
        node = self.repository.get_node(node_id)
        if not node or node.status not in {"active", "archived"}:
            raise ValueError(f"memory node not found: {node_id}")
        include_neighbors = bool(args.get("include_neighbors", True))
        payload = {
            "id": node.id,
            "type": node.type,
            "label": node.label,
            "scope": node.scope,
            "project_id": node.project_id,
            "status": node.status,
            "confidence": node.confidence,
            "text": node.text,
            "metadata": dict(node.metadata or {}),
            "evidence": list(node.evidence or []),
        }
        if include_neighbors:
            neighbors: list[dict[str, Any]] = []
            for edge in self.repository.list_edges():
                other = ""
                if edge.source == node.id:
                    other = edge.target
                elif edge.target == node.id:
                    other = edge.source
                if not other:
                    continue
                related = self.repository.get_node(other)
                if not related or related.status != "active":
                    continue
                neighbors.append({
                    "edge_type": edge.type,
                    "id": related.id,
                    "type": related.type,
                    "label": related.label,
                    "chunk_role": dict(related.metadata or {}).get("chunk_role"),
                    "text_preview": (related.text or "")[:400],
                })
                if len(neighbors) >= 12:
                    break
            payload["neighbors"] = neighbors
        try:
            self.memory_lifecycle.refresh_nodes([node.id], "memory_get")
        except Exception:
            pass
        try:
            self.repository.record_memory_event(node.id, "accessed", actor="tool", details={"include_neighbors": include_neighbors})
        except Exception:
            pass  # history is best-effort, never blocks reads
        return payload

    def _tool_fs_list(self, args: dict[str, Any]) -> dict[str, Any]:
        project_id = str(args.get("project_id") or "architectos")
        limit = max(1, min(int(args.get("limit") or 80), 200))
        relative = str(args.get("path") or "").strip().strip("./")
        outside = self._path_outside_project(project_id, relative) if relative else None
        if outside is not None:
            self._require_sandbox_permission(outside, "list")
            if not outside.is_dir():
                raise ValueError(f"directory is not readable: {outside}")
            files = []
            for item in sorted(outside.iterdir(), key=lambda path: path.name.lower())[:limit]:
                files.append({"path": str(item), "name": item.name, "dir": item.is_dir()})
            return {"project_id": project_id, "root": str(outside), "path": str(outside), "files": files, "count": len(files), "outside": True}
        listed = self.project_files(project_id, limit=limit)
        files = list(listed.get("files") or [])
        if relative:
            prefix = relative.replace("\\", "/").rstrip("/") + "/"
            files = [
                item for item in files
                if str(item.get("path") or "").replace("\\", "/") == relative
                or str(item.get("path") or "").replace("\\", "/").startswith(prefix)
            ]
        return {"project_id": project_id, "root": listed.get("root"), "path": relative or ".", "files": files[:limit], "count": min(len(files), limit)}

    def _tool_fs_search(self, args: dict[str, Any]) -> dict[str, Any]:
        project_id = str(args.get("project_id") or "architectos")
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("fs_search requires query")
        mode = str(args.get("mode") or "name")
        limit = max(1, min(int(args.get("limit") or 40), 100))
        if mode != "content":
            # A pasted class name or absolute path never matches a file name verbatim.
            derived = self._class_name_from_file_ref(query)
            if derived and derived.lower() != query.lower():
                result = self.project_search(project_id=project_id, query=derived, mode=mode, limit=limit)
                if result.get("hits"):
                    result["query"] = query
                    result["resolved_query"] = derived
                    return result
        return self.project_search(project_id=project_id, query=query, mode=mode, limit=limit)

    def _tool_fs_write(self, args: dict[str, Any]) -> dict[str, Any]:
        project_id = str(args.get("project_id") or "architectos")
        path = str(args.get("path") or "").strip()
        if not path:
            raise ValueError("fs_write requires path")
        text = args.get("text")
        if text is None:
            text = args.get("content")
        body = str(text if text is not None else "")
        outside = self._path_outside_project(project_id, path)
        if outside is not None:
            self._require_sandbox_permission(outside, "write")
            outside.parent.mkdir(parents=True, exist_ok=True)
            outside.write_text(body, encoding="utf-8")
            return {"success": True, "project_id": project_id, "path": str(outside), "size": outside.stat().st_size, "outside": True}
        return self.save_project_file({"project_id": project_id, "path": path, "text": body})


    # --- Terminal ----------------------------------------------------------

    def terminal_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        root = self._project_root(project_id)
        command = str(payload.get("command") or "").strip()
        if not command:
            raise ValueError("terminal command is required")
        risk = self._terminal_command_risk(command)
        allow_destructive = bool(payload.get("allow_destructive") or payload.get("approved"))
        confirm = str(payload.get("destructive_confirm") or "").strip()
        if risk and not (allow_destructive and confirm == TERMINAL_DESTRUCTIVE_CONFIRM):
            return {
                "project_id": project_id,
                "root": str(root),
                "command": command,
                "status": "blocked",
                "returncode": None,
                "stdout": "",
                "stderr": (
                    f"Blocked risky command: {risk}. "
                    f"To run it, set allow_destructive=true and destructive_confirm={TERMINAL_DESTRUCTIVE_CONFIRM!r}."
                ),
                "duration_ms": 0,
                "redacted": False,
                "risk": risk,
                "requires_confirm": TERMINAL_DESTRUCTIVE_CONFIRM,
            }
        shell_id = self._terminal_shell_id(str(payload.get("shell") or "auto"))
        shell = self._terminal_shell_command(shell_id)
        timeout = min(max(int(payload.get("timeout_seconds") or 20), 1), 120)
        started = datetime.now(timezone.utc)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        popen_kwargs: dict[str, Any] = {
            "cwd": str(root),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "env": env,
            "shell": False,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen([*shell, command], **popen_kwargs)
        except OSError as exc:
            return {
                "project_id": project_id,
                "root": str(root),
                "command": command,
                "shell": shell_id,
                "status": "unavailable",
                "returncode": None,
                "stdout": "",
                "stderr": f"Terminal shell failed: {exc}",
                "duration_ms": 0,
                "redacted": False,
            }
        timed_out = False
        returncode: int | None
        try:
            stdout_raw, stderr_raw = proc.communicate(timeout=timeout)
            returncode = proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            self._terminal_kill_tree(proc)
            try:
                drained_out, drained_err = proc.communicate(timeout=2)
            except Exception:
                self._terminal_abandon_pipes(proc)
                drained_out, drained_err = "", ""
            stdout_raw = f"{self._terminal_pipe_text(exc.stdout)}{self._terminal_pipe_text(drained_out)}"
            stderr_raw = f"{self._terminal_pipe_text(exc.stderr)}{self._terminal_pipe_text(drained_err)}"
            returncode = None
        duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        stdout_raw = self._terminal_pipe_text(stdout_raw)
        stderr_raw = self._terminal_pipe_text(stderr_raw)
        if timed_out and not stderr_raw.strip():
            stderr_raw = f"Command timed out after {timeout}s."
        if timed_out and self._terminal_looks_like_mcp_stdio(command) and not stdout_raw.strip():
            stderr_raw = (
                f"{stderr_raw.rstrip()}\n"
                "MCP stdio servers (npx …/server-filesystem) wait for JSON-RPC on stdin "
                "and usually print nothing here. Use SETUP → MCP → Test, or Open external shell."
            )
        stdout, stdout_redacted = self.security_policy.redact_text(stdout_raw[:80_000])
        stderr, stderr_redacted = self.security_policy.redact_text(stderr_raw[:20_000])
        if timed_out:
            status = "timeout"
        elif returncode == 0:
            status = "ok"
        else:
            status = "error"
        return {
            "project_id": project_id,
            "root": str(root),
            "command": command,
            "shell": shell_id,
            "status": status,
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": duration_ms if not timed_out else timeout * 1000,
            "redacted": stdout_redacted or stderr_redacted,
        }

    def terminal_open(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "architectos")
        root = self._project_root(project_id)
        commands = self._terminal_open_commands(root)
        errors = []
        for command in commands:
            try:
                subprocess.Popen(command, cwd=str(root), shell=False)
                return {"project_id": project_id, "root": str(root), "status": "opened", "command": command}
            except OSError as exc:
                errors.append(str(exc))
        return {"project_id": project_id, "root": str(root), "status": "unavailable", "message": "; ".join(errors[-3:]) or "No terminal launcher found."}

    def _terminal_shell_id(self, requested: str) -> str:
        requested = requested.lower().strip()
        if requested in TERMINAL_SHELLS:
            return requested
        if os.name == "nt":
            return "cmd" if shutil.which("cmd.exe") else "powershell"
        return "bash" if shutil.which("bash") else "sh"

    def _terminal_shell_command(self, shell_id: str) -> list[str]:
        command = TERMINAL_SHELLS.get(shell_id) or TERMINAL_SHELLS[self._terminal_shell_id("auto")]
        executable = shutil.which(command[0])
        if not executable:
            raise OSError(f"shell executable not found: {command[0]}")
        return [executable, *command[1:]]

    @staticmethod
    def _terminal_kill_tree(proc: subprocess.Popen[str]) -> None:
        """Kill the shell and any npx/node grandchildren that inherited the pipes."""
        if proc.poll() is not None:
            return
        pid = proc.pid
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=8,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            proc.kill()
        except OSError:
            pass

    @staticmethod
    def _terminal_abandon_pipes(proc: subprocess.Popen[str]) -> None:
        for stream in (proc.stdout, proc.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass

    @staticmethod
    def _terminal_pipe_text(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    @staticmethod
    def _terminal_looks_like_mcp_stdio(command: str) -> bool:
        lowered = command.lower()
        return "mcp" in lowered or "modelcontextprotocol" in lowered or "server-filesystem" in lowered

    def _terminal_command_risk(self, command: str) -> str:
        normalized = command.strip()
        for pattern in TERMINAL_DANGEROUS_PATTERNS:
            match = pattern.search(normalized)
            if match:
                return match.group(0)
        return ""

    def _terminal_open_commands(self, root: Path) -> list[list[str]]:
        if os.name == "nt":
            commands = []
            wt = shutil.which("wt.exe") or shutil.which("wt")
            if wt:
                commands.append([wt, "-d", str(root)])
            powershell = shutil.which("powershell.exe")
            if powershell:
                literal_root = str(root).replace("'", "''")
                commands.append([powershell, "-NoLogo", "-NoExit", "-Command", f"Set-Location -LiteralPath '{literal_root}'"])
            cmd = shutil.which("cmd.exe")
            if cmd:
                commands.append([cmd, "/k", f"cd /d {root}"])
            return commands
        candidates = [
            ("x-terminal-emulator", ["-e", "sh", "-lc", f"cd {shlex.quote(str(root))}; exec $SHELL"]),
            ("gnome-terminal", ["--working-directory", str(root)]),
            ("konsole", ["--workdir", str(root)]),
            ("open", ["-a", "Terminal", str(root)]),
        ]
        return [[path, *args] for executable, args in candidates if (path := shutil.which(executable))]
