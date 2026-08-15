"""Outbound URL validation guarding against server-side request forgery (SSRF).

Provider base URLs and remote MCP endpoints are user-controlled configuration:
a crafted config or prompt-injected instruction could point them at loopback
services or cloud metadata endpoints and turn ArchitectOS into an internal
proxy. Every user-controlled outbound HTTP target is validated here before a
request is made. Operators can explicitly opt a trusted endpoint into loopback
access via allow_local; link-local cloud metadata endpoints stay blocked even
then.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse

_ALLOWED_SCHEMES = {"http", "https"}
_DEFAULT_PORTS = {"http": 80, "https": 443}


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
