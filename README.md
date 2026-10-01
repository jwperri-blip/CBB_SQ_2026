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

### Setting up on another Mac (a collaborator)

Everyone runs their own copy with their own ShotQuality account; nothing is shared through GitHub except the code (`.env`, `.auth/`, `data/` and `reports/` are never uploaded).

```bash
git clone https://github.com/jwperri-blip/CBB_SQ_2026.git
cd CBB_SQ_2026
bash scripts/setup-mac.sh          # Python environment, the cbbsq command, its browser, and .env
source .venv/bin/activate
open -e .env                       # your ScoreCenter URL (and login for unattended runs)
cbbsq login --headed               # log in once
```

The setup script picks a Python 3.9+ that isn't Anaconda's base environment, so a broken Anaconda `pip` doesn't get in the way.

**History.** The thresholds need past games. Either collect last season yourself (it takes a while; `--skip-done` lets you stop and resume):

```bash
cbbsq collect --start 2025-11-03 --end 2026-04-06 --skip-done
```

or copy `data/cbbsq.sqlite` from someone who already has it into your own `data/` folder (picks and grades are rebuilt from it on the next `cbbsq update`).

**Daily use** is then the same as anywhere: `cbbsq schedule` (8:00 and 17:00), `cbbsq update` to run it now, and `git pull && pip install -e .` to get the latest code.

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

### Run it automatically every day

`cbbsq daily` (or `cbbsq update`, the same command) is the whole routine, and you can run it yourself any time to update right away: yesterday's final scores (which grades yesterday's picks), today's games and lines (which saves today's picks), then both pages. It does nothing from May to October, so it can be set up ahead of the season and simply starts working in November. If collecting fails (for example the ShotQuality login expired), it still rebuilds the pages from what is already stored and, on a Mac, shows a notification.

**macOS (recommended):**

```bash
cbbsq schedule                      # runs `cbbsq daily` at 8:00 and 17:00 every day
cbbsq schedule --at 9:30 --at 18:00 # other times
cbbsq schedule --serve              # ...and keep the web server running too (see below)
cbbsq schedule --remove             # undo
```

This installs a LaunchAgent in `~/Library/LaunchAgents`. Unlike cron, launchd runs a job the Mac slept through as soon as it wakes. The 8:00 run picks up last night's results and the first lines; the 17:00 run refreshes lines (some aren't posted by 8) before the evening games. The output goes to `data/daily.log`.

Unattended runs need the saved login to stay valid. Put `SQ_EMAIL` / `SQ_PASSWORD` in `.env` so the scraper can log in again by itself; if your account uses Google sign-in, run `cbbsq login --headed` again whenever a notification says collecting failed.

**Linux (cron):** `0 8,17 * * * cd /path/to/CBB_SQ_2026 && .venv/bin/cbbsq daily >> data/daily.log 2>&1`

**Windows:** a Task Scheduler task that runs `C:\path\to\CBB_SQ_2026\.venv\Scripts\cbbsq.exe daily` with "Start in" set to the project folder.

### Open it on other devices

`cbbsq serve` serves the two pages from your computer to your other devices, behind a password (any username). The password is created on first use and stored in `.env` as `CBBSQ_SERVE_PASSWORD`; `cbbsq serve --info` prints it with the addresses.

* **Same Wi-Fi:** open `http://<your computer's address>:8765/` on the phone or tablet.
* **Anywhere, including a friend's device:** install [Tailscale](https://tailscale.com) (free) on the computer and on each device, and use the computer's Tailscale address (`100.x.y.z`). For someone with their own Tailscale account, use "Share..." on your computer in the Tailscale admin console: they can then reach only that one machine.
* The computer has to be on (and awake) for others to load the page. `cbbsq schedule --serve` keeps the server running in the background.

Don't forward the port on your router or put the pages on a public website: the server is plain HTTP, and the pages contain your ShotQuality data, which their terms don't allow you to redistribute.

## 4. Analyze

```bash
cbbsq leaders                         # sorted by SQ net (offense SQ PPP minus SQ PPP allowed)
cbbsq leaders --last 5 --sort off_sq_ppp
cbbsq leaders --sort def_sq_ppp --asc --wide
cbbsq ratings                         # opponent-adjusted SQ ratings (and --metric ppp for actual)
cbbsq team "Penn"                     # game log plus 5-game rolling SQ trends (partial names work)
cbbsq trending                        # heating up: last 5 vs season   (--cooling for drops)
cbbsq luck                            # results beating their shot quality (--unlucky for the reverse)
cbbsq spots                           # today's games: which side luck says to bet (--date, --details)
cbbsq grade                           # which luck edge / last 5 / SQ edge thresholds have won (--target 55)
cbbsq spots --totals                  # the same for over / under: which side of the total to bet
cbbsq audit                           # data checks: misread cards, spreads on the wrong team, name variants...
cbbsq grade --totals                  # thresholds for the over / under picks
cbbsq report                          # reports/dashboard.html, an interactive dashboard
cbbsq export                          # CSVs for Excel / Google Sheets in data/exports/
```

