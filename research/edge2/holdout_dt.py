"""Holdout (2026-10-01..2026-11-13) for Ledger's frozen DualThrust spec (SOL N12 k0.8/0.8, BTC-band, long)."""
import pickle, numpy as np, pandas as pd
from common import *
reg = pickle.load(open("/workspace/edge2-data/reg_idea5.pkl", "rb"))
t = reg["F5|SOL|N12|k0.8|0.8"]
for lbl, m in [("dev 2026 Jan-Sep", (t.entry >= "2026-01-01") & (t.entry < DEV_END)), ("HOLDOUT", (t.entry >= DEV_END) & (t.entry < HOLD_END))]:
    s = t[m]; print(lbl, "n", len(s), {cn: (round(stats_of(s.gross - c)["pf"], 2), round(stats_of(s.gross - c)["mean_bps"], 1)) for cn, c in COSTS.items()})
    if lbl == "HOLDOUT": print(s[["entry", "exit", "gross"]].to_string())
