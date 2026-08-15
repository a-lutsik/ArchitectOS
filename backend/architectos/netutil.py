"""Outbound URL validation guarding against server-side request forgery (SSRF).

Provider base URLs and remote MCP endpoints are user-controlled configuration:
a crafted config or prompt-injected instruction could point them at loopback
services or cloud metadata endpoints and turn ArchitectOS into an internal
proxy. Every user-controlled outbound HTTP target is validated here before a
request is made. Operators can explicitly opt a trusted endpoint into loopback
access via allow_local; link-local cloud metadata endpoints stay blocked even
then.

Redirect hops are re-validated by ``ssl_util.urlopen`` (see
``ValidatingHTTPRedirectHandler``) so a public first hop cannot bounce into
loopback or cloud metadata. OAuth metadata / token / registration URLs from
remote MCP servers must also pass through ``validate_outbound_url``.
"""

from __future__ import annotations

import contextvars
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from typing import Iterator

_ALLOWED_SCHEMES = {"http", "https"}
_DEFAULT_PORTS = {"http": 80, "https": 443}
_MAX_REDIRECTS = 5

# Per-request policy for redirect revalidation (adapters/MCP set this when
# allow_local=True so a trusted local hop may redirect to another local hop).
_outbound_allow_local: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "architectos_outbound_allow_local", default=False
)
_outbound_validate_initial: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "architectos_outbound_validate_initial", default=True
)


@contextmanager
def outbound_policy(*, allow_local: bool = False, validate_initial: bool = True) -> Iterator[None]:
    """Scope allow_local / initial-URL validation for subsequent ``ssl_util.urlopen`` calls."""
    token_local = _outbound_allow_local.set(bool(allow_local))
    token_validate = _outbound_validate_initial.set(bool(validate_initial))
    try:
        yield
    finally:
        _outbound_allow_local.reset(token_local)
        _outbound_validate_initial.reset(token_validate)


def current_allow_local() -> bool:
    return bool(_outbound_allow_local.get())


def current_validate_initial() -> bool:
    return bool(_outbound_validate_initial.get())


def is_loopback_url(url: str) -> bool:
    """True when *url* names a loopback host, without consulting DNS.

    Lets a caller derive its own allow_local from an endpoint the operator
    configured: pointing an integration at a local inference server is a
    supported setup, whereas a remote endpoint should stay under the strict
    policy. Only literal loopback names and addresses count, so a public
    hostname cannot opt itself in by resolving to 127.0.0.1.
    """
    try:
        hostname = urllib.parse.urlparse(url).hostname
    except ValueError:
        return False
    if not hostname:
        return False
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        address: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(address.is_loopback)


def validate_outbound_url(url: str, *, allow_local: bool = False) -> str:
    """Return *url* unchanged when it is safe to call server-side, else raise ValueError.

    Requires an http(s) scheme and a non-empty host. With allow_local=False
    (the default) the host is classified (resolving it once via DNS when it is
    not an IP literal) and loopback, link-local, unspecified, reserved and
    multicast targets are rejected; private RFC1918/ULA LAN addresses stay
    allowed because this is a local-first app. With allow_local=True the
    operator explicitly trusts the endpoint: hostnames are accepted without
    DNS classification (so offline machines and local DNS names still work),
    while IP literals are still classified and loopback is permitted — but
    link-local addresses (cloud metadata endpoints) are always rejected.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise ValueError(f"URL {url!r} is not parseable: {exc}") from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"URL {url!r} must use http or https, got scheme {parsed.scheme!r}.")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"URL {url!r} does not include a host.")
    try:
        port = parsed.port or _DEFAULT_PORTS[scheme]
    except ValueError as exc:
        raise ValueError(f"URL {url!r} has an invalid port: {exc}") from exc
    try:
        ip_literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(hostname)
    except ValueError:
        ip_literal = None
    if ip_literal is None and allow_local:
        return url
    addresses = [ip_literal] if ip_literal is not None else _resolve_host(hostname, port)
    for address in addresses:
        reason = _rejection_reason(address, allow_local=allow_local)
        if reason:
            raise ValueError(f"URL {url!r} targets {reason}, which is not allowed.")
    return url


def _resolve_host(hostname: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve a hostname once into all its addresses ("localhost" is loopback by definition)."""
    # "localhost" and any *.localhost name are loopback by definition (RFC 6761).
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return [ipaddress.ip_address("127.0.0.1")]
    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise ValueError(f"Host {hostname!r} could not be resolved: {exc}") from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        try:
            addresses.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    if not addresses:
        raise ValueError(f"Host {hostname!r} did not resolve to any IP address.")
    return addresses


def _rejection_reason(address: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_local: bool) -> str:
    """Return a human-readable reason when the address is disallowed, else ''."""
    # Classify IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1) as their IPv4 target.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_link_local:
        # 169.254.0.0/16 and fe80::/10 host cloud metadata endpoints — never allowed.
        return f"the link-local address {address}"
    if address.is_loopback and not allow_local:
        return f"the loopback address {address}"
    if address.is_unspecified:
        return f"the unspecified address {address}"
    if address.is_reserved:
        return f"the reserved address {address}"
    if address.is_multicast:
        return f"the multicast address {address}"
    return ""


class ValidatingHTTPRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect Location against the SSRF policy.

    Standard ``urlopen`` follows 3xx without checking the new host, so a
    public first hop could bounce into loopback or cloud metadata. Each hop
    is checked with the same ``allow_local`` policy as the original request.
    """

    def __init__(self, *, allow_local: bool | None = None, max_hops: int = _MAX_REDIRECTS) -> None:
        super().__init__()
        self._allow_local = current_allow_local() if allow_local is None else bool(allow_local)
        self._max_hops = max(1, int(max_hops))
        self._hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        self._hops += 1
        if self._hops > self._max_hops:
            raise urllib.error.HTTPError(req.full_url, 310, "too many redirects", headers, fp)
        try:
            validate_outbound_url(str(newurl), allow_local=self._allow_local)
        except ValueError as exc:
            raise urllib.error.HTTPError(req.full_url, 403, f"redirect blocked: {exc}", headers, fp) from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)
