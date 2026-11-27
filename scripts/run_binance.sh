#!/usr/bin/env bash
# Run a dublin_bot command for the Binance.us setup with the ledger-owner guard set.
#   bash scripts/run_binance.sh golive-check | health | doctor | run-once | dashboard ...
# Live/paper flags come from .env (this script never changes them).
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export MAYO_LEDGER_OWNER=1
# shellcheck disable=SC1091
source .venv/bin/activate
exec python -m dublin_bot.cli "${1:-golive-check}"
