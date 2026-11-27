#!/usr/bin/env bash
# KILL SWITCH: re-lock paper/dry-run in .env. Does not sell anything or cancel orders.
# An open position keeps its exchange-side stop; manage/close it on Binance.us if you want it gone.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cp .env ".env.backup.$(date +%Y%m%d%H%M%S)"
python3 - <<'PY'
import re
vals = {"PAPER_TRADING": "true", "DRY_RUN": "true", "ALLOW_LIVE_TRADING": "false", "LIVE_RISK_ACKNOWLEDGEMENT": ""}
out = []
for line in open(".env"):
    m = re.match(r"^([A-Z_]+)=", line)
    out.append(f"{m.group(1)}={vals[m.group(1)]}\n" if m and m.group(1) in vals else line)
open(".env", "w").writelines(out)
PY
echo "Live trading LOCKED again (paper + dry-run). Stop any running loop with Ctrl+C."
echo "Check Binance.us for any open position or resting stop order and handle it there."
