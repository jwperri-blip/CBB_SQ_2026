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

    # 3. Card read correctly. ShotQuality's SQ PPP isn't always over exactly the same possessions as the
    # score's PPP, so small gaps between score / PPP and SQ score / SQ PPP are the site's own numbers
    # (confirmed against the card text). A gap over 25% means a value went into the wrong row.
    d = final.dropna(subset=need + ["away_sq_ppp", "home_sq_ppp"])
    gaps = []
    for side in ("away", "home"):
        p1 = d[f"{side}_score"] / d[f"{side}_ppp"]
        p2 = d[f"{side}_sq_score"] / d[f"{side}_sq_ppp"]
        gaps.append((p1 - p2).abs() / p1)
    gap = pd.concat(gaps, axis=1).max(axis=1) if len(d) else pd.Series(dtype=float)
    fmt = lambda r: (f"{label(r)}: possessions from score {r.away_score / r.away_ppp:.1f} / {r.home_score / r.home_ppp:.1f}, "  # noqa: E731
                     f"from SQ {r.away_sq_score / r.away_sq_ppp:.1f} / {r.home_sq_score / r.home_sq_ppp:.1f} (away / home)")
    misread = d[gap > 0.25]
    out.append(Finding("WARN" if len(misread) else "OK", "Card values read into the right rows",
                       f"{len(misread)} of {len(d)} games where score / PPP and SQ score / SQ PPP differ by more than "
                       f"25% on possessions (a value read into the wrong row). `cbbsq audit --game <team>` shows the card.",
                       _ex(misread, fmt)))
    out.append(Finding("INFO", "SQ possessions vs scored possessions",
                       f"In {int(((gap > 0.06) & (gap <= 0.25)).sum())} of {len(d)} games ShotQuality's SQ PPP implies "
                       f"6-25% fewer or more possessions than the score's PPP. These are the site's own numbers (its SQ "
                       f"possessions aren't always the scored ones); the metrics use the score's possessions.",
                       _ex(d[(gap > 0.06) & (gap <= 0.25)], fmt, 2)))

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
    # Spreads matched on a weak guess (letters in order, not a prefix or initials) are worth a look.
    from .parse import _abbr_score

    sp = g[g["spread_pre_team"].notna() & g["home_spread_pre"].notna()]
    weak = sp[[max(_abbr_score(t, a), _abbr_score(t, h)) == 1
               for t, a, h in zip(sp["spread_pre_team"], sp["away_team"], sp["home_team"])]] if len(sp) else sp
    out.append(Finding("WARN" if len(weak) else "OK", "Spreads matched confidently",
                       f"{len(weak)} spread(s) were tied to a team only by a weak letter match; check them on the site.",
                       _ex(weak, lambda r: f"{label(r)}: '{r.line_text or ''}' -> home line {r.home_spread_pre:+g}")))

    # 6b. Is "Pregame SQ" a game-day number or the team's current rating? If every team carries one value
    # all season, and it matches the team's full-season SQ PPP better than its early-season SQ PPP, the
    # site is filling in past games with end-of-season ratings: they contain later results (hindsight),
    # so SQ edge on filled-in history would look better than it can be in real time.
    rows = []
    for side in ("away", "home"):
        rows.append(pd.DataFrame({"team": final[f"{side}_team"], "season": final["season"], "date": final["game_date"],
                                  "pre": final[f"{side}_pregame_sq_ppp"], "sq": final[f"{side}_sq_ppp"]}))
    long = pd.concat(rows).dropna().sort_values("date")
    if len(long) >= 200:
        per = long.groupby(["season", "team"])
        stats = pd.DataFrame({"G": per.size(), "distinct": per["pre"].apply(lambda v: v.round(2).nunique()),
                              "pre": per["pre"].mean(), "full": per["sq"].mean(),
                              "early": per["sq"].apply(lambda v: v.head(8).mean())})
        stats = stats[stats["G"] >= 12]
        if len(stats) >= 20:
            static = float((stats["distinct"] == 1).mean())
            r_full = float(stats["pre"].corr(stats["full"]))
            r_early = float(stats["pre"].corr(stats["early"]))
            hindsight = static >= 0.6 and r_full > r_early + 0.05
            out.append(Finding("WARN" if hindsight else "INFO", "Pregame SQ: game-day number or current rating?",
                               f"{static:.0%} of {len(stats)} teams have the same pregame SQ value in every game. It "
                               f"matches teams' full-season SQ PPP (r = {r_full:.2f}) "
                               + ("better than their first-8-games SQ PPP" if r_full > r_early else "no better than their first-8-games SQ PPP")
                               + f" (r = {r_early:.2f}). "
                               + ("That looks like each team's end-of-season rating filled into past games: it contains "
                                  "later results, so SQ edge on filled-in history is hindsight. Judge SQ edge only on picks "
                                  "saved on game day (`cbbsq grade --saved-only`)." if hindsight else
                                  "No sign that later results leaked into past games' pregame values.")))

    # 7. Team names: one team stored under two spellings splits its history
    names = sorted(set(g["away_team"]) | set(g["home_team"]))
    key = lambda n: re.sub(r"[^a-z0-9]", "", n.split(",")[0].lower().replace("state", "st").replace("saint", "st"))  # noqa: E731
    groups: dict[str, list[str]] = {}
    for n in names:
        groups.setdefault(key(n), []).append(n)
    dupes = [v for v in groups.values() if len(v) > 1]
    out.append(Finding("WARN" if dupes else "OK", "One name per team",
                       f"{len(dupes)} team(s) appear under more than one spelling.", [" / ".join(v) for v in dupes[:5]]))
    games_per = pd.concat([g["away_team"], g["home_team"]]).value_counts()
    odd_names = [n for n in names if re.search(r"[,(\[]", n)]
    out.append(Finding("INFO" if odd_names else "OK", "Team names with extra text",
                       f"{len(odd_names)} team name(s) contain a comma or bracket. That's harmless as long as each team "
                       f"has one spelling (see the check above).",
                       [f"{n} ({games_per.get(n, 0)} games)" for n in odd_names[:8]]))

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


