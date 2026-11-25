"""Vercel entry point: every request is routed here (see vercel.json) and
handed to app.handle()."""
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def _serve(self, method: str) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        status, headers, out = app.handle(method, self.path, {k: v for k, v in self.headers.items()}, body)
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(out)

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")

    def do_HEAD(self):
        self._serve("HEAD")
