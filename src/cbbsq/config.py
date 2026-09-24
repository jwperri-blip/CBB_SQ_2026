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

    @classmethod
    def load(cls, env_file: str | os.PathLike | None = ".env") -> "Settings":
        if env_file:
            load_dotenv(Path(env_file))
        env = os.environ.get
        return cls(
            email=env("SQ_EMAIL") or None,
            password=env("SQ_PASSWORD") or None,
            scorecenter_url=env("SQ_SCORECENTER_URL") or None,
            login_url=env("SQ_LOGIN_URL") or None,
            date_mode=(env("SQ_DATE_MODE") or "auto").lower(),
            db_path=Path(env("CBBSQ_DB") or "data/cbbsq.sqlite"),
            raw_dir=Path(env("CBBSQ_RAW_DIR") or "data/raw"),
            profile_dir=Path(env("CBBSQ_PROFILE_DIR") or ".auth/profile"),
            browser_channel=env("CBBSQ_BROWSER_CHANNEL") or None,
            delay_seconds=float(env("CBBSQ_DELAY_SECONDS") or 4),
        )
