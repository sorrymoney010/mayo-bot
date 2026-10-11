"""Walk-forward study + weekly-research hook for order-flow overlays.

``VARIANTS`` is the pre-registered, deliberately small set of overlays per sleeve
(entry filters and exit timers from ``orderflow`` / ``flow_sim``). ``run_study``
scores each on OOS walk-forward folds after fees and applies two gates:

* the repo promotion bar (``promotion.check_strategy``: >= 30 OOS trades, mean > 0,
  90% lower bound > 0, mean ex-best-2 > 0, total P&L >= 0), and
* an improvement gate vs the unmodified sleeve (higher mean, higher compounded OOS
  return, beats the sleeve in >= 3 of 4 folds, stress-cost mean > 0, positive on
  >= 2 of 3 coins).

Only a variant that passes BOTH is a shadow candidate. Paper research only: nothing here
reads or writes settings, env files or locks.
"""
from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .backtest_core import Costs, Spec, add_indicators, metrics, simulate
from .flow_sim import FTrade, Overlay, simulate_flow
from .learner import mean_bounds
from .promotion import check_strategy

WARM = 210
STRESS = Costs(fee_bps=80.0, slippage_bps=10.0, maker_bps=25.0)

SLEEVES = {
    "regime@60m": {"tf": 60, "spec": Spec("regime", {
        "lookback": 20, "atr_mult": 3.0, "min_atr_rank": 0.0, "stop": 0.03, "tp": 0.25,
        "adx_enter": 25.0, "adx_exit": 20.0, "d1": True})},
    "meanrev_mk@240m": {"tf": 240, "spec": Spec("meanrev", {
        "rsi_os": 38.0, "rsi_exit": 55.0, "stop": 0.03, "tp": 0.25, "entry": "limit",
        "limit_offset": 0.001, "d1": True})},
    "trendhold@240m": {"tf": 240, "spec": Spec("trendhold", {"ema_fast": 20, "ema_slow": 100, "d1": True})},
}


def _variants(stops: bool) -> list[Overlay]:
    v = [Overlay("of_no_dist", entry_filter="of_no_dist"),
         Overlay("of_no_bear_abs", entry_filter="of_no_bear_abs"),
         Overlay("of_abs_pref", entry_filter="of_abs_pref"),
         Overlay("of_not_extended", entry_filter="of_not_extended"),
         Overlay("x_climax_all", exit_trigger="buy_climax", exit_frac=1.0),
         Overlay("x_climax_half", exit_trigger="buy_climax", exit_frac=0.5),
         Overlay("x_euphoria_all", exit_trigger="euphoria", exit_frac=1.0),
         Overlay("x_euphoria_half", exit_trigger="euphoria", exit_frac=0.5),
         Overlay("x_shakeout_hold", stop_mode="shakeout"),
         Overlay("ptp_2atr", ptp_atr=2.0),
         Overlay("ptp_4atr", ptp_atr=4.0)]
    if stops:
        v.insert(9, Overlay("x_closestop_ctrl", stop_mode="closestop"))
    return v


VARIANTS = {k: [Overlay("baseline")] + _variants(cfg["spec"].name != "trendhold") for k, cfg in SLEEVES.items()}
OVERLAYS = {o.name: o for vs in VARIANTS.values() for o in vs}


# ── data ───────────────────────────────────────────────────────

def longest_tick_run(data: Path, symbol: str, tf: int = 60, *, which: str = "longest",
                     now: float | None = None) -> tuple[int, int] | None:
    """(start, end) epoch seconds of the longest (or ``which="latest"``) run of fully
    covered tick-built bars."""
    from .pipeline import canonical
    from .pipeline.bars import BarBuilder
    from .pipeline.tickstore import TickStore
    bars = BarBuilder(TickStore(data)).build(canonical(symbol), tf, 0, now=time.time() if now is None else now,
                                             write_cache=False)
    if not len(bars):
        return None
    sec = bars.index.asi8 // 10**9
    cut = np.r_[0, np.nonzero(np.diff(sec) != tf * 60)[0] + 1, len(sec)]
    best = (len(cut) - 2) if which == "latest" else max(range(len(cut) - 1), key=lambda k: cut[k + 1] - cut[k])
    a, b = cut[best], cut[best + 1]
    return int(sec[a]), int(sec[b - 1]) + tf * 60


