"""Shared fixtures: a tiny local HTTP server with fast/slow/error endpoints."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, delay: float = 0.0):
        import time

        if delay:
            time.sleep(delay)
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/fast":
            self._send(200, b"hello world")
        elif self.path == "/slow":
            self._send(200, b"slow body", delay=0.3)
        elif self.path == "/boom":
            self._send(500, b"internal error")
        elif self.path == "/missing":
            self._send(404, b"not here")
        elif self.path == "/secret":
            self._send(200, b"the password is swordfish")
        else:
            self._send(200, b"ok")

    def log_message(self, *args):  # silence test output
        pass


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    thread.join(timeout=2)
