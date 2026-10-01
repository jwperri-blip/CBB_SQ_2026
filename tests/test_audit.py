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
    flipped = game(d, "Team 03", "Team 04", pre=(0.95, 1.12))  # home much better per SQ...
    flipped["home_spread_pre"] = 10.0  # ...but the line makes home a 10-point underdog
    unresolved = game(d, "Team 05", "Team 06", spread_team="ZZZ", spread=-3.5)
    variant = game(d, "St. John's Red Storm", "St Johns Red Storm")
    missing = game(d, "Team 07", "Team 08")
    missing["home_sq_score"] = None
    db.upsert_games(conn, [shifted, flipped, unresolved, variant, missing])
    f = by_title(audit.run(conn))
    assert f["Card values consistent"].level == "WARN" and "Team 01 @ Team 02" in f["Card values consistent"].examples[0]
    assert f["Spread on the right team"].level == "WARN" and "Team 03 @ Team 04" in f["Spread on the right team"].examples[0]
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


def test_audit_finds_dates_with_swapped_pregame_numbers(conn):
    import random

    rng = random.Random(4)
    recs = []
    for day in range(6):
        d = date(2026, 1, 1) + timedelta(days=day)
        for i in range(8):
            a_pre, h_pre = round(rng.uniform(0.95, 1.05), 3), round(rng.uniform(1.06, 1.16), 3)  # home better...
            if day == 3:
                a_pre, h_pre = h_pre, a_pre  # ...except on one date, where the two numbers are swapped
            r = game(d, f"Away {day}-{i}", f"Home {day}-{i}", pre=(a_pre, h_pre))
            r["home_spread_pre"] = -round(rng.uniform(4, 12), 1)  # and the line agrees: home favored
            recs.append(r)
    db.upsert_games(conn, recs)
    f = by_title(audit.run(conn))["Pregame SQ on the right team"]
    assert f.level == "WARN" and len(f.examples) == 1 and f.examples[0].startswith("2026-01-04")


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
