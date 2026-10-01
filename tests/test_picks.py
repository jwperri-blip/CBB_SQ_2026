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


def the_pick(conn, rec):
    return picks.load_picks(conn).set_index("game_id").loc[rec["game_id"]]


def finish(conn, rec, away, home):
    db.upsert_games(conn, [dict(rec, status="Final", away_score=away, home_score=home)])


def test_pick_saved_with_its_numbers_then_graded(conn):
    slate, today = seed(conn)
    new, graded = picks.update(conn, today=slate)  # run on game day
    assert new == 2 and graded == 0  # this game's spread and total picks (earlier dates lack 5 prior games)
    p = the_pick(conn, today)
    spot = analysis.spots_for_date(conn, slate.isoformat()).iloc[0]
    assert p.pick == "Cold Hawks" and p.line == 6.5 and p.source == "saved"
    assert p.luck_edge == pytest.approx(spot.gap) and p.opp_edge == pytest.approx(spot.opp_gap)
    assert p.last5_edge == pytest.approx(spot.l5_gap)
    # ShotQuality projects Hot by ~0.1 PPP x ~70 possessions, about 7: more than the 6.5 line, so it
    # prefers Hot and the SQ edge on the Cold bet is negative.
    assert p.sq_edge == pytest.approx(spot.sq_edge) and p.sq_edge < 0

    finish(conn, today, 70, 74)  # Hot by 4: Cold +6.5 covers by 2.5
    assert picks.update(conn, today=slate + timedelta(days=1)) == (0, 2)
    g = the_pick(conn, today)
    assert g.result == "W" and g.cover == pytest.approx(2.5) and g.units == pytest.approx(100 / 110)
    # Graded picks never change, even if the date is recomputed later.
    picks.save_picks(conn, [slate.isoformat()], today=slate + timedelta(days=30))
    assert the_pick(conn, today).saved_at == g.saved_at


def test_push_void_and_backfill(conn):
    slate, today = seed(conn)
    finish(conn, today, 70, 76)  # Hot by exactly 6 against a 6-point line: a push
    conn.execute("UPDATE games SET home_spread_pre = -6 WHERE game_id = ?", (today["game_id"],))
    conn.commit()
    picks.update(conn, today=slate + timedelta(days=60))  # computed long after: backfill
    g = the_pick(conn, today)
    assert g.source == "backfill" and g.result == "P" and g.units == 0

    other = game(slate, "Some Team", "Another Team", status="Postponed", a=(None,) * 4, h=(None,) * 4)
    db.upsert_games(conn, [other])
    conn.execute("INSERT INTO spot_picks (game_id, pick_date, pick, line, source, saved_at) "
                 "VALUES (?, ?, 'Some Team', 3, 'saved', 'x')", (other["game_id"], slate.isoformat()))
    picks.grade_picks(conn)
    assert tuple(conn.execute("SELECT result, units FROM spot_picks WHERE game_id = ?",
                              (other["game_id"],)).fetchone()) == ("V", 0)


def test_picks_use_only_earlier_games(conn):
    slate, today = seed(conn)
    morning = analysis.spots_for_date(conn, slate.isoformat()).iloc[0]
    # A blowout later the same day must not move that morning's pick.
    db.upsert_games(conn, [game(slate, "Hot Owls", "Late Game", a=(30, 90.0, 0.4, 1.3))])
    picks.save_picks(conn, [slate.isoformat()], today=slate)
    p = the_pick(conn, today)
    assert p.pick == morning.back and p.luck_edge == pytest.approx(morning.gap)


def graded_frame(edges, wins):
    return pd.DataFrame({"luck_edge": edges, "opp_edge": edges, "last5_edge": edges, "sq_edge": [None] * len(edges),
                         "result": ["W" if w else "L" for w in wins],
                         "units": [100 / 110 if w else -1.0 for w in wins]})


