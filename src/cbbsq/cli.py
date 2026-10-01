"""Command-line interface: ``cbbsq <command> --help`` for details."""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import analysis, db, picks
from .config import Settings


def _date(value: str) -> date:
    v = value.strip().lower()
    if v == "today":
        return date.today()
    if v == "yesterday":
        return date.today() - timedelta(days=1)
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"not a date: {value!r} (use YYYY-MM-DD, MM/DD/YYYY, today, yesterday)")


def _print(df: pd.DataFrame, digits: int = 3, rows: int | None = None) -> None:
    if df.empty:
        print("No data. Collect some games first (`cbbsq collect`).")
        return
    with pd.option_context("display.max_rows", rows or len(df), "display.max_columns", 50, "display.width", 250,
                           "display.float_format", lambda v: f"{v:.{digits}f}"):
        print(df.head(rows) if rows else df)


def _team_games(settings: Settings, args) -> pd.DataFrame:
    conn = db.connect(settings.db_path)
    tg = analysis.load_team_games(conn, season=getattr(args, "season", None), start=getattr(args, "since", None),
                                  end=getattr(args, "until", None))
    conn.close()
    return tg


def _resolve_team(tg: pd.DataFrame, query: str) -> str:
    teams = sorted(tg["team"].unique())
    exact = [t for t in teams if t.lower() == query.lower()]
    if exact:
        return exact[0]
    hits = [t for t in teams if query.lower() in t.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        sys.exit(f"No team matching {query!r}. Try `cbbsq teams`.")
    sys.exit(f"{query!r} matches several teams: {', '.join(hits)}")


# ------------------------------------------------------------------ commands
def cmd_login(settings: Settings, args) -> None:
    from .scraper import Scraper

    with Scraper(settings, headless=not args.headed) as s:
        s.open_scorecenter()
        if not s.needs_login() and s._scorecenter_ready(8_000) and not args.force:
            print("Already logged in - session is saved in", settings.profile_dir)
            return
        s.login(interactive=args.headed)
    print("Session saved in", settings.profile_dir)


def cmd_collect(settings: Settings, args) -> None:
    from .collect import collect, daterange, in_season

    if args.start or args.end:
        start = args.start or args.end
        end = args.end or date.today() - timedelta(days=1)
        dates = daterange(start, end)
        if not args.include_offseason:
            dates = [d for d in dates if in_season(d)]
    else:
        dates = args.date or [date.today() - timedelta(days=1)]
    failures = collect(settings, dates, headless=not args.headed, save_html=args.save_html,
                       skip_done=args.skip_done)
    if failures:
        sys.exit(f"{failures} date(s) failed - see messages above (or `cbbsq status`).")


def cmd_watch(settings: Settings, args) -> None:
    """Re-scrape today's slate every N minutes until every game is final."""
    import time

    from .collect import collect

    d = args.date or date.today()
    while True:
        collect(settings, [d], headless=not args.headed)
        conn = db.connect(settings.db_path)
        left = conn.execute("SELECT COUNT(*) FROM games WHERE game_date = ? AND status NOT IN "
                            "('Final','Postponed','Canceled')", (d.isoformat(),)).fetchone()[0]
        conn.close()
        if left == 0:
            print("All games final.")
            return
        print(f"{left} game(s) not final; next check in {args.every} min (Ctrl+C to stop).")
        time.sleep(args.every * 60)


def cmd_probe(settings: Settings, args) -> None:
    """Dump everything about the ScoreCenter page so the scraper can be calibrated."""
    from .parse import parse_cards
    from .scraper import Scraper, suggest_url_template

    out = Path("data/probe") / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    d = args.date or date.today() - timedelta(days=1)
    notes = []
    with Scraper(settings, headless=not args.headed) as s:
        s.ensure_logged_in()
        url_before = s.page.url
        notes.append(f"ScoreCenter URL: {url_before}")
        ctl = s._read_date_control()
        notes.append(f"Date control: {ctl}")
        try:
            s.drain_network()
            s.goto_date(d)
            notes.append(f"Switched to {d} OK; URL now {s.page.url}")
            if "{date" not in settings.scorecenter_url:
                tpl = suggest_url_template(s.page.url, d)
                if tpl:
                    notes.append(f"The URL carries the date. Suggested .env setting:\n  SQ_SCORECENTER_URL={tpl}")
                else:
                    notes.append("The URL does not change with the date; date-picker navigation (SQ_DATE_MODE=ui) will be used.")
        except Exception as exc:
            notes.append(f"Switching to {d} FAILED: {exc}")
        data = s.extract()
        network = s.drain_network()
        s.page.screenshot(path=str(out / "page.png"), full_page=True)
        (out / "page.html").write_text(s.page.content(), encoding="utf-8")
    (out / "cards.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    records, warnings = parse_cards(data["cards"], d)
    (out / "parsed.json").write_text(json.dumps(records, indent=1), encoding="utf-8")
    with gzip.open(out / "network.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(network, fh)
    notes.append(f"Page says: showing {data.get('showing')} of {data.get('total')} games; "
                 f"cards extracted: {len(data['cards'])}; parsed games: {len(records)}")
    notes += [f"warning: {w}" for w in warnings]
    notes.append("JSON API calls seen (possible direct data sources):")
    notes += [f"  {n['status']} {n['method']} {n['url'][:160]} ({len(n['body'])} bytes)" for n in network]
    if records:
        r = records[0]
        notes.append("First parsed game: " + json.dumps({k: v for k, v in r.items() if v is not None}))
    (out / "summary.txt").write_text("\n".join(notes) + "\n", encoding="utf-8")
    print("\n".join(notes))
    print(f"\nProbe files written to {out}/ (page.png, page.html, cards.json, parsed.json, network.json.gz).")
    print("They may contain your account details - review before sharing.")


def cmd_reparse(settings: Settings, args) -> None:
    from .collect import reparse

    reparse(settings)


def cmd_status(settings: Settings, args) -> None:
    conn = db.connect(settings.db_path)
    row = conn.execute("SELECT COUNT(*), MIN(game_date), MAX(game_date), SUM(status='Final') FROM games").fetchone()
    print(f"Database: {settings.db_path}")
    print(f"Games: {row[0]} ({row[3] or 0} final), {row[1]} to {row[2]}")
    runs = pd.read_sql_query(
        "SELECT game_date, started_at, ok, cards_found, games_saved, message FROM scrape_runs "
        "ORDER BY id DESC LIMIT ?", conn, params=[args.limit])
    conn.close()
    print(f"\nLast {args.limit} scrape runs:")
    _print(runs)


def cmd_teams(settings: Settings, args) -> None:
    tg = _team_games(settings, args)
    if tg.empty:
        _print(tg)
        return
    counts = tg.groupby("team").size().rename("games").sort_index()
    if args.search:
        counts = counts[counts.index.str.contains(args.search, case=False)]
    _print(counts.to_frame())


def cmd_leaders(settings: Settings, args) -> None:
    tg = _team_games(settings, args)
    s = analysis.team_summary(tg, last_n=args.last, min_games=args.min_games)
    if not s.empty:
        r = analysis.adjusted_ratings(tg)
        s = s.join(r[["adj_net"]])
        if args.sort not in s.columns:
            sys.exit(f"--sort must be one of: {', '.join(s.columns)}")
        s = s.sort_values(args.sort, ascending=args.asc)
        s.insert(0, "rank", range(1, len(s) + 1))
        if not args.wide:
            keep = ["rank", "G", "W-L", "SQ W-L", "off_sq_ppp", "def_sq_ppp", "sq_net", "adj_net", "net", "luck",
                    "ATS", "SQ ATS"]
            s = s[keep + ([args.sort] if args.sort not in keep else [])]
    _print(s, rows=args.top)


def cmd_ratings(settings: Settings, args) -> None:
    tg = _team_games(settings, args)
    r = analysis.adjusted_ratings(tg, metric=args.metric, min_games=args.min_games)
    if not r.empty:
        print(f"League average {args.metric}: {r.attrs['avg']:.3f}; home edge: {r.attrs['home_adv_ppp']:+.3f} PPP\n")
        r.insert(0, "rank", range(1, len(r) + 1))
    _print(r, rows=args.top)


def cmd_team(settings: Settings, args) -> None:
    tg = _team_games(settings, args)
    if tg.empty:
        _print(tg)
        return
    team = _resolve_team(tg, args.name)
    log = analysis.team_log(tg, team, window=args.window)
    w = args.window
    cols = ["game_date", "side", "opponent", "pts", "opp_pts", "sq_pts", "opp_sq_pts", "sq_ppp", "opp_sq_ppp",
            "sq_pct", "pre_sq_ppp", "spread", "luck", f"sq_ppp_r{w}", f"opp_sq_ppp_r{w}", f"sq_net_ppp_r{w}"]
    print(f"{team}\n")
    _print(log[cols].set_index("game_date"), digits=2)
    summ = analysis.team_summary(tg[tg["team"] == team])
    if not summ.empty:
        print()
        _print(summ.T.rename(columns={team: "season"}))


def cmd_trending(settings: Settings, args) -> None:
    tg = _team_games(settings, args)
    t = analysis.trending(tg, window=args.window, min_games=args.min_games)
    if args.cooling and not t.empty:
        t = t.sort_values("delta_net")
    _print(t, rows=args.top)


def cmd_luck(settings: Settings, args) -> None:
    tg = _team_games(settings, args)
    t = analysis.luck_table(tg, min_games=args.min_games, last_n=args.last)
    if args.unlucky and not t.empty:
        t = t.sort_values("luck")
    _print(t, rows=args.top, digits=2)


def _update_picks(conn) -> None:
    """Save today's (and any missing past) picks, and grade everything that has finished."""
    new, graded = picks.update(conn)
    if new or graded:
        print(f"Picks: {new} new saved, {graded} newly graded (`cbbsq grade` finds the winning thresholds).\n")


def _result_text(result, cover) -> str:
    if result is None or (isinstance(result, float) and pd.isna(result)):
        return "-"
    if result == "V":
        return "void"
    return f"{result} {cover:+g}" if cover is not None and pd.notna(cover) else str(result)


def _print_total_spots(spots: pd.DataFrame, results: pd.DataFrame, on: str, args) -> None:
    view = pd.DataFrame({
        "game": spots["away_team"] + " @ " + spots["home_team"],
        "bet": [f"{b} {v:g}" if isinstance(b, str) and pd.notna(v) else f"no pick (under {args.min_games} games)"
                for b, v in zip(spots["back"], spots["back_line"])],
        "sq_proj": spots["proj_total"].map(lambda v: "-" if pd.isna(v) else f"{v:.1f}"),
        "luck_edge": spots["gap"], "from_opp_shooting": spots["opp_gap"], f"last_{args.recent}": spots["l5_gap"],
        "sq_edge": spots["sq_edge"],
    })
    if len(results):
        view["result"] = [_result_text(*results.loc[g][["result", "cover"]]) if g in results.index else "-"
                          for g in spots["game_id"]]
    print(f"Over / under spots for {on}. Under when both teams' games have scored more than their shots were worth, Over when less.\n"
          f"  sq_proj            ShotQuality's pregame projection turned into a total\n"
          f"  luck_edge          how far these teams' games have run above / below their shots, per 100 possessions\n"
          f"  from_opp_shooting  the part of that edge from opponents' shooting (positive = agrees with the bet)\n"
          f"  last_{args.recent}             the same edge over the last {args.recent} games (positive = agrees)\n"
          f"  sq_edge            points the projection is on the bet's side of the line"
          + ("\n  result             how the bet did (W/L/P and points beat the total by)" if len(results) else "") + "\n")
    for c in ("luck_edge", "from_opp_shooting", f"last_{args.recent}", "sq_edge"):
        view[c] = view[c].map(lambda v, c=c: "-" if pd.isna(v) else (f"{v:.1f}" if c == "luck_edge" else f"{v:+.1f}"))
    _print(view.set_index("game"), rows=args.top)
    if args.details:
        print("\nPoints per 100 possessions above (+) or below (-) the shots in each team's games:")
        for r in spots.head(args.top or len(spots)).itertuples():
            print(f"\n{r.away_team} @ {r.home_team}  (total: {r.total_pre if r.total_pre is not None else '-'})")
            for side in ("away", "home"):
                name, luck = getattr(r, f"{side}_team"), getattr(r, f"{side}_tot_luck")
                if pd.isna(luck):
                    print(f"  {name}: no games yet this season")
                    continue
                print(f"  {name} ({getattr(r, f'{side}_G')} games): {luck:+.1f} = {getattr(r, f'{side}_tot_shoot'):+.1f} own "
                      f"shooting, {getattr(r, f'{side}_tot_opp'):+.1f} opponents' shooting; last {args.recent}: "
                      f"{getattr(r, f'{side}_tot_recent'):+.1f}")


def cmd_spots(settings: Settings, args) -> None:
    on = (args.date or date.today()).isoformat()
    market = "total" if args.totals else "spread"
    conn = db.connect(settings.db_path)
    _update_picks(conn)
    spots = analysis.spots_for_date(conn, on, min_games=args.min_games, recent=args.recent, market=market)
    results = picks.results_for_date(conn, on, market).set_index("game_id")
    conn.close()
    if spots.empty:
        print(f"No games stored for {on}. Run `cbbsq collect --date {on}` first.")
        return
    if args.min_gap:
        spots = spots[spots["gap"] >= args.min_gap]
    if market == "total":
        _print_total_spots(spots, results, on, args)
        return
    fade = spots["away_team"].where(spots["back"] == spots["home_team"], spots["home_team"]).where(spots["back"].notna())
    bet = spots["back"] + spots["back_line"].map(lambda v: "" if pd.isna(v) else (" PK" if v == 0 else f" {v:+g}"))
    view = pd.DataFrame({
        "game": spots["away_team"] + " @ " + spots["home_team"],
        "bet": bet.fillna("no pick (under %d games)" % args.min_games),
        "against": fade.fillna("-"),
        "luck_edge": spots["gap"],
        "from_opp_misses": spots["opp_gap"],
        f"last_{args.recent}": spots["l5_gap"],
        "sq_edge": spots["sq_edge"],
    })
    if len(results):
        view["result"] = [_result_text(*results.loc[g][["result", "cover"]]) if g in results.index else "-"
                          for g in spots["game_id"]]
    print(f"Regression spots for {on}. Bet = the less lucky team.\n"
          f"  luck_edge        extra points per 100 possessions the team you bet against has gained from luck\n"
          f"  from_opp_misses  the part of that edge from opponents missing good shots (mostly chance; positive is better)\n"
          f"  last_{args.recent}           the same edge over the last {args.recent} games (positive = recent games agree)\n"
          f"  sq_edge          points ShotQuality's pregame projection likes the same bet by against the line"
          + ("\n  result           how the bet did against the pre-game line (W/L/P and points beat it by)" if len(results) else "")
          + "\n")
    for c in ("luck_edge", "from_opp_misses", f"last_{args.recent}", "sq_edge"):
        signed = c != "luck_edge"
        view[c] = view[c].map(lambda v: "-" if pd.isna(v) else (f"{v:+.1f}" if signed else f"{v:.1f}"))
    _print(view.set_index("game"), rows=args.top)
    if args.details:
        print("\nEach team's luck per 100 possessions, split into own shooting and opponents missing:")
        for r in spots.head(args.top or len(spots)).itertuples():
            print(f"\n{r.away_team} @ {r.home_team}  (line: {r.line or '-'})")
            for side in ("away", "home"):
                name, luck = getattr(r, f"{side}_team"), getattr(r, f"{side}_luck")
                if pd.isna(luck):
                    print(f"  {name}: no games yet this season")
                    continue
                print(f"  {name} ({getattr(r, f'{side}_G')} games): {luck:+.1f} = "
                      f"{getattr(r, f'{side}_shoot_luck'):+.1f} own shooting, {getattr(r, f'{side}_opp_luck'):+.1f} "
                      f"opponents missing; last {args.recent}: {getattr(r, f'{side}_luck_recent'):+.1f}")


def _pct(v) -> str:
    return "-" if v is None or pd.isna(v) else f"{v:.1%}"


def _stats_view(df: pd.DataFrame) -> pd.DataFrame:
    view = df.copy()
    view["win_pct"] = view["win_pct"].map(_pct)
    view["plus_minus"] = view["plus_minus"].map(lambda v: "-" if pd.isna(v) else f"+/-{v:.1%}")
    view["units"] = view["units"].map(lambda v: f"{v:+.1f}")
    view["roi"] = view["roi"].map(lambda v: "-" if pd.isna(v) else f"{v:+.1%}")
    return view


def cmd_grade(settings: Settings, args) -> None:
    conn = db.connect(settings.db_path)
    _update_picks(conn)
    market = "total" if args.totals else "spread"
    names = picks.metric_names(market)
    allp = picks.load_picks(conn, season=args.season, saved_only=args.saved_only, market=market)
    conn.close()
    g = picks.graded(allp)
    if g.empty:
        print("No graded picks yet. Picks are saved when you run `cbbsq spots` or `cbbsq report`, "
              "and graded once the games are final.")
        return
    target = args.target / 100 if args.target > 1 else args.target
    mins = {"luck_edge": args.min_luck, "opp_edge": args.min_opp, "last5_edge": args.min_last5, "sq_edge": args.min_sq}
    base = picks.filter_picks(g, mins)
    filtered = any(v is not None for v in mins.values())
    overall = picks.stats(g)
    what = "over / under Bet, graded against its pre-game total" if market == "total" else "regression spots Bet, graded against its pre-game line"
    print(f"Every {what} at -110 (break-even {picks.BREAKEVEN:.1%}).")
    print(f"All picks: {overall['bets']} bets, {overall['record']}, {_pct(overall['win_pct'])}, {overall['units']:+.1f} units."
          f" {int(allp['result'].isna().sum())} waiting on results.")
    if filtered:
        s = picks.stats(base)
        rule = " and ".join(f"{names[m]} >= {v:g}" for m, v in mins.items() if v is not None)
        print(f"With {rule}: {s['bets']} bets, {s['record']}, {_pct(s['win_pct'])} "
              f"(+/-{s['plus_minus']:.1%}), {s['units']:+.1f} units." if s["bets"] else f"With {rule}: no bets.")
    print(f"\nLowest threshold that wins {target:.0%} or more over at least {args.min_bets} bets"
          + (" (on top of the filter above)" if filtered else "") + ":\n")
    rows = []
    for m, name in names.items():
        hit = picks.threshold_for(base, m, target, args.min_bets)
        rows.append({"metric": name, "bet when": ("any value" if hit["any"] else f">= {hit['threshold']:g}") if hit else "not reached",
                     **({k: hit[k] for k in ("bets", "record", "win_pct", "plus_minus", "units", "roi")} if hit else
                        {"bets": 0, "record": "-", "win_pct": np.nan, "plus_minus": np.nan, "units": 0.0, "roi": np.nan})})
    _print(_stats_view(pd.DataFrame(rows)).set_index("metric"))
    metrics = [args.metric] if args.metric else list(picks.METRICS)
    for m in metrics:
        lad = picks.ladder(base, m)
        if lad.empty:
            continue
        print(f"\n{names[m]}: win % at each threshold")
        lad["threshold"] = lad["threshold"].map(lambda v: f">= {v:g}")
        _print(_stats_view(lad).set_index("threshold"))
    print("\n+/- is the 95% range around the win %. A threshold picked by looking at results flatters itself:"
          "\ntrust it once the low end of the range clears 52.4%, and check it holds on the next season (--season).")


def cmd_export(settings: Settings, args) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    conn = db.connect(settings.db_path)
    games = pd.read_sql_query("SELECT * FROM games ORDER BY game_date, game_id", conn)
    tg = analysis.load_team_games(conn, final_only=False)
    conn.close()
    games.to_csv(out / "games.csv", index=False)
    tg.to_csv(out / "team_games.csv", index=False)
    final = tg[tg["status"] == "Final"]
    analysis.team_summary(final).to_csv(out / "team_summary.csv")
    analysis.adjusted_ratings(final).to_csv(out / "adjusted_ratings.csv")
    print(f"Wrote games.csv, team_games.csv, team_summary.csv, adjusted_ratings.csv to {out}/")


def cmd_report(settings: Settings, args) -> None:
    from .report import write_report

    tg = _team_games(settings, args)
    on = (args.spots_date or date.today()).isoformat()
    out = Path(args.out)
    pages = {"spread": out, "total": out.with_name(f"{out.stem}-totals{out.suffix}")}
    conn = db.connect(settings.db_path)
    _update_picks(conn)
    for market, path in pages.items():
        spots = analysis.spots_for_date(conn, on, market=market)
        results = picks.results_for_date(conn, on, market)
        if len(spots) and len(results):
            spots = spots.merge(results, on="game_id", how="left")
        graded = picks.graded(picks.load_picks(conn, season=args.season, market=market))
        other = pages["total" if market == "spread" else "spread"].name
        write_report(tg, path, min_games=args.min_games, spots=spots, spots_date=on, graded=graded,
                     market=market, other_page=other)
    conn.close()
    print(f"Dashboard written to {pages['spread'].resolve()}\n"
          f"Totals page written to {pages['total'].resolve()}\nOpen either in your browser; the header switches between them.")


# ---------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cbbsq", description="Log, track and analyze ShotQuality CBB data.")
    p.add_argument("--env", default=".env", help="settings file (default: .env)")
    sub = p.add_subparsers(dest="command", required=True)

    def filters(sp, min_games=1):
        sp.add_argument("--season", type=int, help="season by end year, e.g. 2026 for 2025-26")
        sp.add_argument("--since", type=lambda v: _date(v).isoformat(), help="first game date to include")
        sp.add_argument("--until", type=lambda v: _date(v).isoformat(), help="last game date to include")
        sp.add_argument("--min-games", type=int, default=min_games)
        sp.add_argument("--top", type=int, help="show only the first N rows")

    sp = sub.add_parser("login", help="log in and remember the session")
    sp.add_argument("--headed", action="store_true", help="open a visible browser and log in by hand")
    sp.add_argument("--force", action="store_true", help="log in again even if a session exists")
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("collect", help="scrape one or more dates into the database")
    sp.add_argument("--date", type=_date, action="append", help="date to collect (repeatable; default yesterday)")
    sp.add_argument("--start", type=_date, help="backfill from this date...")
    sp.add_argument("--end", type=_date, help="...through this date (default yesterday)")
    sp.add_argument("--skip-done", action="store_true", help="skip dates whose games are already all final")
    sp.add_argument("--include-offseason", action="store_true", help="don't skip May-October dates in ranges")
    sp.add_argument("--save-html", action="store_true", help="also archive the page HTML")
    sp.add_argument("--headed", action="store_true", help="show the browser while scraping")
    sp.set_defaults(func=cmd_collect)

    sp = sub.add_parser("watch", help="keep re-collecting today's games until all are final")
    sp.add_argument("--date", type=_date)
    sp.add_argument("--every", type=float, default=10, help="minutes between scrapes (default 10)")
    sp.add_argument("--headed", action="store_true")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("probe", help="save a diagnostic snapshot of the ScoreCenter page")
    sp.add_argument("--date", type=_date, help="date to switch to (default yesterday)")
    sp.add_argument("--headed", action="store_true")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("reparse", help="rebuild the games table from the raw archive")
    sp.set_defaults(func=cmd_reparse)

    sp = sub.add_parser("status", help="database coverage and recent scrape runs")
    sp.add_argument("--limit", type=int, default=15)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("teams", help="list teams in the database")
    sp.add_argument("search", nargs="?")
    filters(sp)
    sp.set_defaults(func=cmd_teams)

    sp = sub.add_parser("leaders", help="team leaderboard")
    filters(sp, min_games=3)
    sp.add_argument("--last", type=int, help="use each team's last N games only")
    sp.add_argument("--sort", default="sq_net", help="column to sort by (default sq_net)")
    sp.add_argument("--asc", action="store_true", help="sort ascending (e.g. for def_sq_ppp)")
    sp.add_argument("--wide", action="store_true", help="show every column")
    sp.set_defaults(func=cmd_leaders)

    sp = sub.add_parser("ratings", help="opponent-adjusted SQ ratings")
    filters(sp, min_games=3)
    sp.add_argument("--metric", default="sq_ppp", choices=["sq_ppp", "ppp"], help="rate shot quality or actual PPP")
    sp.set_defaults(func=cmd_ratings)

    sp = sub.add_parser("team", help="one team's game log with rolling trends")
    sp.add_argument("name", help="team name or part of it")
    sp.add_argument("--window", type=int, default=5, help="rolling window (games)")
    filters(sp)
    sp.set_defaults(func=cmd_team)

    sp = sub.add_parser("trending", help="teams whose recent SQ differs most from their season")
    sp.add_argument("--window", type=int, default=5)
    sp.add_argument("--cooling", action="store_true", help="list the biggest drops first")
    filters(sp, min_games=8)
    sp.set_defaults(func=cmd_trending)

    sp = sub.add_parser("luck", help="results vs shot quality: regression candidates")
    sp.add_argument("--unlucky", action="store_true", help="list the unluckiest first")
    sp.add_argument("--last", type=int, help="only each team's last N games (e.g. 5 or 10)")
    filters(sp, min_games=5)
    sp.set_defaults(func=cmd_luck)

    sp = sub.add_parser("spots", help="today's games ranked by the luck gap between the two teams")
    sp.add_argument("--date", type=_date, help="slate date (default today)")
    sp.add_argument("--min-games", type=int, default=5, help="prior games each team needs for a pick")
    sp.add_argument("--recent", type=int, default=5, help="games in the recent-luck column")
    sp.add_argument("--min-gap", type=float, help="only show games with at least this luck edge")
    sp.add_argument("--details", action="store_true", help="also print each team's luck breakdown")
    sp.add_argument("--totals", action="store_true", help="over / under picks instead of spreads")
    sp.add_argument("--top", type=int, help="show only the first N rows")
    sp.set_defaults(func=cmd_spots)

    sp = sub.add_parser("grade", help="grade the saved picks and find the thresholds that win")
    sp.add_argument("--target", type=float, default=55, help="win %% to look for (default 55)")
    sp.add_argument("--min-bets", type=int, default=50, help="fewest bets a threshold must have (default 50)")
    sp.add_argument("--metric", choices=list(picks.METRICS), help="only show this metric's win %% ladder")
    sp.add_argument("--min-luck", type=float, help="only picks with at least this luck edge")
    sp.add_argument("--min-opp", type=float, help="only picks with at least this much from opp. misses (opp. shooting for totals)")
    sp.add_argument("--min-last5", type=float, help="only picks with at least this last-5 edge")
    sp.add_argument("--min-sq", type=float, help="only picks with at least this SQ edge")
    sp.add_argument("--season", type=int, help="season by end year, e.g. 2026 for 2025-26")
    sp.add_argument("--saved-only", action="store_true", help="only picks saved on game day (no filled-in history)")
    sp.add_argument("--totals", action="store_true", help="grade the over / under picks instead of spreads")
    sp.set_defaults(func=cmd_grade)

    sp = sub.add_parser("export", help="write CSV files for Excel / Sheets")
    sp.add_argument("--out", default="data/exports")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("report", help="write the HTML dashboard")
    sp.add_argument("--out", default="reports/dashboard.html")
    sp.add_argument("--spots-date", type=_date, help="slate for the regression spots table (default today)")
    filters(sp, min_games=3)
    sp.set_defaults(func=cmd_report)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = Settings.load(args.env)
    try:
        args.func(settings, args)
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:  # e.g. `cbbsq team X | head`
        sys.stderr.close()
    except Exception as exc:
        from .scraper import ScraperError

        if isinstance(exc, ScraperError):
            sys.exit(f"error: {exc}")
        raise


if __name__ == "__main__":
    main()
