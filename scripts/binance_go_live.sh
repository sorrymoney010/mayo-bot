#!/usr/bin/env bash
# Switch the Binance.us bot to LIVE ($25 first run). Run from the repo root:  bash scripts/binance_go_live.sh
# Refuses unless the preflight has no FAIL, asks you to type a confirmation, then flips the four live
# flags in .env and runs ONE cycle. Undo any time with: bash scripts/binance_stop_live.sh
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export MAYO_LEDGER_OWNER=1
[ -f .env ] && grep -q '^BROKER=binance' .env || { echo "No Binance .env here. Run scripts/setup_binance.sh first."; exit 1; }
# shellcheck disable=SC1091
source .venv/bin/activate

echo "=== 1/4 preflight ==="
set +e; python -m dublin_bot.cli golive-check; CODE=$?; set -e
if [ "$CODE" -ne 0 ]; then
  echo; echo "NOT going live: the preflight has FAIL lines above (usually: deposit funds, or a setting)."
  exit 1
fi

echo; echo "=== 2/4 what will happen ==="
python - <<'PY'
from dublin_bot.config import Settings
s = Settings()
print(f"  Budget ........ ${s.strategy_equity_usd:g}  (quote asset {s.binance_quote})")
print(f"  Strategy ...... {s.strategy} on {s.timeframe_minutes}m bars, coins {s.universe_allowlist}")
print(f"  Risk/trade .... {s.risk_per_trade:.0%} of budget; max {s.max_orders_per_day} orders/day; no leverage")
print(f"  Protection .... exchange-side stop {s.stop_loss_pct:.0%} / take-profit {s.take_profit_pct:.0%} after each entry")
print("  REAL MONEY will be used. Expect small, infrequent trades (~35/yr per the research); the")
print("  edge is modest and unproven live. You can lose the $25.")
PY
echo
read -r -p 'Type exactly  GO LIVE $25  to continue (anything else cancels): ' ANSWER
[ "$ANSWER" = 'GO LIVE $25' ] || { echo "Cancelled. Nothing changed."; exit 0; }

echo; echo "=== 3/4 enabling live flags ==="
cp .env ".env.backup.$(date +%Y%m%d%H%M%S)"
python3 - <<'PY'
import re
vals = {"PAPER_TRADING": "false", "DRY_RUN": "false", "ALLOW_LIVE_TRADING": "true",
        "LIVE_RISK_ACKNOWLEDGEMENT": "I_ACCEPT_LIVE_TRADING_RISK"}
out, seen = [], set()
for line in open(".env"):
    m = re.match(r"^([A-Z_]+)=", line)
    if m and m.group(1) in vals:
        line = f"{m.group(1)}={vals[m.group(1)]}\n"; seen.add(m.group(1))
    out.append(line)
for k, v in vals.items():
    if k not in seen:
        out.append(f"{k}={v}\n")
open(".env", "w").writelines(out)
PY
chmod 600 .env
python -m dublin_bot.cli golive-check | grep -E "live flags|FAIL" || true

echo; echo "=== 4/4 running ONE live cycle ==="
python -m dublin_bot.cli run-once
echo
echo "Now open Binance.us and check: a position (if it bought) AND a resting OCO stop/take-profit order."
echo "If it did not buy, that is normal: the strategy only enters on a confirmed trend signal."
echo "To keep it running every 15 min:  bash scripts/run_binance_loop.sh      To stop live:  bash scripts/binance_stop_live.sh"