def test_stats_threshold_and_ladder():
    # Edges 0..19; the top half all win, the bottom half all lose.
    g = graded_frame([float(i) for i in range(20)], [i >= 10 for i in range(20)])
    s = picks.stats(g)
    assert s["bets"] == 20 and s["record"] == "10-10" and s["win_pct"] == 0.5
    assert s["units"] == pytest.approx(10 * 100 / 110 - 10)
    hit = picks.threshold_for(g, "luck_edge", 0.6, min_bets=5)
    # >= 3.5 (the 0.5-step grid's first hit) keeps edges 4..19: 10-6, 62.5%.
    assert hit["threshold"] == 3.5 and hit["record"] == "10-6" and not hit["any"]
    assert picks.threshold_for(g, "luck_edge", 0.5, min_bets=5)["any"]  # everything already hits 50%
    assert picks.threshold_for(g, "luck_edge", 0.95, min_bets=11) is None  # 100% only with 10 bets or fewer
    assert picks.threshold_for(g, "sq_edge", 0.5, min_bets=1) is None  # no numbers at all
    lad = picks.ladder(g, "luck_edge")
    assert lad["threshold"].is_monotonic_increasing and lad.iloc[0]["bets"] == 20
    assert lad["win_pct"].is_monotonic_increasing
    both = picks.filter_picks(g, {"luck_edge": 5, "opp_edge": 12, "sq_edge": None})
    assert list(both["luck_edge"]) == [12.0 + i for i in range(8)]


def test_whole_season_backfill_and_report(conn, tmp_path):
    simulate_season(conn)
    # Give every game a line so the picks can be graded.
    conn.execute("UPDATE games SET home_spread_pre = -2.5")
    conn.commit()
    new, graded = picks.update(conn)
    assert new > 100 and graded == new
    assert picks.update(conn) == (0, 0)  # nothing new on a second run
    g = picks.graded(picks.load_picks(conn))
    assert g["luck_edge"].notna().all() and g["result"].isin(["W", "L", "P"]).all()
    from cbbsq.report import write_report

    html = write_report(analysis.load_team_games(conn), tmp_path / "d.html", graded=g).read_text()
    assert '"graded":[{' in html


def test_dashboard_threshold_card_matches_python(conn, tmp_path, chromium_ok):
    from playwright.sync_api import sync_playwright

    from cbbsq.report import write_report

    simulate_season(conn)
    rows = conn.execute("SELECT game_id FROM games ORDER BY game_id").fetchall()
    for i, (gid,) in enumerate(rows):  # varied lines so results differ
        conn.execute("UPDATE games SET home_spread_pre = ? WHERE game_id = ?", (-4.5 + (i % 10), gid))
    conn.commit()
    picks.update(conn)
    g = picks.graded(picks.load_picks(conn))
    out = write_report(analysis.load_team_games(conn), tmp_path / "d.html", graded=g)
    with sync_playwright() as p:
        page = p.chromium.launch().new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(out.resolve().as_uri() + "#thresholds")
        for target in (52, 55):
            page.fill("#tgt", str(target))
            page.fill("#tminb", "20")
            first = page.locator("#find tbody tr").first.locator("td")
            hit = picks.threshold_for(g, "luck_edge", target / 100, 20)
            if hit is None:
                assert first.nth(1).inner_text() == "not reached"
            else:
                assert first.nth(1).inner_text() in ("any value", f"≥ {hit['threshold']:g}")
                assert first.nth(3).inner_text() == hit["record"]
        # Filter by luck edge: the tiles show the same record as Python's filter.
        page.fill("#f_luck_edge", "2")
        filtered = picks.stats(picks.filter_picks(g, {"luck_edge": 2}))
        assert filtered["record"] in page.locator("#worktiles").inner_text()
        # The season section under the title follows the same filter, with units in parentheses.
        summary = page.locator("#summary").inner_text()
        assert "Luck edge ≥ 2" in summary and filtered["record"] in summary
        assert f"({filtered['units']:+.1f}u)" in summary
        assert not errors


def test_total_pick_direction_numbers_and_grading(conn):
    slate, today = seed(conn)
    # Hot Owls' games: they score 10 over their shots, opponents 2 under: +8 at the 'hot' end.
    # Cold Hawks' games: they score 10 under, opponents 2 over: -8. Make Hot's games run hotter.
    conn.execute("UPDATE games SET home_score = home_score + 6 WHERE home_team = 'Hot Owls' AND status = 'Final'")
    conn.commit()
    spot = analysis.spots_for_date(conn, slate.isoformat(), market="total").iloc[0]
    assert spot.back == "Under" and spot.combined > 0 and spot.gap == pytest.approx(spot.combined)
    assert spot.back_line == 150.5
    # Every number is signed so that positive agrees with the pick.
    assert spot.opp_gap == pytest.approx((spot.away_tot_opp + spot.home_tot_opp) / 2)
    assert spot.sq_edge == pytest.approx(150.5 - spot.proj_total)

    picks.update(conn, today=slate)
    p = picks.load_picks(conn, market="total").set_index("game_id").loc[today["game_id"]]
    assert p.pick == "Under" and p.line == 150.5 and p.luck_edge == pytest.approx(spot.gap)

    finish(conn, today, 70, 74)  # 144 total: Under 150.5 wins by 6.5
    picks.update(conn, today=slate + timedelta(days=1))
    g = picks.load_picks(conn, market="total").set_index("game_id").loc[today["game_id"]]
    assert g.result == "W" and g.cover == pytest.approx(6.5) and g.units == pytest.approx(100 / 110)
    # The spread pick is graded separately, in its own table.
    assert the_pick(conn, today).result == "W"