def load_flow_sleeve_frames(data: Path, symbols: list[str], *, cache: str | None = None,
                            tfs: tuple[int, ...] = (60, 240), which: str = "longest",
                            now: float | None = None) -> dict:
    """{symbol: {"span": (a,b), tf: (d, fb)}} with indicator + D1 + flow columns."""
    from .daily_filter import attach_d1, riskon_table
    from .orderflow import continuous_1m, build_flow_1m, fine_features, minute_features, resample_bars, tf_features
    from .study_data import _ohlc, merge_daily
    out: dict = {}
    for sym in symbols:
        span = longest_tick_run(data, sym, which=which, now=now)
        if span is None:
            continue
        a, b = span
        base = sym.replace("/", "")
        m1 = build_flow_1m(data / "ticks" / base, end=b,
                           cache_dir=(Path(cache) / base) if cache else None)
        m1 = m1[m1.index >= a]
        c1 = continuous_1m(m1, a, b)
        mf = minute_features(c1)
        fb = fine_features(c1, mf)
        daily = merge_daily(_ohlc(sym, 1440, data), resample_bars(c1, 1440))
        table = riskon_table(daily) if daily is not None and len(daily) else None
        rec: dict = {"span": span, "days": round((b - a) / 86400, 1)}
        for tf in tfs:
            d = add_indicators(tf_features(c1, mf, fb, tf))
            d = attach_d1(d, table, tf) if table is not None else d
            rec[tf] = (d.reset_index(drop=True), fb)
        out[sym] = rec
    return out


# ── scoring ────────────────────────────────────────────────────

