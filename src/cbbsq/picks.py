"""Save each signal's pick for every game, grade it once the game is final, and score what works.

Every pick uses only games played before its date, so picks computed later for a past date
("backfill") are what you would have seen that morning. Picks computed on or before game day
are marked "saved"; both are graded the same way, against the pre-game line, at -110.

Signals (one pick per game each, when it has a line and both teams have enough games):
  luck         bet the team that has been less lucky this season (the regression spots pick)
  opp_misses   bet the team whose opponents have shot less luckily against it
  last5        bet the team that has been less lucky over its last 5 games
  all_agree    the luck pick, only when opp_misses and last5 point the same way
  sq_spread    ShotQuality's pregame SQ projection vs the spread
  sq_total     ShotQuality's pregame SQ projection vs the total (over / under)
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from . import analysis, db
from .parse import season_for

STRATEGIES = {
    "luck": "Fade luck (season)",
    "opp_misses": "Fade opponents' misses",
    "last5": "Fade luck (last 5)",
    "all_agree": "All three luck signals agree",
    "sq_spread": "SQ projection vs spread",
    "sq_total": "SQ projection vs total",
}
WIN_UNITS = 100 / 110  # profit on a winning bet at -110
BREAKEVEN = 110 / 210  # 52.4%
_FINAL_VOID = ("Postponed", "Canceled")


def _projection_calibration(prior: pd.DataFrame) -> tuple[float, float, float]:
    """From earlier games: points per (pregame SQ PPP x possession), the home edge left over, and
    average possessions. Keeps SQ projections on the same scale as real scores and lines."""
    d = prior.dropna(subset=["pre_sq_ppp", "opp_pre_sq_ppp", "pts", "opp_pts", "poss"])
    avg_poss = float(prior["poss"].mean()) if prior["poss"].notna().any() else 68.0
    if len(d) < 30:
        return 1.0, 0.0, avg_poss
    ratio = float(d["pts"].sum() / (d["pre_sq_ppp"] * d["poss"]).sum())
    home = d[d["side"] == "home"]
    proj = (home["pre_sq_ppp"] - home["opp_pre_sq_ppp"]) * home["poss"] * ratio
    home_bias = float((home["margin"] - proj).mean()) if len(home) >= 15 else 0.0
    return ratio, home_bias, avg_poss


def picks_for_date(prior: pd.DataFrame, slate: pd.DataFrame, *, min_games: int = 5,
                   recent: int = 5) -> list[dict]:
    """Every signal's pick for one slate. ``prior`` = that season's final team-games before the date."""
    if slate.empty:
        return []
    spots = analysis.regression_spots(prior, slate, min_games=min_games, recent=recent).set_index("game_id")
    ratio, home_bias, avg_poss = _projection_calibration(prior)
    team_poss = prior.groupby("team")["poss"].mean() if len(prior) else pd.Series(dtype=float)
    out = []
    for g in slate.itertuples(index=False):
        s = spots.loc[g.game_id]
        hs = float(g.home_spread_pre) if pd.notna(g.home_spread_pre) else None

        def spread_pick(home: bool, edge: float, strategy: str) -> None:
            if hs is None or not np.isfinite(edge) or edge <= 0:
                return
            out.append({"game_id": g.game_id, "strategy": strategy, "pick": g.home_team if home else g.away_team,
                        "line": hs if home else -hs, "edge": float(edge)})

        enough = s["away_G"] >= min_games and s["home_G"] >= min_games
        if enough and pd.notna(s["back"]):
            back_home = s["back"] == g.home_team
            spread_pick(back_home, s["gap"], "luck")
            if s["opp_gap"] > 0 and s["l5_gap"] > 0:
                spread_pick(back_home, s["gap"], "all_agree")
        if enough:
            # Bet the side whose number is lower (less lucky); positive diff means the home side is lower.
            for strategy, col in (("opp_misses", "opp_luck"), ("last5", "luck_recent")):
                diff = s[f"away_{col}"] - s[f"home_{col}"]
                if pd.notna(diff) and diff != 0:
                    spread_pick(diff > 0, abs(diff), strategy)

        a_pre, h_pre = g.away_pregame_sq_ppp, g.home_pregame_sq_ppp
        if pd.notna(a_pre) and pd.notna(h_pre):
            tp = [team_poss.get(t) for t in (g.away_team, g.home_team)]
            tp = [v for v in tp if v is not None and pd.notna(v)]
            poss = float(np.mean(tp)) if tp else avg_poss
            proj_margin = (h_pre - a_pre) * poss * ratio + home_bias  # home team's projected margin
            if hs is not None:
                diff = proj_margin - (-hs)  # projection minus the market's home margin
                if diff != 0:
                    spread_pick(diff > 0, abs(diff), "sq_spread")
            if pd.notna(g.total_pre):
                diff = (h_pre + a_pre) * poss * ratio - float(g.total_pre)
                if diff != 0:
                    out.append({"game_id": g.game_id, "strategy": "sq_total", "pick": "Over" if diff > 0 else "Under",
                                "line": float(g.total_pre), "edge": abs(float(diff))})
    return out


def _dates_to_compute(conn: sqlite3.Connection, today: date) -> list[str]:
    """Dates never computed, dates with games not yet final, and the last two days (lines can arrive late)."""
    rows = conn.execute(
        "SELECT g.game_date, MAX(CASE WHEN g.status IN ('Final','Postponed','Canceled') THEN 0 ELSE 1 END), "
        "r.pick_date FROM games g LEFT JOIN pick_runs r ON r.pick_date = g.game_date GROUP BY g.game_date"
    ).fetchall()
    recent = (today - timedelta(days=1)).isoformat()
    return sorted(d for d, unfinished, done in rows if done is None or unfinished or d >= recent)


def save_picks(conn: sqlite3.Connection, dates: Optional[Iterable[str]] = None, *, today: Optional[date] = None,
               min_games: int = 5, recent: int = 5) -> int:
    """Compute and store picks (all dates that need it by default). Graded picks are never changed, and
    a pick first saved on game day keeps its 'saved' mark."""
    today = today or date.today()
    dates = sorted(set(dates)) if dates is not None else _dates_to_compute(conn, today)
    if not dates:
        return 0
    tg_all = analysis.load_team_games(conn)
    stamp = db.utcnow()
    saved = 0
    for on in dates:
        d = date.fromisoformat(on)
        prior = tg_all[(tg_all["season"] == season_for(d)) & (tg_all["game_date"] < on)]
        source = "saved" if d >= today else "backfill"
        for p in picks_for_date(prior, analysis.load_slate(conn, on), min_games=min_games, recent=recent):
            conn.execute(
                "INSERT INTO picks (game_id, strategy, pick_date, pick, line, edge, source, saved_at) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(game_id, strategy) DO UPDATE SET "
                "pick = excluded.pick, line = excluded.line, edge = excluded.edge, saved_at = excluded.saved_at, "
                "source = CASE WHEN picks.source = 'saved' THEN 'saved' ELSE excluded.source END "
                "WHERE picks.result IS NULL",
                (p["game_id"], p["strategy"], on, p["pick"], p["line"], p["edge"], source, stamp))
            saved += 1
        conn.execute("INSERT INTO pick_runs (pick_date, computed_at) VALUES (?, ?) "
                     "ON CONFLICT(pick_date) DO UPDATE SET computed_at = excluded.computed_at", (on, stamp))
    conn.commit()
    return saved


def grade_picks(conn: sqlite3.Connection) -> int:
    """Grade every ungraded pick whose game is final (or void it if the game was postponed/canceled)."""
    rows = conn.execute(
        "SELECT p.game_id, p.strategy, p.pick, p.line, g.status, g.away_team, g.home_team, g.away_score, "
        "g.home_score FROM picks p JOIN games g ON g.game_id = p.game_id "
        "WHERE p.result IS NULL AND g.status IN ('Final','Postponed','Canceled')"
    ).fetchall()
    stamp = db.utcnow()
    graded = 0
    for r in rows:
        if r["status"] in _FINAL_VOID:
            result, cover, units = "V", None, 0.0
        elif r["away_score"] is None or r["home_score"] is None:
            continue
        else:
            a, h = r["away_score"], r["home_score"]
            if r["pick"] in ("Over", "Under"):
                cover = (a + h - r["line"]) * (1 if r["pick"] == "Over" else -1)
            else:
                margin = h - a if r["pick"] == r["home_team"] else a - h
                cover = margin + r["line"]
            result = "W" if cover > 0 else "L" if cover < 0 else "P"
            units = WIN_UNITS if result == "W" else -1.0 if result == "L" else 0.0
        conn.execute("UPDATE picks SET result = ?, cover = ?, units = ?, graded_at = ? WHERE game_id = ? AND strategy = ?",
                     (result, cover, units, stamp, r["game_id"], r["strategy"]))
        graded += 1
    conn.commit()
    return graded


def update(conn: sqlite3.Connection, *, today: Optional[date] = None) -> tuple[int, int]:
    """Save any picks that are due, then grade whatever has finished. Returns (new picks, newly graded)."""
    before = conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0]
    save_picks(conn, today=today)
    new = conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0] - before
    return new, grade_picks(conn)


def load_picks(conn: sqlite3.Connection, *, season: Optional[int] = None, saved_only: bool = False) -> pd.DataFrame:
    q = "SELECT * FROM picks WHERE 1=1"
    params: list = []
    if saved_only:
        q += " AND source = 'saved'"
    df = pd.read_sql_query(q + " ORDER BY pick_date, game_id, strategy", conn, params=params)
    if season and len(df):
        df = df[df["pick_date"].map(lambda v: season_for(date.fromisoformat(v))) == season]
    return df


def luck_results(conn: sqlite3.Connection, on: str) -> pd.DataFrame:
    """Graded results of the regression spots pick (the 'luck' signal) for one date."""
    return pd.read_sql_query("SELECT game_id, result, cover FROM picks WHERE pick_date = ? AND strategy = 'luck' "
                             "AND result IS NOT NULL", conn, params=[on])


def _row(strategy: str, bucket: str, g: pd.DataFrame) -> dict:
    w, l, p = (int((g["result"] == x).sum()) for x in "WLP")
    decided = w + l
    pct = w / decided if decided else np.nan
    return {
        "strategy": strategy, "signal": STRATEGIES.get(strategy, strategy), "edge_size": bucket,
        "bets": w + l + p, "record": f"{w}-{l}" + (f"-{p}" if p else ""),
        "win_pct": pct,
        "plus_minus": 1.96 * np.sqrt(pct * (1 - pct) / decided) if decided else np.nan,
        "units": float(g["units"].sum()),
        "roi": float(g["units"].sum() / (w + l + p)) if w + l + p else np.nan,
    }


def scorecard(picks: pd.DataFrame) -> pd.DataFrame:
    """Record, win %, units and ROI per signal, overall and split into thirds by edge size."""
    cols = ["strategy", "signal", "edge_size", "bets", "record", "win_pct", "plus_minus", "units", "roi"]
    graded = picks[picks["result"].isin(["W", "L", "P"])] if len(picks) else picks
    if graded is None or graded.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    for strategy in [s for s in STRATEGIES if s in set(graded["strategy"])]:
        g = graded[graded["strategy"] == strategy]
        rows.append(_row(strategy, "All", g))
        if len(g) >= 9:
            ranks = g["edge"].rank(method="first")
            thirds = pd.qcut(ranks, 3, labels=False)
            for i, name in enumerate(("Small", "Medium", "Large")):
                part = g[thirds == i]
                rows.append(_row(strategy, f"{name} ({part['edge'].min():.1f}-{part['edge'].max():.1f})", part))
    return pd.DataFrame(rows, columns=cols)
