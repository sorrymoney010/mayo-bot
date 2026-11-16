#!/usr/bin/env bash
# Read-only collection; no broker keys, order endpoints or live trading.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then PYTHON=python3; fi
"$PYTHON" scripts/fetch_kraken_history.py ohlc --symbols BTC/USD ETH/USD SOL/USD --tfs 60 240 1440
if [[ "${1:-}" == "--with-trades" ]]; then
  "$PYTHON" scripts/fetch_kraken_history.py trades --symbols BTC/USD ETH/USD SOL/USD --days 90
elif [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--with-trades]" >&2; exit 2
fi
"$PYTHON" - <<'PY'
import csv, json
from datetime import datetime, timezone
from pathlib import Path
root = Path("data")
manifest = {
    "strategy": "regime_trend_v1",
    "collected_at_utc": datetime.now(timezone.utc).isoformat(),
    "source": "Kraken public REST OHLC and optional trades",
    "symbols": ["BTC/USD", "ETH/USD", "SOL/USD"],
    "timeframes_minutes": [60, 240, 1440],
    "caveat": "OHLC REST returns about 720 recent bars per timeframe; check gaps/coverage.",
    "files": [],
}
for sym in ("BTC", "ETH", "SOL"):
    for tf in (60, 240, 1440):
        p = root / f"kraken_{sym}USD_{tf}m.csv"
        if not p.is_file():
            raise SystemExit(f"Missing required collection file: {p}")
        with p.open(newline="") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            raise SystemExit(f"No bars collected for {p}")
        manifest["files"].append({
            "path": str(p), "rows": len(rows),
            "first_utc": rows[0].get("time"),
            "last_utc": rows[-1].get("time"),
        })
out = root / "regime_trend_v1_collection.json"
out.write_text(json.dumps(manifest, indent=2) + "\n")
print(f"Regime data collection manifest: {out}")
PY
