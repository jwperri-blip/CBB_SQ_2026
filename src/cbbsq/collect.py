"""Collect ScoreCenter data for one or more dates and store it."""

from __future__ import annotations

import gzip
import json
import random
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from . import db
from .config import Settings
from .parse import parse_cards
from .scraper import LoginRequired, Scraper


def daterange(start: date, end: date) -> list[date]:
    step = 1 if end >= start else -1
    return [start + timedelta(days=i * step) for i in range(abs((end - start).days) + 1)]


def in_season(d: date) -> bool:
    """Division I games run from early November to early April."""
    return d.month >= 11 or d.month <= 4


def write_raw(raw_dir: Path, result: dict, stamp: str) -> Path:
    """Archive everything scraped for a date (gzip JSON) so it can be re-parsed later."""
    folder = raw_dir / result["date"]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stamp}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(result, fh)
    return path


def store_result(conn, result: dict, scraped_at: str) -> tuple[int, list[str]]:
    d = date.fromisoformat(result["date"])
    records, warnings = parse_cards(result["cards"], d)
    saved = db.upsert_games(conn, records, scraped_at=scraped_at)
    if result.get("showing") is not None and len(result["cards"]) != result["showing"]:
        warnings.append(f"page says {result['showing']} games but {len(result['cards'])} cards were read")
    return saved, warnings


def collect(settings: Settings, dates: Iterable[date], *, headless: bool = True, save_html: bool = False,
            skip_done: bool = False, log: Callable[[str], None] = print) -> int:
    """Scrape each date and store it. Returns the number of dates that failed."""
    dates = list(dates)
    conn = db.connect(settings.db_path)
    if skip_done:
        done = db.collected_dates(conn)
        skipped = [d for d in dates if d.isoformat() in done]
        dates = [d for d in dates if d.isoformat() not in done]
        if skipped:
            log(f"Skipping {len(skipped)} date(s) already collected (all games final).")
    if not dates:
        log("Nothing to collect.")
        return 0

    failures = 0
    with Scraper(settings, headless=headless, log=log) as scraper:
        scraper.ensure_logged_in()
        for i, d in enumerate(dates):
            if i:
                time.sleep(settings.delay_seconds * random.uniform(0.75, 1.5))
            run_id = db.start_run(conn, d.isoformat())
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            try:
                try:
                    result = scraper.scrape_date(d, save_html=save_html)
                except LoginRequired:
                    log("Session expired; logging in again...")
                    scraper.login()
                    result = scraper.scrape_date(d, save_html=save_html)
                write_raw(settings.raw_dir, result, stamp)
                saved, warnings = store_result(conn, result, db.utcnow())
                finals = sum(1 for c in result["cards"] if str(c.get("status") or "").lower().startswith("final"))
                log(f"{d}: {len(result['cards'])} games ({finals} final), {saved} saved")
                for w in warnings:
                    log(f"  warning: {w}")
                page_count = result.get("showing")
                if page_count is None:
                    page_count = len(result["cards"])
                db.finish_run(conn, run_id, ok=True, cards_found=len(result["cards"]), games_saved=saved,
                              page_count=page_count, message="; ".join(warnings) or None)
            except LoginRequired:
                db.finish_run(conn, run_id, ok=False, message="login required")
                raise
            except Exception as exc:  # keep going with the next date
                failures += 1
                log(f"{d}: FAILED - {exc}")
                db.finish_run(conn, run_id, ok=False, message=str(exc)[:500])
    conn.close()
    return failures


def reparse(settings: Settings, log: Callable[[str], None] = print) -> int:
    """Rebuild the games table from the raw archive (after a parser fix)."""
    conn = db.connect(settings.db_path)
    files = sorted(settings.raw_dir.glob("*/*.json.gz"))
    if not files:
        log(f"No raw captures found in {settings.raw_dir}.")
        return 0
    conn.execute("DELETE FROM games")
    conn.execute("DELETE FROM snapshots")
    total = 0
    for path in files:  # oldest capture first so later scrapes win
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            result = json.load(fh)
        stamp = datetime.strptime(path.name.split(".")[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        saved, _ = store_result(conn, result, stamp.isoformat())
        total += saved
    log(f"Re-parsed {len(files)} raw capture(s), {total} game rows written.")
    conn.close()
    return total