def fold_edges(n: int, folds: int, warm: int = WARM) -> list[int]:
    usable = n - warm
    return [warm + (usable * k) // (folds + 1) for k in range(folds + 2)]


def fold_of(i: int, edges: list[int]) -> int | None:
    for k in range(1, len(edges) - 1):
        if edges[k] <= i < edges[k + 1]:
            return k
    return None


def stats(nets_bps: list[float]) -> dict:
    a = np.asarray(nets_bps, float)
    if not len(a):
        return {"n": 0, "mean": None, "median": None, "win": None, "ex_best2": None, "lb90": None}
    s = np.sort(a)
    _m, lo, _hi = mean_bounds(list(a), 0.90)
    return {"n": int(len(a)), "mean": round(float(a.mean()), 1), "median": round(float(np.median(a)), 1),
            "win": round(float((a > 0).mean()), 3),
            "ex_best2": round(float(s[:-2].mean()), 1) if len(a) > 2 else None,
            "lb90": None if lo is None else round(float(lo), 1)}


def _rows(trs: list) -> list[dict]:
    out = []
    for t in trs:
        notional = min(500.0 * 0.01 / max(t.stop_frac, 1e-6), 500.0 * 0.40)
        out.append({"net_bps": t.net * 1e4, "pnl": notional * t.net})
    return out


def score(trs_by_sym: dict[str, list], edges_by_sym: dict[str, list[int]]) -> dict:
    oos, by_fold, by_coin = [], {}, {}
    for sym, trs in trs_by_sym.items():
        mine = [(fold_of(t.entry_i, edges_by_sym[sym]), t) for t in trs]
        mine = [(k, t) for k, t in mine if k is not None]
        by_coin[sym] = stats([t.net * 1e4 for _k, t in mine])
        for k, t in mine:
            by_fold.setdefault(k, []).append(t.net * 1e4)
        oos += [t for _k, t in mine]
    m = metrics(oos)
    return {**stats([t.net * 1e4 for t in oos]), "oos_return_pct": round(m.total_return_pct, 2),
            "max_dd_pct": round(m.max_dd_pct, 2), "by_coin": by_coin,
            "by_fold": {k: stats(v) for k, v in sorted(by_fold.items())},
            "bar": check_strategy(_rows(oos)), "_trades": oos}


def improvement(var: dict, base: dict, stress_mean: float | None) -> dict:
    folds_better = sum(1 for k, s in var["by_fold"].items()
                       if s["mean"] is not None and (base["by_fold"].get(k, {}).get("mean") is None
                                                     or s["mean"] > base["by_fold"][k]["mean"]))
    coins_pos = sum(1 for s in var["by_coin"].values() if (s["mean"] or -1) > 0)
    checks = {
        "mean_above_baseline": (var["mean"] or -1e9) > (base["mean"] or -1e9),
        "return_above_baseline": var["oos_return_pct"] > base["oos_return_pct"],
        "beats_baseline_in_3_of_4_folds": folds_better >= 3,
        "stress_mean_positive": (stress_mean or -1) > 0,
        "positive_on_2_of_3_coins": coins_pos >= 2,
    }
    return {"checks": checks, "passes": all(checks.values()), "folds_better": folds_better,
            "failed": [k for k, ok in checks.items() if not ok]}


def buy_and_hold_oos(frames: dict, tf: int, costs: Costs, folds: int) -> dict:
    rets, dds = [], []
    for rec in frames.values():
        d = rec[tf][0]
        e = fold_edges(len(d), folds)
        px0, px1 = float(d["open"].iloc[e[1]]), float(d["close"].iloc[-1])
        rets.append(costs.net_return(px0, px1) * 100)
        c = d["close"].iloc[e[1]:].astype(float)
        dds.append(float(((c.cummax() - c) / c.cummax()).max() * 100))
    return {"mean_return_pct": round(float(np.mean(rets)), 2), "mean_max_dd_pct": round(float(np.mean(dds)), 2),
            "per_coin_return_pct": [round(x, 2) for x in rets]}


def walk_forward_select(results: dict[str, dict[str, list]], edges: dict[str, list[int]], folds: int) -> dict:
    """Pick the overlay with the best IS (fold k-1) mean, score it on fold k. Fold 1 uses baseline."""
    chosen, oos = [], []
    names = list(results)
    for k in range(1, folds + 1):
        best, best_m = "baseline", -math.inf
        if k > 1:
            for nm in names:
                xs = [t.net for sym, trs in results[nm].items() for t in trs
                      if fold_of(t.entry_i, edges[sym]) == k - 1]
                if len(xs) >= 3 and np.mean(xs) > best_m:
                    best, best_m = nm, float(np.mean(xs))
        chosen.append(best)
        oos += [t.net * 1e4 for sym, trs in results[best].items() for t in trs
                if fold_of(t.entry_i, edges[sym]) == k]
    return {"chosen": chosen, **stats(oos)}


def run_study(frames: dict, costs: Costs, *, folds: int = 4, sleeves: dict | None = None,
              variants: dict | None = None) -> dict:
    sleeves = sleeves or SLEEVES
    variants = variants or VARIANTS
    rep: dict = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "costs": costs.__dict__, "stress_costs": STRESS.__dict__,
                 "data": {s: {"span": [time.strftime("%Y-%m-%d %H:%M", time.gmtime(x)) for x in r["span"]],
                              "days": r["days"]} for s, r in frames.items()},
                 "configs_tried": sum(len(v) - 1 for k, v in variants.items() if k in sleeves),
                 "sleeves": {}}
    for sk, cfg in sleeves.items():
        tf, spec = cfg["tf"], cfg["spec"]
        edges = {s: fold_edges(len(r[tf][0]), folds) for s, r in frames.items()}
        oos_days = {s: round((r[tf][0]["time"].iloc[-1] - r[tf][0]["time"].iloc[edges[s][1]]) / 86400, 1)
                    for s, r in frames.items()}
        res_trades: dict[str, dict[str, list]] = {}
        rows: dict[str, dict] = {}
        # sanity: flow_sim baseline vs backtest_core.simulate
        check = {}
        for s, r in frames.items():
            d, _fb = r[tf]
            ref = [t for t in simulate(d, spec, costs) if fold_of(t.entry_i, edges[s])]
            check[s] = {"backtest_core_oos_n": len(ref),
                        "backtest_core_oos_mean": stats([t.net * 1e4 for t in ref])["mean"]}
        for ov in variants[sk]:
            trs = {s: simulate_flow(r[tf][0], r[tf][1], spec, costs, ov, tf_minutes=tf) for s, r in frames.items()}
            st = {s: simulate_flow(r[tf][0], r[tf][1], spec, STRESS, ov, tf_minutes=tf) for s, r in frames.items()}
            res_trades[ov.name] = trs
            sc = score(trs, edges)
            sc["stress_mean"] = score(st, edges)["mean"]
            sc["exit_reasons"] = _reasons(sc["_trades"])
            rows[ov.name] = sc
        base = rows["baseline"]
        for nm, sc in rows.items():
            if nm != "baseline":
                sc["improvement"] = improvement(sc, base, sc["stress_mean"])
                sc["shadow_candidate"] = bool(sc["bar"]["passes"] and sc["improvement"]["passes"])
        for sc in rows.values():
            sc.pop("_trades", None)
        rep["sleeves"][sk] = {
            "tf": tf, "spec": spec.label(), "oos_days": oos_days, "baseline_check": check,
            "buy_and_hold": buy_and_hold_oos(frames, tf, costs, folds),
            "variants": rows, "walk_forward_selection": walk_forward_select(res_trades, edges, folds),
        }
    try:
        rep["event_study"] = event_study(frames)
    except Exception as exc:  # noqa: BLE001 — descriptive section only
        rep["event_study"] = {"error": f"{type(exc).__name__}: {exc}"}
    rep["shadow_candidates"] = [f"{sk}|{nm}" for sk, s in rep["sleeves"].items()
                                for nm, v in s["variants"].items() if v.get("shadow_candidate")]
    return rep


