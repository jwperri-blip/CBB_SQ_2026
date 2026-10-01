"""Save the regression spots pick for every game, grade it, and find the thresholds that win.

One pick per game: the Bet from the regression spots table (the less lucky team) against its pre-game
line, saved with the four numbers behind it:

  luck_edge   how much luckier the other team has been this season (per 100 possessions)
  opp_edge    the part of that edge from opponents missing good shots
  last5_edge  the same edge over the last 5 games
  sq_edge     how many points ShotQuality's pregame projection likes the same bet by against the line

Every pick uses only games played before its date, so picks computed later for a past date
("backfill") are what you would have seen that morning; picks computed on or before game day are
marked "saved". Picks are graded against the pre-game line at -110.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from . import analysis, db
from .parse import season_for

METRICS = {
    "luck_edge": "Luck edge",
    "opp_edge": "From opp. misses",
    "last5_edge": "Last 5",
    "sq_edge": "SQ edge",
}
_SPOT_COLS = {"luck_edge": "gap", "opp_edge": "opp_gap", "last5_edge": "l5_gap", "sq_edge": "sq_edge"}
WIN_UNITS = 100 / 110  # profit on a winning bet at -110
BREAKEVEN = 110 / 210  # 52.4%


def _dates_to_compute(conn: sqlite3.Connection, today: date) -> list[str]:
    """Dates never computed, dates with games not yet final, and the last two days (lines can arrive late)."""
    rows = conn.execute(
        "SELECT g.game_date, MAX(CASE WHEN g.status IN ('Final','Postponed','Canceled') THEN 0 ELSE 1 END), "
        "r.pick_date FROM games g LEFT JOIN spot_pick_runs r ON r.pick_date = g.game_date GROUP BY g.game_date"
    ).fetchall()
    recent = (today - timedelta(days=1)).isoformat()
    return sorted(d for d, unfinished, done in rows if done is None or unfinished or d >= recent)


def save_picks(conn: sqlite3.Connection, dates: Optional[Iterable[str]] = None, *, today: Optional[date] = None,
               min_games: int = 5, recent: int = 5) -> None:
    """Compute and store the pick for every game with a line (all dates that need it by default).
    Graded picks never change, and a pick first saved on game day keeps its 'saved' mark."""
    today = today or date.today()
    dates = sorted(set(dates)) if dates is not None else _dates_to_compute(conn, today)
    if not dates:
        return
    tg_all = analysis.load_team_games(conn)
    stamp = db.utcnow()
    for on in dates:
        d = date.fromisoformat(on)
        prior = tg_all[(tg_all["season"] == season_for(d)) & (tg_all["game_date"] < on)]
        spots = analysis.regression_spots(prior, analysis.load_slate(conn, on), min_games=min_games, recent=recent)
        source = "saved" if d >= today else "backfill"
        for r in spots.itertuples(index=False):
            if pd.isna(r.back) or pd.isna(r.back_line):
                continue
            vals = [None if pd.isna(getattr(r, _SPOT_COLS[m])) else float(getattr(r, _SPOT_COLS[m])) for m in METRICS]
            conn.execute(
                "INSERT INTO spot_picks (game_id, pick_date, pick, line, luck_edge, opp_edge, last5_edge, sq_edge, "
                "source, saved_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(game_id) DO UPDATE SET "
                "pick = excluded.pick, line = excluded.line, luck_edge = excluded.luck_edge, "
                "opp_edge = excluded.opp_edge, last5_edge = excluded.last5_edge, sq_edge = excluded.sq_edge, "
                "saved_at = excluded.saved_at, "
                "source = CASE WHEN spot_picks.source = 'saved' THEN 'saved' ELSE excluded.source END "
                "WHERE spot_picks.result IS NULL",
                [r.game_id, on, r.back, float(r.back_line), *vals, source, stamp])
        conn.execute("INSERT INTO spot_pick_runs (pick_date, computed_at) VALUES (?, ?) "
                     "ON CONFLICT(pick_date) DO UPDATE SET computed_at = excluded.computed_at", (on, stamp))
    conn.commit()


def grade_picks(conn: sqlite3.Connection) -> int:
    """Grade every ungraded pick whose game is final (void it if the game was postponed or canceled)."""
    rows = conn.execute(
        "SELECT p.game_id, p.pick, p.line, g.status, g.home_team, g.away_score, g.home_score "
        "FROM spot_picks p JOIN games g ON g.game_id = p.game_id "
        "WHERE p.result IS NULL AND g.status IN ('Final','Postponed','Canceled')"
    ).fetchall()
    stamp = db.utcnow()
    graded = 0
    for r in rows:
        if r["status"] != "Final":
            result, cover, units = "V", None, 0.0
        elif r["away_score"] is None or r["home_score"] is None:
            continue
        else:
            margin = r["home_score"] - r["away_score"]
            cover = (margin if r["pick"] == r["home_team"] else -margin) + r["line"]
            result = "W" if cover > 0 else "L" if cover < 0 else "P"
            units = WIN_UNITS if result == "W" else -1.0 if result == "L" else 0.0
        conn.execute("UPDATE spot_picks SET result = ?, cover = ?, units = ?, graded_at = ? WHERE game_id = ?",
                     (result, cover, units, stamp, r["game_id"]))
        graded += 1
    conn.commit()
    return graded


def update(conn: sqlite3.Connection, *, today: Optional[date] = None) -> tuple[int, int]:
    """Save any picks that are due, then grade whatever has finished. Returns (new picks, newly graded)."""
    before = conn.execute("SELECT COUNT(*) FROM spot_picks").fetchone()[0]
    save_picks(conn, today=today)
    new = conn.execute("SELECT COUNT(*) FROM spot_picks").fetchone()[0] - before
    return new, grade_picks(conn)


def load_picks(conn: sqlite3.Connection, *, season: Optional[int] = None, saved_only: bool = False) -> pd.DataFrame:
    q = "SELECT * FROM spot_picks" + (" WHERE source = 'saved'" if saved_only else "")
    df = pd.read_sql_query(q + " ORDER BY pick_date, game_id", conn)
    if season and len(df):
        df = df[df["pick_date"].map(lambda v: season_for(date.fromisoformat(v))) == season]
    return df


def results_for_date(conn: sqlite3.Connection, on: str) -> pd.DataFrame:
    return pd.read_sql_query("SELECT game_id, result, cover FROM spot_picks WHERE pick_date = ? "
                             "AND result IS NOT NULL", conn, params=[on])


# ---------------------------------------------------------------- thresholds
def graded(picks: pd.DataFrame) -> pd.DataFrame:
    return picks[picks["result"].isin(["W", "L", "P"])] if len(picks) else picks


def stats(g: pd.DataFrame) -> dict:
    """Record, win %, a 95% range on the win %, units and ROI for a set of graded picks."""
    w, l, p = (int((g["result"] == x).sum()) for x in "WLP")
    decided = w + l
    pct = w / decided if decided else np.nan
    bets = w + l + p
    units = float(g["units"].sum()) if bets else 0.0
    return {"bets": bets, "record": f"{w}-{l}" + (f"-{p}" if p else ""), "win_pct": pct,
            "plus_minus": 1.96 * np.sqrt(pct * (1 - pct) / decided) if decided else np.nan,
            "units": units, "roi": units / bets if bets else np.nan}


def filter_picks(g: pd.DataFrame, mins: dict) -> pd.DataFrame:
    """Picks meeting every minimum, e.g. {"luck_edge": 4, "last5_edge": 0}. A pick missing a filtered number is dropped."""
    mask = pd.Series(True, index=g.index)
    for m, v in mins.items():
        if v is not None:
            mask &= g[m] >= v
    return g[mask]


def _grid(values: pd.Series, step: float = 0.5) -> list[float]:
    v = values.dropna()
    if v.empty:
        return []
    lo, hi = np.floor(v.min() / step) * step, np.ceil(v.max() / step) * step
    return [round(float(x), 2) for x in np.arange(lo, hi + step / 2, step)]


def threshold_for(g: pd.DataFrame, metric: str, target: float, min_bets: int) -> Optional[dict]:
    """The lowest 'metric >= t' (in 0.5 steps) whose picks win at least ``target`` over at least ``min_bets`` bets.
    ``any`` is True when that is every pick with the number, i.e. no threshold is needed."""
    have = int(g[metric].notna().sum())
    for t in _grid(g[metric]):
        sub = g[g[metric] >= t]
        if len(sub) < min_bets:
            break
        s = stats(sub)
        if s["win_pct"] >= target:
            return {"metric": metric, "threshold": t, "any": len(sub) == have, **s}
    return None


def ladder(g: pd.DataFrame, metric: str, steps: int = 10) -> pd.DataFrame:
    """Win % at a range of 'metric >= t' thresholds, from all picks down to the top tenth."""
    cols = ["threshold", "bets", "record", "win_pct", "plus_minus", "units", "roi"]
    v = g[metric].dropna()
    if v.empty:
        return pd.DataFrame(columns=cols)
    qs = sorted({float(np.floor(x * 2) / 2) for x in v.quantile(np.linspace(0, 0.9, steps))})
    return pd.DataFrame([{"threshold": t, **stats(g[g[metric] >= t])} for t in qs], columns=cols)
