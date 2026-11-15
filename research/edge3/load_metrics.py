"""Hourly panel from Binance USDT-M 'metrics' daily files: OI value, crowd L/S account ratio, top-trader L/S
position ratio, taker L/S volume ratio. Value at hour H = last 5m print with create_time < H+1h (known at bar close)."""
import glob, zipfile, io, pickle, numpy as np, pandas as pd
from concurrent.futures import ProcessPoolExecutor
COINS = "BTC ETH SOL XRP DOGE ADA AVAX LINK LTC BNB DOT TRX".split()
def one(c):
    out = []
    for f in sorted(glob.glob(f"/workspace/edge2-data/metrics/{c}USDT-metrics-*.zip")):
        z = zipfile.ZipFile(f); out.append(pd.read_csv(io.BytesIO(z.read(z.namelist()[0]))))
    d = pd.concat(out); d["t"] = pd.to_datetime(d.create_time, utc=True)
    d = d.set_index("t").sort_index()[["sum_open_interest_value", "count_long_short_ratio", "sum_toptrader_long_short_ratio", "sum_taker_long_short_vol_ratio"]].astype(float)
    d.columns = ["oi", "ls_crowd", "ls_top", "taker_ratio"]
    h = d.resample("1h", label="left", closed="left").last()          # last print within [H, H+1h)
    return c, h
if __name__ == "__main__":
    with ProcessPoolExecutor(6) as ex: M = dict(ex.map(one, COINS))
    for c in COINS: print(c, len(M[c]), M[c].index.min(), M[c].index.max(), M[c].oi.isna().mean().round(3))
    pickle.dump(M, open("/workspace/edge2-data/metrics_1h.pkl", "wb"))
