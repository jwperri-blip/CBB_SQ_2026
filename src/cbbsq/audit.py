"""Data checks for the stored games and picks: things that would quietly make a displayed number wrong.

Each check returns a Finding: OK, INFO or WARN, a one-line summary, and a few example games to look at.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import analysis


@dataclass
class Finding:
    level: str  # OK / INFO / WARN
    title: str
    detail: str
    examples: list[str] = field(default_factory=list)


def _ex(df: pd.DataFrame, fmt, n: int = 5) -> list[str]:
    return [fmt(r) for r in df.head(n).itertuples(index=False)]


def run(conn: sqlite3.Connection) -> list[Finding]:
    g = pd.read_sql_query("SELECT * FROM games", conn)
    out: list[Finding] = []
    if g.empty:
        return [Finding("INFO", "No games stored", "Collect some dates first.")]
    final = g[g["status"] == "Final"].copy()
    label = lambda r: f"{r.game_date} {r.away_team} @ {r.home_team}"  # noqa: E731

    # 1. Coverage and failed scrapes
    runs = pd.read_sql_query("SELECT game_date, ok, message FROM scrape_runs", conn)
    failed = runs[runs["ok"] == 0]
    last_ok = set(runs[runs["ok"] == 1]["game_date"])
    still_failed = failed[~failed["game_date"].isin(last_ok)]
    out.append(Finding("WARN" if len(still_failed) else "OK", "Scrape runs",
                       f"{len(final)} final games over {g['game_date'].nunique()} dates; "
                       f"{len(still_failed)} date(s) whose scrape failed and never succeeded later.",
                       [f"{r.game_date}: {r.message}" for r in still_failed.head(5).itertuples()]))

    # 2. Final games missing the numbers the metrics need
    need = ["away_score", "home_score", "away_sq_score", "home_sq_score", "away_ppp", "home_ppp"]
    missing = final[final[need].isna().any(axis=1)]
    out.append(Finding("WARN" if len(missing) else "OK", "Final games with missing numbers",
                       f"{len(missing)} final game(s) lack a score, SQ score or PPP (they drop out of every metric).",
                       _ex(missing, label)))

    # 3. Card read correctly: score/PPP and SQ score/SQ PPP must describe the same possessions
    d = final.dropna(subset=need + ["away_sq_ppp", "home_sq_ppp"])
    bad = []
    for side in ("away", "home"):
        p1 = d[f"{side}_score"] / d[f"{side}_ppp"]
        p2 = d[f"{side}_sq_score"] / d[f"{side}_sq_ppp"]
        bad.append((p1 - p2).abs() / p1 > 0.06)
    shifted = d[bad[0] | bad[1]] if len(d) else d
    out.append(Finding("WARN" if len(shifted) else "OK", "Card values consistent",
                       f"{len(shifted)} of {len(d)} games where score / PPP and SQ score / SQ PPP disagree on "
                       f"possessions by more than 6% (a sign of numbers read into the wrong row).",
                       _ex(shifted, label)))

    # 4. Plausible ranges
    odd = final[(final[["away_score", "home_score"]] < 25).any(axis=1) | (final[["away_score", "home_score"]] > 150).any(axis=1)
                | (final[["away_ppp", "home_ppp"]] < 0.4).any(axis=1) | (final[["away_ppp", "home_ppp"]] > 1.8).any(axis=1)]
    out.append(Finding("WARN" if len(odd) else "OK", "Values in a plausible range",
                       f"{len(odd)} final game(s) with a score outside 25-150 or PPP outside 0.4-1.8.",
                       _ex(odd, lambda r: f"{label(r)}: {r.away_score}-{r.home_score}")))

    # 5. Home / away orientation: home teams win by ~3 on average
    if len(final) >= 50:
        hm = float((final["home_score"] - final["away_score"]).mean())
        lvl = "WARN" if hm < 0 else "OK"
        out.append(Finding(lvl, "Home / away orientation",
                           f"Home teams' average margin is {hm:+.1f} points over {len(final)} games "
                           + ("(negative: the left team on the cards may be the home team, which would flip home "
                              "court in the ratings)." if hm < 0 else "(as expected, home court is positive).")))

    # 6. Spreads that couldn't be tied to a team, and lines that may be on the wrong team
    unresolved = g[g["spread_pre"].notna() & g["home_spread_pre"].isna()]
    out.append(Finding("WARN" if len(unresolved) else "OK", "Spreads tied to a team",
                       f"{len(unresolved)} game(s) have a spread whose team abbreviation couldn't be matched "
                       f"(no spread pick or ATS for them).",
                       _ex(unresolved, lambda r: f"{label(r)}: '{r.spread_pre_team} {r.spread_pre:+g}'")))
    s = g.dropna(subset=["home_spread_pre", "away_pregame_sq_ppp", "home_pregame_sq_ppp"])
    if len(s):
        proj = (s["home_pregame_sq_ppp"] - s["away_pregame_sq_ppp"]) * 68  # rough home margin from ShotQuality
        market = -s["home_spread_pre"]
        flipped = s[((market >= 8) & (proj <= -4)) | ((market <= -8) & (proj >= 4))]
        out.append(Finding("WARN" if len(flipped) else "OK", "Spread on the right team",
                           f"{len(flipped)} of {len(s)} lines favor one team by 8+ while ShotQuality's pregame "
                           f"projection favors the other by 4+ (check these on the site: the spread may be "
                           f"assigned to the wrong team).",
                           _ex(flipped, lambda r: f"{label(r)}: home line {r.home_spread_pre:+g} "
                                                  f"(card: '{r.line_text or ''}'), pregame SQ PPP {r.away_pregame_sq_ppp:.2f} "
                                                  f"away / {r.home_pregame_sq_ppp:.2f} home")))

    # 7. Team names: one team stored under two spellings splits its history
    names = sorted(set(g["away_team"]) | set(g["home_team"]))
    key = lambda n: re.sub(r"[^a-z0-9]", "", n.lower().replace("state", "st").replace("saint", "st"))  # noqa: E731
    groups: dict[str, list[str]] = {}
    for n in names:
        groups.setdefault(key(n), []).append(n)
    dupes = [v for v in groups.values() if len(v) > 1]
    out.append(Finding("WARN" if dupes else "OK", "One name per team",
                       f"{len(dupes)} team(s) appear under more than one spelling.", [" / ".join(v) for v in dupes[:5]]))

    # 8. SQ scoring level (informational): how far real scoring runs above SQ points league-wide
    tg = analysis.load_team_games(conn)
    ok = tg.dropna(subset=["shot_making", "poss"])
    if len(ok):
        lvl_pts = float(ok["shot_making"].mean())
        per100 = float(100 * ok["shot_making"].sum() / ok["poss"].sum())
        out.append(Finding("INFO", "Real vs SQ scoring",
                           f"Teams score {lvl_pts:+.1f} points per game ({per100:+.1f} per 100 possessions) relative to "
                           f"their SQ points. The totals picks measure luck against this league level, so a constant "
                           f"gap (free throws, say) doesn't push every pick to the Under or the Over."))

    # 9. Picks: every final game with a line should have a graded pick
    for market, table, col in (("Spread", "spot_picks", "home_spread_pre"), ("Total", "total_picks", "total_pre")):
        p = pd.read_sql_query(f"SELECT game_id, result FROM {table}", conn)
        ungraded = final[final["game_id"].isin(set(p[p["result"].isna()]["game_id"]))]
        out.append(Finding("WARN" if len(ungraded) else "OK", f"{market} picks graded",
                           f"{int(p['result'].isin(['W', 'L', 'P']).sum())} graded; {len(ungraded)} final game(s) "
                           f"with a pick still ungraded (run `cbbsq update`).", _ex(ungraded, label)))
        if len(p):
            w = p["result"].value_counts()
            wins, losses = int(w.get("W", 0)), int(w.get("L", 0))
            if wins + losses >= 200:
                pct = wins / (wins + losses)
                out.append(Finding("INFO", f"{market} picks overall",
                                   f"Every pick together has won {pct:.1%} of {wins + losses} decided bets. Far from "
                                   f"50% either way over a full season would be surprising and worth a look."))
    return out
