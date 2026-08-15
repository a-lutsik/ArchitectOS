from __future__ import annotations

import os
import ssl
import urllib.request
from pathlib import Path
from typing import Any

from .netutil import (
    ValidatingHTTPRedirectHandler,
    current_allow_local,
    current_validate_initial,
    validate_outbound_url,
)


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
    """HTTPS-aware urlopen that re-validates every redirect hop against the SSRF policy.

    Optional kwargs (consumed here, not forwarded to urllib):
      allow_local: bool — override the active outbound policy for this call.
      validate: bool — when True, validate the initial URL as well. Defaults to
        the active outbound policy (True unless a caller used
        ``outbound_policy(validate_initial=False)``).
      context: ssl.SSLContext — TLS context; defaults to ``ssl_context()``.
    """
    allow_local = bool(kwargs.pop("allow_local", current_allow_local()))
    validate = bool(kwargs.pop("validate", current_validate_initial()))
    context = kwargs.pop("context", None) or ssl_context()
    if kwargs:
        # Refuse unknown kwargs so typos surface early rather than disappearing.
        raise TypeError(f"urlopen() got unexpected keyword argument(s): {sorted(kwargs)!r}")

    target = url.full_url if hasattr(url, "full_url") else str(url)
    if validate:
        validate_outbound_url(target, allow_local=allow_local)

    opener = urllib.request.build_opener(
        ValidatingHTTPRedirectHandler(allow_local=allow_local),
        urllib.request.HTTPSHandler(context=context),
        urllib.request.HTTPHandler(),
    )
    return opener.open(url, data=data, timeout=timeout)
