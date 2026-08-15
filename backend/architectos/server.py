from __future__ import annotations

import hmac
import json
import logging
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import parse_qs, urlparse

from .config import ACCESS_LOG
from .paths import resolve_frontend_root, resolve_project_root
from .service import ArchitectOSService

PROJECT_ROOT = resolve_project_root()
FRONTEND_ROOT = resolve_frontend_root()

_LOG = logging.getLogger(__name__)

# The OAuth callback is a top-level browser redirect from the identity
# provider, so the SPA cannot attach the API token to it.
TOKEN_EXEMPT_PATHS = {"/api/mcp/oauth/callback"}
ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost", "[::1]"}
TOKEN_PLACEHOLDER = '<meta name="architectos-token" content="">'


class ArchitectOSHandler(SimpleHTTPRequestHandler):
    # Created lazily on first use: instantiating the service at import time
    # would create data dirs, an SQLite DB, and daemon threads for a mere
    # `import backend.architectos.server` (and leak state across tests).
    service: ArchitectOSService | None = None

    @classmethod
    def get_service(cls) -> ArchitectOSService:
        if cls.service is None:
            cls.service = ArchitectOSService(PROJECT_ROOT)
        return cls.service

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND_ROOT), **kwargs)

    def _server_port(self) -> int:
        """Port the server bound to; ``self.server`` is the ThreadingHTTPServer from main()."""
        return cast(ThreadingHTTPServer, self.server).server_port

    def _authorize_api(self, path: str) -> bool:
        """Guard /api/* against cross-site requests and DNS rebinding.

        Returns True when the request may proceed; otherwise sends 403.
        """
        if path in TOKEN_EXEMPT_PATHS:
            return True
        host = (self.headers.get("Host") or "").strip()
        hostname, _, host_port = host.rpartition(":")
        if hostname not in ALLOWED_HOSTNAMES or (host_port and host_port != str(self._server_port())):
            self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return False
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or (parsed.port is not None and parsed.port != self._server_port()):
                self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
                return False
        token = self.headers.get("X-ArchitectOS-Token") or ""
        if not hmac.compare_digest(token, self.get_service().auth_token):
            self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return False
        return True

    def _serve_index(self) -> None:
        index_path = FRONTEND_ROOT / "index.html"
        html = index_path.read_text(encoding="utf-8")
        if TOKEN_PLACEHOLDER in html:
            html = html.replace(TOKEN_PLACEHOLDER, f'<meta name="architectos-token" content="{self.get_service().auth_token}">', 1)
        self._html(html)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._serve_index()
            return
        if parsed.path.startswith("/api/") and not self._authorize_api(parsed.path):
            return
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/health":
                self._json(self.get_service().health())
            elif parsed.path == "/api/ready":
                readiness = self.get_service().readiness()
                self._json(readiness, HTTPStatus.OK if readiness.get("ok") else HTTPStatus.SERVICE_UNAVAILABLE)
            elif parsed.path == "/api/version":
                self._json(self.get_service().version())
            elif parsed.path == "/api/ops/backups":
                self._json(self.get_service().list_backups(int(self._first(query, "limit") or "20")))
            elif parsed.path == "/api/projects":
                self._json({"projects": self.get_service().projects()})
            elif parsed.path == "/api/memory/candidates":
                self._json(self.get_service().list_memory_candidates(self._first(query, "project_id") or None, self._first(query, "status") or "candidate", int(self._first(query, "limit") or "50")))
            elif parsed.path == "/api/memory/rescan":
                self._json(self.get_service().memory_rescan_status())
            elif parsed.path == "/api/memory/ingest":
                self._json(self.get_service().memory_ingest_status())
            elif parsed.path == "/api/memory/items":
                self._json(self.get_service().list_memory_items(
                    project_id=self._first(query, "project_id") or None,
                    lifecycle_state=self._first(query, "lifecycle_state") or None,
                    tier=self._first(query, "tier") or None,
                    status=self._first(query, "status") or None,
                    limit=int(self._first(query, "limit") or "50"),
                ))
            elif parsed.path == "/api/memory/search":
                refresh_raw = (self._first(query, "refresh") or "0").strip().lower()
                refresh = refresh_raw in {"1", "true", "yes", "on"}
                filters = None
                filters_raw = (self._first(query, "filters") or "").strip()
                if filters_raw:
                    try:
                        parsed_filters = json.loads(filters_raw)
                        filters = parsed_filters if isinstance(parsed_filters, dict) else None
                    except ValueError:
                        filters = None
                def _opt_float(raw: str) -> float | None:
                    raw = (raw or "").strip()
                    try:
                        return float(raw) if raw else None
                    except ValueError:
                        return None
                min_score_raw = self._first(query, "min_score")
                floor_raw = self._first(query, "relevance_floor")
                self._json(self.get_service().search_memory(
                    self._first(query, "query"),
                    self._first(query, "project_id") or None,
                    self._first(query, "scope") or None,
                    int(self._first(query, "limit") or "8"),
                    refresh=refresh,
                    filters=filters,
                    mode=self._first(query, "mode") or "search",
                    as_of=self._first(query, "as_of") or None,
                    min_score=_opt_float(min_score_raw),
                    relevance_floor=_opt_float(floor_raw),
                ))
            elif parsed.path == "/api/memory/feedback":
                self._json(self.get_service().list_retrieval_feedback(self._first(query, "project_id") or None, int(self._first(query, "limit") or "50")))
            elif parsed.path == "/api/memory/embeddings":
                self._json(self.get_service().memory_embeddings_status(self._first(query, "project_id") or None))
            elif parsed.path == "/api/context":
                self._json(self.get_service().context(self._first(query, "query"), self._first(query, "project_id") or None, self._first(query, "scope") or None, int(self._first(query, "limit") or "8")))
            elif parsed.path == "/api/graph/path":
                self._json(self.get_service().explain_graph_path(self._first(query, "source"), self._first(query, "target")))
            elif parsed.path == "/api/graph":
                self._json(self.get_service().graph(
                    project_id=self._first(query, "project_id") or None,
                    task_id=self._first(query, "task_id") or None,
                    provider_id=self._first(query, "provider_id") or None,
                    pinned=(self._first(query, "pinned") == "1"),
                    node_type=self._first(query, "type") or None,
                    source=self._first(query, "source") or None,
                    scope=self._first(query, "scope") or None,
                    limit=int(self._first(query, "limit") or "200"),
                    search=self._first(query, "q") or self._first(query, "search") or None,
                ))
            elif parsed.path == "/api/project/files":
                self._json(self.get_service().project_files(self._first(query, "project_id") or None, int(self._first(query, "limit") or "80")))
            elif parsed.path == "/api/project/search":
                self._json(self.get_service().project_search(
                    self._first(query, "project_id") or None,
                    self._first(query, "q") or self._first(query, "query") or "",
                    mode=self._first(query, "mode") or "name",
                    limit=int(self._first(query, "limit") or "80"),
                    mask=self._first(query, "mask") or self._first(query, "file_mask") or "",
                ))
            elif parsed.path == "/api/project/file":
                self._json(self.get_service().project_file(self._first(query, "project_id") or None, self._first(query, "path")))
            elif parsed.path == "/api/project/git-diff":
                self._json(self.get_service().project_git_diff(self._first(query, "project_id") or None, self._first(query, "path") or None))
            elif parsed.path == "/api/analytics":
                self._json(self.get_service().analytics(project_id=self._first(query, "project_id") or None))
            elif parsed.path == "/api/tasks":
                self._json(self.get_service().list_tasks(project_id=self._first(query, "project_id") or None))
            elif parsed.path == "/api/chats":
                self._json(self.get_service().list_chats(project_id=self._first(query, "project_id") or None, limit=int(self._first(query, "limit") or "80")))
            elif parsed.path.startswith("/api/chats/") and parsed.path.endswith("/context-pack"):
                self._json(self.get_service().chat_context_pack(parsed.path.split("/")[3], self._first(query, "q") or self._first(query, "query") or "", self._first(query, "project_id") or None))
            elif parsed.path.startswith("/api/chats/") and parsed.path.endswith("/keeper-events"):
                chat_id = parsed.path.split("/")[3]
                self._json({"events": self.get_service().repository.list_keeper_events(self._first(query, "project_id") or None, chat_id, int(self._first(query, "limit") or "30"))})
            elif parsed.path.startswith("/api/chats/"):
                self._json({"chat": self.get_service().get_chat(parsed.path.split("/")[3])})
            elif parsed.path == "/api/provider-runs":
                self._json(self.get_service().provider_runs(project_id=self._first(query, "project_id") or None, limit=int(self._first(query, "limit") or "25")))
            elif parsed.path == "/api/workflows":
                self._json(self.get_service().workflows())
            elif parsed.path == "/api/router/settings":
                self._json(self.get_service().router_settings())
            elif parsed.path == "/api/council":
                self._json(self.get_service().council_config())
            elif parsed.path == "/api/mcp/servers":
                self._json(self.get_service().mcp_servers())
            elif parsed.path == "/api/mcp/oauth/callback":
                result = self.get_service().complete_mcp_auth(query)
                label = self._escape_html(result.get("label") or result.get("id") or "MCP server")
                message = self._escape_html(result.get("message") or "MCP authorization completed.")
                server_id = self._escape_html(result.get("id") or "")
                origin = json.dumps(self._request_origin())
                self._html(
                    "<!doctype html><meta charset='utf-8'>"
                    "<title>ArchitectOS MCP Authorization</title>"
                    "<body style=\"font-family:system-ui,sans-serif;padding:24px;max-width:520px\">"
                    f"<h1>{message}</h1>"
                    f"<p><strong>{label}</strong> is authorized. You can close this window and click <em>Test</em> in ArchitectOS.</p>"
                    "<script>"
                    f"try{{window.opener&&window.opener.postMessage({{type:'mcp-auth-complete',id:{json.dumps(result.get('id') or '')}}},{origin});}}catch(e){{}}"
                    "setTimeout(function(){window.close();},1200);"
                    "</script>"
                    "</body>"
                )
            elif parsed.path.startswith("/api/mcp/servers/") and parsed.path.endswith("/tools"):
                self._json(self.get_service().mcp_tools(parsed.path.split("/")[4]))
            elif parsed.path == "/api/code/servers":
                self._json(self.get_service().code_intel_servers())
            elif parsed.path == "/api/code/languages":
                self._json(self.get_service().code_languages(self._first(query, "project_id") or None))
            elif parsed.path == "/api/files":
                self._json(self.get_service().list_files(self._first(query, "project_id") or None))
            elif parsed.path in {"/api/providers", "/api/router"}:
                self._json(self.get_service().providers())
            elif parsed.path.startswith("/api/providers/") and parsed.path.endswith("/models"):
                self._json(self.get_service().provider_models(parsed.path.split("/")[3]))
            elif parsed.path == "/api/settings":
                self._json(self.get_service().settings())
            elif parsed.path == "/api/bundle/export":
                self._json(self.get_service().export_bundle(self._first(query, "project_id") or "architectos"))
            else:
                super().do_GET()
        except Exception as exc:
            self._error(exc)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/") and not self._authorize_api(parsed.path):
            return
        try:
            payload = self._read_json()
            if parsed.path == "/api/ai/stream":
                self._event_stream(self.get_service().stream_ai(payload))
            elif parsed.path == "/api/chat/message/stream":
                self._event_stream(self.get_service().stream_chat_message(payload))
            elif parsed.path.rstrip("/") in {"/api/ai/run", "/api/chat/run"}:
                self._json(self.get_service().run_ai(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/projects":
                self._json(self.get_service().create_project(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/memory":
                self._json(self.get_service().add_memory(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/security/preview":
                self._json(self.get_service().security_preview(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/ops/backup":
                self._json(self.get_service().create_backup(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/memory/decay/run":
                self._json(self.get_service().run_memory_decay(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/memory/reclassify":
                self._json(self.get_service().reclassify_memory(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/memory/purge-noise":
                self._json(self.get_service().purge_noise_nodes(
                    project_id=payload.get("project_id") or None,
                    dry_run=bool(payload.get("dry_run", False)),
                    hard=bool(payload.get("hard", False)),
                ), HTTPStatus.CREATED)
            elif parsed.path == "/api/memory/embeddings/rebuild":
                self._json(self.get_service().rebuild_memory_embeddings(payload), HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/memory/ingest":
                if bool(payload.get("async", True)):
                    self._json(self.get_service().schedule_memory_ingest(payload), HTTPStatus.ACCEPTED)
                else:
                    self._json(self.get_service().ingest_memory(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/memory/rescan":
                self._json(self.get_service().schedule_memory_rescan({**payload, "trigger": str(payload.get("trigger") or "manual")}), HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/memory/files":
                self._json(self.get_service().add_files_to_memory(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/memory/feedback":
                self._json(self.get_service().record_retrieval_feedback(payload), HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/memory/") and parsed.path.endswith("/promote-long-term"):
                self._json(self.get_service().promote_memory_long_term(parsed.path.split("/")[3], payload), HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/memory/candidates/") and parsed.path.endswith("/promote"):
                self._json(self.get_service().promote_memory_candidate(parsed.path.split("/")[4], payload), HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/memory/candidates/") and parsed.path.endswith("/reject"):
                self._json(self.get_service().reject_memory_candidate(parsed.path.split("/")[4], payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/memory/candidates/batch":
                self._json(self.get_service().batch_update_memory_candidates(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/tasks":
                self._json(self.get_service().create_task(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/chats":
                self._json(self.get_service().create_chat(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/chat/message":
                self._json(self.get_service().post_chat_message(payload), HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/chats/") and parsed.path.endswith("/favorite-message"):
                self._json(self.get_service().favorite_chat_message(parsed.path.split("/")[3], payload), HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/chats/") and parsed.path.endswith("/finalize"):
                self._json(self.get_service().finalize_chat_session(
                    parsed.path.split("/")[3],
                    force=bool(payload.get("force", True)),
                    trigger=str(payload.get("trigger") or "manual"),
                    project_id=payload.get("project_id"),
                ), HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/chats/") and parsed.path.endswith("/keeper-retry"):
                self._json(self.get_service().retry_chat_keeper(parsed.path.split("/")[3]), HTTPStatus.CREATED)
            elif parsed.path == "/api/project/scan":
                self._json(self.get_service().scan_project(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/project/context":
                self._json(self.get_service().selected_file_context(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/terminal/run":
                self._json(self.get_service().terminal_run(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/terminal/open":
                self._json(self.get_service().terminal_open(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/system/folder-picker":
                self._json(self.get_service().system_folder_picker(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/graph/rebuild-links":
                self._json(self.get_service().rebuild_graph_links(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/graph/suggest-links":
                self._json(self.get_service().suggest_memory_links(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/graph/suggest-consolidations":
                self._json(self.get_service().suggest_consolidations(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/graph/edges":
                self._json(self.get_service().create_graph_edge(payload), HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/graph/nodes/") and parsed.path.endswith("/pin"):
                self._json(self.get_service().pin_graph_node(parsed.path.split("/")[4]), HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/graph/nodes/") and parsed.path.endswith("/merge"):
                self._json(self.get_service().merge_graph_nodes(parsed.path.split("/")[4], payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/graph/commands":
                self._json(self.get_service().apply_graph_command(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/bundle/import":
                self._json(self.get_service().import_bundle(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/providers/test-all":
                self._json(self.get_service().test_providers(), HTTPStatus.CREATED)
            elif parsed.path == "/api/providers/connect-env":
                self._json(self.get_service().connect_env_providers(), HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/runs/") and parsed.path.endswith("/cancel"):
                self._json(self.get_service().cancel_run(parsed.path.split("/")[3]), HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/providers/") and parsed.path.endswith("/test"):
                self._json(self.get_service().test_provider(parsed.path.split("/")[3]), HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/providers/") and parsed.path.endswith("/login"):
                self._json(self.get_service().start_provider_login(parsed.path.split("/")[3]), HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/workflows/") and parsed.path.endswith("/run"):
                self._json(self.get_service().run_workflow(parsed.path.split("/")[3], payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/router/preview":
                self._json(self.get_service().routing_preview(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/council/run":
                self._json(self.get_service().run_council(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/council/run/stream":
                self._event_stream(self.get_service().stream_council(payload))
            elif parsed.path == "/api/files":
                self._json(self.get_service().upload_file(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/files/delete":
                self._json(self.get_service().delete_file(payload), HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/mcp/servers/") and parsed.path.endswith("/test"):
                self._json(self.get_service().test_mcp_server(parsed.path.split("/")[4]), HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/mcp/servers/") and parsed.path.endswith("/auth/start"):
                self._json(self.get_service().start_mcp_auth(parsed.path.split("/")[4], payload), HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/mcp/servers/") and parsed.path.endswith("/call"):
                self._json(self.get_service().call_mcp_tool(parsed.path.split("/")[4], payload), HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/code/servers/") and parsed.path.endswith("/test"):
                self._json(self.get_service().test_code_intel_server(parsed.path.split("/")[4]), HTTPStatus.CREATED)
            elif parsed.path.startswith("/api/code/servers/") and parsed.path.endswith("/install"):
                self._json(self.get_service().install_code_intel_server(parsed.path.split("/")[4], payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/code/symbols":
                self._json(self.get_service().code_symbols(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/code/hover":
                self._json(self.get_service().code_hover(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/code/diagnostics":
                self._json(self.get_service().code_diagnostics(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/code/references":
                self._json(self.get_service().code_references(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/project/file/save":
                self._json(self.get_service().save_project_file(payload), HTTPStatus.CREATED)
            elif parsed.path == "/api/project/file/delete":
                self._json(self.get_service().delete_project_file(payload), HTTPStatus.CREATED)
            else:
                self._json({"error": "not found", "path": parsed.path}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._error(exc)

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/") and not self._authorize_api(parsed.path):
            return
        try:
            payload = self._read_json()
            if parsed.path.startswith("/api/tasks/"):
                self._json(self.get_service().update_task(parsed.path.rsplit("/", 1)[-1], payload))
            elif parsed.path.startswith("/api/providers/"):
                self._json(self.get_service().update_provider(parsed.path.rsplit("/", 1)[-1], payload))
            elif parsed.path.startswith("/api/memory/") and parsed.path.endswith("/favorite"):
                self._json(self.get_service().toggle_memory_favorite(parsed.path.split("/")[3]))
            elif parsed.path == "/api/settings":
                self._json(self.get_service().update_settings(payload))
            elif parsed.path == "/api/router/settings":
                self._json(self.get_service().update_router_settings(payload))
            elif parsed.path == "/api/mcp/servers":
                self._json(self.get_service().update_mcp_server(payload))
            elif parsed.path == "/api/code/servers":
                self._json(self.get_service().update_code_intel_server(payload))
            else:
                self._json({"error": "not found", "path": parsed.path}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._error(exc)

    def _read_json(self) -> dict:
        length = int(self.headers.get("content-length") or "0")
        if length == 0:
            return {}
        # Uploads are capped at 2 MB after base64 decode; anything far above
        # that is abusive — don't read unbounded bodies into memory.
        if length > 8 * 1024 * 1024:
            raise ValueError("request body too large")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = body.encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _event_stream(self, events) -> None:
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for event in events:
                payload = f"data: {json.dumps(event, sort_keys=True)}\n\n".encode("utf-8")
                self.wfile.write(payload)
                self.wfile.flush()
        except Exception as exc:
            payload = f"data: {json.dumps({'type': 'error', 'error': str(exc)}, sort_keys=True)}\n\n".encode("utf-8")
            self.wfile.write(payload)
            self.wfile.flush()

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; base-uri 'self'; frame-ancestors 'none'")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        if ACCESS_LOG:
            super().log_message(format, *args)

    def _error(self, exc: Exception) -> None:
        body = {"error": str(exc), "type": exc.__class__.__name__}
        if isinstance(exc, ValueError):
            self._json(body, HTTPStatus.BAD_REQUEST)
            return
        _LOG.exception("Unhandled API error")
        self._json(body, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _request_origin(self) -> str:
        """Own origin for postMessage targets — never '*'."""
        host = (self.headers.get("Host") or "").strip()
        hostname, _, host_port = host.rpartition(":")
        if hostname not in ALLOWED_HOSTNAMES or (host_port and host_port != str(self._server_port())):
            host = f"127.0.0.1:{self._server_port()}"
        return f"http://{host}"

    @staticmethod
    def _first(query: dict[str, list[str]], key: str) -> str:
        return (query.get(key) or [""])[0]

    @staticmethod
    def _escape_html(value: object) -> str:
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
        )


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    ArchitectOSHandler.get_service().start_background_maintenance()
    ArchitectOSHandler.get_service().schedule_startup_memory_rescan()
    server = ThreadingHTTPServer((host, port), ArchitectOSHandler)
    print(f"ArchitectOS running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()
