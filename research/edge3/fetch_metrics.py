"""Public Binance USDT-M futures 'metrics' (OI, long/short ratios, taker L/S vol ratio), daily files from
data.binance.vision, plus BTC 1d spot klines 2022 for 200-day warm-up. No keys."""
import os, urllib.request, datetime as dt
from concurrent.futures import ThreadPoolExecutor
OUT = "/workspace/edge2-data"
COINS = "BTC ETH SOL XRP DOGE ADA AVAX LINK LTC BNB DOT TRX".split()
B = "https://data.binance.vision/data"
def get(a):
    url, dest = a
    if os.path.exists(dest) or os.path.exists(dest + ".404"): return "cached"
    try:
        b = urllib.request.urlopen(url, timeout=60).read()
        open(dest, "wb").write(b); return "ok"
    except urllib.error.HTTPError as e:
        if e.code == 404: open(dest + ".404", "w").close()
        return f"ERR {e.code}"
    except Exception as e: return f"ERR {e}"
os.makedirs(f"{OUT}/metrics", exist_ok=True); os.makedirs(f"{OUT}/spot1d", exist_ok=True)
jobs = []
d = dt.date(2023, 1, 1)
while d <= dt.date(2026, 11, 13):
    for c in COINS:
        s = c + "USDT"; jobs.append((f"{B}/futures/um/daily/metrics/{s}/{s}-metrics-{d}.zip", f"{OUT}/metrics/{s}-metrics-{d}.zip"))
    d += dt.timedelta(days=1)
for y in [2022]:
    for m in range(1, 13):
        jobs.append((f"{B}/spot/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-{y}-{m:02d}.zip", f"{OUT}/spot1d/BTCUSDT-1d-{y}-{m:02d}.zip"))
with ThreadPoolExecutor(48) as ex: res = list(ex.map(get, jobs))
from collections import Counter; print(Counter(r if not r.startswith("ERR") else r[:12] for r in res))
