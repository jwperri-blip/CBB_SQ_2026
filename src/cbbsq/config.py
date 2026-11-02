"""Settings, read from environment variables and an optional ``.env`` file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; existing environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass
class Settings:
    email: str | None
    password: str | None
    scorecenter_url: str | None
    login_url: str | None
    date_mode: str
    db_path: Path
    raw_dir: Path
    profile_dir: Path
    browser_channel: str | None
    delay_seconds: float
    serve_password: str | None = None
    serve_port: int = 8765

    @classmethod
    def load(cls, env_file: str | os.PathLike | None = ".env") -> "Settings":
        """Settings for the current league (see league.py). College basketball reads SQ_* and CBBSQ_*;
        the NBA reads NBA_SQ_SCORECENTER_URL and NBASQ_*, and shares the ShotQuality login (SQ_EMAIL /
        SQ_PASSWORD) unless NBA_SQ_EMAIL / NBA_SQ_PASSWORD are set."""
        from .league import current

        if env_file:
            load_dotenv(Path(env_file))
        env = os.environ.get
        lg = current()
        pre = lg.env_prefix
        sq = "SQ_" if lg.key == "cbb" else f"{lg.key.upper()}_SQ_"

        def both(name: str):  # league-specific value, else the shared one
            return env(sq + name) or env("SQ_" + name) or None

        return cls(
            email=both("EMAIL"),
            password=both("PASSWORD"),
            scorecenter_url=env(lg.url_var) or lg.default_url,
            login_url=both("LOGIN_URL"),
            date_mode=(both("DATE_MODE") or "auto").lower(),
            db_path=Path(env(pre + "DB") or lg.db_file),
            raw_dir=Path(env(pre + "RAW_DIR") or lg.raw_dir),
            profile_dir=Path(env(pre + "PROFILE_DIR") or lg.profile_dir),
            browser_channel=env(pre + "BROWSER_CHANNEL") or env("CBBSQ_BROWSER_CHANNEL") or None,
            delay_seconds=float(env(pre + "DELAY_SECONDS") or env("CBBSQ_DELAY_SECONDS") or 4),
            serve_password=env(pre + "SERVE_PASSWORD") or None,
            serve_port=int(env(pre + "SERVE_PORT") or lg.port),
        )
