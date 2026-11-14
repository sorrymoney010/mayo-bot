"""Build cached panels from the raw zips: 15m/1h spot OHLCV (+taker-buy volume), funding, premium."""
import os, glob, zipfile, io, json, pickle
import numpy as np, pandas as pd
D = os.environ.get("EDGE2_DATA", "/workspace/edge2-data")
COINS = "BTC ETH SOL XRP DOGE ADA AVAX LINK LTC BNB DOT TRX".split()
def _ts(x):
    x = pd.to_numeric(x, errors="coerce")
    return pd.to_datetime(np.where(x > 1e14, x // 1000, x), unit="ms", utc=True)
def read_zip(p, header=None):
    with zipfile.ZipFile(p) as z:
        n = z.namelist()[0]; raw = z.read(n)
    df = pd.read_csv(io.BytesIO(raw), header=None)
    if not str(df.iloc[0, 0]).replace('.', '').isdigit(): df = df.iloc[1:]  # header row
    return df
def kl(files):
    df = pd.concat([read_zip(f) for f in sorted(files)])
    df = df.iloc[:, :11]; df.columns = "t o h l c v ct qv n tbv tbq".split()
    df["t"] = _ts(df["t"]); df = df.set_index("t").drop(columns=["ct"]).astype(float)
    return df[~df.index.duplicated()].sort_index()
def build():
    out = {"k15": {}, "k1h": {}, "fund": {}, "prem": {}}
    for c in COINS:
        s = c + "USDT"
        k = kl(glob.glob(f"{D}/spot15/{s}-15m-*.zip")); out["k15"][c] = k
        out["k1h"][c] = k.resample("1h", label="left", closed="left").agg(
            dict(o="first", h="max", l="min", c="last", v="sum", qv="sum", n="sum", tbv="sum", tbq="sum")).dropna()
        fr = pd.concat([read_zip(f) for f in sorted(glob.glob(f"{D}/funding/{s}-fundingRate-*.zip"))])
        fr = pd.Series(fr.iloc[:, 2].astype(float).values, index=_ts(fr.iloc[:, 0]).floor("h"))
        ok = json.load(open(f"{D}/okx_funding_{c}.json"))
        okx = pd.Series([float(r["realizedRate"] or r["fundingRate"]) for r in ok],
                        index=pd.to_datetime([int(r["fundingTime"]) for r in ok], unit="ms", utc=True).floor("h"))
        okx = okx[okx.index > fr.index.max()]
        out["fund"][c] = pd.concat([fr, okx]).sort_index()
        pm = kl(glob.glob(f"{D}/premium/{s}-1h-*.zip")); out["prem"][c] = pm["c"]
        print(c, len(k), k.index.min(), k.index.max(), "fund", len(out["fund"][c]), out["fund"][c].index.max(), "prem", len(pm))
    pickle.dump(out, open(f"{D}/panel.pkl", "wb"))
def panel():
    return pickle.load(open(f"{D}/panel.pkl", "rb"))
if __name__ == "__main__": build()
