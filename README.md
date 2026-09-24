# CBB_SQ_2026: college basketball Shot Quality tracker

`cbbsq` logs in to your **ShotQuality** account and reads the **CBB ScoreCenter** for each date. It stores every game in a local SQLite database and turns the history into team trends, leaderboards, opponent-adjusted ratings, "luck" (regression) tables and an HTML dashboard.

All data comes from the ScoreCenter game cards, the same numbers you see when logged in:

| Card field | Stored as (away / home) |
|---|---|
| Score | `away_score`, `home_score` |
| SQ Score | `away_sq_score`, `home_sq_score` |
| SQ Percentile | `away_sq_pct`, `home_sq_pct` |
| PTS / Possession: Live | `away_ppp`, `home_ppp` |
| PTS / Possession: Live SQ | `away_sq_ppp`, `home_sq_ppp` |
| PTS / Possession: Pregame SQ | `away_pregame_sq_ppp`, `home_pregame_sq_ppp` |
| Game Line: Pre-Game / Current | `spread_pre_team`, `spread_pre`, `spread_cur_team`, `spread_cur`, plus `home_spread_pre` / `home_spread_cur` from the home team's side |
| Over / Under: Pre-Game / Current | `total_pre`, `total_cur` |
| Status (Live / Final / ...), clock | `status`, `status_detail` |

The left team on a card is stored as **away** and the right team as **home**, which is the usual US scoreboard layout.

> **Terms of use:** this tool automates access to data your subscription already shows you, for your own analysis. Check ShotQuality's Terms of Service before using it, keep the default delay between requests, and don't publish or redistribute the data you collect.

---

## 1. Setup (one time, on your own computer)

You need Python 3.9 or newer.

```bash
git clone https://github.com/jwperri-blip/CBB_SQ_2026.git
cd CBB_SQ_2026
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
playwright install chromium        # the browser the scraper drives
cp .env.example .env               # then edit .env
```

In `.env`:

1. **`SQ_SCORECENTER_URL` (required).** Open the CBB ScoreCenter in your normal browser and copy the address bar.
2. **`SQ_EMAIL` / `SQ_PASSWORD`.** These are only needed for unattended runs. You can skip them and log in by hand instead (next step).

`.env` and the saved browser session (`.auth/`) are git-ignored. Never commit them.

## 2. Log in and calibrate

```bash
cbbsq login --headed      # opens a browser window; log in normally (Google/SSO/CAPTCHA are all fine)
cbbsq probe --date 2026-01-10
```

The login is saved in `.auth/profile`, so later runs can go headless. If it expires, the scraper logs in again using `SQ_EMAIL` / `SQ_PASSWORD`, or you can re-run `cbbsq login --headed`.

`probe` switches the ScoreCenter to that date and writes a diagnostic folder under `data/probe/`: a screenshot, the HTML, the cards it read, the parsed games and the site's own JSON API calls. It also prints:

* how many games the page says it shows compared with how many were read;
* the first parsed game, which you can check against the screen;
* whether the page URL carries the date. If it does, it prints a ready-made `SQ_SCORECENTER_URL=...{date}...` line. Paste it into `.env` and backfills jump straight to each date instead of clicking through the date picker.

## 3. Collect

```bash
cbbsq collect                                   # yesterday's slate (the default)
cbbsq collect --date today                      # today's slate, live games included
cbbsq collect --start 2025-11-03 --end 2026-04-06 --skip-done   # backfill a season
cbbsq watch --every 10                          # re-scrape today every 10 min until all final
cbbsq status                                    # coverage and recent runs
```

* Each run also saves the full raw capture to `data/raw/<date>/<timestamp>.json.gz`: the cards as read plus the site's JSON API responses, with login traffic excluded. If parsing improves later, `cbbsq reparse` rebuilds the database from these files.
* `--skip-done` skips dates whose games are already all Final, so an interrupted backfill can simply be run again.
* Ranges skip May through October unless you pass `--include-offseason`.
* A game scraped while live is updated when it is scraped again after it finishes. The `snapshots` table keeps every scrape, so live SQ swings are logged too.

### Run it automatically every morning

macOS / Linux (`crontab -e`), 9:00 every day:

```cron
0 9 * * * cd /path/to/CBB_SQ_2026 && .venv/bin/cbbsq collect --skip-done >> data/collect.log 2>&1
```

Windows: create a Task Scheduler task that runs `C:\path\to\CBB_SQ_2026\.venv\Scripts\cbbsq.exe collect --skip-done` with "Start in" set to the project folder.

