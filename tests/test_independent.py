"""An independent re-implementation of every pick, number and grade, in plain Python loops over the raw
games table (no pandas, none of the package's helpers), checked against the package on a simulated season
with awkward cases: missing lines and totals, pick'em lines, whole-number lines (pushes), postponed games,
and SQ points that run below real scoring."""
import math
import random
import sqlite3
from collections import defaultdict
from datetime import date, timedelta

import pytest

from cbbsq import db, picks
from test_db_analysis import game

season = lambda d: d.year + 1 if d.month >= 7 else d.year  # noqa: E731


def simulate(path, sq_offset=0.0, seed=11):
    conn = db.connect(path)
    rng = random.Random(seed)
    teams = [f"Team {i:02d}" for i in range(30)]
    off = {t: rng.gauss(0, .06) for t in teams}
    dfn = {t: rng.gauss(0, .06) for t in teams}
    d, recs = date(2025, 11, 4), []
    for _ in range(24):
        order = teams[:]
        rng.shuffle(order)
        for i in range(0, 30, 2):
            a, h = order[i], order[i + 1]
            poss = rng.uniform(62, 74)
            ae, he = 1 + off[a] + dfn[h], 1 + off[h] + dfn[a] + .02
            a_sq, h_sq = ae + rng.gauss(0, .05), he + rng.gauss(0, .05)
            a_pp, h_pp = a_sq + rng.gauss(0, .08), h_sq + rng.gauss(0, .08)
            status = rng.choice(["Final"] * 30 + ["Postponed"]) if d < date(2026, 2, 15) else "Scheduled"
            r = game(d, a, h, status=status,
                     a=(round(a_pp * poss), a_sq * poss - sq_offset, round(a_pp, 2), round(a_sq, 3)),
                     h=(round(h_pp * poss), h_sq * poss - sq_offset, round(h_pp, 2), round(h_sq, 3)),
                     total=rng.choice([None] + [round((ae + he) * 68 * 2) / 2 + k for k in (-3, -.5, 0, 2.5)]),
                     pre=(round(ae + rng.gauss(0, .02), 3), round(he + rng.gauss(0, .02), 3)))
            r["home_spread_pre"] = rng.choice([None, -round((he - ae) * 68 * 2) / 2, round(rng.uniform(-12, 12)), 0.0])
            recs.append(r)
        d += timedelta(days=4)
    db.upsert_games(conn, recs)
    return conn


def team_rows(g):
    """Both sides of a final game, from each team's view, like the team_games view."""
    out = []
    for side, opp in (("away", "home"), ("home", "away")):
        pts, opts = g[f"{side}_score"], g[f"{opp}_score"]
        sq, osq = g[f"{side}_sq_score"], g[f"{opp}_sq_score"]
        ppp, oppp = g[f"{side}_ppp"], g[f"{opp}_ppp"]
        sqppp, osqppp = g[f"{side}_sq_ppp"], g[f"{opp}_sq_ppp"]
        poss = pts / ppp if ppp else (sq / sqppp if sqppp else None)
        oposs = opts / oppp if oppp else (osq / osqppp if osqppp else None)
        out.append(dict(team=g[f"{side}_team"], side=side, date=g["game_date"], game_id=g["game_id"],
                        pts=pts, opts=opts, sq=sq, osq=osq, poss=poss, oposs=oposs,
                        pre=g[f"{side}_pregame_sq_ppp"], opre=g[f"{opp}_pregame_sq_ppp"]))
    return out

def prior_rows(games, on):
    d = date.fromisoformat(on)
    rows = []
    for g in games:
        if g["status"] == "Final" and g["game_date"] < on and season(date.fromisoformat(g["game_date"])) == season(d):
            rows += team_rows(g)
    return rows

def per100(rows, num, poss):
    n = p = 0.0
    for r in rows:
        v, q = num(r), poss(r)
        if v is None or q is None or q <= 0: continue
        n += v; p += q
    return 100 * n / p if p else float("nan")

def luck(rows, recent=5):
    by = defaultdict(list)
    for r in rows: by[r["team"]].append(r)
    out = {}
    for t, rs in by.items():
        last = rs[-recent:]
        shoot = lambda r: r["pts"] - r["sq"]
        opp = lambda r: r["osq"] - r["opts"]
        out[t] = dict(G=len(rs), shoot=per100(rs, shoot, lambda r: r["poss"]), opp=per100(rs, opp, lambda r: r["oposs"]),
                      shoot5=per100(last, shoot, lambda r: r["poss"]), opp5=per100(last, opp, lambda r: r["oposs"]),
                      poss=sum(r["poss"] for r in rs if r["poss"]) / max(1, sum(1 for r in rs if r["poss"])))
    return out

def calib(rows):
    ok = [r for r in rows if None not in (r["pre"], r["opre"], r["pts"], r["opts"], r["poss"])]
    allp = [r["poss"] for r in rows if r["poss"] is not None]
    avg = sum(allp) / len(allp) if allp else 68.0
    if len(ok) < 30: return 1.0, 0.0, avg
    ratio = sum(r["pts"] for r in ok) / sum(r["pre"] * r["poss"] for r in ok)
    home = [r for r in ok if r["side"] == "home"]
    bias = sum((r["pts"] - r["opts"]) - (r["pre"] - r["opre"]) * r["poss"] * ratio for r in home) / len(home) if len(home) >= 15 else 0.0
    return ratio, bias, avg

