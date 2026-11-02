"""League profiles: the same engine runs college basketball (``cbbsq``) and the NBA (``nbasq``).

Everything league-specific lives here: the season calendar, where data / pages / the browser login are
kept, the environment variable names, the background job labels and the web server port. The two leagues
never share a database, a raw archive, a browser profile or a report folder, so they can run side by side.

The current league is process-wide: ``nbasq`` (or ``cbbsq --league nba``) calls ``use("nba")`` before
anything else runs. College basketball is the default, so existing setups behave exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class League:
    key: str               # "cbb" / "nba"
    command: str           # the CLI name: "cbbsq" / "nbasq"
    title: str             # page and notification title
    name: str              # short name used in messages ("CBB", "NBA")
    season_months: tuple   # months with games (1-12)
    season_text: str       # "November to April"
    offseason_text: str    # "May-October"
    first_day: str         # MM-DD a typical season starts, for backfill hints
    env_prefix: str        # settings prefix: CBBSQ_DB, NBASQ_DB, ...
    url_var: str           # the ScoreCenter URL setting
    data_dir: Path
    db_file: Path
    raw_dir: Path
    profile_dir: Path
    reports_dir: Path
    port: int
    score_range: tuple     # plausible final score for one team (audit)
    default_url: str | None = None  # ScoreCenter address used when the URL setting is empty

    def in_season(self, d: date) -> bool:
        return d.month in self.season_months

    @property
    def log_path(self) -> Path:
        return self.data_dir / "daily.log"

    @property
    def dashboard(self) -> Path:
        return self.reports_dir / "dashboard.html"

    @property
    def daily_label(self) -> str:
        return f"com.{self.command}.daily"

    @property
    def serve_label(self) -> str:
        return f"com.{self.command}.serve"


CBB = League(
    key="cbb", command="cbbsq", title="CBB Shot Quality", name="CBB",
    season_months=(11, 12, 1, 2, 3, 4), season_text="November to April", offseason_text="May-October",
    first_day="11-03", env_prefix="CBBSQ_", url_var="SQ_SCORECENTER_URL",
    data_dir=Path("data"), db_file=Path("data/cbbsq.sqlite"), raw_dir=Path("data/raw"),
    profile_dir=Path(".auth/profile"), reports_dir=Path("reports"), port=8765, score_range=(25, 150),
)

NBA = League(
    key="nba", command="nbasq", title="NBA Shot Quality", name="NBA",
    # Regular season late October to mid April, then the play-in and playoffs into June.
    season_months=(10, 11, 12, 1, 2, 3, 4, 5, 6), season_text="October to June", offseason_text="July-September",
    first_day="10-21", env_prefix="NBASQ_", url_var="NBA_SQ_SCORECENTER_URL",
    data_dir=Path("data/nba"), db_file=Path("data/nba/nbasq.sqlite"), raw_dir=Path("data/nba/raw"),
    # Its own browser profile: Chromium locks a profile, so a CBB and an NBA run can't share one.
    profile_dir=Path(".auth/nba-profile"), reports_dir=Path("reports/nba"), port=8766, score_range=(60, 190),
    default_url="https://trader.shotquality.com/nba/scorecenter",
)

LEAGUES = {lg.key: lg for lg in (CBB, NBA)}
_current = CBB


def current() -> League:
    return _current


def use(key: str) -> League:
    global _current
    _current = LEAGUES[key.lower()]
    return _current


# NBA spread chips use team abbreviations ("LAL -3.5"). The general letter matching in parse.py gets most
# of them, but not when a card shows only a nickname ("Nets"), and a few are close (CHA / CHI). This table
# settles them: abbreviation (including the common alternates) -> nickname.
NBA_ABBR = {
    "ATL": "hawks", "BOS": "celtics", "BKN": "nets", "BRK": "nets", "BK": "nets", "CHA": "hornets", "CHO": "hornets",
    "CHI": "bulls", "CLE": "cavaliers", "DAL": "mavericks", "DEN": "nuggets", "DET": "pistons",
    "GSW": "warriors", "GS": "warriors", "HOU": "rockets", "IND": "pacers", "LAC": "clippers", "LAL": "lakers",
    "MEM": "grizzlies", "MIA": "heat", "MIL": "bucks", "MIN": "timberwolves", "NOP": "pelicans", "NO": "pelicans",
    "NOR": "pelicans", "NYK": "knicks", "NY": "knicks", "OKC": "thunder", "ORL": "magic", "PHI": "76ers",
    "PHX": "suns", "PHO": "suns", "POR": "trail blazers", "SAC": "kings", "SAS": "spurs", "SA": "spurs",
    "TOR": "raptors", "UTA": "jazz", "UTAH": "jazz", "WAS": "wizards", "WSH": "wizards",
}
# City / short names a card might use instead of the nickname.
NBA_ALIASES = {
    "hawks": ("atlanta",), "celtics": ("boston",), "nets": ("brooklyn",), "hornets": ("charlotte",),
    "bulls": ("chicago",), "cavaliers": ("cleveland", "cavs"), "mavericks": ("dallas", "mavs"),
    "nuggets": ("denver",), "pistons": ("detroit",), "warriors": ("golden state",), "rockets": ("houston",),
    "pacers": ("indiana",), "clippers": ("la clippers",), "lakers": ("la lakers",), "grizzlies": ("memphis",),
    "heat": ("miami",), "bucks": ("milwaukee",), "timberwolves": ("minnesota", "wolves"),
    "pelicans": ("new orleans",), "knicks": ("new york",), "thunder": ("oklahoma city", "okc"),
    "magic": ("orlando",), "76ers": ("philadelphia", "sixers"), "suns": ("phoenix",),
    "trail blazers": ("portland", "blazers"), "kings": ("sacramento",), "spurs": ("san antonio",),
    "raptors": ("toronto",), "jazz": ("utah",), "wizards": ("washington",),
}


def nba_team_match(abbr: str, team: str) -> bool:
    """True when ``abbr`` is a known NBA abbreviation for ``team`` (by nickname, city or short name)."""
    nick = NBA_ABBR.get(abbr.upper().replace(".", ""))
    if not nick:
        return False
    t = f" {team.lower()} "
    return any(f" {w} " in t or t.strip() == w for w in (nick, *NBA_ALIASES.get(nick, ())))