def game_detail(conn: sqlite3.Connection, query: str, raw_dir) -> list[str]:
    """Everything stored about the games matching ``query`` (part of a team name or game id): the stored
    row, every scrape of it, and the card text the site showed in the latest raw capture of that date."""
    import gzip
    import json
    from pathlib import Path

    q = f"%{query.lower()}%"
    rows = conn.execute("SELECT * FROM games WHERE lower(game_id) LIKE ? OR lower(away_team) LIKE ? OR lower(home_team) LIKE ? "
                        "ORDER BY game_date LIMIT 5", (q, q, q)).fetchall()
    lines: list[str] = []
    fields = ["status", "away_score", "home_score", "away_sq_score", "home_sq_score", "away_ppp", "home_ppp",
              "away_sq_ppp", "home_sq_ppp", "away_pregame_sq_ppp", "home_pregame_sq_ppp", "line_text", "ou_text"]
    for r in rows:
        r = dict(r)
        lines.append(f"=== {r['game_date']} {r['away_team']} @ {r['home_team']}  ({r['game_id']})")
        lines.append("stored: " + ", ".join(f"{f}={r[f]}" for f in fields))
        snaps = conn.execute("SELECT scraped_at, status, data FROM snapshots WHERE game_id = ? ORDER BY scraped_at",
                             (r["game_id"],)).fetchall()
        lines.append(f"{len(snaps)} scrape(s):")
        for sc in snaps:
            d = json.loads(sc[2])
            lines.append(f"  {sc[0]} {sc[1]}: score {d.get('away_score')}-{d.get('home_score')}, SQ {d.get('away_sq_score')}-"
                         f"{d.get('home_sq_score')}, PPP {d.get('away_ppp')}/{d.get('home_ppp')}, SQ PPP "
                         f"{d.get('away_sq_ppp')}/{d.get('home_sq_ppp')}, pregame {d.get('away_pregame_sq_ppp')}/"
                         f"{d.get('home_pregame_sq_ppp')}")
        files = sorted(Path(raw_dir).glob(f"{r['game_date']}/*.json.gz"))
        for path in reversed(files):
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                cards = json.load(fh).get("cards", [])
            hit = [c for c in cards if r["away_team"].split()[0] in (c.get("text") or "") and
                   r["home_team"].split()[0] in (c.get("text") or "")]
            if hit:
                lines.append(f"card text in {path.name}: {hit[0].get('text')}")
                lines.append(f"rows read: {hit[0].get('rows')}")
                break
        else:
            lines.append("no raw capture found for this date")
    return lines or [f"No game matches {query!r}."]
