"""Public-data fetcher for edge2 research (no keys). data.binance.vision spot 15m klines,
USDT-M funding history, 1h premium-index klines; OKX funding for the last weeks."""
import os, sys, io, zipfile, urllib.request, json, datetime as dt
from concurrent.futures import ThreadPoolExecutor
OUT = os.environ.get("EDGE2_DATA", "/workspace/edge2-data")
COINS = "BTC ETH SOL XRP DOGE ADA AVAX LINK LTC BNB DOT TRX".split()
MONTHS = [f"{y}-{m:02d}" for y in range(2023, 2027) for m in range(1, 13) if (y, m) <= (2026, 10)]
DAYS = [f"2026-11-{d:02d}" for d in range(1, 14)]
B = "https://data.binance.vision/data"
def get(url, dest):
    if os.path.exists(dest): return "cached"
    try:
        with urllib.request.urlopen(url, timeout=60) as r: b = r.read()
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f: f.write(b)
        return "ok"
    except Exception as e: return f"ERR {e}"
jobs = []
for c in COINS:
    s = c + "USDT"
    for m in MONTHS:
        jobs.append((f"{B}/spot/monthly/klines/{s}/15m/{s}-15m-{m}.zip", f"{OUT}/spot15/{s}-15m-{m}.zip"))
        jobs.append((f"{B}/futures/um/monthly/fundingRate/{s}/{s}-fundingRate-{m}.zip", f"{OUT}/funding/{s}-fundingRate-{m}.zip"))
        jobs.append((f"{B}/futures/um/monthly/premiumIndexKlines/{s}/1h/{s}-1h-{m}.zip", f"{OUT}/premium/{s}-1h-{m}.zip"))
    for d in DAYS:
        jobs.append((f"{B}/spot/daily/klines/{s}/15m/{s}-15m-{d}.zip", f"{OUT}/spot15/{s}-15m-{d}.zip"))
        jobs.append((f"{B}/futures/um/daily/premiumIndexKlines/{s}/1h/{s}-1h-{d}.zip", f"{OUT}/premium/{s}-1h-{d}.zip"))
with ThreadPoolExecutor(16) as ex:
    res = list(ex.map(lambda j: (j[0], get(*j)), jobs))
errs = [r for r in res if r[1].startswith("ERR")]
print(len(res), "jobs,", len(errs), "errors")
for u, e in errs[:40]: print(e, u)
# OKX funding (recent, public) for Nov holdout days missing from monthly archive
for c in COINS:
    rows, after = [], ""
    for _ in range(10):
        u = f"https://www.okx.com/api/v5/public/funding-rate-history?instId={c}-USDT-SWAP&limit=100" + (f"&after={after}" if after else "")
        try: d = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=30))["data"]
        except Exception as e: print("okx err", c, e); break
        if not d: break
        rows += d; after = d[-1]["fundingTime"]
    json.dump(rows, open(f"{OUT}/okx_funding_{c}.json", "w"))
    print("okx", c, len(rows))
