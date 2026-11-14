import numpy as np, pandas as pd
from load import COINS
H = pd.Timedelta("1h")
def funding_on_bars(fund, idx):
    """Last funding print known at the CLOSE of each 1h bar (print time <= bar open + 1h)."""
    f = fund.sort_index(); f = f[~f.index.duplicated(keep="last")]
    v = f.reindex(f.index.union(idx + H)).ffill().reindex(idx + H)
    v.index = idx; return v
def funding_mean3(fund, idx):
    f = fund.sort_index(); f = f[~f.index.duplicated(keep="last")].rolling(3).mean()
    return funding_on_bars(f, idx)
def rq(x, q, win=2160, minp=720):
    """Trailing quantile of PRIOR bars (shifted) – no lookahead."""
    return x.rolling(win, min_periods=minp).quantile(q).shift(1)
