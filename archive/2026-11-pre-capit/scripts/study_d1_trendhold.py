#!/usr/bin/env python3
"""Daily risk-on filter (D1) + trend-hold@240m study — PRE-REGISTERED.

Data: tick-built pipeline bars (BTC/USD, ETH/USD, SOL/USD; 2026-06-04 ->
2026-10-02) + Kraken PUBLIC daily OHLC for the D1 filter (validated: daily
closes equal the tick-built closes, 0.0 bps). Costs: taker 40 bps + 5 bps
slippage per side, maker 25 bps; stress 80 + 10 bps.

D1 = close_d > SMA50_d and SMA50_d > SMA50_d 5 days earlier, closed UTC days
only, known after the day closes; gates ENTRIES only.

Part A — D1 on the two live sleeves (live specs held fixed):
  regime@60m (taker) and meanrev_mk@240m (post-only limit), each with / without D1.
  View A: OOS folds 1-4 (bar-index segments after the 210-bar warm-up, as in
  study_orderflow.py). View B: walk-forward selection over the default grid.
  Also split by the audit's window (IS < 2026-08-03 <= OOS).
  DECISION (per sleeve): D1 ships ON by default unless it CLEARLY HURTS out of
  sample, defined as BOTH (a) view-A OOS sum of net bps with D1 < without, AND
  (b) D1 fold mean < baseline fold mean in >= 3 of 4 folds (empty fold = 0).

Part B — trend-hold@240m (+D1): entry after a 4h close with D1 on, close >
  EMA100, EMA20 > EMA100, at the next open; exit at the first 4h close below
  EMA100 (next open); no stop. Book: $500, 25% of current equity per coin at
  entry, one position per coin, 75% total exposure cap. Window = Grok audit
  OOS 2026-08-03 00:00 UTC -> data end (book starts flat). Reported vs the
  audit claim (+20.3%, maxDD -4.7%, 1/3-per-coin book) and vs equal-weight
  buy-and-hold over the same window (fees in and out). Also without D1, at
  1/3 per coin, at stress costs, and in-sample (2026-07-09 -> 08-03).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from dublin_bot.backtest_core import Costs, Spec, add_indicators, default_grid, simulate, walk_forward  # noqa: E402
from dublin_bot.daily_filter import attach_d1, riskon_table  # noqa: E402
from dublin_bot.pipeline import PAIRS, SYMBOLS  # noqa: E402
from dublin_bot.pipeline.history import load_pipeline_bars  # noqa: E402
from study_orderflow import fold_edges, fold_of, stats  # noqa: E402

OOS_START = int(datetime(2026, 8, 3, tzinfo=timezone.utc).timestamp())
BASE = Costs(fee_bps=40.0, slippage_bps=5.0, maker_bps=25.0)
STRESS = Costs(fee_bps=80.0, slippage_bps=10.0, maker_bps=25.0)
SLEEVES = {"regime@60m": (60, "regime"), "meanrev_mk@240m": (240, "meanrev_mk")}


def daily_table(daily_dir: Path, sym: str, now: float) -> pd.DataFrame:
    pair = PAIRS[sym][0]
    k = pd.read_csv(daily_dir / f"{pair}_1440.csv")
    return riskon_table(k[["time", "close"]], now=now)


def frame(data_dir, daily_dir, sym, tf, now):
    df, info = load_pipeline_bars(data_dir, sym, tf)
    d = attach_d1(add_indicators(df), daily_table(daily_dir, sym, now), tf)
    return d, info


def with_d1(spec: Spec, on: bool) -> Spec:
    return Spec(spec.name, {**spec.params, "d1": True}) if on else spec


# ── Part A ───────────────────────────────────────────────────────
def part_a(a, now) -> dict:
    out = {}
    grid = default_grid()
    for sleeve, (tf, fam) in SLEEVES.items():
        live = grid[fam][0]
        acc = {v: {"A": [], "S": [], "fold": {k: [] for k in range(1, 5)}, "coin": {}, "B": [],
                   "is": [], "oos": []} for v in ("none", "d1")}
        for sym in a.symbols:
            d, _ = frame(a.data_dir, a.daily_dir, sym, tf, now)
            edges = fold_edges(len(d), 4)
            for v in ("none", "d1"):
                sp = with_d1(live, v == "d1")
                tr = simulate(d, sp, BASE)
                ts = simulate(d, sp, STRESS)
                X = acc[v]
                oos = [(fold_of(t.entry_i, edges), t) for t in tr]
                oos = [(k, t) for k, t in oos if k is not None]
                X["A"] += [t.net for _, t in oos]
                X["S"] += [t.net for t in ts if fold_of(t.entry_i, edges) is not None]
                X["coin"][sym] = stats([t.net for _, t in oos])
                for k, t in oos:
                    X["fold"][k].append(t.net)
                X["is"] += [t.net for t in tr if t.entry_time < OOS_START]
                X["oos"] += [t.net for t in tr if t.entry_time >= OOS_START]
                wf = walk_forward(d, [with_d1(s, v == "d1") for s in grid[fam]], BASE, folds=4)
                X["B"] += [t.net for t in wf.oos_trades]
        res = {}
        for v, X in acc.items():
            fm = {k: (float(np.mean(x)) * 1e4 if x else 0.0) for k, x in X["fold"].items()}
            res[v] = {"A": {**stats(X["A"]), "stress_mean": stats(X["S"])["mean"], "by_coin": X["coin"],
                            "fold_mean_bps": {str(k): round(m, 1) for k, m in fm.items()},
                            "fold_n": {str(k): len(x) for k, x in X["fold"].items()}},
                      "B_walkforward": stats(X["B"]),
                      "audit_IS": stats(X["is"]), "audit_OOS": stats(X["oos"])}
        b, f = res["none"], res["d1"]
        worse_folds = sum(float(f["A"]["fold_mean_bps"][k]) < float(b["A"]["fold_mean_bps"][k]) for k in "1234")
        hurts = (f["A"]["sum"] < b["A"]["sum"]) and worse_folds >= 3
        res["decision"] = {"oos_sum_none": b["A"]["sum"], "oos_sum_d1": f["A"]["sum"],
                           "folds_d1_worse": worse_folds, "clearly_hurts": hurts,
                           "default": "off" if hurts else "on"}
        out[sleeve] = res
    return out


# ── Part B ───────────────────────────────────────────────────────
def book_sim(frames: dict, trades: dict, start: int, end: int | None, frac: float,
             costs: Costs, cap: float = 0.75, equity0: float = 500.0) -> dict:
    """Portfolio replay on the common 4h grid. trades: sym -> list[Trade]."""
    times = sorted(set().union(*[set(f["time"].astype(int)) for f in frames.values()]))
    times = [t for t in times if t >= start and (end is None or t < end)]
    px = {s: f.set_index(f["time"].astype(int)) for s, f in frames.items()}
    ev_in = {s: {int(t.entry_time): t for t in tr if start <= t.entry_time and (end is None or t.entry_time < end)}
             for s, tr in trades.items()}
    fee, slip = costs.fee_bps / 1e4, costs.slippage_bps / 1e4
    cash, pos, curve, closed = equity0, {}, [], []
    realized_curve: list[float] = []  # cash + cost basis of open lots (no mark-to-market)

    def equity_at(t, field):
        return cash + sum(q * float(px[s].at[t, field]) for s, (q, _tr, _n) in pos.items() if t in px[s].index)

    for t in times:
        # exits first (fill at this bar's open)
        for s in list(pos):
            q, tr, notional = pos[s]
            if int(tr.exit_time) == t and tr.reason != "eod_mark":
                proceeds = q * float(px[s].at[t, "open"]) * (1 - slip) * (1 - fee)
                cash += proceeds
                closed.append({"symbol": s, "entry_time": int(tr.entry_time), "exit_time": t,
                               "net_bps": round((proceeds / notional - 1) * 1e4, 1)})
                pos.pop(s)
        for s, tr in ev_in.items():
            if t in tr and s not in pos and t in px[s].index:
                eq = equity_at(t, "open")
                expo = sum(q * float(px[x].at[t, "open"]) for x, (q, _a, _b) in pos.items())
                notional = min(frac * eq, max(0.0, cap * eq - expo), cash)
                if notional <= 1.0:
                    continue
                o = float(px[s].at[t, "open"])
                q = notional * (1 - fee) / (o * (1 + slip))
                cash -= notional
                pos[s] = (q, tr[t], notional)
        curve.append((t, equity_at(t, "close")))
        realized_curve.append(cash + sum(n for _q, _tr, n in pos.values()))
    # mark open positions at the last close, net of exit costs
    if curve:
        t_last = curve[-1][0]
        for s, (q, tr, notional) in pos.items():
            val = q * float(px[s].at[t_last, "close"]) * (1 - slip) * (1 - fee)
            closed.append({"symbol": s, "entry_time": int(tr.entry_time), "exit_time": None,
                           "net_bps": round((val / notional - 1) * 1e4, 1), "open_at_end": True})
            cash += val
        curve[-1] = (t_last, cash)
        realized_curve[-1] = cash
    eq = np.array([e for _, e in curve]) if curve else np.array([equity0])
    peak = np.maximum.accumulate(eq)
    rc = np.array([equity0] + realized_curve)
    rpeak = np.maximum.accumulate(rc)
    return {"return_pct": round((eq[-1] / equity0 - 1) * 100, 2),
            "max_dd_pct": round(float(((eq - peak) / peak).min()) * 100, 2),
            # drawdown of closed-trade equity only (what a realized-PnL curve shows);
            # the mark-to-market max_dd_pct above is the honest risk number.
            "realized_only_dd_pct": round(float(((rc - rpeak) / rpeak).min()) * 100, 2),
            "trades": len(closed), "trade_stats": stats([c["net_bps"] / 1e4 for c in closed]),
            "closed": closed, "first": curve[0][0] if curve else None, "last": curve[-1][0] if curve else None}


def buy_hold(frames: dict, start: int, end: int | None, costs: Costs, equity0: float = 500.0) -> dict:
    fee, slip = costs.fee_bps / 1e4, costs.slippage_bps / 1e4
    per = equity0 / len(frames)
    qty, curve_parts = {}, []
    for s, f in frames.items():
        g = f[(f["time"] >= start) & ((f["time"] < end) if end else True)]
        o = float(g["open"].iloc[0])
        qty[s] = per * (1 - fee) / (o * (1 + slip))
        curve_parts.append(g.set_index(g["time"].astype(int))["close"] * qty[s])
    curve = pd.concat(curve_parts, axis=1).ffill().sum(axis=1)
    final = float(curve.iloc[-1]) * (1 - slip) * (1 - fee)
    vals = curve.to_numpy(float)
    vals[-1] = final
    peak = np.maximum.accumulate(vals)
    by_coin = {s: round((float(f[f["time"] < end]["close"].iloc[-1] if end else f["close"].iloc[-1])
                         / float(f[f["time"] >= start]["open"].iloc[0]) - 1) * 100, 2) for s, f in frames.items()}
    return {"return_pct": round((final / equity0 - 1) * 100, 2),
            "max_dd_pct": round(float(((vals - peak) / peak).min()) * 100, 2), "coin_price_change_pct": by_coin}


def part_b(a, now) -> dict:
    frames, d1_trades, raw_trades = {}, {}, {}
    for sym in a.symbols:
        d, _ = frame(a.data_dir, a.daily_dir, sym, 240, now)
        frames[sym] = d
        d1_trades[sym] = simulate(d, Spec("trendhold", {"d1": True, "warm": 100}), BASE)
        raw_trades[sym] = simulate(d, Spec("trendhold", {"d1": False, "warm": 100}), BASE)
    is_start = int(datetime(2026, 7, 9, tzinfo=timezone.utc).timestamp())
    out = {
        "oos_25pct_d1": book_sim(frames, d1_trades, OOS_START, None, 0.25, BASE),
        "oos_33pct_d1": book_sim(frames, d1_trades, OOS_START, None, 1 / 3, BASE, cap=1.0),
        "oos_25pct_d1_stress": book_sim(frames, {s: simulate(frames[s], Spec("trendhold", {"d1": True, "warm": 100}), STRESS)
                                                 for s in frames}, OOS_START, None, 0.25, STRESS),
        "oos_25pct_noD1": book_sim(frames, raw_trades, OOS_START, None, 0.25, BASE),
        "is_25pct_d1": book_sim(frames, d1_trades, is_start, OOS_START, 0.25, BASE),
        "is_25pct_noD1": book_sim(frames, raw_trades, is_start, OOS_START, 0.25, BASE),
        "buy_hold_oos": buy_hold(frames, OOS_START, None, BASE),
        "buy_hold_is": buy_hold(frames, is_start, OOS_START, BASE),
        "audit_claim": {"return_pct": 20.3, "max_dd_pct": -4.7, "book": "1/3 per coin, 1m-bar fills"},
    }
    per_coin = {}
    for s, tr in d1_trades.items():
        o = [t.net for t in tr if t.entry_time >= OOS_START]
        per_coin[s] = stats(o)
    out["oos_per_coin_trade_stats_d1"] = per_coin
    return out


def fmt(x):
    return "-" if x is None else (f"{x:+.0f}" if isinstance(x, float) else str(x))


def report(res: dict) -> str:
    L = [f"D1 filter + trend-hold study (pre-registered) — {res['generated_at']}",
         "costs: taker 40+5 bps/side, maker 25; stress 80+10. D1 from Kraken public daily OHLC (= tick closes)", ""]
    for sleeve, r in res["part_a"].items():
        L.append(f"== {sleeve}   (net bps per trade)")
        L.append("   variant  view                 n  win%   mean median exbst2 stress    sum")
        for v in ("none", "d1"):
            A = r[v]["A"]
            win = None if A["win"] is None else A["win"] * 100
            L.append(f"   {v:<7}  A fixed OOS f1-4   {A['n']:>4} {fmt(win):>5} {fmt(A['mean']):>6} {fmt(A['median']):>6} "
                     f"{fmt(A['ex_best2']):>6} {fmt(A['stress_mean']):>6} {fmt(A['sum']):>6}   folds {A['fold_mean_bps']}")
            for key, lab in (("B_walkforward", "B walk-fwd select"), ("audit_IS", "audit IS <08-03"),
                             ("audit_OOS", "audit OOS >=08-03")):
                X = r[v][key]
                win = None if X["win"] is None else X["win"] * 100
                L.append(f"   {v:<7}  {lab:<17} {X['n']:>4} {fmt(win):>5} {fmt(X['mean']):>6} {fmt(X['median']):>6} "
                         f"{fmt(X['ex_best2']):>6} {'':>6} {fmt(X['sum']):>6}")
        dcs = r["decision"]
        L.append(f"   DECISION: clearly_hurts={dcs['clearly_hurts']} (OOS sum {fmt(dcs['oos_sum_none'])} -> "
                 f"{fmt(dcs['oos_sum_d1'])}, D1 worse in {dcs['folds_d1_worse']}/4 folds) => default {dcs['default'].upper()}")
        L.append("")
    b = res["part_b"]
    L.append("== trend-hold@240m book ($500)            return%  maxDD%  trades  win%  mean  median  realizedDD%")
    for k in ("oos_25pct_d1", "oos_33pct_d1", "oos_25pct_d1_stress", "oos_25pct_noD1", "is_25pct_d1", "is_25pct_noD1"):
        x = b[k]
        ts = x["trade_stats"]
        win = None if ts["win"] is None else ts["win"] * 100
        L.append(f"   {k:<36} {x['return_pct']:>7} {x['max_dd_pct']:>7} {x['trades']:>7} {fmt(win):>5} "
                 f"{fmt(ts['mean']):>5} {fmt(ts['median']):>7} {x['realized_only_dd_pct']:>9}")
    for k in ("buy_hold_oos", "buy_hold_is"):
        x = b[k]
        L.append(f"   {k:<36} {x['return_pct']:>7} {x['max_dd_pct']:>7}   coins {x['coin_price_change_pct']}")
    L.append("   audit claim (OOS, +D1, 1/3 per coin, 1m fills): +20.3% / maxDD -4.7%")
    L.append("   maxDD% = mark-to-market (honest); realizedDD% = closed-trade equity only (the audit's -4.7% basis)")
    L.append("   OOS per coin (+D1): " + "  ".join(f"{s} n={v['n']} mean={fmt(v['mean'])} med={fmt(v['median'])}"
                                                   for s, v in b["oos_per_coin_trade_stats_d1"].items()))
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--daily-dir", required=True, help="dir with <PAIR>_1440.csv (Kraken public OHLC)")
    ap.add_argument("--symbols", nargs="*", default=list(SYMBOLS))
    ap.add_argument("--out", default=str(ROOT / "data" / "d1_trendhold_results.json"))
    ap.add_argument("--report", default=str(ROOT / "data" / "d1_trendhold_report.txt"))
    a = ap.parse_args()
    a.daily_dir = Path(a.daily_dir)
    now = datetime(2026, 10, 3, tzinfo=timezone.utc).timestamp()  # data end (closed days only)
    res = {"generated_at": datetime.now(timezone.utc).isoformat(), "part_a": part_a(a, now), "part_b": part_b(a, now)}
    Path(a.out).write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    txt = report(res)
    Path(a.report).write_text(txt, encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    main()
