from datetime import date

import pytest

from cbbsq.parse import (clean_team_name, make_game_id, parse_card, parse_cards, parse_lines, resolve_side,
                         season_for, to_number)


def raw_card(**over):
    card = {
        "status": "Final", "detail": None,
        "away": {"name": "West Ga. Wolves", "logo": {"src": "https://x/1.png"}},
        "home": {"name": "Jacksonville Dolphins", "logo": None},
        "rows": {"score": ["43", "75"], "sq_score": ["58.3", "62.4"], "sq_pct": ["30", "39"],
                 "ppp_live": ["0.71", "1.24"], "ppp_live_sq": ["0.96", "1.03"], "ppp_pregame_sq": ["1.06", "1.08"]},
        "line_text": "Pre-Game JAC -1.5", "ou_text": "Pre-Game 141.5",
    }
    card.update(over)
    return card


def test_parse_card_matches_screenshot_values():
    rec = parse_card(raw_card(), date(2026, 1, 10))
    assert rec["game_id"] == "2026-01-10_west-ga-wolves_at_jacksonville-dolphins"
    assert rec["season"] == 2026
    assert (rec["away_score"], rec["home_score"]) == (43, 75)
    assert (rec["away_sq_score"], rec["home_sq_score"]) == (58.3, 62.4)
    assert (rec["away_sq_pct"], rec["home_sq_pct"]) == (30, 39)
    assert (rec["away_ppp"], rec["home_ppp"]) == (0.71, 1.24)
    assert (rec["away_sq_ppp"], rec["home_sq_ppp"]) == (0.96, 1.03)
    assert (rec["away_pregame_sq_ppp"], rec["home_pregame_sq_ppp"]) == (1.06, 1.08)
    assert rec["spread_pre_team"] == "JAC" and rec["spread_pre"] == -1.5
    assert rec["home_spread_pre"] == -1.5  # Jacksonville is home and favored
    assert rec["total_pre"] == 141.5
    assert rec["away_logo"] == "https://x/1.png"


def test_parse_card_needs_both_teams():
    assert parse_card(raw_card(home={"name": None}), date(2026, 1, 10)) is None
    recs, warnings = parse_cards([raw_card(home={"name": ""}), raw_card()], date(2026, 1, 10))
    assert len(recs) == 1 and len(warnings) == 1


@pytest.mark.parametrize("line, ou, expected", [
    ("Pre-Game BOS -6.5 Current BOS -16.5", "Pre-Game 140.5 Current 165.5",
     dict(spread_pre_team="BOS", spread_pre=-6.5, spread_cur_team="BOS", spread_cur=-16.5, total_pre=140.5, total_cur=165.5)),
    ("Pre-Game PK", "Pre-Game 133.0", dict(spread_pre=0.0, spread_pre_team=None, total_pre=133.0)),
    ("Pre-Game: PEN -5.5", None, dict(spread_pre_team="PEN", spread_pre=-5.5, total_pre=None)),
    ("Pre-Game ST.J +3", "Pre-Game 150", dict(spread_pre_team="ST.J", spread_pre=3.0, total_pre=150.0)),
    (None, None, dict(spread_pre=None, total_pre=None)),
])
def test_parse_lines(line, ou, expected):
    out = parse_lines(line, ou)
    for k, v in expected.items():
        assert out[k] == v, k


@pytest.mark.parametrize("abbr, away, home, side", [
    ("BOS", "Army West Point Black Knights", "Boston U. Terriers", "home"),
    ("JAC", "West Ga. Wolves", "Jacksonville Dolphins", "home"),
    ("PEN", "Brown Bears", "Penn Quakers", "home"),
    ("BRO", "Jacksonville Dolphins", "Brown Bears", "home"),
    ("NC", "North Carolina Tar Heels", "Duke Blue Devils", "away"),
    ("UNC", "North Carolina Tar Heels", "Duke Blue Devils", None),  # resolved later from other games
    ("XYZ", "Duke Blue Devils", "Kansas Jayhawks", None),
])
def test_resolve_side(abbr, away, home, side):
    assert resolve_side(abbr, away, home) == side


def test_small_helpers():
    assert season_for(date(2025, 11, 4)) == 2026
    assert season_for(date(2026, 3, 20)) == 2026
    assert clean_team_name("#12  Duke Blue Devils ") == "Duke Blue Devils"
    assert to_number("45.5") == 45.5 and to_number("-") is None and to_number("81", int) == 81
    assert to_number("62%") == 62.0
    assert make_game_id(date(2026, 1, 10), "St. John's Red Storm", "Texas A&M Aggies") == \
        "2026-01-10_st-john-s-red-storm_at_texas-a-m-aggies"
