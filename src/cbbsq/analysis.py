"""Trend and performance analysis over collected games (pandas).

Terminology (per team, per game - all from ShotQuality's ScoreCenter):
  sq_pts      "SQ Score": points the team's shots were worth on average
  sq_ppp      "Live SQ" PTS/possession at the final horn: shot quality per possession
  ppp         actual points per possession
  pre_sq_ppp  ShotQuality's pregame SQ PPP projection
  sq_pct      "SQ Percentile"
Derived:
  sq_margin   sq_pts - opp_sq_pts (who "won" the shot-quality battle)
  shot_making pts - sq_pts         (offense scoring above/below its shot quality)
  shot_defense opp_sq_pts - opp_pts (opponents scoring below their shot quality)
  luck        shot_making + shot_defense = margin - sq_margin
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd


def load_team_games(conn: sqlite3.Connection, *, season: Optional[int] = None, start: Optional[str] = None,
                    end: Optional[str] = None, final_only: bool = True) -> pd.DataFrame:
    """One row per team per game, oldest first, with derived metrics."""
    q, params = "SELECT * FROM team_games WHERE 1=1", []
    if final_only:
        q += " AND status = 'Final'"
    if season:
        q += " AND season = ?"
        params.append(season)
    if start:
        q += " AND game_date >= ?"
        params.append(start)
    if end:
        q += " AND game_date <= ?"
        params.append(end)
    df = pd.read_sql_query(q + " ORDER BY game_date, game_id, side", conn, params=params)
    return add_derived(df)


def add_derived(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    num = ["pts", "opp_pts", "sq_pts", "opp_sq_pts", "sq_pct", "opp_sq_pct", "ppp", "opp_ppp",
           "sq_ppp", "opp_sq_ppp", "pre_sq_ppp", "opp_pre_sq_ppp", "spread", "total_line"]
    for c in num:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    def possessions(pts, ppp, sq_pts, sq_ppp):
        est = pts / ppp.where(ppp > 0)
        return est.fillna(sq_pts / sq_ppp.where(sq_ppp > 0))

    df["poss"] = possessions(df["pts"], df["ppp"], df["sq_pts"], df["sq_ppp"])
    df["opp_poss"] = possessions(df["opp_pts"], df["opp_ppp"], df["opp_sq_pts"], df["opp_sq_ppp"])
    df["margin"] = df["pts"] - df["opp_pts"]
    df["sq_margin"] = df["sq_pts"] - df["opp_sq_pts"]
    df["win"] = (df["margin"] > 0).astype(float).where(df["margin"].notna())
    df["sq_win"] = (df["sq_margin"] > 0).astype(float).where(df["sq_margin"].notna())
    df["sq_net_ppp"] = df["sq_ppp"] - df["opp_sq_ppp"]
    df["net_ppp"] = df["ppp"] - df["opp_ppp"]
    df["shot_making"] = df["pts"] - df["sq_pts"]
    df["shot_defense"] = df["opp_sq_pts"] - df["opp_pts"]
    df["luck"] = df["shot_making"] + df["shot_defense"]
    df["sq_vs_pregame"] = df["sq_ppp"] - df["pre_sq_ppp"]
    df["cover_margin"] = df["margin"] + df["spread"]
    df["sq_cover_margin"] = df["sq_margin"] + df["spread"]
    df["total_pts"] = df["pts"] + df["opp_pts"]
    df["sq_total"] = df["sq_pts"] + df["opp_sq_pts"]
    df["over_margin"] = df["total_pts"] - df["total_line"]
    df["sq_over_margin"] = df["sq_total"] - df["total_line"]
    df["is_home"] = (df["side"] == "home").astype(int)
    if len(df):
        df["game_no"] = df.groupby("team").cumcount() + 1
    else:
        df["game_no"] = pd.Series(dtype=int)
    return df


def _wavg(values: pd.Series, weights: pd.Series) -> float:
    mask = values.notna() & weights.notna() & (weights > 0)
    if mask.any():
        return float((values[mask] * weights[mask]).sum() / weights[mask].sum())
    return float(values.mean()) if values.notna().any() else np.nan


def _record(margins: pd.Series) -> str:
    m = margins.dropna()
    if m.empty:
        return "-"
    return f"{int((m > 0).sum())}-{int((m < 0).sum())}" + (f"-{int((m == 0).sum())}" if (m == 0).any() else "")


def _summarize(g: pd.DataFrame) -> dict:
    return {
        "G": len(g),
        "W-L": _record(g["margin"]),
        "SQ W-L": _record(g["sq_margin"]),
        "off_sq_ppp": _wavg(g["sq_ppp"], g["poss"]),
        "def_sq_ppp": _wavg(g["opp_sq_ppp"], g["opp_poss"]),
        "off_ppp": _wavg(g["ppp"], g["poss"]),
        "def_ppp": _wavg(g["opp_ppp"], g["opp_poss"]),
        "sq_pct": g["sq_pct"].mean(),
        "opp_sq_pct": g["opp_sq_pct"].mean(),
        "margin": g["margin"].mean(),
        "sq_margin": g["sq_margin"].mean(),
        "shot_making": g["shot_making"].mean(),
        "shot_defense": g["shot_defense"].mean(),
        "luck": g["luck"].mean(),
        "sq_vs_pregame": g["sq_vs_pregame"].mean(),
        "ATS": _record(g["cover_margin"]),
        "SQ ATS": _record(g["sq_cover_margin"]),
    }


def team_summary(tg: pd.DataFrame, *, last_n: Optional[int] = None, min_games: int = 1) -> pd.DataFrame:
    """Per-team averages (optionally over each team's last N games)."""
    if tg.empty:
        return pd.DataFrame()
    if last_n:
        tg = tg.groupby("team", group_keys=False).tail(last_n)
    rows = {team: _summarize(g) for team, g in tg.groupby("team")}
    out = pd.DataFrame.from_dict(rows, orient="index")
    out.index.name = "team"
    out["sq_net"] = out["off_sq_ppp"] - out["def_sq_ppp"]
    out["net"] = out["off_ppp"] - out["def_ppp"]
    out = out[out["G"] >= min_games]
    cols = ["G", "W-L", "SQ W-L", "off_sq_ppp", "def_sq_ppp", "sq_net", "off_ppp", "def_ppp", "net",
            "sq_pct", "opp_sq_pct", "margin", "sq_margin", "shot_making", "shot_defense", "luck",
            "sq_vs_pregame", "ATS", "SQ ATS"]
    return out[cols].sort_values("sq_net", ascending=False)


def adjusted_ratings(tg: pd.DataFrame, *, metric: str = "sq_ppp", shrink: float = 2.0,
                     min_games: int = 1) -> pd.DataFrame:
    """Opponent-adjusted offensive/defensive ratings (ridge regression).

    Models each team-game as  metric = mu + off[team] + def[opponent] + home_adv * (+1 home / -1 away)
    weighted by possessions. ``adj_def`` is what an average offense would post against the team
    (lower is better); ``adj_net = adj_off - adj_def``. ``shrink`` pulls teams with few games
    toward average (in units of roughly one game).
    """
    d = tg.dropna(subset=[metric]).copy()
    if d.empty:
        return pd.DataFrame(columns=["G", "adj_off", "adj_def", "adj_net"])
    teams = sorted(set(d["team"]) | set(d["opponent"]))
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    p = 2 * n + 2  # mu, home, off[n], def[n]
    w = d["poss"].fillna(d["poss"].mean() if d["poss"].notna().any() else 1.0).to_numpy(float)
    w = w / w.mean()
    y = d[metric].to_numpy(float)
    ti = d["team"].map(idx).to_numpy()
    oi = d["opponent"].map(idx).to_numpy()
    h = np.where(d["side"].to_numpy() == "home", 1.0, -1.0)

    xtx = np.zeros((p, p))
    xty = np.zeros(p)
    for k in range(len(d)):
        cols = (0, 1, 2 + ti[k], 2 + n + oi[k])
        vals = (1.0, h[k], 1.0, 1.0)
        for a, va in zip(cols, vals):
            xty[a] += w[k] * va * y[k]
            for b, vb in zip(cols, vals):
                xtx[a, b] += w[k] * va * vb
    reg = np.full(p, shrink)
    reg[:2] = 1e-9  # don't shrink the intercept / home term
    beta = np.linalg.solve(xtx + np.diag(reg), xty)
    mu, off, deff = beta[0], beta[2:2 + n], beta[2 + n:]
    # center so an average team is exactly average
    off, deff = off - off.mean(), deff - deff.mean()
    games = d.groupby("team").size()
    out = pd.DataFrame({
        "G": [int(games.get(t, 0)) for t in teams],
        "adj_off": mu + off,
        "adj_def": mu + deff,
    }, index=pd.Index(teams, name="team"))
    out["adj_net"] = out["adj_off"] - out["adj_def"]
    out.attrs["home_adv_ppp"] = 2 * beta[1]
    out.attrs["avg"] = mu
    return out[out["G"] >= min_games].sort_values("adj_net", ascending=False)


def team_log(tg: pd.DataFrame, team: str, window: int = 5) -> pd.DataFrame:
    """A team's game log with rolling averages of the key SQ metrics."""
    g = tg[tg["team"] == team].copy()
    if g.empty:
        return g
    for col in ("sq_ppp", "opp_sq_ppp", "sq_net_ppp", "ppp", "opp_ppp", "luck", "sq_margin"):
        g[f"{col}_r{window}"] = g[col].rolling(window, min_periods=1).mean()
    return g


def trending(tg: pd.DataFrame, *, window: int = 5, min_games: int = 8) -> pd.DataFrame:
    """Compare each team's last ``window`` games with its season: who is heating up / cooling off."""
    season = team_summary(tg, min_games=min_games)
    if season.empty:
        return season
    recent = team_summary(tg[tg["team"].isin(season.index)], last_n=window)
    out = pd.DataFrame({
        "G": season["G"],
        "sq_net": season["sq_net"],
        f"sq_net_last{window}": recent["sq_net"],
        "delta_net": recent["sq_net"] - season["sq_net"],
        "delta_off": recent["off_sq_ppp"] - season["off_sq_ppp"],
        "delta_def": recent["def_sq_ppp"] - season["def_sq_ppp"],
        f"luck_last{window}": recent["luck"],
    })
    slopes = {}
    for team, g in tg[tg["team"].isin(season.index)].groupby("team"):
        s = g["sq_net_ppp"].dropna().tail(max(window * 2, 10))
        slopes[team] = np.polyfit(np.arange(len(s)), s.to_numpy(), 1)[0] if len(s) >= 3 else np.nan
    out["slope_per_game"] = pd.Series(slopes)
    return out.sort_values("delta_net", ascending=False)


def luck_table(tg: pd.DataFrame, *, min_games: int = 5, last_n: Optional[int] = None) -> pd.DataFrame:
    """Teams whose results beat (or trail) their shot quality the most - regression candidates.
    With ``last_n``, only each team's last N games count and only teams with all N are listed."""
    s = team_summary(tg, last_n=last_n, min_games=last_n or min_games)
    if s.empty:
        return s
    cols = ["G", "W-L", "SQ W-L", "margin", "sq_margin", "shot_making", "shot_defense", "luck", "ATS", "SQ ATS"]
    return s[cols].sort_values("luck", ascending=False)


def team_luck(tg: pd.DataFrame, *, recent: int = 5) -> pd.DataFrame:
    """Each team's luck per 100 possessions, split into its two parts.

    ``shoot_luck``  own points minus own SQ points (partly real shooting skill)
    ``opp_luck``    opponents' SQ points minus their points (mostly noise)
    ``luck``        the sum; ``luck_recent`` is the same over the last ``recent`` games.
    Per 100 possessions so fast-paced teams don't look luckier just for playing more possessions.
    """
    if tg.empty:
        return pd.DataFrame(columns=["G", "shoot_luck", "opp_luck", "luck", "luck_recent"])

    def per100(df: pd.DataFrame, num: str, poss: str) -> pd.Series:
        ok = df[num].notna() & df[poss].notna() & (df[poss] > 0)
        parts = pd.DataFrame({"team": df["team"], "n": df[num].where(ok, 0.0), "p": df[poss].where(ok, 0.0)})
        sums = parts.groupby("team")[["n", "p"]].sum()
        return 100 * sums["n"] / sums["p"].where(sums["p"] > 0)

    recent_rows = tg.groupby("team", group_keys=False).tail(recent)
    out = pd.DataFrame({"G": tg.groupby("team").size()})
    out["shoot_luck"] = per100(tg, "shot_making", "poss")
    out["opp_luck"] = per100(tg, "shot_defense", "opp_poss")
    out["luck"] = out["shoot_luck"] + out["opp_luck"]
    out["luck_recent"] = per100(recent_rows, "shot_making", "poss") + per100(recent_rows, "shot_defense", "opp_poss")
    out.index.name = "team"
    return out


def load_slate(conn: sqlite3.Connection, on: str) -> pd.DataFrame:
    """Every game stored for one date (any status), with the pre-game line."""
    return pd.read_sql_query(
        "SELECT game_id, game_date, status, status_detail, away_team, home_team, home_spread_pre, "
        "spread_pre_team, spread_pre, total_pre, away_pregame_sq_ppp, home_pregame_sq_ppp "
        "FROM games WHERE game_date = ? ORDER BY game_id",
        conn, params=[on])


def regression_spots(tg: pd.DataFrame, slate: pd.DataFrame, *, min_games: int = 5,
                     recent: int = 5) -> pd.DataFrame:
    """Rank a slate by the luck gap between the two teams.

    ``tg`` must hold only games played before the slate's date, so nothing from the future leaks in.
    ``gap`` = luckier team's luck minus the other's (per 100 possessions); ``opp_gap`` is the same for
    opponents' shooting alone, and ``l5_gap`` the same over each team's last ``recent`` games (positive when
    recent luck points the same way as the season). ``back`` is the less lucky team, with its pre-game line in ``back_line``.
    Teams with fewer than ``min_games`` prior games get no pick. This is a screen, not a validated
    model: no threshold has been backtested yet.
    """
    cols = ["game_id", "status", "away_team", "home_team", "line", "total_pre",
            "away_G", "away_shoot_luck", "away_opp_luck", "away_luck", "away_luck_recent",
            "home_G", "home_shoot_luck", "home_opp_luck", "home_luck", "home_luck_recent",
            "gap", "opp_gap", "l5_gap", "back", "back_line"]
    if slate.empty:
        return pd.DataFrame(columns=cols)
    luck = team_luck(tg, recent=recent)
    out = []
    for g in slate.itertuples(index=False):
        row = {"game_id": g.game_id, "status": g.status, "away_team": g.away_team, "home_team": g.home_team,
               "total_pre": g.total_pre}
        hs = g.home_spread_pre if pd.notna(g.home_spread_pre) else None
        if hs is not None:
            row["line"] = f"{g.home_team} {'PK' if hs == 0 else f'{hs:+g}'}"
        elif pd.notna(g.spread_pre):
            row["line"] = f"{g.spread_pre_team or ''} {g.spread_pre:+g}".strip()
        else:
            row["line"] = None
        for side, team in (("away", g.away_team), ("home", g.home_team)):
            t = luck.loc[team] if team in luck.index else None
            row[f"{side}_G"] = int(t["G"]) if t is not None else 0
            for k in ("shoot_luck", "opp_luck", "luck", "luck_recent"):
                row[f"{side}_{k}"] = float(t[k]) if t is not None else np.nan
        ok = row["away_G"] >= min_games and row["home_G"] >= min_games
        if ok and pd.notna(row["away_luck"]) and pd.notna(row["home_luck"]):
            diff = row["away_luck"] - row["home_luck"]
            back_home = diff > 0
            row["gap"] = abs(diff)
            sign = 1 if back_home else -1
            row["opp_gap"] = (row["away_opp_luck"] - row["home_opp_luck"]) * sign
            row["l5_gap"] = (row["away_luck_recent"] - row["home_luck_recent"]) * sign
            row["back"] = g.home_team if back_home else g.away_team
            row["back_line"] = (hs if back_home else -hs) if hs is not None else None
        else:
            row.update(gap=np.nan, opp_gap=np.nan, l5_gap=np.nan, back=None, back_line=None)
        out.append(row)
    df = pd.DataFrame(out, columns=cols)
    return df.sort_values("gap", ascending=False, na_position="last").reset_index(drop=True)


def spots_for_date(conn: sqlite3.Connection, on: str, *, min_games: int = 5, recent: int = 5) -> pd.DataFrame:
    """Regression spots for one date, using only that season's games played before it."""
    from .parse import season_for

    d = date.fromisoformat(on)
    tg = load_team_games(conn, season=season_for(d), end=(d - timedelta(days=1)).isoformat())
    return regression_spots(tg, load_slate(conn, on), min_games=min_games, recent=recent)
