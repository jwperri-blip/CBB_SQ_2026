import random
from datetime import date, timedelta

import numpy as np
import pytest

from cbbsq import analysis, db
from cbbsq.parse import GAME_FIELDS, make_game_id, season_for
from cbbsq.report import write_report


def game(d, away, home, *, status="Final", a=(70, 68.0, 1.0, 0.97), h=(75, 71.0, 1.07, 1.01),
         spread_team=None, spread=None, total=140.5, pre=(1.02, 1.04)):
    """a/h = (points, SQ points, PPP, SQ PPP)."""
    rec = {f: None for f in GAME_FIELDS}
    rec.update(game_id=make_game_id(d, away, home), game_date=d.isoformat(), season=season_for(d), status=status,
               away_team=away, home_team=home,
               away_score=a[0], away_sq_score=a[1], away_ppp=a[2], away_sq_ppp=a[3],
               home_score=h[0], home_sq_score=h[1], home_ppp=h[2], home_sq_ppp=h[3],
               away_pregame_sq_ppp=pre[0], home_pregame_sq_ppp=pre[1], away_sq_pct=40, home_sq_pct=60,
               spread_pre_team=spread_team, spread_pre=spread, total_pre=total)
    if spread is not None and spread_team:
        rec["home_spread_pre"] = spread if spread_team == home[:3].upper() else None
    return rec


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.sqlite")
    yield c
    c.close()


def test_upsert_live_then_final_and_no_downgrade(conn):
    d = date(2026, 1, 10)
    live = game(d, "Army West Point Black Knights", "Boston U. Terriers", status="Live", a=(53, 49.1, 1.11, 1.03))
    db.upsert_games(conn, [live])
    final = game(d, "Army West Point Black Knights", "Boston U. Terriers", a=(80, 71.0, 1.1, 1.0))
    final["away_sq_pct"] = None  # a missing value must not erase the known one
    db.upsert_games(conn, [final])
    stale = dict(final, status="Live", away_score=1)
    db.upsert_games(conn, [stale])
    row = conn.execute("SELECT status, away_score, away_sq_pct FROM games").fetchone()
    assert tuple(row) == ("Final", 80, 40)
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 3


def test_team_games_view_perspective(conn):
    d = date(2026, 1, 10)
    rec = game(d, "West Ga. Wolves", "Jacksonville Dolphins", a=(43, 58.3, 0.71, 0.96), h=(75, 62.4, 1.24, 1.03))
    rec["home_spread_pre"] = -1.5
    db.upsert_games(conn, [rec])
    tg = analysis.load_team_games(conn)
    jax = tg[tg.team == "Jacksonville Dolphins"].iloc[0]
    wga = tg[tg.team == "West Ga. Wolves"].iloc[0]
    assert jax.spread == -1.5 and wga.spread == 1.5
    assert jax.margin == 32 and wga.margin == -32
    assert jax.sq_margin == pytest.approx(4.1)
    assert jax.luck == pytest.approx(32 - 4.1)  # won by 32, shots said 4.1
    assert jax.cover_margin == pytest.approx(30.5) and wga.cover_margin == pytest.approx(-30.5)
    assert jax.poss == pytest.approx(75 / 1.24)


def test_line_abbreviation_learned_from_other_games(conn):
    d = date(2026, 1, 10)
    g1 = game(d, "North Carolina Tar Heels", "Duke Blue Devils", spread_team="UNC", spread=-2.5)
    g2 = game(d + timedelta(days=3), "Wake Forest Demon Deacons", "North Carolina Tar Heels",
              spread_team="UNC", spread=-8.0)
    db.upsert_games(conn, [g1, g2])
    rows = dict(conn.execute("SELECT home_team, home_spread_pre FROM games").fetchall())
    assert rows["Duke Blue Devils"] == 2.5  # UNC (away) -2.5 -> home +2.5
    assert rows["North Carolina Tar Heels"] == -8.0


def test_collected_dates(conn):
    d = date(2026, 1, 10)
    db.upsert_games(conn, [game(d, "A Owls", "B Hawks"), game(d + timedelta(days=1), "C Owls", "D Hawks", status="Live")])
    run = db.start_run(conn, "2026-01-12")
    db.finish_run(conn, run, ok=True, page_count=0)
    assert db.collected_dates(conn) == {"2026-01-10", "2026-01-12"}


def simulate_season(conn, n_teams=24, games_each=24, seed=7):
    """Round-robin-ish season where each team has a true SQ offense/defense."""
    rng = random.Random(seed)
    teams = [f"Team {i:02d}" for i in range(n_teams)]
    off = {t: rng.gauss(0, 0.06) for t in teams}
    dfn = {t: rng.gauss(0, 0.06) for t in teams}
    recs, d = [], date(2025, 11, 3)
    for rnd in range(games_each):
        order = teams[:]
        rng.shuffle(order)
        for i in range(0, n_teams, 2):
            away, home = order[i], order[i + 1]
            poss = rng.uniform(62, 74)
            a_sq = 1.0 + off[away] + dfn[home] - 0.01 + rng.gauss(0, 0.04)
            h_sq = 1.0 + off[home] + dfn[away] + 0.01 + rng.gauss(0, 0.04)
            a_ppp, h_ppp = a_sq + rng.gauss(0, 0.08), h_sq + rng.gauss(0, 0.08)
            recs.append(game(d, away, home,
                             a=(round(a_ppp * poss), a_sq * poss, round(a_ppp, 2), round(a_sq, 3)),
                             h=(round(h_ppp * poss), h_sq * poss, round(h_ppp, 2), round(h_sq, 3))))
        d += timedelta(days=3)
    db.upsert_games(conn, recs)
    return off, dfn


def test_adjusted_ratings_recover_truth(conn):
    off, dfn = simulate_season(conn)
    tg = analysis.load_team_games(conn)
    r = analysis.adjusted_ratings(tg, shrink=0.5)
    true_net = {t: off[t] - dfn[t] for t in off}
    corr = np.corrcoef([true_net[t] for t in r.index], r["adj_net"])[0, 1]
    assert corr > 0.95
    assert r.attrs["home_adv_ppp"] == pytest.approx(0.02, abs=0.015)


def test_summary_trending_luck_and_report(conn, tmp_path):
    simulate_season(conn)
    tg = analysis.load_team_games(conn)
    s = analysis.team_summary(tg, min_games=5)
    assert len(s) == 24 and s["G"].min() == 24
    assert s["sq_net"].is_monotonic_decreasing
    # possession-weighted SQ PPP should sit close to the per-game mean
    t0 = s.index[0]
    assert s.loc[t0, "off_sq_ppp"] == pytest.approx(tg[tg.team == t0]["sq_ppp"].mean(), abs=0.01)
    last5 = analysis.team_summary(tg, last_n=5)
    assert (last5["G"] == 5).all()
    tr = analysis.trending(tg, window=5)
    assert {"delta_net", "slope_per_game"} <= set(tr.columns) and len(tr) == 24
    lk = analysis.luck_table(tg)
    assert lk["luck"].is_monotonic_decreasing
    log = analysis.team_log(tg, t0, window=5)
    assert "sq_net_ppp_r5" in log and len(log) == 24
    out = write_report(tg, tmp_path / "dash.html")
    html = out.read_text()
    assert "/*__DATA__*/" not in html and t0 in html


def test_empty_database_is_handled(conn, tmp_path):
    tg = analysis.load_team_games(conn)
    assert tg.empty
    assert analysis.team_summary(tg).empty
    assert analysis.adjusted_ratings(tg).empty
    assert analysis.trending(tg).empty
    assert write_report(tg, tmp_path / "empty.html").exists()
