from datetime import date, timedelta

import pytest

from cbbsq import analysis, audit, db, picks
from test_db_analysis import game, simulate_season


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.sqlite")
    yield c
    c.close()


def by_title(findings):
    return {f.title: f for f in findings}


def test_clean_season_has_no_warnings(conn):
    simulate_season(conn)
    f = by_title(audit.run(conn))
    assert all(x.level != "WARN" for x in f.values()), [x for x in f.values() if x.level == "WARN"]
    assert "Real vs SQ scoring" in f


def test_audit_flags_the_problems_it_is_for(conn):
    simulate_season(conn)
    d = date(2026, 3, 1)
    shifted = game(d, "Team 01", "Team 02", a=(70, 45.0, 1.0, 1.0))  # SQ score read from the wrong row
    weak = game(d, "Texas Southern Tigers", "Alcorn St. Braves", spread_team="TXS", spread=-4.5)  # letters in order only
    weak["line_text"] = "Pre-Game TXS -4.5"
    from cbbsq.parse import _home_spread

    weak["home_spread_pre"] = _home_spread("TXS", -4.5, weak["away_team"], weak["home_team"])
    unresolved = game(d, "Team 05", "Team 06", spread_team="ZZZ", spread=-3.5)
    variant = game(d, "St. John's Red Storm", "St Johns Red Storm")
    missing = game(d, "Team 07", "Team 08")
    missing["home_sq_score"] = None
    db.upsert_games(conn, [shifted, weak, unresolved, variant, missing])
    f = by_title(audit.run(conn))
    rows = f["Card values read into the right rows"]
    assert rows.level == "WARN" and len(rows.examples) == 1 and "Team 01 @ Team 02" in rows.examples[0]
    assert f["Spreads matched confidently"].level == "WARN" and "Texas Southern" in f["Spreads matched confidently"].examples[0]
    assert f["Spreads tied to a team"].level == "WARN"
    assert f["One name per team"].level == "WARN" and "St. John's" in f["One name per team"].examples[0]
    assert f["Final games with missing numbers"].level == "WARN"


def test_audit_spots_away_home_swapped(conn):
    simulate_season(conn)
    conn.execute("UPDATE games SET home_score = away_score - 6")
    conn.commit()
    assert by_title(audit.run(conn))["Home / away orientation"].level == "WARN"


def test_dashboard_does_not_count_a_side_without_a_line_as_a_bet(conn, tmp_path, chromium_ok):
    from playwright.sync_api import sync_playwright

    from cbbsq.report import write_pages

    simulate_season(conn)
    conn.execute("UPDATE games SET home_spread_pre = -2.5, total_pre = 140.5")
    conn.commit()
    slate = date(2026, 3, 20)
    with_line = game(slate, "Team 01", "Team 02", status="Scheduled", a=(None,) * 4, h=(None,) * 4)
    with_line["home_spread_pre"] = -4.5
    no_line = game(slate, "Team 03", "Team 04", status="Scheduled", a=(None,) * 4, h=(None,) * 4, total=None)
    db.upsert_games(conn, [with_line, no_line])
    picks.update(conn, today=slate)
    pages = write_pages(conn, analysis.load_team_games(conn), tmp_path / "dashboard.html", slate.isoformat())
    with sync_playwright() as p:
        page = p.chromium.launch().new_page()
        page.goto(pages["spread"].resolve().as_uri() + "#today")
        bets = page.locator("#spots tbody tr td:nth-child(2)").all_inner_texts()
        assert any(b.endswith("(no line yet)") for b in bets) and any(b.endswith("-4.5") or b.endswith("+4.5") for b in bets)
        today = page.locator("#summary .sgroup").first.inner_text()
        assert "2 games · 1 picks" in today  # the side without a line isn't a bet
        page.goto(pages["total"].resolve().as_uri() + "#today")
        assert any(b.endswith("(no total yet)") for b in page.locator("#spots tbody tr td:nth-child(2)").all_inner_texts())


def test_game_detail_shows_scrapes_and_card_text(conn, tmp_path):
    import gzip
    import json

    d = date(2026, 1, 5)
    live = game(d, "Oregon Ducks", "Rutgers Scarlet Knights", status="Live", a=(30, 28.0, 1.0, 0.93))
    db.upsert_games(conn, [live])
    db.upsert_games(conn, [game(d, "Oregon Ducks", "Rutgers Scarlet Knights", a=(70, 66.0, 1.0, 0.94))])
    raw = tmp_path / "raw" / d.isoformat()
    raw.mkdir(parents=True)
    with gzip.open(raw / "20260106T120000Z.json.gz", "wt") as fh:
        json.dump({"date": d.isoformat(), "cards": [{"text": "Oregon Ducks 70 Rutgers Scarlet Knights 75 SQ Score ...",
                                                     "rows": {"score": ["70", "75"]}}]}, fh)
    out = "\n".join(audit.game_detail(conn, "oregon", tmp_path / "raw"))
    assert "2 scrape(s)" in out and " Live:" in out and " Final:" in out
    assert "card text in 20260106T120000Z.json.gz: Oregon Ducks 70" in out
    assert audit.game_detail(conn, "nobody", tmp_path / "raw") == ["No game matches 'nobody'."]


def test_audit_spots_end_of_season_ratings_filled_into_past_games(conn):
    import random

    rng = random.Random(9)
    teams = [f"Team {i:02d}" for i in range(30)]
    true = {t: rng.gauss(1.0, 0.05) for t in teams}
    drift = {t: rng.gauss(0, 0.05) for t in teams}  # teams improve or decline over the season
    games, d = [], date(2025, 11, 4)
    for rnd in range(24):
        order = teams[:]
        rng.shuffle(order)
        for i in range(0, 30, 2):
            a, h = order[i], order[i + 1]
            f = rnd / 23
            a_sq = true[a] + drift[a] * f + rng.gauss(0, 0.05)
            h_sq = true[h] + drift[h] * f + rng.gauss(0, 0.05)
            games.append((d, a, h, a_sq, h_sq))
        d += timedelta(days=4)
    season_avg = {t: sum(x[3] for x in games if x[1] == t) / max(1, sum(1 for x in games if x[1] == t)) for t in teams}
    for t in teams:  # average over both sides
        vals = [x[3] for x in games if x[1] == t] + [x[4] for x in games if x[2] == t]
        season_avg[t] = sum(vals) / len(vals)
    # every past game carries the team's end-of-season value as "pregame"
    db.upsert_games(conn, [game(gd, a, h, a=(70, a_sq * 68, round(a_sq, 2), round(a_sq, 3)),
                                h=(70, h_sq * 68, round(h_sq, 2), round(h_sq, 3)),
                                pre=(round(season_avg[a], 2), round(season_avg[h], 2)))
                           for gd, a, h, a_sq, h_sq in games])
    f = by_title(audit.run(conn))["Pregame SQ: game-day number or current rating?"]
    assert f.level == "WARN" and "100% of 30 teams" in f.detail and "--saved-only" in f.detail
