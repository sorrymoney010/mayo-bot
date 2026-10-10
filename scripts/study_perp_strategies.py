#!/usr/bin/env python3
"""Walk-forward study of Kraken Futures perp strategies (PF_XBTUSD/PF_ETHUSD/PF_SOLUSD).

PAPER research only. Reads public caches made by scripts/fetch_futures_charts.py
(read-only), writes reports/perp_study_<stamp>.{json,md}. No keys, no orders.

    python scripts/study_perp_strategies.py --data /workspace/futures-data
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dublin_bot import perp_research as pr  # noqa: E402
from dublin_bot import perp_strategies as ps  # noqa: E402
from dublin_bot.study_data import unique_report  # noqa: E402

PERPS = {"BTC/USD": "PF_XBTUSD", "ETH/USD": "PF_ETHUSD", "SOL/USD": "PF_SOLUSD"}
SPOT_SIDE_COST = (40.0 + 10.0) / 1e4   # repo spot model: 40 bps taker + 10 bps slippage per side


def iso(t):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(t)))


def families():
    """(family, tf, [(label, builder, params)], allowed_levs, kind)."""
    F = []
    for tf in (60, 240):
        F.append(("trend_ls", tf, [(f"ema{s}", ps.trend_ls, {"ema_slow": s}) for s in (100, 80)], (1.0, 2.0), "perp"))
    for tf in (60, 240):
        F.append(("regime_ls", tf, [(f"lb{lb}_k{k}", ps.regime_ls, {"lookback": lb, "atr_mult": k})
                                    for lb, k in ((20, 3.0), (55, 3.0), (20, 2.0))], (1.0, 2.0), "perp"))
    F.append(("d1_flip_ls", 60, [(f"bear_{b}", ps.d1_flip, {"bear": b}) for b in ("strict", "repo")], (1.0, 2.0), "perp"))
    F.append(("funding_fade", 60, [(f"q{q}_h{h}", ps.funding_fade, {"q": q, "hold_h": h})
                                   for q in (0.90, 0.95) for h in (24, 72)], (1.0, 2.0), "perp"))
    F.append(("donchian_ls", 240, [(f"N{a}_M{b}_vf{int(v)}", ps.donchian_ls,
                                    {"entry_days": a, "exit_days": b, "vol_filter": v})
                                   for a, b in ((20, 10), (55, 20)) for v in (False, True)], (1.0, 2.0), "perp"))
    for tf in (60, 240):
        F.append(("trend_long_perp", tf, [(f"ema{s}", ps.trend_ls, {"ema_slow": s, "sides": "long"}) for s in (100, 80)], (1.0, 2.0), "perp"))
        F.append(("trend_long_spot", tf, [(f"ema{s}", ps.trend_ls, {"ema_slow": s, "sides": "long"}) for s in (100, 80)], (1.0,), "spot"))
    F.append(("d1_long_perp", 60, [("d1", ps.d1_flip, {"sides": "long"})], (1.0, 2.0), "perp"))
    F.append(("d1_long_spot", 60, [("d1", ps.d1_flip, {"sides": "long"})], (1.0,), "spot"))
    return F


def spot_frame(d: pd.DataFrame) -> pd.DataFrame:
    """Spot proxy: Kraken Futures index ('spot' chart) closes; no funding, no liquidation."""
    s = d.copy()
    s["open"] = s["s_open"]
    s["close"] = s["s_close"]
    s["high"] = np.maximum(s["s_open"], s["s_close"])
    s["low"] = np.minimum(s["s_open"], s["s_close"])
    s["m_open"], s["m_high"], s["m_low"] = s["open"], np.inf, -np.inf
    s["rate"] = 0.0
    return s


def run_window(name, hourly, t0, t1, slip_bps, now, *, note):
    costs = pr.Costs(fee_bps=pr.TAKER_BPS, slip_bps=slip_bps)
    spot_costs = pr.Costs(fee_bps=40.0, slip_bps=10.0)
    edges = pr.time_edges(t0, t1, folds=4)
    oos_t0 = edges[1]
    prepared = {}
    for tf in (60, 240):
        for sym, h in hourly.items():
            prepared[(tf, sym)] = pr.prepare(h, tf, now=now)
    syms = list(hourly)
    out = {"window": name, "note": note, "start": iso(t0), "end": iso(t1),
           "oos_start": iso(oos_t0), "oos_days": round((t1 - oos_t0) / 86400, 1),
           "segments": [iso(e) for e in edges], "slip_bps": slip_bps, "results": []}
    out["buy_and_hold_oos"] = pr.buy_hold({s: prepared[(60, s)] for s in syms}, oos_t0, t1, SPOT_SIDE_COST)
    n_cfg = 0
    for fam, tf, specs, levs, kind in families():
        bar_h = tf / 60
        for lev in levs:
            spec_trades = {}
            for lab, fn, p in specs:
                trs = []
                for sym in syms:
                    d = prepared[(tf, sym)]
                    if kind == "spot":
                        d = spot_frame(d)
                    kw = {"bar_hours": bar_h} if fn in (ps.funding_fade, ps.donchian_ls) else {}
                    sig = fn(d, p, **kw)
                    warm = max(int(np.searchsorted(d["time"].to_numpy(), t0)), 210)
                    trs += pr.simulate(d, sig, symbol=sym, lev=lev,
                                       costs=spot_costs if kind == "spot" else costs,
                                       warm=warm, max_hold=int(sig.get("max_hold", 0)))
                spec_trades[lab] = [x for x in trs if x.entry_time < t1]
                n_cfg += 1
            oos, chosen = pr.walk_forward(spec_trades, edges, default=specs[0][0])
            summ = pr.summarize(oos, lev=lev, symbols=syms, t0=oos_t0, t1=t1)
            per_sym = {s: pr.summarize([x for x in oos if x.symbol == s], lev=lev, symbols=[s], t0=oos_t0, t1=t1)
                       for s in syms}
            for v in per_sym.values():
                v.pop("checks", None)
            # Diagnostics: every spec held fixed over the whole OOS (no selection),
            # long/short split and per-fold mean of the walk-forward OOS trades.
            per_spec = {}
            for lab, trs in spec_trades.items():
                o_ = [x for x in trs if oos_t0 <= x.entry_time < t1]
                sm = pr.summarize(o_, lev=lev, symbols=syms, t0=oos_t0, t1=t1)
                per_spec[lab] = {k: sm.get(k) for k in ("trades", "mean_net_bps", "lcb90_bps", "mean_ex_best2_bps",
                                                        "oos_return_pct", "max_dd_pct", "passes")}
            split = {}
            for nm, sd in (("long", 1), ("short", -1)):
                b = [x.net * 1e4 for x in oos if x.side == sd]
                split[nm] = {"trades": len(b), "mean_net_bps": round(float(np.mean(b)), 1) if b else None,
                             "sum_net_bps": round(float(np.sum(b)), 1) if b else 0.0}
            folds_ = []
            for k in range(1, len(edges) - 1):
                b = [x.net * 1e4 for x in oos if edges[k] <= x.entry_time < edges[k + 1]]
                folds_.append({"from": iso(edges[k]), "trades": len(b),
                               "mean_net_bps": round(float(np.mean(b)), 1) if b else None})
            out["results"].append({"family": fam, "tf": tf, "leverage": lev, "kind": kind,
                                   "specs": [s[0] for s in specs], "chosen_per_fold": chosen,
                                   "oos": summ, "per_symbol": per_sym,
                                   "per_spec_fixed_oos": per_spec, "long_short": split, "per_fold": folds_,
                                   "oos_trades": [{"symbol": x.symbol, "side": x.side, "entry": iso(x.entry_time),
                                                   "exit": iso(x.exit_time), "net_bps": round(x.net * 1e4, 1),
                                                   "funding_bps": round(x.funding * 1e4, 1), "reason": x.reason}
                                                  for x in sorted(oos, key=lambda z: z.entry_time)]})
    out["configs_simulated"] = n_cfg
    out["carry"] = run_carry(prepared, syms, edges, t1, slip_bps)
    out["configs_simulated"] += out["carry"]["configs"]
    return out


def run_carry(prepared, syms, edges, t1, slip_bps):
    """Cash-and-carry: long spot (index proxy) + short perp, equal notional.
    Enter when the trailing 72 h mean hourly funding annualised > thr; exit when it
    turns negative. Fees: spot 40+10 bps per side (repo model), perp 5+slip per side.
    Capital: spot notional + perp margin (notional/lev) -> capital_mult = 1 + 1/lev."""
    res = {"configs": 0, "results": []}
    oos_t0 = edges[1]
    for lev in (1.0, 2.0):
        spec_trades = {}
        for thr in (0.10, 0.20):
            trs = []
            for sym in syms:
                d = prepared[(60, sym)]
                n = len(d)
                r = d["rate"].to_numpy(float)
                f72 = pd.Series(r).rolling(72, min_periods=72).mean().to_numpy(float)
                ann = f72 * 24 * 365
                c, o = d["close"].to_numpy(float), d["open"].to_numpy(float)
                so, sc = d["s_open"].to_numpy(float), d["s_close"].to_numpy(float)
                mh = d["m_high"].to_numpy(float)
                t = d["time"].to_numpy(np.int64)
                rate0 = np.nan_to_num(r)
                i = max(int(np.searchsorted(t, edges[0])), 210)
                side_cost = (40 + 10) / 1e4 + (pr.TAKER_BPS + slip_bps) / 1e4
                while i < n - 1:
                    if not (np.isfinite(ann[i]) and ann[i] > thr):
                        i += 1
                        continue
                    e = i + 1
                    pe, se_ = o[e], so[e]
                    liq = pr.liq_price(pe, -1, lev)
                    fund, j, reason, path = 0.0, e, "", []
                    while j < n:
                        if mh[j] >= liq:
                            reason = "perp_liquidation"
                            break
                        fund += rate0[j]
                        path.append((int(t[j]), (sc[j] / se_ - 1) - (c[j] / pe - 1) - side_cost + fund))
                        if (np.isfinite(ann[j]) and ann[j] < 0) and j + 1 < n:
                            j += 1
                            reason = "funding_negative"
                            break
                        j += 1
                    j = min(j, n - 1)
                    if reason == "perp_liquidation":
                        net = (sc[j] / se_ - 1) - 1.0 / lev - side_cost + fund
                        px_p = liq
                    else:
                        reason = reason or "eod_mark"
                        px_p = o[j] if reason == "funding_negative" else c[j]
                        px_s = so[j] if reason == "funding_negative" else sc[j]
                        net = (px_s / se_ - 1) - (px_p / pe - 1) - 2 * side_cost + fund
                    trs.append(pr.PTrade(sym, -1, e, j, int(t[e]), int(t[j]), float(pe), float(px_p),
                                         float(net - fund + 2 * side_cost), float(2 * side_cost), float(fund),
                                         float(net), reason, lev, path))
                    i = j
            spec_trades[f"thr{int(thr*100)}"] = [x for x in trs if x.entry_time < t1]
            res["configs"] += 1
        oos, chosen = pr.walk_forward(spec_trades, edges, default="thr10")
        cm = 1.0 + 1.0 / lev
        # equity_curve applies lev x net / capital_mult; for carry the notional per $ capital is 1/cm
        summ = pr.summarize(oos, lev=1.0, symbols=syms, t0=oos_t0, t1=t1, capital_mult=cm)
        res["results"].append({"family": "carry_spot_long_perp_short", "tf": 60, "leverage_perp": lev,
                               "capital_per_notional": cm, "chosen_per_fold": chosen, "oos": summ,
                               "note": "net_bps is per unit notional of ONE leg; return/DD on total capital"})
    # Reference: average funding a short earned over the OOS window
    ref = {}
    for sym in syms:
        d = prepared[(60, sym)]
        m = (d["time"] >= oos_t0) & (d["time"] < t1)
        r = d.loc[m, "rate"].dropna()
        ref[sym] = {"hours_with_rate": int(len(r)), "sum_rate_pct": round(float(r.sum()) * 100, 3),
                    "annualised_pct": round(float(r.mean()) * 24 * 365 * 100, 2) if len(r) else None}
    res["funding_received_by_short_oos"] = ref
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/workspace/futures-data")
    ap.add_argument("--slip-bps", type=float, nargs="*", default=[2.0])
    ap.add_argument("--proxy", action="store_true", help="also run the long window with proxy funding")
    args = ap.parse_args()
    data = Path(args.data)
    now = time.time()
    hourly = {s: pr.load_perp_hourly(data, p) for s, p in PERPS.items()}
    meta = json.loads((data / "fetch_meta.json").read_text())
    first_fund = min(int(h.loc[h["rate"].notna(), "time"].min()) for h in hourly.values())
    t1 = min(int(h["time"].max()) for h in hourly.values())  # last (partial) bar excluded from entries
    rep = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
           "data": meta, "fees": {"perp_taker_bps": pr.TAKER_BPS, "perp_maker_bps": pr.MAKER_BPS,
                                  "source": "https://support.kraken.com/articles/360048917612-fee-schedule (checked 2026-10-10, $0+ tier)",
                                  "spot_model_bps_per_side": "40 taker + 10 slippage (repo Costs default)",
                                  "maintenance_margin": pr.MAINT},
           "windows": []}
    for slip in args.slip_bps:
        rep["windows"].append(run_window("real_funding", hourly, first_fund, t1, slip, now,
                                         note="Only the period with Kraken's published hourly funding history."))
    if args.proxy:
        from dublin_bot.perp_research import funding_proxy_fill
        hp = {s: funding_proxy_fill(h) for s, h in hourly.items()}
        start = int(min(h["time"].min() for h in hourly.values())) + 60 * 86400
        rep["windows"].append(run_window("long_proxy_funding", hp, start, t1, args.slip_bps[0], now,
                                         note="Perp candles since listing; funding before the published window is a "
                                              "PROXY fitted on the overlap (see funding_proxy)."))
        rep["funding_proxy"] = {s: h.attrs.get("proxy_fit") for s, h in hp.items()}
    paths = unique_report(ROOT / "reports", "perp_study", ".json")
    paths[".json"].write_text(json.dumps(rep, indent=1, default=str))
    print(f"wrote {paths['.json']}")
    for w in rep["windows"]:
        print(f"\n== {w['window']} slip={w['slip_bps']} OOS {w['oos_start']} -> {w['end']} ({w['oos_days']} d) configs={w['configs_simulated']}")
        print("   buy&hold:", w["buy_and_hold_oos"])
        rows = w["results"] + w["carry"]["results"]
        for r in rows:
            o = r["oos"]
            print(f"  {r['family']:<28} tf={r['tf']:<4} lev={r.get('leverage', r.get('leverage_perp'))} n={o['trades']:<4} "
                  f"mean={o.get('mean_net_bps')} lcb={o.get('lcb90_bps')} med={o.get('median_net_bps')} "
                  f"ex2={o.get('mean_ex_best2_bps')} win={o.get('win_rate')} L/S={o.get('longs')}/{o.get('shorts')} "
                  f"fund={o.get('mean_funding_bps')} liq={o.get('liquidations')} ret={o.get('oos_return_pct')}% "
                  f"dd={o.get('max_dd_pct')}% {'PASS' if o.get('passes') else 'fail:' + ','.join(o.get('failed', []))}")
        print("   funding to shorts OOS:", w["carry"]["funding_received_by_short_oos"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
