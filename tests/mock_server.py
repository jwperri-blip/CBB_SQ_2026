"""A tiny stand-in for the ShotQuality site, used by the integration tests."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).parent
GAMES = json.loads((HERE / "fixtures" / "games.json").read_text())
EMAIL, PASSWORD, TOKEN = "fan@example.com", "hunter2", "tok-123"
BIG_SLATE_DATE = "2026-02-07"


def _big_slate(n: int = 150) -> list[dict]:
    """A full Saturday: n final games between made-up teams, deterministic values."""
    games = []
    for i in range(n):
        a, h = 60 + i % 25, 70 - i % 17
        games.append({
            "status": "Final", "detail": None,
            "away": {"name": f"Away Team {i:03d} Owls", "score": a, "sq": a + 1.5, "pct": i % 100,
                     "ppp": round(a / 68, 2), "sqppp": round((a + 1.5) / 68, 2), "pre": 1.01},
            "home": {"name": f"Home Team {i:03d} Hawks", "score": h, "sq": h - 2.5, "pct": (i * 7) % 100,
                     "ppp": round(h / 68, 2), "sqppp": round((h - 2.5) / 68, 2), "pre": 1.04},
            "line": {"pre": f"HOM -{i % 12}.5"}, "ou": {"pre": "140.5"},
        })
    return games


GAMES[BIG_SLATE_DATE] = _big_slate()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/login", "/"):
            self._send(200, (HERE / "mock_site" / "login.html").read_bytes(), "text/html")
        elif url.path == "/scorecenter":
            self._send(200, (HERE / "mock_site" / "scorecenter.html").read_bytes(), "text/html")
        elif url.path == "/api/cbb/scorecenter":
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._send(401, b'{"error":"unauthorized"}', "application/json")
                return
            day = parse_qs(url.query).get("date", [""])[0]
            self._send(200, json.dumps({"date": day, "games": GAMES.get(day, [])}).encode(), "application/json")
        elif url.path.startswith("/logos/"):
            self._send(404, b"", "image/png")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if urlparse(self.path).path != "/api/login":
            self._send(404, b"", "text/plain")
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if body.get("email") == EMAIL and body.get("password") == PASSWORD:
            self._send(200, json.dumps({"token": TOKEN}).encode(), "application/json")
        else:
            self._send(401, b'{"error":"bad credentials"}', "application/json")


def serve() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":
    srv, base = serve()
    print(f"mock ShotQuality at {base}/scorecenter  (login {EMAIL} / {PASSWORD})")
    threading.Event().wait()
