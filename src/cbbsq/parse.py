"""Normalize raw ScoreCenter card data into flat game records.

The browser-side extractor (``extract_cards.js``) returns strings exactly as
they appear on the page. Everything here is pure Python so it can be unit
tested and re-run over archived raw captures when the parsing improves.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

# Columns stored per game, in table order (see db.py).
GAME_FIELDS = [
    "game_id", "game_date", "season", "status", "status_detail",
    "away_team", "home_team", "away_logo", "home_logo",
    "away_score", "home_score",
    "away_sq_score", "home_sq_score",
    "away_sq_pct", "home_sq_pct",
    "away_ppp", "home_ppp",
    "away_sq_ppp", "home_sq_ppp",
    "away_pregame_sq_ppp", "home_pregame_sq_ppp",
    "spread_pre_team", "spread_pre", "spread_cur_team", "spread_cur",
    "home_spread_pre", "home_spread_cur",
    "total_pre", "total_cur",
    "line_text", "ou_text",
]

_ROW_MAP = {
    "score": ("away_score", "home_score", int),
    "sq_score": ("away_sq_score", "home_sq_score", float),
    "sq_pct": ("away_sq_pct", "home_sq_pct", float),
    "ppp_live": ("away_ppp", "home_ppp", float),
    "ppp_live_sq": ("away_sq_ppp", "home_sq_ppp", float),
    "ppp_pregame_sq": ("away_pregame_sq_ppp", "home_pregame_sq_ppp", float),
}

_PICK = r"PK|PICK|EVEN|EV"
_LINE_RE = re.compile(
    r"(?P<kind>Pre-?Game|Opening|Open|Current|Live)\s*:?\s*"
    r"(?:(?P<team>[A-Za-z][A-Za-z.&'\-]{0,11})\s+)?"
    r"(?P<val>[-+]?\d+(?:\.\d+)?|" + _PICK + r")(?![\d.])",
    re.IGNORECASE,
)


def season_for(d: date) -> int:
    """College basketball season label = the calendar year the season ends in.

    Games from November 2025 through April 2026 belong to season 2026.
    """
    return d.year + 1 if d.month >= 7 else d.year


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def make_game_id(game_date: date, away: str, home: str) -> str:
    return f"{game_date.isoformat()}_{slugify(away)}_at_{slugify(home)}"


def to_number(value: Any, kind=float) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").rstrip("%")
    if text in {"", "-", "--", "—", "N/A", "n/a"}:
        return None
    try:
        num = float(text)
    except ValueError:
        return None
    return int(round(num)) if kind is int else num


def clean_team_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    name = re.sub(r"\s+", " ", name).strip()
    # Drop a leading ranking such as "#12 " or "12 " (no D-I name starts with a digit).
    name = re.sub(r"^#?\d{1,2}\s+", "", name)
    return name or None


def normalize_status(status: Optional[str]) -> Optional[str]:
    if not status:
        return None
    s = status.strip().lower()
    if s.startswith("final"):
        return "Final"
    if s.startswith("half"):
        return "Halftime"
    if s.startswith("live") or s.startswith("in progress"):
        return "Live"
    if s.startswith(("scheduled", "upcoming", "pre")):
        return "Scheduled"
    if s.startswith("postponed"):
        return "Postponed"
    if s.startswith(("canceled", "cancelled")):
        return "Canceled"
    return status.strip()


def _team_letters(name: str) -> str:
    return re.sub(r"[^A-Z]", "", name.upper())


def _abbr_score(abbr: str, team: str) -> int:
    """How plausibly ``abbr`` (e.g. "BOS") abbreviates ``team`` ("Boston U. Terriers")."""
    abbr = re.sub(r"[^A-Z]", "", abbr.upper())
    words = [re.sub(r"[^A-Z]", "", w.upper()) for w in team.split()]
    words = [w for w in words if w]
    if not abbr or not words:
        return 0
    letters = _team_letters(team)
    if letters.startswith(abbr) or words[0].startswith(abbr):
        return 3
    initials = "".join(w[0] for w in words)
    if initials.startswith(abbr):
        return 2
    if abbr[0] == letters[0]:
        it = iter(letters)
        if all(ch in it for ch in abbr):  # ordered subsequence
            return 1
    return 0


def resolve_side(abbr: Optional[str], away: Optional[str], home: Optional[str]) -> Optional[str]:
    """Return "away"/"home" for the team a line abbreviation refers to, if unambiguous."""
    if not abbr or not away or not home:
        return None
    a, h = _abbr_score(abbr, away), _abbr_score(abbr, home)
    if a > h:
        return "away"
    if h > a:
        return "home"
    return None


def parse_lines(line_text: Optional[str], ou_text: Optional[str]) -> dict:
    """Parse the "Game Line" and "Over / Under" chips.

    ``"Pre-Game BOS -6.5 Current BOS -16.5"`` -> spread_pre_team="BOS", spread_pre=-6.5, ...
    ``"Pre-Game 140.5 Current 165.5"`` -> total_pre=140.5, total_cur=165.5
    """
    out: dict = {
        "spread_pre_team": None, "spread_pre": None,
        "spread_cur_team": None, "spread_cur": None,
        "total_pre": None, "total_cur": None,
    }
    for m in _LINE_RE.finditer(line_text or ""):
        slot = "pre" if m.group("kind").lower().startswith(("pre", "open")) else "cur"
        val = m.group("val")
        num = 0.0 if re.fullmatch(_PICK, val, re.IGNORECASE) else float(val)
        team = m.group("team")
        if team and team.upper() in {"PK", "PICK", "EVEN", "EV"}:
            team = None
        if out[f"spread_{slot}"] is None:
            out[f"spread_{slot}_team"] = team.upper() if team else None
            out[f"spread_{slot}"] = num
    for m in _LINE_RE.finditer(ou_text or ""):
        slot = "pre" if m.group("kind").lower().startswith(("pre", "open")) else "cur"
        val = m.group("val")
        if re.fullmatch(_PICK, val, re.IGNORECASE):
            continue
        if out[f"total_{slot}"] is None:
            out[f"total_{slot}"] = float(val)
    return out


def _home_spread(team: Optional[str], value: Optional[float], away: str, home: str) -> Optional[float]:
    """Express a spread from the home team's perspective (negative = home favored)."""
    if value is None:
        return None
    if value == 0:
        return 0.0
    side = resolve_side(team, away, home)
    if side == "home":
        return value
    if side == "away":
        return -value
    return None


