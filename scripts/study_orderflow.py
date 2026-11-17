#!/usr/bin/env python3
"""Do tick order-flow features add OUT-OF-SAMPLE edge to the two paper sleeves?

Runs entirely off the market-data pipeline (tick store -> bars -> technicals),
BTC/USD, ETH/USD, SOL/USD only, with the fee-aware execution model of
``backtest_core`` (taker 40 bps/side + 5 bps slippage = 90 bps round trip;
maker 25 bps/side for the meanrev limit entries / TP exits).

Sleeves (exactly the live paper rules):
  regime@60m       backtest family "regime"     (taker entries)
  meanrev_mk@240m  backtest family "meanrev_mk" (post-only limit entries)

Candidate filters (pre-registered, fixed thresholds, nothing fitted):
  regime:  ofi_pos, ofi_z_pos, flow3_pos
  meanrev: ofi_pos, flow3_pos, ofi_rising

Two views, both out-of-sample:
  A. FIXED live spec (no parameter selection) +/- filter, scored on the four
     walk-forward OOS folds (segments 1..4 after the 210-bar warm-up).
  B. Walk-forward SELECTION (pick the best grid spec on fold k-1, score on k)
     with the filter applied to every grid spec vs without.

Promotion rules (ALL must hold, pooled over the 3 coins, view A, base costs):
  1. >= 30 OOS trades with the filter
  2. mean net bps > 0 AND > the unfiltered mean
  3. filter beats unfiltered mean in >= 3 of 4 folds
  4. mean net bps still > 0 after dropping the 2 best trades
  5. mean net bps > 0 at stress costs (80 bps/side + 10 bps slippage)
  6. mean net bps > 0 on >= 2 of the 3 coins
  7. view B (walk-forward selection with the filter) mean OOS net bps > 0

    .venv/bin/python scripts/study_orderflow.py --data-dir data
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dublin_bot.backtest_core import (  # noqa: E402
    Costs, Spec, add_indicators, default_grid, simulate, walk_forward,
)
from dublin_bot.pipeline import SYMBOLS  # noqa: E402
from dublin_bot.pipeline.history import load_pipeline_bars  # noqa: E402

DATA = ROOT / "data"
WARM = 210
SLEEVES = {
    "regime@60m": {"tf": 60, "family": "regime", "filters": ["ofi_pos", "ofi_z_pos", "flow3_pos"]},
    "meanrev_mk@240m": {"tf": 240, "family": "meanrev_mk",
                        "filters": ["ofi_pos", "flow3_pos", "ofi_rising"]},
}
RULES = {"min_trades": 30, "min_folds_better": 3, "min_coins_positive": 2}


def with_filter(spec: Spec, flt: str | None) -> Spec:
    return spec if not flt else Spec(spec.name, {**spec.params, "flt": flt})


def fold_edges(n: int, folds: int) -> list[int]:
    usable = n - WARM
    return [WARM + (usable * k) // (folds + 1) for k in range(folds + 2)]


def fold_of(entry_i: int, edges: list[int]) -> int | None:
    for k in range(1, len(edges) - 1):
        if edges[k] <= entry_i < edges[k + 1]:
            return k
    return None


def stats(nets: list[float]) -> dict:
    """Per-trade NET returns (fraction) -> summary in bps."""
    a = np.array(nets, dtype=float) * 1e4
    if not len(a):
        return {"n": 0, "win": None, "mean": None, "median": None, "ex_best2": None,
                "top2_share": None, "sum": 0.0}
    srt = np.sort(a)
    ex = srt[:-2] if len(a) > 2 else np.array([])
    tot = float(a.sum())
    return {"n": int(len(a)), "win": round(float((a > 0).mean()), 3),
            "mean": round(float(a.mean()), 1), "median": round(float(np.median(a)), 1),
            "ex_best2": round(float(ex.mean()), 1) if len(ex) else None,
            "top2_share": round(float(srt[-2:].sum() / tot), 2) if tot > 0 and len(a) >= 2 else None,
            "sum": round(tot, 1)}


def run(a) -> dict:
    base = Costs(fee_bps=a.fee_bps, slippage_bps=a.slip_bps, maker_bps=a.maker_bps)
    stress = Costs(fee_bps=80.0, slippage_bps=10.0, maker_bps=a.maker_bps)
    grid = default_grid()
    out: dict = {"generated_at": datetime.now(timezone.utc).isoformat(),
                 "costs": {"base": base.__dict__, "stress": stress.__dict__},
                 "rules": RULES, "data": {}, "sleeves": {}}
    for sleeve, cfg in SLEEVES.items():
        tf, fam = cfg["tf"], cfg["family"]
        specs = grid[fam]
        live = specs[0]
        variants = [None] + cfg["filters"]
        res: dict = {"live_spec": live.label(), "variants": {}}
        per = {v or "none": {"A": {"trades": [], "stress": [], "by_coin": {}, "by_fold": {}},
                             "B": {"trades": [], "by_coin": {}, "chosen": {}}} for v in variants}
        for sym in a.symbols:
            df, info = load_pipeline_bars(a.data_dir, sym, tf)
            out["data"][f"{sym}@{tf}m"] = info
            if len(df) < WARM + (a.folds + 1) * 20:
                print(f"{sym} {tf}m: only {len(df)} contiguous pipeline bars — skipped", flush=True)
                continue
            d = add_indicators(df)
            edges = fold_edges(len(d), a.folds)
            for v in variants:
                key = v or "none"
                tr = simulate(d, with_filter(live, v), base)
                tr_s = simulate(d, with_filter(live, v), stress)
                oos = [(fold_of(t.entry_i, edges), t) for t in tr]
                oos = [(k, t) for k, t in oos if k is not None]
                oos_s = [t for t in tr_s if fold_of(t.entry_i, edges) is not None]
                A = per[key]["A"]
                A["trades"] += [t.net for _, t in oos]
                A["stress"] += [t.net for t in oos_s]
                A["by_coin"][sym] = stats([t.net for _, t in oos])
                A["by_coin"][sym]["stress_mean"] = stats([t.net for t in oos_s])["mean"]
                for k, t in oos:
                    A["by_fold"].setdefault(k, []).append(t.net)
                wf = walk_forward(d, [with_filter(s, v) for s in specs], base, folds=a.folds)
                B = per[key]["B"]
                B["trades"] += [t.net for t in wf.oos_trades]
                B["by_coin"][sym] = stats([t.net for t in wf.oos_trades])
                B["chosen"][sym] = wf.chosen
        base_A = per["none"]["A"]
        for v in variants:
            key = v or "none"
            A, B = per[key]["A"], per[key]["B"]
            row = {
                "A_fixed_live_spec": {
                    **stats(A["trades"]), "stress_mean": stats(A["stress"])["mean"],
                    "by_coin": A["by_coin"],
                    "by_fold": {str(k): stats(x) for k, x in sorted(A["by_fold"].items())},
                },
                "B_walkforward_selection": {**stats(B["trades"]), "by_coin": B["by_coin"],
                                            "chosen": B["chosen"]},
            }
            if v:
                row["promotion"] = promotion(A, B, base_A)
            res["variants"][key] = row
        out["sleeves"][sleeve] = res
    return out


def promotion(A: dict, B: dict, base_A: dict) -> dict:
    s, sb = stats(A["trades"]), stats(base_A["trades"])
    folds_better = 0
    for k, xs in A["by_fold"].items():
        m = np.mean(xs) if xs else -np.inf
        bm = np.mean(base_A["by_fold"].get(k, [])) if base_A["by_fold"].get(k) else -np.inf
        folds_better += int(m > bm)
    coins_pos = sum(1 for c in A["by_coin"].values() if (c["mean"] or -1) > 0)
    checks = {
        "trades>=30": s["n"] >= RULES["min_trades"],
        "mean>0_and>baseline": bool(s["n"] and s["mean"] > 0 and (sb["mean"] is None or s["mean"] > sb["mean"])),
        f"beats_baseline_in>={RULES['min_folds_better']}_of_4_folds": folds_better >= RULES["min_folds_better"],
        "ex_best2>0": bool(s["ex_best2"] is not None and s["ex_best2"] > 0),
        "stress_mean>0": bool((stats(A["stress"])["mean"] or -1) > 0),
        f"positive_on>={RULES['min_coins_positive']}_coins": coins_pos >= RULES["min_coins_positive"],
        "walkforward_selection_mean>0": bool((stats(B["trades"])["mean"] or -1) > 0),
    }
    return {"checks": checks, "folds_better": folds_better, "coins_positive": coins_pos,
            "promote": all(checks.values())}


def fmt(x, w=7):
    return ("-" if x is None else f"{x:.0f}" if isinstance(x, float) else str(x)).rjust(w)


def report(out: dict) -> str:
    L = []
    c = out["costs"]["base"]
    L.append(f"Order-flow filter study (pipeline bars, BTC/ETH/SOL) — generated {out['generated_at']}")
    L.append(f"costs: taker {c['fee_bps']:g}+{c['slippage_bps']:g} bps/side "
             f"({2 * (c['fee_bps'] + c['slippage_bps']):g} bps round trip), maker {c['maker_bps']:g} bps/side; "
             "stress: 80+10 bps/side")
    L.append("data (latest contiguous fully-covered tick-built bars):")
    for k, info in out["data"].items():
        L.append(f"  {k:<14} bars={info.get('bars')} days={info.get('days')} holes_skipped={info.get('holes', 0)}")
    for sleeve, res in out["sleeves"].items():
        L.append("")
        L.append(f"== {sleeve}  live spec {res['live_spec']}")
        L.append("A. fixed live spec, OOS folds 1-4 (net bps per trade)")
        L.append("   variant      coin        n   win%    mean  median ex-best2 top2share  stress")
        for v, row in res["variants"].items():
            A = row["A_fixed_live_spec"]
            for coin, s in A["by_coin"].items():
                L.append(f"   {v:<12} {coin:<8} {fmt(s['n'], 4)} {fmt(None if s['win'] is None else s['win'] * 100, 6)}"
                         f" {fmt(s['mean'])} {fmt(s['median'])} {fmt(s['ex_best2'], 8)} "
                         f"{fmt(s['top2_share'], 9) if s['top2_share'] is None else str(s['top2_share']).rjust(9)}"
                         f" {fmt(s.get('stress_mean'))}")
            L.append(f"   {v:<12} {'ALL':<8} {fmt(A['n'], 4)} {fmt(None if A['win'] is None else A['win'] * 100, 6)}"
                     f" {fmt(A['mean'])} {fmt(A['median'])} {fmt(A['ex_best2'], 8)} "
                     f"{fmt(A['top2_share'], 9) if A['top2_share'] is None else str(A['top2_share']).rjust(9)}"
                     f" {fmt(A['stress_mean'])}")
            L.append("   " + v.ljust(12) + " folds:   " + "  ".join(
                f"f{k} n={s['n']} {fmt(s['mean'], 0).strip()}bps" for k, s in A["by_fold"].items()))
        L.append("B. walk-forward selection over the grid (IS fold k-1 -> OOS fold k)")
        for v, row in res["variants"].items():
            B = row["B_walkforward_selection"]
            coins = "  ".join(f"{c.split('/')[0]} n={s['n']} {fmt(s['mean'], 0).strip()}" for c, s in B["by_coin"].items())
            L.append(f"   {v:<12} ALL n={B['n']} win={fmt(None if B['win'] is None else B['win'] * 100, 0).strip()}% "
                     f"mean={fmt(B['mean'], 0).strip()} median={fmt(B['median'], 0).strip()} | {coins}")
        L.append("Promotion (all rules must pass):")
        for v, row in res["variants"].items():
            if "promotion" not in row:
                continue
            p = row["promotion"]
            failed = [k for k, ok in p["checks"].items() if not ok]
            L.append(f"   {v:<12} {'PROMOTE' if p['promote'] else 'no'}"
                     + ("" if p["promote"] else f"  (failed: {', '.join(failed)})"))
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(DATA))
    ap.add_argument("--symbols", nargs="*", default=list(SYMBOLS))
    ap.add_argument("--fee-bps", type=float, default=80.0)
    ap.add_argument("--slip-bps", type=float, default=5.0)
    ap.add_argument("--maker-bps", type=float, default=40.0)
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--out", default=str(DATA / "orderflow_study_results.json"))
    ap.add_argument("--report", default=str(DATA / "orderflow_study_report.txt"))
    a = ap.parse_args()
    out = run(a)
    Path(a.out).write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    txt = report(out)
    Path(a.report).write_text(txt, encoding="utf-8")
    print(txt)
    print(f"wrote {a.out} and {a.report}")


if __name__ == "__main__":
    main()
