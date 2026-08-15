from __future__ import annotations

import hmac
import json
import logging
import re
from collections.abc import Callable
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
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
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def _dispatch(self, method: str) -> None:
        """Route one request through the ROUTES table.

        Shared pipeline for every verb: index page shortcut (GET only) ->
        /api/* authorization -> query parsing -> JSON body (POST/PATCH) ->
        first matching route wins. Unmatched GETs fall through to static
        file serving; unmatched POST/PATCH get a JSON 404. All exceptions
        funnel into _error.
        """
        parsed = urlparse(self.path)
        if method == "GET" and parsed.path in {"/", "/index.html"}:
            self._serve_index()
            return
        if parsed.path.startswith("/api/") and not self._authorize_api(parsed.path):
            return
        query = parse_qs(parsed.query)
        try:
            payload = self._read_json() if method in {"POST", "PATCH"} else {}
            for route_method, pattern, handler in ROUTES:
                if route_method != method:
                    continue
                match = pattern.match(parsed.path)
                if match is not None:
                    handler(self, match, query, payload)
                    return
            if method == "GET":
                super().do_GET()
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

    # --- Route handlers ------------------------------------------------
    # One method per ROUTES entry, each taking (match, query, payload):
    # the regex match (named groups hold path params), the parsed query
    # string, and the decoded JSON body ({} for GET). Grouped by verb.

    # GET
    def _get_health(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().health())

    def _get_ready(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        readiness = self.get_service().readiness()
        self._json(readiness, HTTPStatus.OK if readiness.get("ok") else HTTPStatus.SERVICE_UNAVAILABLE)

    def _get_version(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().version())

    def _get_ops_backups(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().list_backups(int(self._first(query, "limit") or "20")))

    def _get_projects(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json({"projects": self.get_service().projects()})

    def _get_memory_candidates(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().list_memory_candidates(self._first(query, "project_id") or None, self._first(query, "status") or "candidate", int(self._first(query, "limit") or "50")))

    def _get_memory_rescan(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().memory_rescan_status())

    def _get_memory_ingest(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().memory_ingest_status())

    def _get_memory_items(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().list_memory_items(
            project_id=self._first(query, "project_id") or None,
            lifecycle_state=self._first(query, "lifecycle_state") or None,
            tier=self._first(query, "tier") or None,
            status=self._first(query, "status") or None,
            limit=int(self._first(query, "limit") or "50"),
        ))

    def _get_memory_search(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
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

    def _get_memory_feedback(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().list_retrieval_feedback(self._first(query, "project_id") or None, int(self._first(query, "limit") or "50")))

    def _get_memory_embeddings(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().memory_embeddings_status(self._first(query, "project_id") or None))

    def _get_context(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().context(self._first(query, "query"), self._first(query, "project_id") or None, self._first(query, "scope") or None, int(self._first(query, "limit") or "8")))

    def _get_graph_path(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().explain_graph_path(self._first(query, "source"), self._first(query, "target")))

    def _get_graph(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
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

    def _get_project_files(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().project_files(self._first(query, "project_id") or None, int(self._first(query, "limit") or "80")))

    def _get_project_search(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().project_search(
            self._first(query, "project_id") or None,
            self._first(query, "q") or self._first(query, "query") or "",
            mode=self._first(query, "mode") or "name",
            limit=int(self._first(query, "limit") or "80"),
            mask=self._first(query, "mask") or self._first(query, "file_mask") or "",
        ))

    def _get_project_file(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().project_file(self._first(query, "project_id") or None, self._first(query, "path")))

    def _get_project_git_diff(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().project_git_diff(self._first(query, "project_id") or None, self._first(query, "path") or None))

    def _get_analytics(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().analytics(project_id=self._first(query, "project_id") or None))

    def _get_tasks(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().list_tasks(project_id=self._first(query, "project_id") or None))

    def _get_chats(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().list_chats(project_id=self._first(query, "project_id") or None, limit=int(self._first(query, "limit") or "80")))

    def _get_chat_context_pack(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().chat_context_pack(match.group("chat_id"), self._first(query, "q") or self._first(query, "query") or "", self._first(query, "project_id") or None))

    def _get_chat_keeper_events(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        chat_id = match.group("chat_id")
        self._json({"events": self.get_service().chat_keeper_events(self._first(query, "project_id") or None, chat_id, int(self._first(query, "limit") or "30"))})

    def _get_chat(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json({"chat": self.get_service().get_chat(match.group("chat_id"))})

    def _get_provider_runs(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().provider_runs(project_id=self._first(query, "project_id") or None, limit=int(self._first(query, "limit") or "25")))

    def _get_workflows(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().workflows())

    def _get_router_settings(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().router_settings())

    def _get_council(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().council_config())

    def _get_mcp_servers(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().mcp_servers())

    def _get_mcp_oauth_callback(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
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

    def _get_mcp_server_tools(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().mcp_tools(match.group("server_id")))

    def _get_code_servers(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().code_intel_servers())

    def _get_code_languages(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().code_languages(self._first(query, "project_id") or None))

    def _get_files(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().list_files(self._first(query, "project_id") or None))

    def _get_providers(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().providers())

    def _get_provider_models(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().provider_models(match.group("provider_id")))

    def _get_settings(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().settings())

    def _get_bundle_export(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().export_bundle(self._first(query, "project_id") or "architectos"))

    # POST
    def _post_ai_stream(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._event_stream(self.get_service().stream_ai(payload))

    def _post_chat_message_stream(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._event_stream(self.get_service().stream_chat_message(payload))

    def _post_ai_run(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().run_ai(payload), HTTPStatus.CREATED)

    def _post_project(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().create_project(payload), HTTPStatus.CREATED)

    def _post_memory(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().add_memory(payload), HTTPStatus.CREATED)

    def _post_security_preview(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().security_preview(payload), HTTPStatus.CREATED)

    def _post_ops_backup(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().create_backup(payload), HTTPStatus.CREATED)

    def _post_memory_decay_run(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().run_memory_decay(payload), HTTPStatus.CREATED)

    def _post_memory_reclassify(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().reclassify_memory(payload), HTTPStatus.CREATED)

    def _post_memory_purge_noise(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().purge_noise_nodes(
            project_id=payload.get("project_id") or None,
            dry_run=bool(payload.get("dry_run", False)),
            hard=bool(payload.get("hard", False)),
        ), HTTPStatus.CREATED)

    def _post_memory_embeddings_rebuild(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().rebuild_memory_embeddings(payload), HTTPStatus.ACCEPTED)

    def _post_memory_ingest(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        if bool(payload.get("async", True)):
            self._json(self.get_service().schedule_memory_ingest(payload), HTTPStatus.ACCEPTED)
        else:
            self._json(self.get_service().ingest_memory(payload), HTTPStatus.CREATED)

    def _post_memory_rescan(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().schedule_memory_rescan({**payload, "trigger": str(payload.get("trigger") or "manual")}), HTTPStatus.ACCEPTED)

    def _post_memory_files(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().add_files_to_memory(payload), HTTPStatus.CREATED)

    def _post_memory_feedback(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().record_retrieval_feedback(payload), HTTPStatus.CREATED)

    def _post_memory_promote_long_term(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().promote_memory_long_term(match.group("memory_id"), payload), HTTPStatus.CREATED)

    def _post_memory_candidate_promote(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().promote_memory_candidate(match.group("candidate_id"), payload), HTTPStatus.CREATED)

    def _post_memory_candidate_reject(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().reject_memory_candidate(match.group("candidate_id"), payload), HTTPStatus.CREATED)

    def _post_memory_candidates_batch(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().batch_update_memory_candidates(payload), HTTPStatus.CREATED)

    def _post_task(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().create_task(payload), HTTPStatus.CREATED)

    def _post_chat(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().create_chat(payload), HTTPStatus.CREATED)

    def _post_chat_message(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().post_chat_message(payload), HTTPStatus.CREATED)

    def _post_chat_favorite_message(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().favorite_chat_message(match.group("chat_id"), payload), HTTPStatus.CREATED)

    def _post_chat_finalize(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().finalize_chat_session(
            match.group("chat_id"),
            force=bool(payload.get("force", True)),
            trigger=str(payload.get("trigger") or "manual"),
            project_id=payload.get("project_id"),
        ), HTTPStatus.CREATED)

    def _post_chat_keeper_retry(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().retry_chat_keeper(match.group("chat_id")), HTTPStatus.CREATED)

    def _post_project_scan(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().scan_project(payload), HTTPStatus.CREATED)

    def _post_project_context(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().selected_file_context(payload), HTTPStatus.CREATED)

    def _post_terminal_run(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().terminal_run(payload), HTTPStatus.CREATED)

    def _post_terminal_open(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().terminal_open(payload), HTTPStatus.CREATED)

    def _post_system_folder_picker(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().system_folder_picker(payload), HTTPStatus.CREATED)

    def _post_graph_rebuild_links(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().rebuild_graph_links(payload), HTTPStatus.CREATED)

    def _post_graph_suggest_links(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().suggest_memory_links(payload), HTTPStatus.CREATED)

    def _post_graph_suggest_consolidations(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().suggest_consolidations(payload), HTTPStatus.CREATED)

    def _post_graph_edge(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().create_graph_edge(payload), HTTPStatus.CREATED)

    def _post_graph_node_pin(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().pin_graph_node(match.group("node_id")), HTTPStatus.CREATED)

    def _post_graph_nodes_merge(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().merge_graph_nodes(match.group("node_id"), payload), HTTPStatus.CREATED)

    def _post_graph_commands(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().apply_graph_command(payload), HTTPStatus.CREATED)

    def _post_bundle_import(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().import_bundle(payload), HTTPStatus.CREATED)

    def _post_providers_test_all(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().test_providers(), HTTPStatus.CREATED)

    def _post_providers_connect_env(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().connect_env_providers(), HTTPStatus.CREATED)

    def _post_run_cancel(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().cancel_run(match.group("run_id")), HTTPStatus.CREATED)

    def _post_provider_test(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().test_provider(match.group("provider_id")), HTTPStatus.CREATED)

    def _post_provider_login(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().start_provider_login(match.group("provider_id")), HTTPStatus.CREATED)

    def _post_workflow_run(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().run_workflow(match.group("workflow_id"), payload), HTTPStatus.CREATED)

    def _post_router_preview(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().routing_preview(payload), HTTPStatus.CREATED)

    def _post_council_run(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().run_council(payload), HTTPStatus.CREATED)

    def _post_council_run_stream(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._event_stream(self.get_service().stream_council(payload))

    def _post_file(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().upload_file(payload), HTTPStatus.CREATED)

    def _post_file_delete(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().delete_file(payload), HTTPStatus.CREATED)

    def _post_mcp_server_test(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().test_mcp_server(match.group("server_id")), HTTPStatus.CREATED)

    def _post_mcp_server_auth_start(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().start_mcp_auth(match.group("server_id"), payload), HTTPStatus.CREATED)

    def _post_mcp_server_call(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().call_mcp_tool(match.group("server_id"), payload), HTTPStatus.CREATED)

    def _post_code_server_test(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().test_code_intel_server(match.group("server_id")), HTTPStatus.CREATED)

    def _post_code_server_install(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().install_code_intel_server(match.group("server_id"), payload), HTTPStatus.CREATED)

    def _post_code_symbols(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().code_symbols(payload), HTTPStatus.CREATED)

    def _post_code_hover(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().code_hover(payload), HTTPStatus.CREATED)

    def _post_code_diagnostics(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().code_diagnostics(payload), HTTPStatus.CREATED)

    def _post_code_references(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().code_references(payload), HTTPStatus.CREATED)

    def _post_project_file_save(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().save_project_file(payload), HTTPStatus.CREATED)

    def _post_project_file_delete(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().delete_project_file(payload), HTTPStatus.CREATED)

    # PATCH
    def _patch_task(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().update_task(match.group("task_id"), payload))

    def _patch_provider(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().update_provider(match.group("provider_id"), payload))

    def _patch_memory_favorite(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().toggle_memory_favorite(match.group("memory_id")))

    def _patch_settings(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().update_settings(payload))

    def _patch_router_settings(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().update_router_settings(payload))

    def _patch_mcp_servers(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().update_mcp_server(payload))

    def _patch_code_servers(self, match: re.Match[str], query: dict[str, list[str]], payload: dict[str, Any]) -> None:
        self._json(self.get_service().update_code_intel_server(payload))


# (method, path regex, handler) — matched in table order, first match wins,
# so entries keep the exact relative order of the former if/elif chains
# (e.g. /api/chats/<id>/context-pack before plain /api/chats/<id>, and
# /api/memory/<id>/promote-long-term before /api/memory/candidates/<id>/...).
# Path params are single non-empty segments, e.g. (?P<chat_id>[^/]+).
RouteHandler = Callable[[ArchitectOSHandler, re.Match[str], dict[str, list[str]], dict[str, Any]], None]
ROUTES: list[tuple[str, re.Pattern[str], RouteHandler]] = [
    # GET
    ("GET", re.compile(r"^/api/health$"), ArchitectOSHandler._get_health),
    ("GET", re.compile(r"^/api/ready$"), ArchitectOSHandler._get_ready),
    ("GET", re.compile(r"^/api/version$"), ArchitectOSHandler._get_version),
    ("GET", re.compile(r"^/api/ops/backups$"), ArchitectOSHandler._get_ops_backups),
    ("GET", re.compile(r"^/api/projects$"), ArchitectOSHandler._get_projects),
    ("GET", re.compile(r"^/api/memory/candidates$"), ArchitectOSHandler._get_memory_candidates),
    ("GET", re.compile(r"^/api/memory/rescan$"), ArchitectOSHandler._get_memory_rescan),
    ("GET", re.compile(r"^/api/memory/ingest$"), ArchitectOSHandler._get_memory_ingest),
    ("GET", re.compile(r"^/api/memory/items$"), ArchitectOSHandler._get_memory_items),
    ("GET", re.compile(r"^/api/memory/search$"), ArchitectOSHandler._get_memory_search),
    ("GET", re.compile(r"^/api/memory/feedback$"), ArchitectOSHandler._get_memory_feedback),
    ("GET", re.compile(r"^/api/memory/embeddings$"), ArchitectOSHandler._get_memory_embeddings),
    ("GET", re.compile(r"^/api/context$"), ArchitectOSHandler._get_context),
    ("GET", re.compile(r"^/api/graph/path$"), ArchitectOSHandler._get_graph_path),
    ("GET", re.compile(r"^/api/graph$"), ArchitectOSHandler._get_graph),
    ("GET", re.compile(r"^/api/project/files$"), ArchitectOSHandler._get_project_files),
    ("GET", re.compile(r"^/api/project/search$"), ArchitectOSHandler._get_project_search),
    ("GET", re.compile(r"^/api/project/file$"), ArchitectOSHandler._get_project_file),
    ("GET", re.compile(r"^/api/project/git-diff$"), ArchitectOSHandler._get_project_git_diff),
    ("GET", re.compile(r"^/api/analytics$"), ArchitectOSHandler._get_analytics),
    ("GET", re.compile(r"^/api/tasks$"), ArchitectOSHandler._get_tasks),
    ("GET", re.compile(r"^/api/chats$"), ArchitectOSHandler._get_chats),
    ("GET", re.compile(r"^/api/chats/(?P<chat_id>[^/]+)/context-pack$"), ArchitectOSHandler._get_chat_context_pack),
    ("GET", re.compile(r"^/api/chats/(?P<chat_id>[^/]+)/keeper-events$"), ArchitectOSHandler._get_chat_keeper_events),
    ("GET", re.compile(r"^/api/chats/(?P<chat_id>[^/]+)$"), ArchitectOSHandler._get_chat),
    ("GET", re.compile(r"^/api/provider-runs$"), ArchitectOSHandler._get_provider_runs),
    ("GET", re.compile(r"^/api/workflows$"), ArchitectOSHandler._get_workflows),
    ("GET", re.compile(r"^/api/router/settings$"), ArchitectOSHandler._get_router_settings),
    ("GET", re.compile(r"^/api/council$"), ArchitectOSHandler._get_council),
    ("GET", re.compile(r"^/api/mcp/servers$"), ArchitectOSHandler._get_mcp_servers),
    ("GET", re.compile(r"^/api/mcp/oauth/callback$"), ArchitectOSHandler._get_mcp_oauth_callback),
    ("GET", re.compile(r"^/api/mcp/servers/(?P<server_id>[^/]+)/tools$"), ArchitectOSHandler._get_mcp_server_tools),
    ("GET", re.compile(r"^/api/code/servers$"), ArchitectOSHandler._get_code_servers),
    ("GET", re.compile(r"^/api/code/languages$"), ArchitectOSHandler._get_code_languages),
    ("GET", re.compile(r"^/api/files$"), ArchitectOSHandler._get_files),
    ("GET", re.compile(r"^/api/(?:providers|router)$"), ArchitectOSHandler._get_providers),
    ("GET", re.compile(r"^/api/providers/(?P<provider_id>[^/]+)/models$"), ArchitectOSHandler._get_provider_models),
    ("GET", re.compile(r"^/api/settings$"), ArchitectOSHandler._get_settings),
    ("GET", re.compile(r"^/api/bundle/export$"), ArchitectOSHandler._get_bundle_export),
    # POST
    ("POST", re.compile(r"^/api/ai/stream$"), ArchitectOSHandler._post_ai_stream),
    ("POST", re.compile(r"^/api/chat/message/stream$"), ArchitectOSHandler._post_chat_message_stream),
    # rstrip("/") in the old chain accepted any number of trailing slashes.
    ("POST", re.compile(r"^/api/(?:ai|chat)/run/*$"), ArchitectOSHandler._post_ai_run),
    ("POST", re.compile(r"^/api/projects$"), ArchitectOSHandler._post_project),
    ("POST", re.compile(r"^/api/memory$"), ArchitectOSHandler._post_memory),
    ("POST", re.compile(r"^/api/security/preview$"), ArchitectOSHandler._post_security_preview),
    ("POST", re.compile(r"^/api/ops/backup$"), ArchitectOSHandler._post_ops_backup),
    ("POST", re.compile(r"^/api/memory/decay/run$"), ArchitectOSHandler._post_memory_decay_run),
    ("POST", re.compile(r"^/api/memory/reclassify$"), ArchitectOSHandler._post_memory_reclassify),
    ("POST", re.compile(r"^/api/memory/purge-noise$"), ArchitectOSHandler._post_memory_purge_noise),
    ("POST", re.compile(r"^/api/memory/embeddings/rebuild$"), ArchitectOSHandler._post_memory_embeddings_rebuild),
    ("POST", re.compile(r"^/api/memory/ingest$"), ArchitectOSHandler._post_memory_ingest),
    ("POST", re.compile(r"^/api/memory/rescan$"), ArchitectOSHandler._post_memory_rescan),
    ("POST", re.compile(r"^/api/memory/files$"), ArchitectOSHandler._post_memory_files),
    ("POST", re.compile(r"^/api/memory/feedback$"), ArchitectOSHandler._post_memory_feedback),
    ("POST", re.compile(r"^/api/memory/(?P<memory_id>[^/]+)/promote-long-term$"), ArchitectOSHandler._post_memory_promote_long_term),
    ("POST", re.compile(r"^/api/memory/candidates/(?P<candidate_id>[^/]+)/promote$"), ArchitectOSHandler._post_memory_candidate_promote),
    ("POST", re.compile(r"^/api/memory/candidates/(?P<candidate_id>[^/]+)/reject$"), ArchitectOSHandler._post_memory_candidate_reject),
    ("POST", re.compile(r"^/api/memory/candidates/batch$"), ArchitectOSHandler._post_memory_candidates_batch),
    ("POST", re.compile(r"^/api/tasks$"), ArchitectOSHandler._post_task),
    ("POST", re.compile(r"^/api/chats$"), ArchitectOSHandler._post_chat),
    ("POST", re.compile(r"^/api/chat/message$"), ArchitectOSHandler._post_chat_message),
    ("POST", re.compile(r"^/api/chats/(?P<chat_id>[^/]+)/favorite-message$"), ArchitectOSHandler._post_chat_favorite_message),
    ("POST", re.compile(r"^/api/chats/(?P<chat_id>[^/]+)/finalize$"), ArchitectOSHandler._post_chat_finalize),
    ("POST", re.compile(r"^/api/chats/(?P<chat_id>[^/]+)/keeper-retry$"), ArchitectOSHandler._post_chat_keeper_retry),
    ("POST", re.compile(r"^/api/project/scan$"), ArchitectOSHandler._post_project_scan),
    ("POST", re.compile(r"^/api/project/context$"), ArchitectOSHandler._post_project_context),
    ("POST", re.compile(r"^/api/terminal/run$"), ArchitectOSHandler._post_terminal_run),
    ("POST", re.compile(r"^/api/terminal/open$"), ArchitectOSHandler._post_terminal_open),
    ("POST", re.compile(r"^/api/system/folder-picker$"), ArchitectOSHandler._post_system_folder_picker),
    ("POST", re.compile(r"^/api/graph/rebuild-links$"), ArchitectOSHandler._post_graph_rebuild_links),
    ("POST", re.compile(r"^/api/graph/suggest-links$"), ArchitectOSHandler._post_graph_suggest_links),
    ("POST", re.compile(r"^/api/graph/suggest-consolidations$"), ArchitectOSHandler._post_graph_suggest_consolidations),
    ("POST", re.compile(r"^/api/graph/edges$"), ArchitectOSHandler._post_graph_edge),
    ("POST", re.compile(r"^/api/graph/nodes/(?P<node_id>[^/]+)/pin$"), ArchitectOSHandler._post_graph_node_pin),
    ("POST", re.compile(r"^/api/graph/nodes/(?P<node_id>[^/]+)/merge$"), ArchitectOSHandler._post_graph_nodes_merge),
    ("POST", re.compile(r"^/api/graph/commands$"), ArchitectOSHandler._post_graph_commands),
    ("POST", re.compile(r"^/api/bundle/import$"), ArchitectOSHandler._post_bundle_import),
    ("POST", re.compile(r"^/api/providers/test-all$"), ArchitectOSHandler._post_providers_test_all),
    ("POST", re.compile(r"^/api/providers/connect-env$"), ArchitectOSHandler._post_providers_connect_env),
    ("POST", re.compile(r"^/api/runs/(?P<run_id>[^/]+)/cancel$"), ArchitectOSHandler._post_run_cancel),
    ("POST", re.compile(r"^/api/providers/(?P<provider_id>[^/]+)/test$"), ArchitectOSHandler._post_provider_test),
    ("POST", re.compile(r"^/api/providers/(?P<provider_id>[^/]+)/login$"), ArchitectOSHandler._post_provider_login),
    ("POST", re.compile(r"^/api/workflows/(?P<workflow_id>[^/]+)/run$"), ArchitectOSHandler._post_workflow_run),
    ("POST", re.compile(r"^/api/router/preview$"), ArchitectOSHandler._post_router_preview),
    ("POST", re.compile(r"^/api/council/run$"), ArchitectOSHandler._post_council_run),
    ("POST", re.compile(r"^/api/council/run/stream$"), ArchitectOSHandler._post_council_run_stream),
    ("POST", re.compile(r"^/api/files$"), ArchitectOSHandler._post_file),
    ("POST", re.compile(r"^/api/files/delete$"), ArchitectOSHandler._post_file_delete),
    ("POST", re.compile(r"^/api/mcp/servers/(?P<server_id>[^/]+)/test$"), ArchitectOSHandler._post_mcp_server_test),
    ("POST", re.compile(r"^/api/mcp/servers/(?P<server_id>[^/]+)/auth/start$"), ArchitectOSHandler._post_mcp_server_auth_start),
    ("POST", re.compile(r"^/api/mcp/servers/(?P<server_id>[^/]+)/call$"), ArchitectOSHandler._post_mcp_server_call),
    ("POST", re.compile(r"^/api/code/servers/(?P<server_id>[^/]+)/test$"), ArchitectOSHandler._post_code_server_test),
    ("POST", re.compile(r"^/api/code/servers/(?P<server_id>[^/]+)/install$"), ArchitectOSHandler._post_code_server_install),
    ("POST", re.compile(r"^/api/code/symbols$"), ArchitectOSHandler._post_code_symbols),
    ("POST", re.compile(r"^/api/code/hover$"), ArchitectOSHandler._post_code_hover),
    ("POST", re.compile(r"^/api/code/diagnostics$"), ArchitectOSHandler._post_code_diagnostics),
    ("POST", re.compile(r"^/api/code/references$"), ArchitectOSHandler._post_code_references),
    ("POST", re.compile(r"^/api/project/file/save$"), ArchitectOSHandler._post_project_file_save),
    ("POST", re.compile(r"^/api/project/file/delete$"), ArchitectOSHandler._post_project_file_delete),
    # PATCH
    ("PATCH", re.compile(r"^/api/tasks/(?P<task_id>[^/]+)$"), ArchitectOSHandler._patch_task),
    ("PATCH", re.compile(r"^/api/providers/(?P<provider_id>[^/]+)$"), ArchitectOSHandler._patch_provider),
    ("PATCH", re.compile(r"^/api/memory/(?P<memory_id>[^/]+)/favorite$"), ArchitectOSHandler._patch_memory_favorite),
    ("PATCH", re.compile(r"^/api/settings$"), ArchitectOSHandler._patch_settings),
    ("PATCH", re.compile(r"^/api/router/settings$"), ArchitectOSHandler._patch_router_settings),
    ("PATCH", re.compile(r"^/api/mcp/servers$"), ArchitectOSHandler._patch_mcp_servers),
    ("PATCH", re.compile(r"^/api/code/servers$"), ArchitectOSHandler._patch_code_servers),
]


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    ArchitectOSHandler.get_service().start_background_maintenance()
    ArchitectOSHandler.get_service().schedule_startup_memory_rescan()
    server = ThreadingHTTPServer((host, port), ArchitectOSHandler)
    print(f"ArchitectOS running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()