def my_picks(games, on, market):
    rows = prior_rows(games, on); L = luck(rows); ratio, bias, avg = calib(rows)
    out = {}
    for g in games:
        if g["game_date"] != on: continue
        a, h = L.get(g["away_team"]), L.get(g["home_team"])
        if not a or not h or a["G"] < 5 or h["G"] < 5: continue
        tp = [x["poss"] for x in (a, h)]; poss = sum(tp) / 2
        if market == "spread":
            hs = g["home_spread_pre"]
            al, hl = a["shoot"] + a["opp"], h["shoot"] + h["opp"]
            if hs is None or al == hl: continue
            back_home = al > hl; sgn = 1 if back_home else -1
            sq = None
            if g["away_pregame_sq_ppp"] is not None and g["home_pregame_sq_ppp"] is not None:
                sq = ((g["home_pregame_sq_ppp"] - g["away_pregame_sq_ppp"]) * poss * ratio + bias + hs) * sgn
            out[g["game_id"]] = dict(pick=g["home_team"] if back_home else g["away_team"], line=hs if back_home else -hs,
                                     luck_edge=abs(al - hl), opp_edge=(a["opp"] - h["opp"]) * sgn,
                                     last5_edge=((a["shoot5"] + a["opp5"]) - (h["shoot5"] + h["opp5"])) * sgn, sq_edge=sq)
        else:
            tot = g["total_pre"]
            lg = per100(rows, lambda r: r["pts"] - r["sq"], lambda r: r["poss"])  # league level, removed from both ends
            comb = ((a["shoot"] - lg - a["opp"] - lg) + (h["shoot"] - lg - h["opp"] - lg)) / 2
            if tot is None or comb == 0: continue
            sgn = 1 if comb > 0 else -1
            sq = None
            if g["away_pregame_sq_ppp"] is not None and g["home_pregame_sq_ppp"] is not None:
                sq = (tot - (g["away_pregame_sq_ppp"] + g["home_pregame_sq_ppp"]) * poss * ratio) * sgn
            out[g["game_id"]] = dict(pick="Under" if sgn > 0 else "Over", line=tot, luck_edge=abs(comb),
                                     opp_edge=((-a["opp"] - lg) + (-h["opp"] - lg)) / 2 * sgn,
                                     last5_edge=((a["shoot5"] - a["opp5"] - 2 * lg) + (h["shoot5"] - h["opp5"] - 2 * lg)) / 2 * sgn, sq_edge=sq)
    return out

def grade(g, p):
    if g["status"] != "Final": return None
    a, h = g["away_score"], g["home_score"]
    if p["pick"] in ("Over", "Under"):
        c = (a + h - p["line"]) * (1 if p["pick"] == "Over" else -1)
    else:
        c = ((h - a) if p["pick"] == g["home_team"] else (a - h)) + p["line"]
    return ("W" if c > 0 else "L" if c < 0 else "P"), c



def check(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    games = [dict(r) for r in con.execute("SELECT * FROM games ORDER BY game_date, game_id")]
    conn = db.connect(path)
    picks.update(conn)
    n = 0
    for market in ("spread", "total"):
        stored = {r["game_id"]: r for r in picks.load_picks(conn, market=market).to_dict("records")}
        for on in sorted({g["game_date"] for g in games}):
            mine = my_picks(games, on, market)
            theirs = {k: v for k, v in stored.items() if v["pick_date"] == on}
            assert set(mine) == set(theirs), (market, on)
            for gid, p in mine.items():
                q = theirs[gid]
                assert (p["pick"], p["line"]) == (q["pick"], q["line"]), (market, gid)
                for k in ("luck_edge", "opp_edge", "last5_edge", "sq_edge"):
                    if p[k] is None:
                        assert q[k] is None or math.isnan(q[k]), (market, gid, k)
                    else:
                        assert q[k] == pytest.approx(p[k], abs=1e-6), (market, gid, k)
                gr = grade(next(x for x in games if x["game_id"] == gid), p)
                if gr:
                    res, cov = gr
                    assert (q["result"], q["cover"]) == (res, pytest.approx(cov)), (market, gid)
                    assert q["units"] == pytest.approx(100 / 110 if res == "W" else -1 if res == "L" else 0)
                    n += 1
                elif q["result"] is not None:
                    assert q["result"] == "V"
    return n


def test_every_pick_number_and_grade_matches_an_independent_implementation(tmp_path):
    simulate(tmp_path / "a.sqlite")
    assert check(tmp_path / "a.sqlite") > 400


def test_totals_do_not_drift_when_sq_points_run_below_real_scoring(tmp_path):
    simulate(tmp_path / "a.sqlite")
    simulate(tmp_path / "b.sqlite", sq_offset=6.0)  # same games; SQ points 6 low for every team
    assert check(tmp_path / "a.sqlite") > 400 and check(tmp_path / "b.sqlite") > 400
    load = lambda f, m: picks.load_picks(db.connect(tmp_path / f), market=m).set_index("game_id")  # noqa: E731
    a, b = load("a.sqlite", "total"), load("b.sqlite", "total")
    assert 0.35 < (b["pick"] == "Under").mean() < 0.65  # not every pick pushed to one side
    assert set(a.index) == set(b.index)
    assert (a["pick"] == b["pick"].reindex(a.index)).mean() > 0.95
    sa, sb = load("a.sqlite", "spread"), load("b.sqlite", "spread")
    assert set(sa.index) == set(sb.index) and (sa["pick"] == sb["pick"].reindex(sa.index)).all()