def parse_card(card: dict, game_date: date) -> Optional[dict]:
    """Turn one raw card (from extract_cards.js) into a flat game record.

    Returns None when the card doesn't identify both teams.
    """
    away = clean_team_name((card.get("away") or {}).get("name"))
    home = clean_team_name((card.get("home") or {}).get("name"))
    if not away or not home:
        return None

    rec: dict = {f: None for f in GAME_FIELDS}
    rec.update(
        game_id=make_game_id(game_date, away, home),
        game_date=game_date.isoformat(),
        season=season_for(game_date),
        status=normalize_status(card.get("status")),
        status_detail=card.get("detail"),
        away_team=away,
        home_team=home,
        away_logo=((card.get("away") or {}).get("logo") or {}).get("src"),
        home_logo=((card.get("home") or {}).get("logo") or {}).get("src"),
        line_text=card.get("line_text"),
        ou_text=card.get("ou_text"),
    )
    rows = card.get("rows") or {}
    for key, (away_col, home_col, kind) in _ROW_MAP.items():
        pair = rows.get(key) or [None, None]
        rec[away_col] = to_number(pair[0], kind)
        rec[home_col] = to_number(pair[1], kind)

    rec.update(parse_lines(rec["line_text"], rec["ou_text"]))
    rec["home_spread_pre"] = _home_spread(rec["spread_pre_team"], rec["spread_pre"], away, home)
    rec["home_spread_cur"] = _home_spread(rec["spread_cur_team"], rec["spread_cur"], away, home)

    # A card that says nothing about status but has a final-looking score stays
    # unknown on purpose: analysis only trusts games the site marks Final.
    return rec


def parse_cards(cards: list[dict], game_date: date) -> tuple[list[dict], list[str]]:
    """Parse all cards; returns (records, warnings)."""
    records, warnings, seen = [], [], set()
    for i, card in enumerate(cards):
        rec = parse_card(card, game_date)
        if rec is None:
            warnings.append(f"card {i}: could not read team names ({(card.get('text') or '')[:80]!r})")
            continue
        if rec["game_id"] in seen:
            continue
        seen.add(rec["game_id"])
        if rec["status"] == "Final" and (rec["away_sq_score"] is None or rec["home_sq_score"] is None):
            warnings.append(f"{rec['away_team']} @ {rec['home_team']}: final but SQ Score missing")
        records.append(rec)
    return records, warnings
