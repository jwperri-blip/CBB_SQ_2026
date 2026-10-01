from datetime import date, timedelta

import pandas as pd
import pytest

from cbbsq import analysis, db, picks
from test_db_analysis import game, simulate_season


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.sqlite")
    yield c
    c.close()


def seed(conn, d0=date(2026, 1, 1)):
    """Six games each for a team scoring well above its shots (Hot) and one below (Cold), then Cold @ Hot."""
    recs = []
    for i in range(6):
        d = d0 + timedelta(days=i)
        recs.append(game(d, f"Filler {i}", "Hot Owls", a=(60, 62.0, 0.86, 0.88), h=(80, 70.0, 1.14, 1.0)))
        recs.append(game(d, "Cold Hawks", f"Other {i}", a=(60, 70.0, 0.86, 1.0), h=(70, 68.0, 1.0, 0.97)))
    slate = d0 + timedelta(days=10)
    today = game(slate, "Cold Hawks", "Hot Owls", status="Scheduled", a=(None,) * 4, h=(None,) * 4,
                 total=150.5, pre=(1.00, 1.10))
    today["home_spread_pre"] = -6.5
    db.upsert_games(conn, recs + [today])
    return slate, today


def slate_picks(conn, rec):
    p = picks.load_picks(conn)
    return p[p.game_id == rec["game_id"]].set_index("strategy")


def finish(conn, rec, away, home):
    db.upsert_games(conn, [dict(rec, status="Final", away_score=away, home_score=home)])


def test_picks_saved_then_graded(conn):
    slate, today = seed(conn)
    picks.update(conn, today=slate)  # run on game day (the earlier, final dates get filled in too)
    p = slate_picks(conn, today)
    assert set(p.index) >= {"luck", "opp_misses", "sq_spread", "sq_total"} and p["result"].isna().all()
    assert p.loc["luck", "pick"] == "Cold Hawks" and p.loc["luck", "line"] == 6.5
    assert p.loc["luck", "source"] == "saved"
    # ShotQuality projects Hot by about 0.1 PPP x ~70 possessions, more than the 6.5 line: back Hot.
    assert p.loc["sq_spread", "pick"] == "Hot Owls" and p.loc["sq_spread", "line"] == -6.5
    assert p.loc["sq_total", "pick"] in ("Over", "Under") and p.loc["sq_total", "line"] == 150.5

    finish(conn, today, 70, 74)  # Hot wins by 4: Cold +6.5 covers by 2.5, Hot -6.5 loses by 2.5
    new, graded = picks.update(conn, today=slate + timedelta(days=1))
    assert new == 0 and graded == len(p)
    g = slate_picks(conn, today)
    assert g.loc["luck", "result"] == "W" and g.loc["luck", "cover"] == pytest.approx(2.5)
    assert g.loc["luck", "units"] == pytest.approx(100 / 110)
    assert g.loc["sq_spread", "result"] == "L" and g.loc["sq_spread", "units"] == -1
    over = g.loc["sq_total", "pick"] == "Over"
    assert g.loc["sq_total", "result"] == ("L" if over else "W")  # 144 total vs 150.5
    assert g.loc["sq_total", "cover"] == pytest.approx(-6.5 if over else 6.5)
    assert (g["source"] == "saved").all()

    # Graded picks never change, even if the slate is recomputed later.
    picks.save_picks(conn, [slate.isoformat()], today=slate + timedelta(days=30))
    assert slate_picks(conn, today).loc["luck", "saved_at"] == g.loc["luck", "saved_at"]


def test_push_void_and_backfill(conn):
    slate, today = seed(conn)
    finish(conn, today, 70, 76)  # Hot by exactly 6 against a 6-point line: a push
    conn.execute("UPDATE games SET home_spread_pre = -6 WHERE game_id = ?", (today["game_id"],))
    conn.commit()
    picks.update(conn, today=slate + timedelta(days=60))  # computed long after: backfill
    g = slate_picks(conn, today)
    assert g.loc["luck", "source"] == "backfill"
    assert g.loc["luck", "result"] == "P" and g.loc["luck", "units"] == 0

    other = game(slate, "Some Team", "Another Team", status="Postponed", a=(None,) * 4, h=(None,) * 4)
    db.upsert_games(conn, [other])
    conn.execute("INSERT INTO picks (game_id, strategy, pick_date, pick, line, edge, source, saved_at) "
                 "VALUES (?, 'luck', ?, 'Some Team', 3, 1, 'saved', 'x')", (other["game_id"], slate.isoformat()))
    picks.grade_picks(conn)
    assert conn.execute("SELECT result, units FROM picks WHERE game_id = ?", (other["game_id"],)).fetchone()[:] == ("V", 0)


def test_picks_use_only_earlier_games(conn):
    slate, today = seed(conn)
    before = {p["strategy"]: p for p in picks.picks_for_date(
        analysis.load_team_games(conn, end=(slate - timedelta(days=1)).isoformat()), analysis.load_slate(conn, slate.isoformat()))}
    # A blowout later the same day must not move that morning's picks.
    db.upsert_games(conn, [game(slate, "Hot Owls", "Late Game", a=(30, 90.0, 0.4, 1.3))])
    picks.save_picks(conn, [slate.isoformat()], today=slate)
    saved = slate_picks(conn, today)
    for s, p in before.items():
        assert saved.loc[s, "pick"] == p["pick"] and saved.loc[s, "edge"] == pytest.approx(p["edge"])


def test_scorecard_buckets_and_math():
    rows = []
    for i in range(12):
        rows.append({"strategy": "luck", "edge": float(i), "result": "W" if i >= 6 else "L",
                     "units": 100 / 110 if i >= 6 else -1.0})
    rows.append({"strategy": "luck", "edge": 99.0, "result": None, "units": None})  # ungraded: ignored
    card = picks.scorecard(pd.DataFrame(rows))
    allrow = card[card.edge_size == "All"].iloc[0]
    assert allrow.bets == 12 and allrow.record == "6-6" and allrow.win_pct == 0.5
    assert allrow.units == pytest.approx(6 * 100 / 110 - 6)
    sizes = card[card.edge_size != "All"]
    assert list(sizes.edge_size.str.split().str[0]) == ["Small", "Medium", "Large"]
    assert list(sizes.record) == ["0-4", "2-2", "4-0"]
    assert picks.scorecard(pd.DataFrame(columns=["strategy", "edge", "result", "units"])).empty


def test_whole_season_backfill_and_report(conn, tmp_path):
    simulate_season(conn)
    new, graded = picks.update(conn)
    assert new > 0 and graded > 0
    assert picks.update(conn) == (0, 0)  # nothing new on a second run
    card = picks.scorecard(picks.load_picks(conn))
    assert set(card.signal) == {"SQ projection vs total"}  # this simulated season has totals but no spreads
    from cbbsq.report import write_report

    html = write_report(analysis.load_team_games(conn), tmp_path / "d.html", scorecard=card).read_text()
    assert "SQ projection vs total" in html
