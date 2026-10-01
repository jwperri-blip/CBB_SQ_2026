"""End-to-end tests against the local mock ScoreCenter (tests/mock_site)."""

import sqlite3
import time
from datetime import date

import pytest

from cbbsq.collect import collect, reparse
from cbbsq.config import Settings
from cbbsq.scraper import LoginRequired, Scraper, suggest_url_template
from mock_server import BIG_SLATE_DATE, EMAIL, PASSWORD

pytestmark = pytest.mark.usefixtures("chromium_ok")


def make_settings(tmp_path, url, password=PASSWORD, **over):
    kw = dict(email=EMAIL, password=password, scorecenter_url=url, login_url=None, date_mode="auto",
              db_path=tmp_path / "db.sqlite", raw_dir=tmp_path / "raw", profile_dir=tmp_path / "profile",
              browser_channel=None, delay_seconds=0)
    kw.update(over)
    return Settings(**kw)


@pytest.mark.parametrize("path", [
    "/scorecenter?date={date}",      # date in the URL
    "/scorecenter",                  # popup calendar date picker
    "/scorecenter?picker=input",     # typed date input
    "/scorecenter?picker=shadcn&date=2026-09-24",  # shadcn calendar opening months away
])
def test_collect_all_date_modes(tmp_path, mock_site, path):
    s = make_settings(tmp_path, mock_site + path)
    msgs = []
    failures = collect(s, [date(2026, 1, 10), date(2026, 1, 11), date(2025, 12, 30)], log=msgs.append)
    assert failures == 0
    assert not [m for m in msgs if "warning" in m]  # page count and cards read agree on every date
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    rows = {r["game_id"]: dict(r) for r in conn.execute("SELECT * FROM games")}
    assert len(rows) == 6
    g = rows["2026-01-10_west-ga-wolves_at_jacksonville-dolphins"]
    assert g["status"] == "Final"
    assert (g["away_score"], g["home_score"], g["away_sq_score"], g["home_sq_score"]) == (43, 75, 58.3, 62.4)
    assert (g["away_sq_pct"], g["home_sq_pct"]) == (30, 39)
    assert (g["away_ppp"], g["home_ppp"], g["away_sq_ppp"], g["home_sq_ppp"]) == (0.71, 1.24, 0.96, 1.03)
    assert (g["away_pregame_sq_ppp"], g["home_pregame_sq_ppp"]) == (1.06, 1.08)
    assert (g["home_spread_pre"], g["total_pre"]) == (-1.5, 141.5)
    live = rows["2026-01-10_army-west-point-black-knights_at_boston-u-terriers"]
    assert (live["status"], live["status_detail"]) == ("Live", "2nd 11:06")
    assert (live["spread_cur"], live["total_cur"], live["home_spread_cur"]) == (-16.5, 165.5, -16.5)
    sched = rows["2026-01-10_utah-tech-trailblazers_at_tarleton-st-texans"]
    assert sched["status"] == "Scheduled" and sched["away_score"] is None
    assert (sched["away_pregame_sq_ppp"], sched["home_pregame_sq_ppp"]) == (1.01, 1.04)
    # single-game dates, including one reached by paging the calendar back a year
    assert rows["2026-01-11_jacksonville-dolphins_at_brown-bears"]["status_detail"] == "OT"
    assert rows["2025-12-30_penn-quakers_at_west-ga-wolves"]["home_spread_pre"] == 0.0
    # raw archive written, including the site's own JSON API responses
    assert len(list(s.raw_dir.glob("*/*.json.gz"))) == 3
    runs = conn.execute("SELECT COUNT(*) FROM scrape_runs WHERE ok = 1").fetchone()[0]
    assert runs == 3



def test_slow_big_slate_is_waited_for(tmp_path, mock_site):
    # the real site can take 15 s or more to show a big slate after a date pick
    s = make_settings(tmp_path, mock_site + "/scorecenter?picker=shadcn&date=2026-09-24&lag=18000")
    msgs = []
    assert collect(s, [date(2026, 1, 11), date(2026, 1, 10)], log=msgs.append) == 0
    assert not [m for m in msgs if "warning" in m]
    assert sqlite3.connect(s.db_path).execute("SELECT COUNT(*) FROM games").fetchone()[0] == 5


def test_raw_archive_contains_api_json_and_reparse(tmp_path, mock_site):
    import gzip
    import json

    s = make_settings(tmp_path, mock_site + "/scorecenter?date={date}")
    collect(s, [date(2026, 1, 10)], log=lambda m: None)
    (path,) = s.raw_dir.glob("2026-01-10/*.json.gz")
    raw = json.load(gzip.open(path, "rt"))
    urls = [n["url"] for n in raw["network"]]
    assert any("/api/cbb/scorecenter?date=2026-01-10" in u for u in urls)
    assert not any("/api/login" in u for u in urls)  # auth traffic is never archived
    assert reparse(s, log=lambda m: None) == 4


def test_skip_done_and_session_reuse(tmp_path, mock_site):
    s = make_settings(tmp_path, mock_site + "/scorecenter?date={date}")
    collect(s, [date(2026, 1, 11)], log=lambda m: None)
    msgs = []
    collect(s, [date(2026, 1, 11)], skip_done=True, log=msgs.append)
    assert any("Skipping 1" in m for m in msgs)
    # the persistent profile keeps the login: no credentials needed any more
    s2 = make_settings(tmp_path, mock_site + "/scorecenter?date={date}", password=None, email=None)
    msgs = []
    assert collect(s2, [date(2025, 12, 30)], log=msgs.append) == 0
    assert not any("Logged in" in m for m in msgs)


def test_wrong_password_raises(tmp_path, mock_site):
    s = make_settings(tmp_path, mock_site + "/scorecenter", password="wrong")
    with pytest.raises(LoginRequired):
        with Scraper(s, log=lambda m: None) as scraper:
            scraper.ensure_logged_in()


def test_big_slate_extracts_every_card_quickly(tmp_path, mock_site):
    s = make_settings(tmp_path, mock_site + "/scorecenter?date={date}")
    with Scraper(s, log=lambda m: None) as scraper:
        scraper.ensure_logged_in()
        t0 = time.time()
        result = scraper.scrape_date(date.fromisoformat(BIG_SLATE_DATE))
        elapsed = time.time() - t0
    assert result["showing"] == 150 and len(result["cards"]) == 150
    names = {c["home"]["name"] for c in result["cards"]}
    assert len(names) == 150
    c = next(c for c in result["cards"] if c["home"]["name"] == "Home Team 007 Hawks")
    assert c["rows"]["score"] == ["67", "63"] and c["rows"]["sq_score"] == ["68.5", "60.5"]
    assert elapsed < 60


def test_suggest_url_template():
    d = date(2026, 1, 10)
    assert suggest_url_template("https://x/cbb/scorecenter?date=2026-01-10", d) == "https://x/cbb/scorecenter?date={date}"
    assert suggest_url_template("https://x/scores/20260110", d) == "https://x/scores/{date:%Y%m%d}"
    assert suggest_url_template("https://x/scorecenter", d) is None
