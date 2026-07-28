from __future__ import annotations

import os
import ssl
import urllib.request
from pathlib import Path
from typing import Any


def ca_bundle_path() -> str | None:
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        override = os.environ.get(key)
        if override and Path(override).is_file():
            return override
    try:
        import certifi

        path = certifi.where()
    except Exception:
        return None
    return path if path and Path(path).is_file() else None


def ssl_context() -> ssl.SSLContext:
    cafile = ca_bundle_path()
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


def configure_default_ssl() -> str | None:
    """Use certifi when the OS/Python CA store is missing (common on macOS python.org installs)."""
    path = ca_bundle_path()
    if not path:
        return None
    os.environ.setdefault("SSL_CERT_FILE", path)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", path)
    return path


def urlopen(url: Any, data: Any = None, timeout: float | None = None, **kwargs: Any):
    if "context" not in kwargs:
        kwargs["context"] = ssl_context()
    return urllib.request.urlopen(url, data=data, timeout=timeout, **kwargs)