EVENT_FEATURES = ("imb_60", "net_z_60", "lnet_z_60", "imb_240", "net_z_240", "lnet_z_240", "vwap_dev_z", "ret_z_240")


def event_study(frames: dict, tf: int = 60) -> dict:
    """Descriptive only (not used for any selection): do flow features at a 1h close rank
    forward 4h / 24h returns? Spearman IC on the first vs second half of each coin, and mean
    forward returns after 15m climax / euphoria / absorbed-dip events vs unconditional."""
    ic: dict = {}
    events: dict = {}
    for sym, rec in frames.items():
        d = rec[tf][0]
        lc = np.log(d["close"].astype(float))
        n = len(d)
        half = WARM + (n - WARM) // 2
        for f in EVENT_FEATURES:
            if f not in d:
                continue
            for h in (4, 24):
                fwd = (lc.shift(-h) - lc) * 1e4
                for seg, sl in (("first_half", slice(WARM, half)), ("second_half", slice(half, n))):
                    x, y = d[f].iloc[sl], fwd.iloc[sl]
                    m = x.notna() & y.notna()
                    val = x[m].rank().corr(y[m].rank()) if m.sum() > 30 else None
                    ic.setdefault(f"{f}|{h}h", {}).setdefault(seg, {})[sym] = \
                        None if val is None or not np.isfinite(val) else round(float(val), 3)
        fb = rec[tf][1]
        lf = np.log(fb["close"].astype(float))
        f96 = (lf.shift(-96) - lf) * 1e4
        f16 = (lf.shift(-16) - lf) * 1e4
        ev = {"unconditional": {"n": int(f96.notna().sum()), "fwd4h": round(float(f16.mean()), 1),
                                "fwd24h": round(float(f96.mean()), 1)}}
        for c in ("buy_climax", "sell_climax", "euphoria", "dip_absorbed"):
            m = fb[c].to_numpy(bool)
            ev[c] = {"n": int(m.sum()), "fwd4h": round(float(f16[m].mean()), 1) if m.any() else None,
                     "fwd24h": round(float(f96[m].mean()), 1) if m.any() else None}
        events[sym] = ev
    return {"ic": ic, "events": events}


def _reasons(trs: list) -> dict:
    out: dict[str, int] = {}
    for t in trs:
        for r in t.reason.split("+"):
            out[r] = out.get(r, 0) + 1
    return out


def _f(x, nd=0):
    return "-" if x is None else f"{x:.{nd}f}"


