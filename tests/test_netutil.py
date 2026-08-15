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


if __name__ == "__main__":
    unittest.main()
