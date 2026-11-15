"""Spot 1d klines 2022-01..2026-10 monthly + Nov 2026 daily files for the 12 coins (200-day EMA / ADX regime labels)."""
import os, urllib.request
from concurrent.futures import ThreadPoolExecutor
OUT = "/workspace/edge2-data/spot1d"; os.makedirs(OUT, exist_ok=True)
COINS = "BTC ETH SOL XRP DOGE ADA AVAX LINK LTC BNB DOT TRX".split(); B = "https://data.binance.vision/data/spot"
jobs = []
for c in COINS:
    s = c + "USDT"
    for y in range(2022, 2027):
        for m in range(1, 13):
            if (y, m) <= (2026, 10): jobs.append((f"{B}/monthly/klines/{s}/1d/{s}-1d-{y}-{m:02d}.zip", f"{OUT}/{s}-1d-{y}-{m:02d}.zip"))
    for d in range(1, 14): jobs.append((f"{B}/daily/klines/{s}/1d/{s}-1d-2026-11-{d:02d}.zip", f"{OUT}/{s}-1d-2026-11-{d:02d}.zip"))
def get(a):
    u, p = a
    if os.path.exists(p): return 0
    try: open(p, "wb").write(urllib.request.urlopen(u, timeout=60).read()); return 0
    except Exception as e: print("ERR", u, e); return 1
with ThreadPoolExecutor(16) as ex: print("errors", sum(ex.map(get, jobs)), "of", len(jobs))
