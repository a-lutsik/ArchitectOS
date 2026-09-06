"""Unit tests for the launcher single-instance guard (running_instance_url)."""

from __future__ import annotations

import os
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backend.architectos.launcher import _pid_is_alive, running_instance_url


class _ArchitectosMarkedHandler(BaseHTTPRequestHandler):
    """Serves a page that looks like the ArchitectOS SPA shell."""

    def do_GET(self) -> None:  # noqa: N802
        body = (
            b'<html><head><meta name="architectos-token" content="t">'
            b"</head><body>ArchitectOS Memory Graph</body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence test noise
        pass


class LauncherSingleInstanceTests(unittest.TestCase):
    def test_free_port_reports_no_running_instance(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
        sock.close()
        self.assertIsNone(running_instance_url("127.0.0.1", port))

    def test_foreign_listener_is_not_mistaken_for_architectos(self) -> None:
        # A non-ArchitectOS process on the port must NOT be "reused".
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = int(sock.getsockname()[1])
        try:
            self.assertIsNone(running_instance_url("127.0.0.1", port))
        finally:
            sock.close()

    def test_architectos_marked_listener_is_reused(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ArchitectosMarkedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = running_instance_url("127.0.0.1", int(server.server_port))
            self.assertEqual(url, f"http://127.0.0.1:{server.server_port}")
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_pid_liveness_guard(self) -> None:
        self.assertTrue(_pid_is_alive(os.getpid()))
        self.assertFalse(_pid_is_alive(0))
        self.assertFalse(_pid_is_alive(-1))
        self.assertFalse(_pid_is_alive(2_000_000_000))


if __name__ == "__main__":
    unittest.main()
