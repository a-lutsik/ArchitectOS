from __future__ import annotations

import os
import ssl
import unittest
from unittest import mock

from backend.architectos import ssl_util


class SslUtilTests(unittest.TestCase):
    def test_ssl_context_uses_certifi_when_available(self) -> None:
        try:
            import certifi
        except ImportError:
            self.skipTest("certifi is not installed")
        ctx = ssl_util.ssl_context()
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertEqual(ssl_util.ca_bundle_path(), certifi.where())

    def test_configure_default_ssl_sets_env_when_missing(self) -> None:
        try:
            import certifi
        except ImportError:
            self.skipTest("certifi is not installed")
        env = {k: v for k, v in os.environ.items() if k not in {"SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"}}
        with mock.patch.dict(os.environ, env, clear=True):
            path = ssl_util.configure_default_ssl()
            self.assertEqual(path, certifi.where())
            self.assertEqual(os.environ.get("SSL_CERT_FILE"), certifi.where())


if __name__ == "__main__":
    unittest.main()
