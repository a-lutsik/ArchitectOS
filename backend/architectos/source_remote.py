"""Remote source drivers: HTTP(S) and FTP/SFTP."""

from __future__ import annotations

import ftplib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any, Callable

from .files import TEXT_LIKE_EXTENSIONS, decode_text
from .netutil import outbound_policy, validate_outbound_url
from .ssl_util import urlopen


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip = False
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr"}:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._chunks.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self._chunks)).strip()


def html_to_text(raw: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(raw)
    except Exception:
        return raw.strip()
    return parser.text() or raw.strip()


def _auth_header(http_cfg: dict[str, Any]) -> dict[str, str]:
    auth = str(http_cfg.get("auth") or "none").lower()
    headers = dict(http_cfg.get("headers") or {})
    if auth == "bearer":
        token = str(http_cfg.get("bearer_token") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif auth == "basic":
        user = str(http_cfg.get("username") or "").strip()
        password = str(http_cfg.get("password") or "").strip()
        if user:
            import base64

            token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
    return {str(k): str(v) for k, v in headers.items()}


def _http_method(http_cfg: dict[str, Any]) -> str:
    method = str(http_cfg.get("method") or "GET").strip().upper() or "GET"
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
        raise ValueError(f"Unsupported HTTP method: {method}")
    return method


def _http_request(http_cfg: dict[str, Any], *, max_bytes: int = 400_000) -> tuple[int, str, bytes, str]:
    """Execute one HTTP(S) request. Returns status, content_type, body bytes, validated URL."""
    url = str(http_cfg.get("url") or "").strip()
    if not url:
        raise ValueError("HTTP url is required")
    validated = validate_outbound_url(url, allow_local=bool(http_cfg.get("allow_local")))
    method = _http_method(http_cfg)
    headers = _auth_header(http_cfg)
    body_raw = http_cfg.get("body")
    data: bytes | None = None
    if method not in {"GET", "HEAD"} and body_raw not in (None, ""):
        if isinstance(body_raw, (dict, list)):
            data = json.dumps(body_raw).encode("utf-8")
            if not any(str(k).lower() == "content-type" for k in headers):
                headers["Content-Type"] = "application/json"
        else:
            data = str(body_raw).encode("utf-8")
            if not any(str(k).lower() == "content-type" for k in headers):
                headers["Content-Type"] = (
                    "application/json"
                    if str(body_raw).lstrip().startswith(("{", "["))
                    else "text/plain; charset=utf-8"
                )
    req = urllib.request.Request(validated, data=data, method=method, headers=headers)
    timeout = float(http_cfg.get("timeout") or (20 if max_bytes <= 120_000 else 30))
    with outbound_policy(allow_local=bool(http_cfg.get("allow_local"))):
        with urlopen(req, timeout=timeout) as response:
            status = int(getattr(response, "status", None) or response.getcode() or 200)
            content_type = str(response.headers.get("Content-Type") or "")
            body = response.read(max_bytes)
    return status, content_type, body, validated


def _looks_like_json(content_type: str, text: str) -> bool:
    ct = content_type.lower()
    if "json" in ct:
        return True
    stripped = text.lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


def resolve_dot_path(data: Any, path: str) -> Any:
    """Resolve a simple dotted path (a.b.c). Empty path returns data."""
    text = str(path or "").strip()
    if not text:
        return data
    cur: Any = data
    for part in text.split("."):
        key = part.strip()
        if not key:
            continue
        if isinstance(cur, dict):
            if key not in cur:
                raise KeyError(f"path not found: {text}")
            cur = cur[key]
        elif isinstance(cur, list):
            try:
                cur = cur[int(key)]
            except (ValueError, IndexError) as exc:
                raise KeyError(f"path not found: {text}") from exc
        else:
            raise KeyError(f"path not found: {text}")
    return cur


def detect_json_paths(data: Any, *, prefix: str = "", limit: int = 40) -> list[str]:
    """Collect a shallow list of dotted paths for probe UI hints."""
    out: list[str] = []

    def walk(node: Any, path: str, depth: int) -> None:
        if len(out) >= limit or depth > 4:
            return
        if isinstance(node, dict):
            for key, value in list(node.items())[:24]:
                child = f"{path}.{key}" if path else str(key)
                out.append(child)
                if len(out) >= limit:
                    return
                if isinstance(value, (dict, list)) and depth < 3:
                    walk(value, child, depth + 1)
        elif isinstance(node, list) and node:
            child = f"{path}[0]" if path else "[0]"
            # Prefer items_path style without index for arrays of objects.
            if path:
                out.append(path)
            sample = node[0]
            if isinstance(sample, dict):
                for key in list(sample.keys())[:12]:
                    out.append(f"{path}.{key}" if path else str(key))
                    if len(out) >= limit:
                        return

    walk(data, prefix, 0)
    # Dedupe preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for item in out:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique[:limit]


def _field_value(item: Any, field: str) -> str:
    if not field:
        return ""
    try:
        value = resolve_dot_path(item, field) if "." in field else (
            item.get(field) if isinstance(item, dict) else None
        )
    except KeyError:
        value = None
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)[:4000]
    return str(value).strip()


def extract_json_items(data: Any, extract: dict[str, Any] | None, *, max_items: int = 100) -> list[dict[str, str]]:
    """Turn JSON payload into {label, text, ref} rows using extract mapping."""
    extract = dict(extract or {})
    items_path = str(extract.get("items_path") or "").strip()
    label_field = str(extract.get("label_field") or "").strip()
    text_fields_raw = extract.get("text_fields") or []
    if isinstance(text_fields_raw, str):
        text_fields = [part.strip() for part in text_fields_raw.split(",") if part.strip()]
    else:
        text_fields = [str(part).strip() for part in text_fields_raw if str(part).strip()]

    try:
        root = resolve_dot_path(data, items_path) if items_path else data
    except KeyError:
        root = data

    if isinstance(root, list):
        rows = root[: max(1, int(max_items or 100))]
    elif isinstance(root, dict):
        rows = [root]
    else:
        return [{
            "label": "value",
            "text": str(root).strip()[:12000],
            "ref": items_path or "value",
        }]

    out: list[dict[str, str]] = []
    for index, item in enumerate(rows):
        if not isinstance(item, dict):
            text = str(item).strip()
            if len(text) < 4:
                continue
            out.append({"label": f"item {index + 1}", "text": text[:12000], "ref": f"{items_path or 'item'}[{index}]"})
            continue
        label = _field_value(item, label_field) if label_field else ""
        if not label:
            for fallback in ("title", "name", "id", "subject"):
                label = _field_value(item, fallback)
                if label:
                    break
        if not label:
            label = f"item {index + 1}"
        if text_fields:
            chunks = [_field_value(item, field) for field in text_fields]
            text = "\n\n".join(chunk for chunk in chunks if chunk)
        else:
            # Keep only scalar fields to avoid dumping huge nested service blobs.
            scalars = []
            for key, value in item.items():
                if isinstance(value, (dict, list)):
                    continue
                scalars.append(f"{key}: {value}")
            text = "\n".join(scalars) if scalars else json.dumps(item, ensure_ascii=False)[:12000]
        if len(text.strip()) < 4:
            continue
        out.append({
            "label": label[:140],
            "text": text[:12000],
            "ref": f"{items_path or 'item'}[{index}]",
        })
    return out


def _build_http_candidates_from_response(
    *,
    project_id: str,
    source: dict[str, Any],
    validated: str,
    content_type: str,
    body: bytes,
    stable_id: Callable[..., str],
    max_items: int = 100,
    preview_limit: int | None = None,
) -> list[dict[str, Any]]:
    http_cfg = dict((source.get("config") or {}).get("http") or {})
    extract = dict(http_cfg.get("extract") or {})
    mode = str(extract.get("mode") or "auto").strip().lower() or "auto"
    text = decode_text(body)
    kind = str(source.get("kind") or "docs")
    node_type = "Doc" if kind in {"docs", "wiki"} else "Artifact"
    source_id = str(source.get("id") or "")
    source_name = str(source.get("name") or "")
    use_json = mode == "json" or (mode == "auto" and _looks_like_json(content_type, text))
    if use_json and mode != "text":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if payload is not None and (extract.get("items_path") or extract.get("text_fields") or extract.get("label_field") or mode == "json"):
            rows = extract_json_items(payload, extract, max_items=max_items)
            if preview_limit is not None:
                rows = rows[:preview_limit]
            candidates: list[dict[str, Any]] = []
            for row in rows:
                ref = f"{validated}#{row['ref']}"
                candidates.append({
                    "id": stable_id("candidate", project_id, source_id or "http", ref),
                    "project_id": project_id,
                    "source_type": kind,
                    "source_ref": ref,
                    "label": f"{source_name}: {row['label']}".strip(": ")[:180] if source_name else row["label"][:180],
                    "type": node_type,
                    "scope": "project",
                    "text": row["text"],
                    "confidence": 0.72,
                    "metadata": {
                        "template": "http_json",
                        "source_id": source_id,
                        "source_name": source_name,
                        "url": validated,
                        "item_ref": row["ref"],
                    },
                })
            if candidates:
                return candidates
        if payload is not None and mode == "json":
            # JSON mode without useful extract: store compact JSON of root / first items only.
            compact = json.dumps(payload, ensure_ascii=False)[:12000]
            if len(compact) >= 4:
                return [{
                    "id": stable_id("candidate", project_id, source_id or "http", validated),
                    "project_id": project_id,
                    "source_type": kind,
                    "source_ref": validated,
                    "label": f"{source_name}: json".strip(": ")[:180] if source_name else "json",
                    "type": node_type,
                    "scope": "project",
                    "text": compact,
                    "confidence": 0.65,
                    "metadata": {"template": "http_json", "source_id": source_id, "source_name": source_name, "url": validated},
                }]

    if "html" in content_type.lower():
        content = html_to_text(text)
        title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        label = re.sub(r"\s+", " ", title_match.group(1)).strip()[:140] if title_match else validated
    else:
        content = text.strip()
        label = validated.rstrip("/").split("/")[-1] or validated
    if len(content) < 4:
        return []
    return [{
        "id": stable_id("candidate", project_id, source_id or "http", validated),
        "project_id": project_id,
        "source_type": kind,
        "source_ref": validated,
        "label": f"{source_name}: {label}".strip(": ")[:180] if source_name else label[:180],
        "type": node_type,
        "scope": "project",
        "text": content[:12000],
        "confidence": 0.7,
        "metadata": {
            "template": "http_page",
            "source_id": source_id,
            "source_name": source_name,
            "url": validated,
        },
    }]


def fetch_http_probe(http_cfg: dict[str, Any]) -> dict[str, Any]:
    status, content_type, body, validated = _http_request(http_cfg, max_bytes=120_000)
    text = decode_text(body)
    title = ""
    excerpt = text[:400]
    json_preview = ""
    detected_paths: list[str] = []
    preview_candidates: list[dict[str, Any]] = []
    if "html" in content_type.lower():
        match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        if match:
            title = re.sub(r"\s+", " ", match.group(1)).strip()[:180]
        excerpt = html_to_text(text)[:400]
    elif _looks_like_json(content_type, text):
        try:
            payload = json.loads(text)
            json_preview = json.dumps(payload, ensure_ascii=False, indent=2)[:4000]
            detected_paths = detect_json_paths(payload)
            excerpt = json_preview[:400]
            preview_candidates = _build_http_candidates_from_response(
                project_id="probe",
                source={"id": "probe", "name": "probe", "kind": "docs", "config": {"http": http_cfg}},
                validated=validated,
                content_type=content_type,
                body=body,
                stable_id=lambda *parts: "preview:" + ":".join(str(p) for p in parts)[:120],
                max_items=100,
                preview_limit=3,
            )
            # Slim preview for UI
            preview_candidates = [
                {"label": item.get("label"), "text": str(item.get("text") or "")[:400], "source_ref": item.get("source_ref")}
                for item in preview_candidates
            ]
        except json.JSONDecodeError:
            pass
    return {
        "ok": True,
        "status": status,
        "content_type": content_type,
        "bytes": len(body),
        "title": title or validated,
        "sample": excerpt,
        "json_preview": json_preview,
        "detected_paths": detected_paths,
        "preview_candidates": preview_candidates,
    }


def ingest_http_candidates(
    project_id: str,
    source: dict[str, Any],
    *,
    stable_id: Callable[..., str],
    deadline_expired: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    if deadline_expired and deadline_expired():
        return []
    cfg = dict(source.get("config") or {})
    http_cfg = dict(cfg.get("http") or {})
    if not str(http_cfg.get("url") or "").strip():
        return []
    _status, content_type, body, validated = _http_request(http_cfg, max_bytes=400_000)
    max_items = int(cfg.get("max_items") or 0) or 100
    return _build_http_candidates_from_response(
        project_id=project_id,
        source=source,
        validated=validated,
        content_type=content_type,
        body=body,
        stable_id=stable_id,
        max_items=max_items,
    )


def _load_paramiko_private_key(pem: str, passphrase: str | None = None):
    """Parse OpenSSH/PEM private key for paramiko."""
    import paramiko  # type: ignore[import-untyped]
    from io import StringIO

    text = str(pem or "").strip()
    if not text:
        raise ValueError("Private key is empty")
    password = passphrase or None
    loaders = (
        getattr(paramiko, "Ed25519Key", None),
        getattr(paramiko, "RSAKey", None),
        getattr(paramiko, "ECDSAKey", None),
        getattr(paramiko, "DSSKey", None),
    )
    errors: list[Exception] = []
    for cls in loaders:
        if cls is None:
            continue
        buf = StringIO(text)
        try:
            return cls.from_private_key(buf, password=password)
        except Exception as exc:  # noqa: BLE001 — try next key type
            errors.append(exc)
    detail = str(errors[-1]) if errors else "unsupported format"
    raise ValueError(f"Could not parse private key ({detail})")


def _ftp_connect(ftp_cfg: dict[str, Any]):
    url = str(ftp_cfg.get("url") or "").strip()
    if not url:
        raise ValueError("FTP url is required")
    parsed = urllib.parse.urlparse(url)
    scheme = (parsed.scheme or "ftp").lower()
    host = parsed.hostname or ""
    port = parsed.port or (22 if scheme == "sftp" else 21)
    username = str(ftp_cfg.get("username") or parsed.username or "anonymous")
    password = str(ftp_cfg.get("password") or parsed.password or "")
    remote_path = str(ftp_cfg.get("remote_path") or parsed.path or "/").strip() or "/"
    private_key = str(ftp_cfg.get("private_key") or "").strip()
    key_passphrase = str(ftp_cfg.get("private_key_passphrase") or "").strip() or None
    if scheme == "sftp":
        try:
            import paramiko  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ValueError("SFTP requires optional dependency paramiko (pip install paramiko)") from exc
        pkey = _load_paramiko_private_key(private_key, key_passphrase) if private_key else None
        if not password and pkey is None:
            raise ValueError("SFTP needs a password or a private key")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            host,
            port=port,
            username=username,
            password=password or None,
            pkey=pkey,
            look_for_keys=False,
            allow_agent=False,
            timeout=20,
        )
        sftp = client.open_sftp()
        return ("sftp", sftp, client, remote_path)
    if private_key:
        raise ValueError("Private key auth is only supported for sftp:// URLs")
    client = ftplib.FTP()
    client.connect(host, port, timeout=20)
    client.login(username, password)
    return ("ftp", client, None, remote_path)


def ftp_probe(ftp_cfg: dict[str, Any]) -> dict[str, Any]:
    kind, client, extra, remote_path = _ftp_connect(ftp_cfg)
    try:
        if kind == "sftp":
            names = client.listdir(remote_path)
        else:
            names = client.nlst(remote_path)
        sample = names[0] if names else ""
        return {"ok": True, "count": len(names), "sample": sample, "path": remote_path}
    finally:
        if kind == "sftp":
            client.close()
            if extra:
                extra.close()
        else:
            client.quit()


def _match_glob(name: str, pattern: str) -> bool:
    from fnmatch import fnmatch

    return fnmatch(name, pattern)


def ingest_ftp_candidates(
    project_id: str,
    source: dict[str, Any],
    *,
    stable_id: Callable[..., str],
    deadline_expired: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    cfg = dict(source.get("config") or {})
    ftp_cfg = dict(cfg.get("ftp") or {})
    kind, client, extra, remote_path = _ftp_connect(ftp_cfg)
    globs = list(ftp_cfg.get("include_glob") or ["**/*.md", "**/*.txt"])
    candidates: list[dict[str, Any]] = []
    source_id = str(source.get("id") or "")
    source_name = str(source.get("name") or "")

    def add_file(path: str, data: bytes) -> None:
        suffix = PurePosixPath(path).suffix.lower()
        if suffix and suffix not in TEXT_LIKE_EXTENSIONS:
            return
        text = decode_text(data)
        if len(text.strip()) < 4:
            return
        candidates.append({
            "id": stable_id("candidate", project_id, source_id, path),
            "project_id": project_id,
            "source_type": str(source.get("kind") or "docs"),
            "source_ref": path,
            "label": f"{source_name}: {PurePosixPath(path).name}"[:180],
            "type": "Doc",
            "scope": "project",
            "text": text[:12000],
            "confidence": 0.68,
            "metadata": {"template": "ftp_file", "source_id": source_id, "source_name": source_name, "path": path},
        })

    try:
        if kind == "sftp":
            for entry in client.listdir_attr(remote_path):
                if deadline_expired and deadline_expired():
                    break
                name = entry.filename
                full = f"{remote_path.rstrip('/')}/{name}"
                if not any(_match_glob(name, pat.replace("**/", "")) for pat in globs):
                    continue
                if entry.st_size > 400_000:
                    continue
                try:
                    with client.open(full) as handle:
                        add_file(full, handle.read(400_000))
                except OSError:
                    continue
        else:
            for name in client.nlst(remote_path):
                if deadline_expired and deadline_expired():
                    break
                if not any(_match_glob(name, pat.replace("**/", "")) for pat in globs):
                    continue
                lines: list[str] = []
                client.retrlines(f"RETR {name}", lines.append)
                add_file(name, "\n".join(lines).encode())
    finally:
        if kind == "sftp":
            client.close()
            if extra:
                extra.close()
        else:
            client.quit()
    return candidates
