"""Independent data loaders for the TREND-6 verification (research/edge6).
Primary: Coinbase Exchange public daily candles (BTC-USD, ETH-USD), fetched by fetch_data.py.
Cross-checks: Binance Vision spot 1d klines BTCUSDT/ETHUSDT (the original study's source),
Binance.US BTCUSD/ETHUSD 1d (gap Jul-2023..Feb-2025 because USD pairs were suspended)."""
import glob, io, os, zipfile
import pandas as pd

DATA = os.environ.get("EDGE6_DATA", "/workspace/t6data")
VISION = "/workspace/edge2-data/xs1d"


def _csv(path):
    d = pd.read_csv(path)
    d.index = pd.to_datetime(d.ts, unit="s").dt.normalize()
    d = d[["open", "high", "low", "close", "volume"]].astype(float)
    d = d[~d.index.duplicated()].sort_index()
    return d.reindex(pd.date_range(d.index[0], d.index[-1], freq="D")).ffill()


def _vision(sym):
    parts = []
    for z in sorted(glob.glob(f"{VISION}/{sym}-1d-*.zip")):
        with zipfile.ZipFile(z) as zf:
            raw = zf.read(zf.namelist()[0]).decode()
        df = pd.read_csv(io.StringIO(raw), header=None)
        if not str(df.iloc[0, 0]).isdigit():
            df = df.iloc[1:]
        parts.append(df.iloc[:, :6].astype(float))
    d = pd.concat(parts)
    t = d[0].astype("int64")
    t = t.where(t < 10**14, t // 1000)  # 2025+ files use microseconds
    d.index = pd.to_datetime(t, unit="ms").dt.normalize()
    d = d.iloc[:, 1:6]
    d.columns = ["open", "high", "low", "close", "volume"]
    d = d[~d.index.duplicated()].sort_index()
    return d.reindex(pd.date_range(d.index[0], d.index[-1], freq="D")).ffill()


def load(source):
    if source == "coinbase":
        return {"BTC": _csv(f"{DATA}/coinbase_BTC-USD.csv"), "ETH": _csv(f"{DATA}/coinbase_ETH-USD.csv")}
    if source == "binanceus":
        return {"BTC": _csv(f"{DATA}/binance_BTCUSD.csv"), "ETH": _csv(f"{DATA}/binance_ETHUSD.csv")}
    if source == "vision":
        return {"BTC": _vision("BTCUSDT"), "ETH": _vision("ETHUSDT")}
    raise ValueError(source)
