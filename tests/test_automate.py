import base64
import plistlib
import threading
import urllib.error
import urllib.request
from datetime import date, timedelta

import pytest

from cbbsq import automate, db, picks
from cbbsq.config import Settings
from test_db_analysis import simulate_season


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("CBBSQ_SERVE_PASSWORD", raising=False)
    s = Settings.load(None)
    s.db_path = tmp_path / "t.sqlite"
    s.serve_password = None
    return s


def test_daily_skips_the_off_season(settings, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(automate, "collect", lambda *a, **k: calls.append(a) or 0)
    out = tmp_path / "r" / "dashboard.html"
    assert automate.run_daily(settings, today=date(2026, 10, 1), out=out, log=lambda m: None) == 0
    assert calls == [] and not out.exists()


def test_daily_collects_yesterday_and_today_then_writes_both_pages(settings, tmp_path, monkeypatch):
    conn = db.connect(settings.db_path)
    simulate_season(conn)
    conn.execute("UPDATE games SET home_spread_pre = -3.5, total_pre = 140.5")
    conn.commit()
    last = date.fromisoformat(conn.execute("SELECT MAX(game_date) FROM games").fetchone()[0])
    conn.close()
    calls = []
    monkeypatch.setattr(automate, "collect", lambda s, dates, **k: calls.append((list(dates), k)) or 0)
    out = tmp_path / "r" / "dashboard.html"
    today = last + timedelta(days=1)
    assert automate.run_daily(settings, today=today, out=out, log=lambda m: None) == 0
    (dates, kwargs), = calls
    assert dates == [today - timedelta(days=1), today] and kwargs["skip_done"]
    assert out.exists() and out.with_name("dashboard-totals.html").exists()
    conn = db.connect(settings.db_path)
    assert len(picks.graded(picks.load_picks(conn))) > 0 and len(picks.graded(picks.load_picks(conn, market="total"))) > 0
    conn.close()


def test_daily_still_rebuilds_pages_when_collecting_fails(settings, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("login expired")

    monkeypatch.setattr(automate, "collect", boom)
    logs = []
    out = tmp_path / "r" / "dashboard.html"
    assert automate.run_daily(settings, today=date(2026, 11, 5), out=out, log=logs.append) == 1
    assert out.exists() and any("login expired" in m for m in logs)


def test_launchd_plists(tmp_path):
    daily = plistlib.loads(automate.launchd_plist("com.cbbsq.daily", ["py", "-m", "cbbsq.cli", "daily"], tmp_path,
                                                  tmp_path / "daily.log", times=["10:00", "17:30"]))
    assert daily["Label"] == "com.cbbsq.daily" and daily["ProgramArguments"][-1] == "daily"
    assert daily["StartCalendarInterval"] == [{"Hour": 10, "Minute": 0}, {"Hour": 17, "Minute": 30}]
    assert daily["WorkingDirectory"] == str(tmp_path) and "KeepAlive" not in daily
    server = plistlib.loads(automate.launchd_plist("com.cbbsq.serve", ["py"], tmp_path, tmp_path / "s.log", keep_alive=True))
    assert server["KeepAlive"] and server["RunAtLoad"] and "StartCalendarInterval" not in server
    with pytest.raises(ValueError):
        automate.launchd_plist("x", ["py"], tmp_path, tmp_path / "l", times=["25:00"])


def test_schedule_is_macos_only(tmp_path, monkeypatch):
    monkeypatch.setattr(automate.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="macOS"):
        automate.install_schedule(tmp_path / ".env")


def test_password_is_generated_once_and_saved(settings, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("SQ_EMAIL=a@b.c")
    pw = automate.ensure_password(settings, env)
    assert len(pw) >= 10 and f"CBBSQ_SERVE_PASSWORD={pw}" in env.read_text()
    assert env.read_text().startswith("SQ_EMAIL=a@b.c\n")
    settings.serve_password = pw
    assert automate.ensure_password(settings, env) == pw and env.read_text().count("CBBSQ_SERVE_PASSWORD") == 1


def test_server_requires_the_password(tmp_path):
    (tmp_path / "dashboard.html").write_text("<p>spreads</p>")
    server = automate.make_server(tmp_path, "s3cret", host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def get(path, password=None):
        req = urllib.request.Request(base + path)
        if password is not None:
            req.add_header("Authorization", "Basic " + base64.b64encode(f"me:{password}".encode()).decode())
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read().decode(), resp.headers
        except urllib.error.HTTPError as err:
            return err.code, "", err.headers

    try:
        assert get("/dashboard.html")[0] == 401
        assert get("/dashboard.html", "wrong")[0] == 401
        status, body, headers = get("/dashboard.html", "s3cret")
        assert status == 200 and "spreads" in body and headers["Cache-Control"] == "no-store"
        status, body, _ = get("/", "s3cret")  # / goes to the spreads page
        assert status == 200 and "spreads" in body
        assert get("/../t.sqlite", "s3cret")[0] == 404  # nothing outside the reports folder
    finally:
        server.shutdown()
        server.server_close()
