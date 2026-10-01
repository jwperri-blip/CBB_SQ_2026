#!/usr/bin/env bash
# One-time setup on a Mac: a private Python environment in .venv, the cbbsq command, its browser, and .env.
# Run from anywhere:  bash scripts/setup-mac.sh        (add --skip-browser to skip the Chromium download)
set -euo pipefail
cd "$(dirname "$0")/.."

# Use a Python 3.9+ that is not Anaconda's base environment (its pip is often broken).
if command -v conda >/dev/null 2>&1 && [ -n "${CONDA_PREFIX:-}" ]; then
  echo "Note: leaving the Anaconda environment for this setup."
fi
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 /usr/bin/python3 python3; do
  path=$(command -v "$cand" 2>/dev/null || true)
  [ -n "$path" ] || continue
  case "$path" in *anaconda*|*miniconda*|*conda/*) continue ;; esac
  if "$path" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then PY="$path"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.9 or newer is needed. Install it from https://www.python.org/downloads/ (or 'xcode-select --install'), then run this again." >&2
  exit 1
fi
echo "Using $PY ($("$PY" -V 2>&1))"

[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e .
if [ "${1:-}" != "--skip-browser" ]; then
  .venv/bin/python -m playwright install chromium
fi
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env - open it and fill in SQ_SCORECENTER_URL (and SQ_EMAIL / SQ_PASSWORD for unattended runs)."
fi

cat <<'MSG'

Setup done. Next:
  source .venv/bin/activate        # each time you open a new terminal here
  open -e .env                     # fill in your ShotQuality details
  cbbsq login --headed             # log in once in a browser window
  cbbsq status                     # check the database
  cbbsq schedule                   # daily updates at 8:00 and 17:00 (`cbbsq update` runs one now)
To get the latest code later:  git pull && pip install -e .
MSG
