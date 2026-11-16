"""Historical false-kill check for TREND-6 kill rules, using the ACTUAL paper runner
(src/dublin_bot/trend6.py) replayed on Binance global daily data (data.binance.vision cache).
26 quarterly paper-start dates 2019-01 .. 2025-04, each replayed for up to 2 years (to 2026-10-31).
Old rule (removed): >= 30 closed round trips and PF < 1.10 (checked after every close).
New rules: K-REL (>= 30 trips AND behind 50/50 B&H AND > 10% below peak AND below 30 days ago), K-DD 45%."""
import json, shutil, sys
from pathlib import Path
import pandas as pd
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(Path(__file__).parent)]
from dublin_bot import trend6 as T  # noqa: E402
import data  # noqa: E402

px = data.load("vision")
feed = {}
for c in T.COINS:
    d = px[c].rename(columns={"open": "o", "high": "h", "low": "l", "close": "c"})[["o", "h", "l", "c"]].copy()
    d.index = d.index.tz_localize("UTC")
    d["ct"] = (d.index.astype("int64") // 10**6) + 86_399_999
    feed[c] = d
END = pd.Timestamp("2026-10-31", tz="UTC")
rows = []
for st in pd.date_range("2019-01-01", "2025-04-01", freq="3MS", tz="UTC"):
    out = Path(f"/tmp/t6hist/{st.date()}"); shutil.rmtree(out, ignore_errors=True)
    stop = min(st + pd.Timedelta(days=730), END)
    clock = {"now": st + pd.Timedelta("1h")}
    def fb(c, clock=clock):
        return feed[c][feed[c].index <= clock["now"].normalize()]
    r = T.Trend6Paper(out, now=lambda clock=clock: clock["now"], fetch_bus=fb,
                      fetch_warm=lambda c, d: None, fetch_hour_open=lambda c, d: None)
    r.run_cycle()
    clock["now"] = stop + pd.Timedelta("1h"); s = r.run_cycle()
    fills = [json.loads(x) for x in (out / "fills.jsonl").read_text().splitlines()]
    closed = [f for f in fills if f["book"] == T.PRIMARY and "round_trip_pnl" in f]
    old_at = None; pnl = []
    for f in closed:
        pnl.append(f["round_trip_pnl"]); pf = T.profit_factor(pnl)
        if len(pnl) >= 30 and pf is not None and pf < 1.10:
            old_at = f["exec_at"][:10]; break
    pf30 = T.profit_factor(pnl[:30]) if len(closed) >= 30 else None
    pf30 = T.profit_factor([f["round_trip_pnl"] for f in closed[:30]]) if len(closed) >= 30 else None
    k = s["killed"]
    rows.append(dict(start=str(st.date()), trips=len(closed), pf_first30=None if pf30 is None else round(pf30, 2),
                     old_fires=old_at, new_fires=(k["at"][:10] + " " + k["reasons"][0][:5]) if k else None,
                     eq=s["books"][T.PRIMARY]["equity_mark"], bench=s["bench_5050_hold"]))
    print(rows[-1], flush=True)
n = len(rows)
summary = dict(starts=n, old_fires=sum(r["old_fires"] is not None for r in rows),
               new_fires=sum(r["new_fires"] is not None for r in rows),
               new_kdd=sum(bool(r["new_fires"] and "K-DD" in r["new_fires"]) for r in rows),
               ahead_of_bench_2y=sum(r["eq"] > r["bench"] for r in rows))
print(summary)
json.dump(dict(rows=rows, summary=summary), open(Path(__file__).parent / "kill_rule_history.json", "w"), indent=1)