## 4. Analyze

```bash
cbbsq leaders                         # sorted by SQ net (offense SQ PPP minus SQ PPP allowed)
cbbsq leaders --last 5 --sort off_sq_ppp
cbbsq leaders --sort def_sq_ppp --asc --wide
cbbsq ratings                         # opponent-adjusted SQ ratings (and --metric ppp for actual)
cbbsq team "Penn"                     # game log plus 5-game rolling SQ trends (partial names work)
cbbsq trending                        # heating up: last 5 vs season   (--cooling for drops)
cbbsq luck                            # results beating their shot quality (--unlucky for the reverse)
cbbsq report                          # reports/dashboard.html, an interactive dashboard
cbbsq export                          # CSVs for Excel / Google Sheets in data/exports/
```

Most commands accept `--season 2026` (the 2025-26 season), `--since` / `--until`, `--min-games` and `--top`.

### What the metrics mean

| Metric | Meaning |
|---|---|
| `off_sq_ppp` / `def_sq_ppp` | Possession-weighted SQ points per possession generated / allowed |
| `sq_net` | `off_sq_ppp - def_sq_ppp`: shot-quality dominance, less noisy than results |
| `SQ W-L` | Record if every game went to the team with the higher SQ Score |
| `shot_making` | Points minus SQ points per game: shooting above or below its shots |
| `shot_defense` | Opponent SQ points minus opponent points: opponents missing more than their shots suggest |
| `luck` | `shot_making + shot_defense` = actual margin minus SQ margin. Large positive means likely to regress |
| `sq_vs_pregame` | Game SQ PPP minus ShotQuality's pregame projection: beating expectations |
| `adj_off` / `adj_def` / `adj_net` | Ratings adjusted for opponent strength and home court (possession-weighted ridge regression) |
| `ATS` / `SQ ATS` | Record against the pregame spread, using actual scores and then SQ scores |
| `delta_net`, `slope_per_game` | Last-N SQ net minus season SQ net, and the per-game trend of SQ net |

The database (`data/cbbsq.sqlite`) also works with any SQLite tool, pandas or Excel. The `team_games` view has one row per team per game from that team's point of view.

```python
import sqlite3, pandas as pd
from cbbsq.analysis import load_team_games
tg = load_team_games(sqlite3.connect("data/cbbsq.sqlite"), season=2026)
```

## How the scraper works (and why it should survive site updates)

* **Browser automation with Playwright.** The ScoreCenter only renders after login, so the scraper drives a real Chromium with a persistent profile. Cookies, localStorage and IndexedDB are all kept, like your everyday browser. Set `CBBSQ_BROWSER_CHANNEL=chrome` to use your installed Chrome.
* **Layout-based extraction** (`src/cbbsq/extract_cards.js`). It does not rely on CSS class names, which change with every site deploy. It finds each card by its "SQ Score" label and reads the numbers to the left (away) and right (home) of each row label: Score, SQ Score, SQ Percentile, Live, Live SQ, Pregame SQ. The tests run it against two different markups of the same visual card.
* **Date switching.** It uses the URL (a `{date}` placeholder) when possible, otherwise the page's date picker (typed input or popup calendar). After switching, it checks that the page really shows the requested date.
* **Completeness checks.** Every run compares the cards it read with the page's "Showing N of M games" and logs any mismatch in `scrape_runs`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Login did not reach the ScoreCenter` | Run `cbbsq login --headed` and log in by hand, or set `SQ_LOGIN_URL` |
| `Could not find the ScoreCenter date control` / wrong date | Run `cbbsq probe`; if the URL carries the date, use the suggested `{date}` URL |
| `page says N games but M cards were read` | Run `cbbsq probe` and look at `page.png` and `cards.json` |
| Numbers look shifted or missing | `cbbsq probe` and compare `parsed.json` with the screenshot; after a fix, run `cbbsq reparse` |

The files in `data/probe/` may contain account details, so review them before sharing.

## Development

```bash
pip install -e ".[dev]"
pytest            # unit tests, plus end-to-end tests against a local mock ScoreCenter
python tests/mock_server.py   # run the mock site yourself (login fan@example.com / hunter2)
```

The mock site (`tests/mock_site/`) copies the real page's layout: login gate, date picker, and live, final and scheduled cards. The end-to-end tests log in, switch dates by URL, typed input and popup calendar, read a 150-game slate, and check every stored value.
