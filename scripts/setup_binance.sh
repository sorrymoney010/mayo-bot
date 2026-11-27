#!/usr/bin/env bash
# One-shot Binance.us setup. Run from the repo root:  bash scripts/setup_binance.sh
# Prompts for the key/secret with hidden input, writes .env (chmod 600), installs, runs the preflight.
# Leaves every live lock ON. It never places an order and never flips the live flags.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[ -f .env.binance.example ] || { echo "Run from the mayo-bot repo on branch claude/amazing-franklin-2m57m8"; exit 1; }

SKIP_ENV=0
if [ -f .env ] && grep -q '^BROKER=binance' .env && ! grep -q 'PASTE_' .env; then
  read -r -p ".env already has Binance keys. Keep it and just run install + preflight? [Y/n] " A
  case "${A:-Y}" in [Nn]*) ;; *) SKIP_ENV=1 ;; esac
fi
if [ "$SKIP_ENV" = 1 ]; then
  cp .env ".env.backup.$(date +%Y%m%d%H%M%S)"
  python3 - <<'PY'
import re
old = {}
for line in open(".env"):
    m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
    if m:
        old[m.group(1)] = m.group(2)
keep = ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_QUOTE", "PAPER_TRADING", "DRY_RUN",
        "ALLOW_LIVE_TRADING", "LIVE_RISK_ACKNOWLEDGEMENT")
out = []
for line in open(".env.binance.example"):
    m = re.match(r"^([A-Z_]+)=(.*)$", line.rstrip("\n"))
    if m and m.group(1) in keep and m.group(1) in old:
        line = f"{m.group(1)}={old[m.group(1)]}\n"
    out.append(line)
open(".env", "w").writelines(out)
PY
  chmod 600 .env
  echo ".env refreshed from the template (kept: keys, BINANCE_QUOTE, live flags)."
fi
if [ "$SKIP_ENV" = 0 ] && [ -f .env ]; then
  cp .env ".env.backup.$(date +%Y%m%d%H%M%S)"
  echo "Existing .env backed up."
fi

if [ "$SKIP_ENV" = 0 ]; then
read -r -s -p "Binance.us API key (hidden): " KEY; echo
read -r -s -p "Binance.us API secret (hidden): " SECRET; echo
[ -n "$KEY" ] && [ -n "$SECRET" ] || { echo "Key and secret are both required."; exit 1; }

# Write .env from the template, substituting the two lines (values passed via env, not argv).
KEY="$KEY" SECRET="$SECRET" python3 - <<'PY'
import os
out = []
for line in open(".env.binance.example"):
    if line.startswith("BINANCE_API_KEY="):
        line = "BINANCE_API_KEY=" + os.environ["KEY"] + "\n"
    elif line.startswith("BINANCE_API_SECRET="):
        line = "BINANCE_API_SECRET=" + os.environ["SECRET"] + "\n"
    out.append(line)
open(".env", "w").writelines(out)
PY
chmod 600 .env
unset KEY SECRET
echo ".env written (locks ON: PAPER_TRADING=true DRY_RUN=true ALLOW_LIVE_TRADING=false)."
fi

# Need Python >= 3.11 (macOS system python3 is 3.9 and cannot install this project).
PY=""
for c in python3.13 python3.12 python3.11 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
if [ -z "$PY" ]; then
  echo "No Python 3.11+ found. Install one, then re-run this script:  brew install python@3.12"
  echo "(Your .env is already written; nothing else was changed.)"
  exit 1
fi
echo "Using $PY ($("$PY" --version))"
# Recreate the venv if it was built with an old Python.
if [ -d .venv ] && ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  rm -rf .venv
fi
[ -d .venv ] || "$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q --upgrade pip
pip install -q -e .
echo
echo "=== golive-check (places no orders) ==="
set +e
MAYO_LEDGER_OWNER=1 python -m dublin_bot.cli golive-check
CODE=$?
echo
echo "Exit code $CODE (0 = no FAIL). Copy everything above back to Claude. Your keys are not shown in it."
exit 0
