"""SQLite storage.

* ``games``       - latest known state of every game (one row per game)
* ``snapshots``   - append-only log of every scrape of every game (tracks live games too)
* ``scrape_runs`` - one row per date scraped, for auditing coverage and failures
* ``team_games``  - view with one row per team per game, from that team's perspective
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .parse import GAME_FIELDS

_INTEGER = {"season", "away_score", "home_score"}
_NUMERIC = {
    "away_score", "home_score", "away_sq_score", "home_sq_score", "away_sq_pct", "home_sq_pct",
    "away_ppp", "home_ppp", "away_sq_ppp", "home_sq_ppp", "away_pregame_sq_ppp", "home_pregame_sq_ppp",
    "spread_pre", "spread_cur", "home_spread_pre", "home_spread_cur", "total_pre", "total_cur",
}


def _col_type(name: str) -> str:
    if name in _INTEGER:
        return "INTEGER"
    return "REAL" if name in _NUMERIC else "TEXT"


_GAME_COLS = ",\n    ".join(
    f"{f} {_col_type(f)}{' PRIMARY KEY' if f == 'game_id' else ''}" for f in GAME_FIELDS
)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS games (
    {_GAME_COLS},
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_games_date ON games(game_date);
CREATE INDEX IF NOT EXISTS idx_games_away ON games(away_team);
CREATE INDEX IF NOT EXISTS idx_games_home ON games(home_team);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    scraped_at TEXT NOT NULL,
    status TEXT,
    status_detail TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_game ON snapshots(game_id, scraped_at);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    cards_found INTEGER,
    games_saved INTEGER,
    page_count INTEGER,
    ok INTEGER NOT NULL DEFAULT 0,
    message TEXT
);

DROP VIEW IF EXISTS team_games;
CREATE VIEW team_games AS
SELECT game_id, game_date, season, status, 'away' AS side,
       away_team AS team, home_team AS opponent,
       away_score AS pts, home_score AS opp_pts,
       away_sq_score AS sq_pts, home_sq_score AS opp_sq_pts,
       away_sq_pct AS sq_pct, home_sq_pct AS opp_sq_pct,
       away_ppp AS ppp, home_ppp AS opp_ppp,
       away_sq_ppp AS sq_ppp, home_sq_ppp AS opp_sq_ppp,
       away_pregame_sq_ppp AS pre_sq_ppp, home_pregame_sq_ppp AS opp_pre_sq_ppp,
       -home_spread_pre AS spread, total_pre AS total_line
FROM games
UNION ALL
SELECT game_id, game_date, season, status, 'home' AS side,
       home_team, away_team,
       home_score, away_score,
       home_sq_score, away_sq_score,
       home_sq_pct, away_sq_pct,
       home_ppp, away_ppp,
       home_sq_ppp, away_sq_ppp,
       home_pregame_sq_ppp, away_pregame_sq_ppp,
       home_spread_pre, total_pre
FROM games;
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_games(conn: sqlite3.Connection, records: Iterable[dict], scraped_at: str | None = None) -> int:
    """Store scraped records. Every record is logged to ``snapshots``; ``games``
    keeps the latest state, except that a Final game is never downgraded by a
    later non-final scrape and empty values never erase known ones."""
    scraped_at = scraped_at or utcnow()
    saved = 0
    for rec in records:
        conn.execute(
            "INSERT INTO snapshots (game_id, scraped_at, status, status_detail, data) VALUES (?,?,?,?,?)",
            (rec["game_id"], scraped_at, rec.get("status"), rec.get("status_detail"), json.dumps(rec)),
        )
        row = conn.execute("SELECT status FROM games WHERE game_id = ?", (rec["game_id"],)).fetchone()
        if row and row["status"] == "Final" and rec.get("status") != "Final":
            continue
        cols = GAME_FIELDS + ["first_seen_at", "updated_at"]
        values = [rec.get(f) for f in GAME_FIELDS] + [scraped_at, scraped_at]
        updates = ", ".join(
            f"{c} = COALESCE(excluded.{c}, games.{c})" for c in GAME_FIELDS if c != "game_id"
        )
        conn.execute(
            f"INSERT INTO games ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
            f"ON CONFLICT(game_id) DO UPDATE SET {updates}, updated_at = excluded.updated_at",
            values,
        )
        saved += 1
    conn.commit()
    resolve_line_teams(conn)
    return saved


def resolve_line_teams(conn: sqlite3.Connection) -> int:
    """Fill in home_spread_* for lines whose team abbreviation couldn't be matched by name
    (e.g. "UNC"), by learning each abbreviation from every game it appears in: the team
    common to all of those games is the one it stands for."""
    fixed = 0
    for slot in ("pre", "cur"):
        rows = conn.execute(
            f"SELECT spread_{slot}_team AS abbr, away_team, home_team FROM games WHERE spread_{slot}_team IS NOT NULL"
        ).fetchall()
        candidates: dict[str, set] = {}
        for r in rows:
            teams = {r["away_team"], r["home_team"]}
            candidates[r["abbr"]] = candidates.get(r["abbr"], teams) & teams
        for abbr, teams in candidates.items():
            if len(teams) != 1:
                continue
            (team,) = teams
            cur = conn.execute(
                f"UPDATE games SET home_spread_{slot} = CASE WHEN home_team = ? THEN spread_{slot} "
                f"ELSE -spread_{slot} END WHERE spread_{slot}_team = ? AND home_spread_{slot} IS NULL "
                f"AND spread_{slot} IS NOT NULL AND ? IN (home_team, away_team)",
                (team, abbr, team),
            )
            fixed += cur.rowcount
    conn.commit()
    return fixed


def start_run(conn: sqlite3.Connection, game_date: str) -> int:
    cur = conn.execute(
        "INSERT INTO scrape_runs (game_date, started_at) VALUES (?, ?)", (game_date, utcnow())
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, *, ok: bool, cards_found: int | None = None,
               games_saved: int | None = None, page_count: int | None = None, message: str | None = None) -> None:
    conn.execute(
        "UPDATE scrape_runs SET finished_at=?, ok=?, cards_found=?, games_saved=?, page_count=?, message=? "
        "WHERE id=?",
        (utcnow(), int(ok), cards_found, games_saved, page_count, message, run_id),
    )
    conn.commit()


def collected_dates(conn: sqlite3.Connection, final_only: bool = True) -> set[str]:
    """Dates whose games are all Final (so re-scraping them is unnecessary)."""
    if final_only:
        q = ("SELECT game_date FROM games GROUP BY game_date "
             "HAVING SUM(CASE WHEN status IN ('Final','Postponed','Canceled') THEN 0 ELSE 1 END) = 0")
    else:
        q = "SELECT DISTINCT game_date FROM games"
    dates = {r[0] for r in conn.execute(q)}
    # Dates with a successful run that found zero games (off days) count as collected too.
    dates |= {r[0] for r in conn.execute(
        "SELECT game_date FROM scrape_runs WHERE ok = 1 AND page_count = 0")}
    return dates
