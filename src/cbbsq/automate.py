"""Daily routine, a macOS schedule for it, and a small password-protected web server for the pages.

* ``run_daily``   - collect yesterday's finals and today's slate, then rebuild both pages
* ``launchd_plist`` / ``install_schedule`` / ``remove_schedule`` - run it (and the server) in the background
* ``serve``       - serve the reports folder over HTTP with a password, for other devices
"""

from __future__ import annotations

import base64
import hmac
import os
import plistlib
import secrets
import socket
import subprocess
import sys
from datetime import date, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import analysis, db, picks
from .collect import collect, in_season
from .config import Settings
from .report import write_pages

DAILY_LABEL = "com.cbbsq.daily"
SERVE_LABEL = "com.cbbsq.serve"
DEFAULT_TIMES = ("08:00", "17:00")  # morning: results and the day's first lines; evening: refreshed lines


def notify(title: str, message: str) -> None:
    """A macOS notification (does nothing elsewhere), so a failed background run doesn't go unnoticed."""
    if sys.platform != "darwin":
        return

    def quote(text: str) -> str:  # AppleScript string literal
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

    script = f"display notification {quote(message)} with title {quote(title)}"
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def run_daily(settings: Settings, *, today: Optional[date] = None, out: Path = Path("reports/dashboard.html"),
              headless: bool = True, force: bool = False, log: Callable[[str], None] = print) -> int:
    """Yesterday's final scores (grades yesterday's picks), today's games and lines (saves today's picks),
    then both pages. Off-season days (May-October) are skipped unless ``force``. Returns 0 if all went well."""
    today = today or date.today()
    if not force and not in_season(today):
        log(f"{today}: off-season, nothing to do (the season runs November to April).")
        return 0
    problems = 0
    try:
        # One browser session. Yesterday is skipped if all its games are already final; today is re-scraped
        # on every run until its games are final (lines move during the day).
        problems += collect(settings, [today - timedelta(days=1), today], headless=headless, skip_done=True, log=log)
    except Exception as exc:  # e.g. the login expired: still rebuild the pages from what we have
        problems += 1
        log(f"Collecting failed: {exc}")
        notify("CBB Shot Quality", f"Daily update could not collect: {str(exc)[:120]}")
    conn = db.connect(settings.db_path)
    new, graded = picks.update(conn, today=today)
    tg = analysis.load_team_games(conn)
    pages = write_pages(conn, tg, out, today.isoformat())
    conn.close()
    log(f"Picks: {new} new, {graded} graded. Pages: {pages['spread']} and {pages['total']}")
    if problems:
        notify("CBB Shot Quality", "Daily update finished with problems - see data/daily.log")
    return 1 if problems else 0


# ------------------------------------------------------------------ schedule (macOS launchd)
def _parse_time(value: str) -> dict:
    hour, minute = (int(x) for x in value.split(":"))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"not a time: {value!r} (use HH:MM, 24-hour)")
    return {"Hour": hour, "Minute": minute}


def launchd_plist(label: str, args: list[str], workdir: Path, log_path: Path, *,
                  times: Iterable[str] = (), keep_alive: bool = False) -> bytes:
    """A LaunchAgent: run ``args`` at each HH:MM in ``times`` (launchd catches up a run missed while the
    Mac slept), or keep it running (``keep_alive``, for the web server)."""
    plist: dict = {
        "Label": label,
        "ProgramArguments": args,
        "WorkingDirectory": str(workdir),
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                 "PYTHONUNBUFFERED": "1"},
    }
    if times:
        plist["StartCalendarInterval"] = [_parse_time(t) for t in times]
    if keep_alive:
        plist["RunAtLoad"] = True
        plist["KeepAlive"] = True
    return plistlib.dumps(plist)


def _agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], check=False, capture_output=True, text=True)


def _load(plist_path: Path, label: str) -> None:
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{label}")  # replace an older copy if there is one
    res = _launchctl("bootstrap", domain, str(plist_path))
    if res.returncode != 0:
        raise RuntimeError(f"launchctl could not load {plist_path.name}: {res.stderr.strip() or res.stdout.strip()}")


