"""Self-contained HTML dashboard (no server, no external scripts)."""

from __future__ import annotations

import json
import math
from datetime import datetime
from importlib import resources
from pathlib import Path

import pandas as pd

from . import analysis

LOG_COLS = ["game_date", "team", "opponent", "side", "pts", "opp_pts", "sq_pts", "opp_sq_pts", "sq_ppp",
            "opp_sq_ppp", "ppp", "opp_ppp", "sq_pct", "pre_sq_ppp", "spread", "margin", "sq_margin", "luck"]


def _clean(value):
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if hasattr(value, "item"):  # numpy scalar
        return _clean(value.item())
    return value


def _records(df: pd.DataFrame, digits: int = 4) -> list[dict]:
    df = df.round(digits)
    return [{k: _clean(v) for k, v in row.items()} for row in df.to_dict(orient="records")]


def build_payload(tg: pd.DataFrame, *, min_games: int = 3, trend_window: int = 5) -> dict:
    summaries = {}
    for key, last_n in (("all", None), ("last10", 10), ("last5", 5)):
        s = analysis.team_summary(tg, last_n=last_n, min_games=1)
        summaries[key] = _records(s.reset_index()) if not s.empty else []
    ratings = analysis.adjusted_ratings(tg, min_games=1)
    trend = analysis.trending(tg, window=trend_window, min_games=1)
    extra = pd.DataFrame(index=pd.Index(sorted(tg["team"].unique()) if len(tg) else [], name="team"))
    if not ratings.empty:
        extra = extra.join(ratings[["adj_off", "adj_def", "adj_net"]])
    if not trend.empty:
        extra = extra.join(trend[["delta_net"]])
    logs = {team: _records(g[LOG_COLS]) for team, g in tg.groupby("team")} if len(tg) else {}
    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "games": int(tg["game_id"].nunique()) if len(tg) else 0,
        "first_date": tg["game_date"].min() if len(tg) else None,
        "last_date": tg["game_date"].max() if len(tg) else None,
        "min_games": min_games,
        "trend_window": trend_window,
        "summaries": summaries,
        "extra": _records(extra.reset_index()) if len(extra) else [],
        "logs": logs,
    }


def write_report(tg: pd.DataFrame, out_path: Path, **kwargs) -> Path:
    payload = build_payload(tg, **kwargs)
    template = resources.files("cbbsq").joinpath("templates/dashboard.html").read_text(encoding="utf-8")
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(template.replace("/*__DATA__*/null", data), encoding="utf-8")
    return out_path