def render_md(rep: dict) -> str:
    L = [f"# Order-flow filters + exit timers — {rep['generated_at']}", "",
         "Paper research only. Ticks read-only; no lock, setting or live process touched.",
         f"Costs: taker {rep['costs']['fee_bps']:g} + slip {rep['costs']['slippage_bps']:g} bps/side, "
         f"maker {rep['costs']['maker_bps']:g}; stress 80+10. Configs tried: {rep['configs_tried']} "
         "(pre-registered, fixed thresholds, one run).", ""]
    for s, info in rep["data"].items():
        L.append(f"- data {s}: longest contiguous tick run {info['span'][0]} .. {info['span'][1]} UTC ({info['days']} d)")
    for sk, s in rep["sleeves"].items():
        bh = s["buy_and_hold"]
        L += ["", f"## {sk}  `{s['spec']}`",
              f"OOS days per coin: {s['oos_days']}; buy-and-hold over the OOS window: mean "
              f"{bh['mean_return_pct']}% (per coin {bh['per_coin_return_pct']}), mean max DD {bh['mean_max_dd_pct']}%",
              f"backtest_core cross-check (baseline OOS n/mean per coin): "
              + ", ".join(f"{k.split('/')[0]} {v['backtest_core_oos_n']}/{_f(v['backtest_core_oos_mean'])}"
                          for k, v in s["baseline_check"].items()), "",
              "| variant | n | mean bps | median | win% | lb90 | ex-best2 | OOS ret% | maxDD% | stress mean | folds>base | bar | improves | candidate |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for nm, v in s["variants"].items():
            imp = v.get("improvement")
            L.append(f"| {nm} | {v['n']} | {_f(v['mean'])} | {_f(v['median'])} | "
                     f"{_f(None if v['win'] is None else v['win'] * 100)} | {_f(v['lb90'])} | {_f(v['ex_best2'])} | "
                     f"{v['oos_return_pct']:.2f} | {v['max_dd_pct']:.2f} | {_f(v['stress_mean'])} | "
                     f"{'-' if not imp else imp['folds_better']} | {'PASS' if v['bar']['passes'] else 'fail'} | "
                     f"{'-' if not imp else ('yes' if imp['passes'] else 'no')} | "
                     f"{'YES' if v.get('shadow_candidate') else '-'} |")
        wf = s["walk_forward_selection"]
        L.append("")
        L.append(f"Walk-forward selection over this sleeve's variants (IS fold k-1 -> OOS fold k): "
                 f"n={wf['n']} mean={_f(wf['mean'])} median={_f(wf['median'])} chosen={wf['chosen']}")
    es = rep.get("event_study") or {}
    if es.get("ic"):
        L += ["", "## Descriptive: do flow features rank forward returns? (Spearman IC, 1h bars; not used for selection)",
              "| feature | horizon | first half BTC/ETH/SOL | second half BTC/ETH/SOL |", "|---|---|---|---|"]
        for key, segs in es["ic"].items():
            f, h = key.split("|")
            fh = "/".join(_f(v, 3) for v in segs.get("first_half", {}).values())
            sh = "/".join(_f(v, 3) for v in segs.get("second_half", {}).values())
            L.append(f"| {f} | {h} | {fh} | {sh} |")
        L += ["", "Mean forward return (bps) after 15m events:", "",
              "| coin | event | n | fwd 4h | fwd 24h |", "|---|---|---|---|---|"]
        for sym, ev in es["events"].items():
            for nm, v in ev.items():
                L.append(f"| {sym} | {nm} | {v['n']} | {_f(v['fwd4h'])} | {_f(v['fwd24h'])} |")
    L += ["", "Shadow candidates (bar AND improvement gate): " + (", ".join(rep["shadow_candidates"]) or "none")]
    return "\n".join(L) + "\n"


# ── forward replay for shadow-stage overlays ───────────────────

FORWARD_WARM = {"regime@60m": 210, "meanrev_mk@240m": 60, "trendhold@240m": 110}


def forward_replay(reg: dict, frames: dict, costs: Costs) -> dict[str, dict[str, list[dict]]]:
    """Replay every shadow/paper overlay AND its unmodified sleeve with frozen rules over
    ``frames`` (the LATEST contiguous tick run). Returns closed-trade rows per record;
    ``flow_promotion.record_forward_trades`` keeps only trades entered after shadow start."""
    out: dict[str, dict[str, list[dict]]] = {}
    base_cache: dict[str, list[dict]] = {}
    for rid, rec in reg.items():
        if rec.get("stage") not in ("shadow", "paper"):
            continue
        sk = rec.get("sleeve")
        cfg = SLEEVES.get(sk)
        if cfg is None:
            continue
        ovd = rec.get("overlay") or {}
        try:
            ov = Overlay(**ovd)
        except TypeError:
            continue
        tf, spec, warm = cfg["tf"], cfg["spec"], FORWARD_WARM.get(sk, WARM)

        def rows(o: Overlay) -> list[dict]:
            res = []
            for sym, r in frames.items():
                if tf not in r or len(r[tf][0]) < warm + 5:
                    continue
                for t in simulate_flow(r[tf][0], r[tf][1], spec, costs, o, tf_minutes=tf, warm=warm):
                    notional = min(500.0 * 0.01 / max(t.stop_frac, 1e-6), 500.0 * 0.40)
                    res.append({"symbol": sym, "entry_time": t.entry_time, "exit_time": t.exit_time,
                                "net_bps": round(t.net * 1e4, 2), "pnl": round(notional * t.net, 4),
                                "reason": t.reason})
            return res

        if sk not in base_cache:
            base_cache[sk] = rows(Overlay("baseline"))
        out[rid] = {"variant": rows(ov), "baseline": base_cache[sk]}
    return out