def install_schedule(env_file: Path, *, times: Iterable[str] = DEFAULT_TIMES, serve: bool = False,
                     workdir: Optional[Path] = None, log: Callable[[str], None] = print) -> list[Path]:
    """Install (or replace) the daily job, and optionally the always-on web server, for this user."""
    if sys.platform != "darwin":
        raise RuntimeError("`cbbsq schedule` sets up macOS background jobs. On Linux use cron "
                           "(see the README); on Windows, Task Scheduler.")
    workdir = (workdir or Path.cwd()).resolve()
    (workdir / "data").mkdir(exist_ok=True)
    base = [sys.executable, "-m", "cbbsq.cli", "--env", str(Path(env_file).resolve())]
    jobs = [(DAILY_LABEL, launchd_plist(DAILY_LABEL, base + ["daily"], workdir, workdir / "data" / "daily.log",
                                        times=list(times)))]
    if serve:
        jobs.append((SERVE_LABEL, launchd_plist(SERVE_LABEL, base + ["serve"], workdir, workdir / "data" / "serve.log",
                                                keep_alive=True)))
    _agents_dir().mkdir(parents=True, exist_ok=True)
    written = []
    for label, content in jobs:
        path = _agents_dir() / f"{label}.plist"
        path.write_bytes(content)
        _load(path, label)
        written.append(path)
        log(f"Installed {path}")
    return written


def remove_schedule(log: Callable[[str], None] = print) -> None:
    for label in (DAILY_LABEL, SERVE_LABEL):
        path = _agents_dir() / f"{label}.plist"
        if sys.platform == "darwin":
            _launchctl("bootout", f"gui/{os.getuid()}/{label}")
        if path.exists():
            path.unlink()
            log(f"Removed {path}")


# ------------------------------------------------------------------ web server
def ensure_password(settings: Settings, env_file: Path) -> str:
    """The server's password: CBBSQ_SERVE_PASSWORD from .env, or a new random one saved there."""
    if settings.serve_password:
        return settings.serve_password
    password = secrets.token_urlsafe(9)
    env_file = Path(env_file)
    existing = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    sep = "" if not existing or existing.endswith("\n") else "\n"
    env_file.write_text(existing + sep + f"\n# Password for `cbbsq serve` (any username)\nCBBSQ_SERVE_PASSWORD={password}\n",
                        encoding="utf-8")
    os.environ["CBBSQ_SERVE_PASSWORD"] = password
    return password


class _Handler(SimpleHTTPRequestHandler):
    """Static files from the reports folder, behind HTTP Basic auth (any username, the password)."""

    password = ""

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except Exception:
            return False
        return hmac.compare_digest(given.encode(), self.password.encode())

    def do_GET(self):  # noqa: N802 (http.server naming)
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="CBB Shot Quality"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path in ("/", ""):
            self.send_response(302)
            self.send_header("Location", "/dashboard.html")
            self.end_headers()
            return
        super().do_GET()

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")  # always show the latest rebuild
        super().end_headers()

    def log_message(self, fmt, *args):  # quieter log: one line per request
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")


def _addresses() -> list[str]:
    """This machine's addresses worth printing: Tailscale (100.x) first, then the local network."""
    found = set()
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], check=False, capture_output=True, text=True, timeout=3)
        found |= {line.strip() for line in out.stdout.splitlines() if line.strip()}
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        found.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return sorted(found, key=lambda a: (not a.startswith("100."), a))


def make_server(reports: Path, password: str, host: str = "0.0.0.0", port: int = 8765) -> ThreadingHTTPServer:
    handler = partial(type("Handler", (_Handler,), {"password": password}), directory=str(Path(reports).resolve()))
    return ThreadingHTTPServer((host, port), handler)


def serve(reports: Path, password: str, host: str = "0.0.0.0", port: int = 8765,
          log: Callable[[str], None] = print) -> None:
    server = make_server(reports, password, host, port)
    log(f"Serving {Path(reports).resolve()} on port {port} (password protected; any username).")
    for addr in _addresses():
        tag = " (Tailscale)" if addr.startswith("100.") else " (this network)"
        log(f"  http://{addr}:{port}/{tag}")
    log("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
