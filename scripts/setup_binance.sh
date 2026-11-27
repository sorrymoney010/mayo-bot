#!/usr/bin/env bash
# One-shot Binance.us setup. Run from the repo root:  bash scripts/setup_binance.sh
# Prompts for the key/secret with hidden input, writes .env (chmod 600), installs, runs the preflight.
# Leaves every live lock ON. It never places an order and never flips the live flags.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[ -f .env.binance.example ] || { echo "Run from the mayo-bot repo on branch claude/amazing-franklin-2m57m8"; exit 1; }

if [ -f .env ]; then
  cp .env ".env.backup.$(date +%Y%m%d%H%M%S)"
  echo "Existing .env backed up."
fi

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

[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -e . 
echo
echo "=== golive-check (places no orders) ==="
set +e
python -m dublin_bot.cli golive-check
CODE=$?
echo
echo "Exit code $CODE (0 = no FAIL). Copy everything above back to Claude. Your keys are not shown in it."
exit 0
