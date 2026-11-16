#!/usr/bin/env python3
"""Mean-reversion (maker entry) sample study on pipeline bars — PRE-REGISTERED.

Question: does the live meanrev_mk logic give a bigger, still-positive
out-of-sample sample on faster bars (1h, 15m) or in a small neighborhood of
the live 4h settings? BTC/USD, ETH/USD, SOL/USD only, tick-built pipeline bars,
fee-aware backtest_core model (maker 25 bps/side for limit entries / TP exits,
taker 40 bps + 5 bps slippage otherwise; stress 80 bps + 10 bps).

Candidates (fixed before the run, nothing else is tried):
  tf       in {240, 60, 15} minutes
  rsi_os   in {33, 38, 43}
  rsi_exit in {50, 55, 60}
  entry=limit, limit_offset=0.001, stop=0.03, tp=0.25 (live values)
Baseline = the live sleeve: tf=240, rsi_os=38, rsi_exit=55.  (26 candidates.)

Out of sample = the same four TIME folds for every timeframe: the 240m series
after its 210-bar warm-up is cut into 5 equal segments; segments 1..4 are the
OOS folds (trades attributed by entry time). The live spec is held fixed.

Promotion (ALL must hold, pooled over the 3 coins, base costs):
  1. >= 30 OOS trades
  2. mean net bps > 0 and median net bps > 0
  3. mean net bps > 0 after dropping the 2 best trades
  4. mean net bps > 0 at stress costs
  5. beats the baseline's per-fold mean in >= 3 of 4 folds (an empty fold = 0 bps, flat)
  6. mean net bps > 0 on >= 2 of the 3 coins
  7. walk-forward selection inside that timeframe's 9-point grid (IS fold k-1 ->
     OOS fold k) has mean OOS net bps > 0
A passing variant may be added as an extra PAPER sleeve; otherwise nothing changes.

    .venv/bin/python scripts/study_meanrev.py --data-dir data
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from dublin_bot.backtest_core import Costs, Spec, add_indicators, simulate, walk_forward  # noqa: E402
from dublin_bot.pipeline import SYMBOLS  # noqa: E402
from dublin_bot.pipeline.history import load_pipeline_bars  # noqa: E402
from study_orderflow import WARM, stats  # noqa: E402

TFS = (240, 60, 15)
RSI_OS = (33.0, 38.0, 43.0)
RSI_EXIT = (50.0, 55.0, 60.0)
BASE_KEY = "240m_os38_ex55"
FOLDS = 4


def spec(os_: float, ex: float) -> Spec:
    return Spec("meanrev", {"rsi_os": os_, "rsi_exit": ex, "stop": 0.03, "tp": 0.25,
                            "entry": "limit", "limit_offset": 0.001})


def key(tf: int, os_: float, ex: float) -> str:
    return f"{tf}m_os{os_:.0f}_ex{ex:.0f}"


def time_edges(d240) -> list[float]:
    n = len(d240)
    usable = n - WARM
    idx = [WARM + (usable * k) // (FOLDS + 1) for k in range(FOLDS + 2)]
    t = d240["time"].to_numpy(float)
    return [float(t[i]) if i < n else float("inf") for i in idx]


def fold_t(ts: float, edges: list[float]) -> int | None:
    for k in range(1, len(edges) - 1):
        if edges[k] <= ts < edges[k + 1]:
            return k
    return None


def run(a) -> dict:
    base = Costs(fee_bps=40.0, slippage_bps=5.0, maker_bps=25.0)
    stress = Costs(fee_bps=80.0, slippage_bps=10.0, maker_bps=25.0)
    grid = list(itertools.product(RSI_OS, RSI_EXIT))
    acc = {key(tf, o, e): {"tf": tf, "rsi_os": o, "rsi_exit": e, "trades": [], "stress": [],
                           "by_coin": {}, "by_fold": {k: [] for k in range(1, FOLDS + 1)}}
           for tf in TFS for o, e in grid}
    wf = {tf: {"trades": [], "by_coin": {}, "chosen": {}} for tf in TFS}
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "data": {}, "oos_window": {}}
    for sym in a.symbols:
        d240_raw, info = load_pipeline_bars(a.data_dir, sym, 240)
        edges = time_edges(d240_raw)
        out["oos_window"][sym] = {"start": datetime.fromtimestamp(edges[1], timezone.utc).isoformat(),
                                  "fold_starts": [datetime.fromtimestamp(x, timezone.utc).isoformat()
                                                  for x in edges[1:-1]]}
        for tf in TFS:
            df, info = (d240_raw, info) if tf == 240 else load_pipeline_bars(a.data_dir, sym, tf)
            out["data"][f"{sym}@{tf}m"] = {k: info.get(k) for k in ("bars", "days", "holes_skipped")}
            d = add_indicators(df)
            for o, e in grid:
                k = key(tf, o, e)
                sp = spec(o, e)
                tr = [(fold_t(t.entry_time, edges), t) for t in simulate(d, sp, base)]
                tr = [(f, t) for f, t in tr if f is not None]
                ts = [t for t in simulate(d, sp, stress) if fold_t(t.entry_time, edges) is not None]
                A = acc[k]
                A["trades"] += [t.net for _, t in tr]
                A["stress"] += [t.net for t in ts]
                A["by_coin"][sym] = {**stats([t.net for _, t in tr]),
                                     "stress_mean": stats([t.net for t in ts])["mean"]}
                for f, t in tr:
                    A["by_fold"][f].append(t.net)
            w = walk_forward(d, [spec(o, e) for o, e in grid], base, folds=FOLDS)
            wf[tf]["trades"] += [t.net for t in w.oos_trades]
            wf[tf]["by_coin"][sym] = stats([t.net for t in w.oos_trades])
            wf[tf]["chosen"][sym] = w.chosen
    base_f = {f: (np.mean(x) * 1e4 if x else 0.0) for f, x in acc[BASE_KEY]["by_fold"].items()}
    res = {}
    for k, A in acc.items():
        s = stats(A["trades"])
        fm = {f: (np.mean(x) * 1e4 if x else 0.0) for f, x in A["by_fold"].items()}
        better = sum(fm[f] > base_f[f] for f in fm)
        coins_pos = sum(1 for c in A["by_coin"].values() if (c["mean"] or -1) > 0)
        sm = stats(A["stress"])["mean"]
        wfm = stats(wf[A["tf"]]["trades"])["mean"]
        checks = {
            "trades>=30": s["n"] >= 30,
            "mean>0_and_median>0": bool(s["n"] and s["mean"] > 0 and s["median"] > 0),
            "ex_best2>0": bool(s["ex_best2"] is not None and s["ex_best2"] > 0),
            "stress_mean>0": bool(sm is not None and sm > 0),
            "beats_baseline_in>=3_of_4_folds": better >= 3,
            "positive_on>=2_coins": coins_pos >= 2,
            "tf_walkforward_selection_mean>0": bool(wfm is not None and wfm > 0),
        }
        res[k] = {"tf": A["tf"], "rsi_os": A["rsi_os"], "rsi_exit": A["rsi_exit"], **s,
                  "stress_mean": sm, "by_coin": A["by_coin"],
                  "fold_mean_bps": {str(f): round(v, 1) for f, v in fm.items()},
                  "fold_n": {str(f): len(x) for f, x in A["by_fold"].items()},
                  "folds_better": better, "coins_positive": coins_pos,
                  "baseline": k == BASE_KEY, "checks": checks,
                  "promote": k != BASE_KEY and all(checks.values())}
    out["variants"] = res
    out["walkforward_selection"] = {str(tf): {**stats(v["trades"]), "by_coin": v["by_coin"],
                                              "chosen": v["chosen"]} for tf, v in wf.items()}
    out["promoted"] = [k for k, v in res.items() if v["promote"]]
    return out


def fmt(x, w=6):
    return ("-" if x is None else f"{x:.0f}" if isinstance(x, float) else str(x)).rjust(w)


def report(out: dict) -> str:
    L = [f"Mean-reversion maker-entry sample study (pre-registered) — {out['generated_at']}",
         "costs: maker 25 bps/side, taker 40+5 bps/side; stress 80+10 bps/side; OOS = 4 common time folds",
         "OOS window start: " + ", ".join(f"{s} {v['start'][:10]}" for s, v in out["oos_window"].items()),
         "data: " + "  ".join(f"{k} bars={v['bars']} days={v['days']}" for k, v in out["data"].items()),
         "", "variant           n  win%   mean median exbst2 stress  folds>base coins+  BTC(n/mean) ETH(n/mean) SOL(n/mean)  verdict"]
    for k, v in sorted(out["variants"].items(), key=lambda kv: (-kv[1]["tf"], kv[0])):
        bc = v["by_coin"]
        coin = "  ".join(f"{bc.get(s, {}).get('n', 0):>3}/{fmt(bc.get(s, {}).get('mean'), 5)}"
                         for s in ("BTC/USD", "ETH/USD", "SOL/USD"))
        fails = [c for c, ok in v["checks"].items() if not ok]
        verdict = "BASELINE" if v["baseline"] else ("PROMOTE" if v["promote"] else "no: " + ",".join(fails))
        win = None if v["win"] is None else v["win"] * 100
        L.append(f"{k:<16}{v['n']:>3} {fmt(win, 5)} {fmt(v['mean'])} {fmt(v['median'])} {fmt(v['ex_best2'])} "
                 f"{fmt(v['stress_mean'])}  {v['folds_better']:>5}/4 {v['coins_positive']:>6}/3  {coin}  {verdict}")
    L.append("")
    for tf, w in out["walkforward_selection"].items():
        L.append(f"walk-forward selection {tf}m: n={w['n']} mean={fmt(w['mean'], 0)} median={fmt(w['median'], 0)}")
    L.append("promoted: " + (", ".join(out["promoted"]) or "none"))
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--symbols", nargs="*", default=list(SYMBOLS))
    ap.add_argument("--out", default=str(ROOT / "data" / "meanrev_study_results.json"))
    ap.add_argument("--report", default=str(ROOT / "data" / "meanrev_study_report.txt"))
    a = ap.parse_args()
    out = run(a)
    Path(a.out).write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    txt = report(out)
    Path(a.report).write_text(txt, encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    main()