def test_total_over_and_push(conn):
    slate, today = seed(conn)
    conn.execute("UPDATE games SET home_score = home_score - 30 WHERE home_team = 'Hot Owls' AND status = 'Final'")
    conn.commit()
    spot = analysis.spots_for_date(conn, slate.isoformat(), market="total").iloc[0]
    assert spot.back == "Over" and spot.combined < 0
    assert spot.sq_edge == pytest.approx(spot.proj_total - 150.5)  # Over: projection above the line agrees
    finish(conn, today, 70, 74)  # 144 points...
    conn.execute("UPDATE games SET total_pre = 144 WHERE game_id = ?", (today["game_id"],))  # ...on a 144 line: a push
    conn.commit()
    picks.update(conn, today=slate + timedelta(days=5))
    g = picks.load_picks(conn, market="total").set_index("game_id").loc[today["game_id"]]
    assert g.pick == "Over" and g.result == "P" and g.units == 0 and g.source == "backfill"


def test_total_picks_use_only_earlier_games(conn):
    slate, today = seed(conn)
    morning = analysis.spots_for_date(conn, slate.isoformat(), market="total").iloc[0]
    db.upsert_games(conn, [game(slate, "Hot Owls", "Late Game", a=(130, 60.0, 1.9, 0.9))])
    picks.save_picks(conn, [slate.isoformat()], today=slate, market="total")
    p = picks.load_picks(conn, market="total").set_index("game_id").loc[today["game_id"]]
    assert p.pick == morning.back and p.luck_edge == pytest.approx(morning.gap)


def test_report_writes_spreads_and_totals_pages(conn, tmp_path, chromium_ok, monkeypatch):
    from playwright.sync_api import sync_playwright

    from cbbsq.cli import main

    simulate_season(conn)
    rows = conn.execute("SELECT game_id FROM games ORDER BY game_id").fetchall()
    for i, (gid,) in enumerate(rows):
        conn.execute("UPDATE games SET home_spread_pre = ?, total_pre = ? WHERE game_id = ?", (-4.5 + i % 10, 130 + i % 15, gid))
    conn.commit()
    env = tmp_path / "test.env"
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    env.write_text(f"CBBSQ_DB={db_path}\n")
    monkeypatch.setenv("CBBSQ_DB", db_path)  # an earlier test's .env must not win
    last = conn.execute("SELECT MAX(game_date) FROM games").fetchone()[0]
    out = tmp_path / "reports" / "dashboard.html"
    main(["--env", str(env), "report", "--out", str(out), "--spots-date", last])
    totals = out.with_name("dashboard-totals.html")
    assert out.exists() and totals.exists()
    g = picks.graded(picks.load_picks(conn, market="total"))
    assert len(g) > 50 and set(g["pick"]) <= {"Over", "Under"}
    with sync_playwright() as p:
        page = p.chromium.launch().new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(totals.resolve().as_uri() + "#today")
        heads = [h.rstrip(" ↓↑") for h in page.locator("#spots thead th").all_text_contents()]
        assert heads[:7] == ["Game", "Bet", "SQ proj.", "Luck edge", "From opp. shooting", "Last 5", "SQ edge"]
        assert page.locator("#spots tbody tr").first.locator("td").nth(1).inner_text().split()[0] in ("Over", "Under")
        # Each page keeps its own filter; the header switch keeps the tab.
        page.goto(totals.resolve().as_uri() + "#thresholds")
        page.fill("#f_sq_edge", "1")
        before = picks.filter_picks(g[g["pick_date"] < last], {"sq_edge": 1})  # season to date stops before the slate
        assert picks.stats(before)["record"] in page.locator("#summary").inner_text()
        page.click("#mswitch a")
        page.wait_for_url("**/dashboard.html#thresholds")
        assert page.input_value("#f_sq_edge") == ""
        assert not errors
