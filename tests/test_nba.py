"""The NBA profile (`nbasq`): same engine, its own settings, data, pages, calendar and team matching."""

from datetime import date, timedelta

import pytest

from cbbsq import automate, cli, db, league, picks
from cbbsq.audit import run as audit_run
from cbbsq.collect import in_season
from cbbsq.config import Settings
from cbbsq.parse import resolve_side
from test_db_analysis import game, simulate_season


@pytest.fixture
def nba():
    lg = league.use("nba")
    yield lg
    league.use("cbb")


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("SQ_EMAIL", "SQ_PASSWORD", "SQ_SCORECENTER_URL", "NBA_SQ_SCORECENTER_URL", "NBA_SQ_EMAIL",
                "CBBSQ_DB", "NBASQ_DB", "CBBSQ_SERVE_PASSWORD", "NBASQ_SERVE_PASSWORD", "NBASQ_SERVE_PORT"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_cbb_is_the_default_and_unchanged(clean_env):
    assert league.current() is league.CBB
    s = Settings.load(None)
    assert str(s.db_path) == "data/cbbsq.sqlite" and str(s.raw_dir) == "data/raw"
    assert str(s.profile_dir) == ".auth/profile" and s.serve_port == 8765


def test_nba_settings_are_separate(nba, clean_env):
    clean_env.setenv("SQ_SCORECENTER_URL", "https://sq.test/cbb")
    clean_env.setenv("NBA_SQ_SCORECENTER_URL", "https://sq.test/nba")
    clean_env.setenv("SQ_EMAIL", "me@example.com")
    clean_env.setenv("CBBSQ_SERVE_PASSWORD", "cbb-secret")
    s = Settings.load(None)
    assert s.scorecenter_url == "https://sq.test/nba"
    assert s.email == "me@example.com"  # one ShotQuality login for both
    assert str(s.db_path) == "data/nba/nbasq.sqlite" and str(s.raw_dir) == "data/nba/raw"
    assert str(s.profile_dir) == ".auth/nba-profile" and s.serve_port == 8766
    assert s.serve_password is None  # never the college server's password


def test_nba_never_falls_back_to_the_college_scorecenter(nba, clean_env):
    clean_env.setenv("SQ_SCORECENTER_URL", "https://sq.test/cbb")
    assert Settings.load(None).scorecenter_url == "https://trader.shotquality.com/nba/scorecenter"


def test_nba_season_calendar(nba):
    assert in_season(date(2026, 10, 21)) and in_season(date(2027, 6, 10)) and in_season(date(2027, 4, 1))
    assert not in_season(date(2027, 7, 15)) and not in_season(date(2026, 9, 30))
    league.use("cbb")
    assert not in_season(date(2026, 10, 21))


@pytest.mark.parametrize("abbr, away, home, side", [
    ("LAL", "Los Angeles Clippers", "Los Angeles Lakers", "home"),
    ("LAC", "LA Clippers", "Los Angeles Lakers", "away"),
    ("CHA", "Chicago Bulls", "Charlotte Hornets", "home"),
    ("CHI", "Chicago Bulls", "Charlotte Hornets", "away"),
    ("BKN", "Nets", "Knicks", "away"),           # nickname-only cards
    ("NYK", "Nets", "Knicks", "home"),
    ("GS", "Warriors", "Suns", "away"),
    ("PHX", "Golden State", "Phoenix", "home"),  # city-only cards
    ("PHI", "Philadelphia 76ers", "Boston Celtics", "away"),
    ("SAS", "San Antonio Spurs", "Sacramento Kings", "away"),
    ("SAC", "San Antonio Spurs", "Sacramento Kings", "home"),
])
def test_nba_spread_abbreviations(nba, abbr, away, home, side):
    assert resolve_side(abbr, away, home) == side


def test_nba_table_is_not_used_for_college():
    # "CHI" would mean the Bulls in the NBA; for college it's letter matching only.
    assert resolve_side("CHI", "Chicago St. Cougars", "Buffalo Bulls") == "away"


def test_parser_defaults_follow_the_league(clean_env):
    league.use("nba")
    try:
        args = cli.build_parser().parse_args(["report"])
        assert args.out == "reports/nba/dashboard.html"
        args = cli.build_parser().parse_args(["serve"])
        assert args.dir == "reports/nba"
    finally:
        league.use("cbb")
    assert cli.build_parser().parse_args(["report"]).out == "reports/dashboard.html"


def test_main_selects_the_league(clean_env, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_status", lambda s, a: seen.append((league.current().key, str(s.db_path))))
    cli.main_nba(["--env", "", "status"])
    cli.main(["--env", "", "--league", "nba", "status"])
    cli.main(["--env", "", "status"])
    assert seen == [("nba", "data/nba/nbasq.sqlite"), ("nba", "data/nba/nbasq.sqlite"), ("cbb", "data/cbbsq.sqlite")]


def test_nba_daily_writes_nba_pages(nba, clean_env, tmp_path, monkeypatch):
    s = Settings.load(None)
    s.db_path = tmp_path / "nba.sqlite"
    conn = db.connect(s.db_path)
    simulate_season(conn)
    conn.execute("UPDATE games SET home_spread_pre = -3.5, total_pre = 228.5")
    conn.commit()
    last = date.fromisoformat(conn.execute("SELECT MAX(game_date) FROM games").fetchone()[0])
    conn.close()
    monkeypatch.setattr(automate, "collect", lambda *a, **k: 0)
    out = tmp_path / "nba" / "dashboard.html"
    assert automate.run_daily(s, today=last + timedelta(days=1), out=out, log=lambda m: None, force=True) == 0
    html = out.read_text(encoding="utf-8")
    assert "<title>NBA Shot Quality</title>" in html and "CBB Shot Quality" not in html
    assert "'nbasq_" in html and "'cbbsq_" not in html
    assert out.with_name("dashboard-totals.html").exists()
    conn = db.connect(s.db_path)
    assert len(picks.graded(picks.load_picks(conn))) > 0
    conn.close()


def test_nba_daily_skips_the_off_season(nba, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(automate, "collect", lambda *a, **k: calls.append(a) or 0)
    s = Settings.load(None)
    s.db_path = tmp_path / "nba.sqlite"
    out = tmp_path / "dashboard.html"
    assert automate.run_daily(s, today=date(2026, 8, 1), out=out, log=lambda m: None) == 0
    assert calls == [] and not out.exists()


def test_nba_audit_accepts_nba_scores(nba, tmp_path):
    conn = db.connect(tmp_path / "a.sqlite")
    d = date(2026, 10, 25)
    db.upsert_games(conn, [
        game(d, "Boston Celtics", "Indiana Pacers", a=(158, 150.0, 1.5, 1.42), h=(161, 152.0, 1.53, 1.45)),
        game(d, "Utah Jazz", "Miami Heat", a=(40, 50.0, 0.42, 0.5), h=(110, 108.0, 1.1, 1.08)),
    ])
    f = next(x for x in audit_run(conn) if x.title == "Values in a plausible range")
    assert "1 final game(s)" in f.detail and "60-190" in f.detail and "Utah" in f.examples[0]
    conn.close()
