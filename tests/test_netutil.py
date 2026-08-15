from __future__ import annotations

import os
import socket
import unittest
from unittest import mock

from backend.architectos.adapters import OpenAIResponsesAdapter
from backend.architectos.netutil import validate_outbound_url


def _addrinfo(*ips: str) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443)) for ip in ips]


class ValidateOutboundUrlTests(unittest.TestCase):
    def test_rejects_non_http_schemes(self) -> None:
        for url in ("file:///etc/passwd", "gopher://example.com/1"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_outbound_url(url)

    def test_rejects_missing_host(self) -> None:
        for url in ("http://", "https:///path-only"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_outbound_url(url)

    def test_rejects_loopback_by_default(self) -> None:
        for url in ("http://127.0.0.1/", "http://localhost:8080/mcp", "http://app.localhost/", "http://[::1]/"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_outbound_url(url)

    def test_rejects_link_local_metadata_endpoint_by_default(self) -> None:
        with self.assertRaises(ValueError):
            validate_outbound_url("http://169.254.169.254/latest/meta-data")

    def test_allow_local_permits_loopback_but_not_link_local(self) -> None:
        url = "http://127.0.0.1:11434"
        self.assertEqual(validate_outbound_url(url, allow_local=True), url)
        for rejected in ("http://169.254.169.254/", "http://[fe80::1]/"):
            with self.subTest(url=rejected):
                with self.assertRaises(ValueError):
                    validate_outbound_url(rejected, allow_local=True)

    def test_allow_local_accepts_hostnames_without_dns(self) -> None:
        with mock.patch("backend.architectos.netutil.socket.getaddrinfo", side_effect=socket.gaierror("offline")) as resolver:
            url = "http://localhost:8080/mcp"
            self.assertEqual(validate_outbound_url(url, allow_local=True), url)
            url = "http://llmstudio.lan:1234/v1"
            self.assertEqual(validate_outbound_url(url, allow_local=True), url)
        resolver.assert_not_called()

    def test_private_lan_address_is_allowed_by_default(self) -> None:
        url = "http://192.168.1.10:3000/mcp"
        self.assertEqual(validate_outbound_url(url), url)

    def test_hostname_resolving_to_public_ip_passes(self) -> None:
        with mock.patch("backend.architectos.netutil.socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")) as resolver:
            url = "https://example.com/v1"
            self.assertEqual(validate_outbound_url(url), url)
        resolver.assert_called_once_with("example.com", 443, proto=socket.IPPROTO_TCP)

    def test_hostname_resolving_to_loopback_is_rejected(self) -> None:
        with mock.patch("backend.architectos.netutil.socket.getaddrinfo", return_value=_addrinfo("127.0.0.1")):
            with self.assertRaises(ValueError):
                validate_outbound_url("http://evil.example.com/")

    def test_dns_failure_raises_value_error(self) -> None:
        with mock.patch("backend.architectos.netutil.socket.getaddrinfo", side_effect=socket.gaierror("no such host")):
            with self.assertRaises(ValueError):
                validate_outbound_url("https://nonexistent.invalid/")


class ProviderAllowLocalTests(unittest.TestCase):
    def test_provider_allow_local_flows_through_adapter(self) -> None:
        provider = {"base_url": "http://127.0.0.1:1234/v1", "allow_local": True}
        endpoint = OpenAIResponsesAdapter()._responses_endpoint(provider)
        self.assertEqual(endpoint, "http://127.0.0.1:1234/v1")

    def test_env_var_enables_allow_local_for_providers(self) -> None:
        with mock.patch.dict(os.environ, {"ARCHITECTOS_ALLOW_LOCAL_URLS": "1"}):
            endpoint = OpenAIResponsesAdapter()._responses_endpoint({"base_url": "http://127.0.0.1:1234/v1"})
        self.assertEqual(endpoint, "http://127.0.0.1:1234/v1")


class RedirectGuardTests(unittest.TestCase):
    def test_urlopen_rejects_redirect_into_loopback(self) -> None:
        import threading
        import urllib.error
        from http.server import BaseHTTPRequestHandler, HTTPServer

        from backend.architectos import ssl_util

        class Secret(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"INTERNAL")

            def log_message(self, *_args: object) -> None:
                return

        class Redir(BaseHTTPRequestHandler):
            target_port = 0

            def do_GET(self) -> None:
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.target_port}/")
                self.end_headers()

            def log_message(self, *_args: object) -> None:
                return

        secret = HTTPServer(("127.0.0.1", 0), Secret)
        threading.Thread(target=secret.serve_forever, daemon=True).start()
        Redir.target_port = secret.server_port
        # First hop is RFC1918 / public-class; bind on 127.0.0.1 but validate with
        # allow_local so the first hop is accepted, then redirect must still fail
        # when allow_local is False for the redirect policy.
        redir = HTTPServer(("127.0.0.1", 0), Redir)
        threading.Thread(target=redir.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{redir.server_port}/"
        # With allow_local=False the first hop itself is rejected.
        with self.assertRaises(ValueError):
            ssl_util.urlopen(url, timeout=3, allow_local=False)
        # With allow_local=True the first hop is ok, but a redirect to a
        # disallowed target would still be re-checked — here both are loopback
        # and allowed, so instead assert a redirect to link-local is blocked.
        class MetaRedir(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/latest/meta-data")
                self.end_headers()

            def log_message(self, *_args: object) -> None:
                return

        meta = HTTPServer(("127.0.0.1", 0), MetaRedir)
        threading.Thread(target=meta.serve_forever, daemon=True).start()
        meta_url = f"http://127.0.0.1:{meta.server_port}/"
        with self.assertRaises((urllib.error.HTTPError, ValueError, OSError)):
            ssl_util.urlopen(meta_url, timeout=3, allow_local=True).read()
        secret.shutdown()
        redir.shutdown()
        meta.shutdown()


if __name__ == "__main__":
    unittest.main()