Most commands accept `--season 2026` (the 2025-26 season), `--since` / `--until`, `--min-games` and `--top`.

### Regression spots (today's slate)

Teams that have been lucky tend to come back to earth. `cbbsq spots` (and the table at the top of the dashboard) lists each game on a date and names the side to bet: the team that has been **less** lucky. Collect the slate first with `cbbsq collect --date today`; `--date` (or `cbbsq report --spots-date`) picks another day.

| Column | Meaning |
|---|---|
| Bet / Against | The less lucky team (with its pre-game line) and the luckier team |
| Luck edge | How many more points per 100 possessions the team you bet against has gained from luck this season. Bigger = stronger spot |
| From opp. misses | How much of that edge comes from opponents missing good shots, which is mostly chance. Positive is better |
| Last 5 | The same edge over the last 5 games. Positive means recent games agree |
| SQ edge | How many points ShotQuality's pregame projection likes the same bet by against the line |

Click a game on the dashboard (or add `--details` in the terminal) for each team's numbers. Only games before the date are used, so a past date shows what you would have seen that morning. Teams with fewer than 5 earlier games get no pick. See the next section for how each number has done.

### Grading the picks and finding thresholds

Every time you run `cbbsq spots` or `cbbsq report`, each game's Bet from the regression spots table is saved (the `spot_picks` table) with its line and four numbers: **Luck edge**, **From opp. misses**, **Last 5** and **SQ edge** (how many points ShotQuality's pregame projection likes the same bet by against the line; negative means it prefers the other side). Once the game is final the pick is graded against that line at −110 (win +0.91 units, loss −1, push 0; postponed games are voided). The first run also fills in every past date already in the database, using only the games before each date, so last season is graded straight away. Picks saved on game day are marked `saved`, filled-in ones `backfill` (`--saved-only` scores only the first kind).

`cbbsq grade` answers "how big does each number need to be to win X%?":

```bash
cbbsq grade                               # lowest threshold per number that wins 55% over 50+ bets, plus a win % ladder
cbbsq grade --target 57 --min-bets 100    # a different target
cbbsq grade --min-luck 4 --min-last5 0    # combine: only picks with luck edge >= 4 and last 5 >= 0
cbbsq grade --metric sq_edge --season 2026
```

**Filled-in history vs game-day picks.** ShotQuality's "Pregame SQ" can be filled into past games with each team's *current* rating when you collect after the fact, which already reflects later results; `cbbsq audit` checks for this. Numbers built on it (SQ edge especially) then look better in filled-in history than they can in real time. Picks saved on game day don't have that problem: use `cbbsq grade --saved-only`, or set **History: Saved on game day** on the dashboard's Thresholds tab, to judge a threshold on those alone.

The dashboard's **Find your threshold** card does the same interactively: set a target win %, click a number's row to add it to the filter, stack filters, and the spots table ticks today's games that pass. The ± next to each win % is its 95% range. A threshold found by looking back at results flatters itself, so trust it once the low end of the range clears 52.4%, and check that it holds on new games or the next season.

### Totals (over / under)

The same system runs separately for totals. Each team's games are compared with what their shots were worth at both ends (its own shot-making plus its opponents'); when the two teams' games have been scoring more than their shots deserved, the total may be inflated, so the pick is the **Under**, and the **Over** when less. The four numbers are measured in the pick's direction (positive agrees with the bet): **Luck edge** (how far the games have run above or below their shots, per 100 possessions), **From opp. shooting**, **Last 5**, and **SQ edge** (ShotQuality's pregame projection turned into a total, minus the line, on the pick's side). Scoring luck is measured against the league: the season-to-date gap between real and SQ points across all teams is taken out first, so a constant difference (for example if SQ points don't fully count free throws) doesn't push every pick to the Under or the Over. Picks are saved to their own `total_picks` table, graded on total points against the pre-game total, and `cbbsq grade --totals` / `cbbsq spots --totals` work exactly like the spreads versions.

`cbbsq report` writes two pages side by side: `reports/dashboard.html` (spreads) and `reports/dashboard-totals.html` (totals), with a Spreads / Totals switch in the header. Each page keeps its own threshold filter.

Daily routine: `cbbsq collect` (yesterday's final scores), `cbbsq collect --date today` (today's games and lines), then `cbbsq report`, which grades yesterday's picks and saves today's.

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
