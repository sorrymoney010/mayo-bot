#!/usr/bin/env python3
"""Daily time-series trend-following quick check: BTC, ETH, SOL, $100 Kraken spot.

Research only. Public daily OHLC. No keys, no live flags, no process restarts.
Primary rule is a close copy of Zarattini, Pagani, Barbon 2025,
"Catching Crypto Trends" (SSRN 5209907 / SFI RP 25-80).
"""
from __future__ import annotations
import json, math, sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import (
    ASSETS, SYM, COSTS, START_EQUITY, END, DEV_END, TEST_START, YTD_START,
    ORDERMIN, COSTMIN, N_PERM, PERM_SEED, REBAL_THRESH,
    load_vision, load_kraken, Book, decided_for_variant, metrics_from_run,
    yearly_folds, buy_and_hold, permute_ohlc, btc_only_paper_replication,
    passes_bar, json_safe, fmt_pct, fmt_num, fmt_pf,
)

OUT_REPORT = Path("/workspace/mayo-bot/reports/daily_trend_quickcheck_2026-11-15.md")
OUT_JSON = Path("/workspace/mayo-bot/research/daily_trend/results.json")

VARIANTS = [
    dict(id="z_combo_vol25_cap100", name="Zarattini Combo 25% vol, 100% cap",
         kind="combo", use_vol=True, cap=1.0, thresh=REBAL_THRESH, primary=True),
    dict(id="z_combo_vol25_cap200", name="Zarattini Combo 25% vol, 200% cap (paper, not spot-feasible)",
         kind="combo", use_vol=True, cap=2.0, thresh=REBAL_THRESH, primary=False),
    dict(id="z_combo_eq", name="Zarattini Combo equal-weight (no vol target)",
         kind="combo", use_vol=False, cap=1.0, thresh=None, primary=False),
    dict(id="sma50_vol", name="Close > SMA 50, 25% vol, 100% cap",
         kind="sma", n=50, use_vol=True, cap=1.0, thresh=REBAL_THRESH),
    dict(id="sma100_vol", name="Close > SMA 100, 25% vol, 100% cap",
         kind="sma", n=100, use_vol=True, cap=1.0, thresh=REBAL_THRESH),
    dict(id="sma200_vol", name="Close > SMA 200, 25% vol, 100% cap",
         kind="sma", n=200, use_vol=True, cap=1.0, thresh=REBAL_THRESH),
    dict(id="sma100_eq", name="Close > SMA 100, equal 1/N (no vol target)",
         kind="sma", n=100, use_vol=False, cap=1.0, thresh=None),
    dict(id="don20_exit10_vol", name="Donchian 20-high entry / 10-low exit, 25% vol",
         kind="don", n_in=20, n_out=10, use_vol=True, cap=1.0, thresh=REBAL_THRESH),
    dict(id="don20_exit20_vol", name="Donchian 20-high entry / 20-low exit, 25% vol",
         kind="don", n_in=20, n_out=20, use_vol=True, cap=1.0, thresh=REBAL_THRESH),
    dict(id="don50_exit10_vol", name="Donchian 50-high entry / 10-low exit, 25% vol",
         kind="don", n_in=50, n_out=10, use_vol=True, cap=1.0, thresh=REBAL_THRESH),
    dict(id="don50_exit20_vol", name="Donchian 50-high entry / 20-low exit, 25% vol",
         kind="don", n_in=50, n_out=20, use_vol=True, cap=1.0, thresh=REBAL_THRESH),
    dict(id="don50_exit20_eq", name="Donchian 50-high entry / 20-low exit, equal 1/N",
         kind="don", n_in=50, n_out=20, use_vol=False, cap=1.0, thresh=None),
]

def unf_stats(unf, fills):
    n_f, n_u = len(fills), len(unf)
    turn = sum(x["notional"] for x in fills) or 0.0
    turn_u = sum(x["notional"] for x in unf) or 0.0
    notionals = [x["notional"] for x in fills if x["notional"] > 0]
    return dict(
        n_fills=n_f, n_unfillable=n_u,
        unfillable_frac=(n_u / n_f if n_f else float("nan")),
        unfillable_turnover_frac=(turn_u / turn if turn else float("nan")),
        median_notional=float(np.median(notionals)) if notionals else float("nan"),
        min_notional_seen=float(min(notionals)) if notionals else float("nan"),
        max_notional_seen=float(max(notionals)) if notionals else float("nan"),
    )

def kraken_vs_binance_basis(bn, kr):
    out = {}
    for a in ASSETS:
        j = bn[a].join(kr[a], lsuffix="_bn", rsuffix="_kr", how="inner")
        if j.empty:
            out[a] = dict(n=0)
            continue
        basis = j["c_kr"] / j["c_bn"] - 1.0
        out[a] = dict(n=int(len(j)), start=str(j.index[0].date()), end=str(j.index[-1].date()),
                      median_bps=float(basis.median() * 1e4),
                      mean_abs_bps=float(basis.abs().mean() * 1e4),
                      max_abs_bps=float(basis.abs().max() * 1e4))
    return out

def main():
    print("Loading Binance Vision daily bars...")
    ohlc = {a: load_vision(SYM[a]) for a in ASSETS}
    for a, df in ohlc.items():
        gaps = pd.date_range(df.index.min(), df.index.max(), freq="D").difference(df.index)
        print(f"  {a}: {df.index.min().date()} -> {df.index.max().date()}  n={len(df)}  gaps={len(gaps)}")
    print("Loading Kraken public OHLC...")
    kr = {a: load_kraken(a) for a in ASSETS}
    for a, df in kr.items():
        print(f"  Kraken {a}: {df.index.min().date()} -> {df.index.max().date()}  n={len(df)}")
    basis = kraken_vs_binance_basis(ohlc, kr)
    print("Basis bps:", basis)

    cal = pd.DatetimeIndex(sorted(set().union(*[set(df.index) for df in ohlc.values()])))
    cal = cal[cal <= END]
    ohlc_r = {}
    for a, df in ohlc.items():
        ohlc_r[a] = df.reindex(cal)
        listed = ohlc_r[a]["c"].first_valid_index()
        if listed is not None:
            ohlc_r[a].loc[listed:] = ohlc_r[a].loc[listed:].ffill()

    opens = {a: ohlc_r[a]["o"].to_numpy() for a in ASSETS}
    closes = {a: ohlc_r[a]["c"].to_numpy() for a in ASSETS}
    book = Book(cal, opens, closes, {a: np.isfinite(opens[a]) for a in ASSETS})
    last_px = {a: float(ohlc[a]["c"].iloc[-1]) for a in ASSETS}
    min_notional_now = {a: max(COSTMIN[a], ORDERMIN[a] * last_px[a]) for a in ASSETS}
    print("Last closes", last_px, "min notionals", min_notional_now)

    decided_map, ready_map = {}, {}
    for v in VARIANTS:
        print("Signals", v["id"], "...")
        d, r = decided_for_variant(v, ohlc_r)
        decided_map[v["id"]], ready_map[v["id"]] = d, r

    results = {}
    for v in VARIANTS:
        results[v["id"]] = {"meta": {k: v[k] for k in v}}
        book.ready = ready_map[v["id"]]
        for cname, rt in COSTS.items():
            print(f"  run {v['id']} {cname}")
            run = book.run(decided_map[v["id"]], rt_cost=rt, start_equity=START_EQUITY, start=None, end=END)
            run_test = book.run(decided_map[v["id"]], rt_cost=rt, start_equity=START_EQUITY,
                                start=TEST_START, end=END)
            full = metrics_from_run(run, cal)
            dev = metrics_from_run(run, cal, end=DEV_END)
            test = metrics_from_run(run, cal, start=TEST_START, end=END)
            ytd = metrics_from_run(run, cal, start=YTD_START, end=END)
            test100 = metrics_from_run(run_test, cal, start=TEST_START, end=END)
            folds_full = yearly_folds(run, cal)
            folds_test = yearly_folds(run_test, cal, start=TEST_START, end=END)
            results[v["id"]][cname] = dict(
                full=full, dev=dev, test=test, ytd2026=ytd, test100=test100,
                folds_full=folds_full, folds_test=folds_test,
                bar_test=passes_bar(test, yearly_folds(run, cal, start=TEST_START, end=END)),
                bar_full=passes_bar(full, folds_full),
                bar_test100=passes_bar(test100, folds_test),
                unfillable_full=unf_stats(run.get("unfillable") or [], run.get("fills") or []),
                unfillable_test100=unf_stats(run_test.get("unfillable") or [], run_test.get("fills") or []),
            )
            if cname == "80bps":
                results[v["id"]]["trades_test100_80bps"] = [
                    {k: t[k] for k in t if k not in ("entry_i", "exit_i")}
                    for t in (run_test.get("trades") or [])
                ]

    bh = {}
    for cname, rt in list(COSTS.items()) + [("0bps", 0.0)]:
        bh[cname] = dict(
            full=buy_and_hold(book, rt, cal[0], END),
            dev=buy_and_hold(book, rt, cal[0], DEV_END),
            test=buy_and_hold(book, rt, TEST_START, END),
            ytd2026=buy_and_hold(book, rt, YTD_START, END),
        )
    sol_ready = ohlc["SOL"].index.min() + pd.Timedelta(days=360)
    common_start = max(sol_ready, pd.Timestamp("2021-08-11"))
    bh["80bps_common"] = buy_and_hold(book, COSTS["80bps"], common_start, END)
    bh["0bps_common"] = buy_and_hold(book, 0.0, common_start, END)

    print("BTC-only paper replication...")
    btc_rep = btc_only_paper_replication(ohlc["BTC"])
    print(btc_rep)

    def score_test_80(vid):
        m = results[vid]["80bps"]["test100"]
        if m.get("empty"):
            return (-9, -9)
        pf = m.get("tr_pf", float("nan"))
        if pf is None or (isinstance(pf, float) and math.isnan(pf)):
            pf = -9
        return (pf, m.get("sharpe", -9) or -9)

    primary_id = "z_combo_vol25_cap100"
    best_id = max((v["id"] for v in VARIANTS), key=score_test_80)
    print("Best variant on test $100 80bps:", best_id, score_test_80(best_id))

    # persist intermediate results before the slow permutation so a crash is not a total loss
    payload = dict(
        asof="2026-11-15",
        source={
            "prices": "Binance Vision spot 1d klines, last complete bar 2026-11-14. api.binance.com returned HTTP 451; Vision S3 used instead.",
            "kraken": "api.kraken.com/0/public/OHLC interval=1440, 721 bars 2024-11-25..2026-11-15 (15th dropped). AssetPairs fetched same day.",
            "paper": "Zarattini, Pagani, Barbon 2025, Catching Crypto Trends, SSRN 5209907, SFI RP 25-80, 2025-04-09.",
        },
        last_px=last_px, min_notional_now=min_notional_now, ordermin=ORDERMIN, costmin=COSTMIN,
        costs=COSTS, basis=basis, common_start=str(common_start.date()),
        btc_paper_replication_10bps=btc_rep, variants=results, buy_and_hold=bh,
        permutation={}, kraken_robustness_primary_80bps={},
        best_test100_80bps=best_id, primary=primary_id, n_variants=len(VARIANTS),
        execution="Signal at close t, fill at next open t+1, daily return is open-to-open. Cost = (round-trip/2)*|dw|.",
    )
    OUT_JSON.write_text(json.dumps(json_safe(payload), indent=2))
    print("Wrote intermediate", OUT_JSON)

    # permutation
    perm_ids = [primary_id] + ([best_id] if best_id != primary_id else [])
    rng = np.random.default_rng(PERM_SEED)
    o_np = {a: ohlc_r[a]["o"].to_numpy().copy() for a in ASSETS}
    h_np = {a: ohlc_r[a]["h"].to_numpy().copy() for a in ASSETS}
    l_np = {a: ohlc_r[a]["l"].to_numpy().copy() for a in ASSETS}
    c_np = {a: ohlc_r[a]["c"].to_numpy().copy() for a in ASSETS}
    perm_out = {}
    for vid in perm_ids:
        v = next(x for x in VARIANTS if x["id"] == vid)
        actual = results[vid]["80bps"]["test100"]
        act_sharpe, act_cagr = actual.get("sharpe", float("nan")), actual.get("cagr", float("nan"))
        act_pf, act_ret = actual.get("tr_pf", float("nan")), actual.get("tot_ret", float("nan"))
        print(f"Permutation {vid} ({N_PERM} runs)...")
        beat_s = beat_c = beat_p = beat_r = valid = 0
        sharpe_list = []
        for i in range(N_PERM):
            ohlc_p = {}
            for a in ASSETS:
                c = c_np[a]
                finite = np.isfinite(c) & np.isfinite(o_np[a])
                sl = np.where(finite)[0]
                s0, s1 = int(sl[0]), int(sl[-1]) + 1
                no, nh, nl, nc = permute_ohlc(o_np[a][s0:s1], h_np[a][s0:s1], l_np[a][s0:s1], c_np[a][s0:s1], rng)
                df = ohlc_r[a].copy()
                df.iloc[s0:s1, df.columns.get_loc("o")] = no
                df.iloc[s0:s1, df.columns.get_loc("h")] = nh
                df.iloc[s0:s1, df.columns.get_loc("l")] = nl
                df.iloc[s0:s1, df.columns.get_loc("c")] = nc
                ohlc_p[a] = df
            d, rdy = decided_for_variant(v, ohlc_p)
            book_p = Book(cal, {a: ohlc_p[a]["o"].to_numpy() for a in ASSETS},
                          {a: ohlc_p[a]["c"].to_numpy() for a in ASSETS}, rdy)
            run_p = book_p.run(d, rt_cost=COSTS["80bps"], start_equity=START_EQUITY,
                               start=TEST_START, end=END)
            m = metrics_from_run(run_p, cal, start=TEST_START, end=END)
            if m.get("empty") or not np.isfinite(m.get("sharpe", np.nan)):
                continue
            valid += 1
            sharpe_list.append(m["sharpe"])
            if m["sharpe"] >= act_sharpe:
                beat_s += 1
            if np.isfinite(m.get("cagr", np.nan)) and np.isfinite(act_cagr) and m["cagr"] >= act_cagr:
                beat_c += 1
            if np.isfinite(m.get("tr_pf", np.nan)) and np.isfinite(act_pf) and m["tr_pf"] >= act_pf:
                beat_p += 1
            if np.isfinite(m.get("tot_ret", np.nan)) and np.isfinite(act_ret) and m["tot_ret"] >= act_ret:
                beat_r += 1
            if (i + 1) % 50 == 0:
                print(f"    {i+1}/{N_PERM} valid={valid}")
        perm_out[vid] = dict(
            n=N_PERM, valid=valid,
            p_sharpe=(beat_s + 1) / (valid + 1) if valid else None,
            p_cagr=(beat_c + 1) / (valid + 1) if valid else None,
            p_pf=(beat_p + 1) / (valid + 1) if valid else None,
            p_tot_ret=(beat_r + 1) / (valid + 1) if valid else None,
            actual_sharpe=act_sharpe, actual_cagr=act_cagr, actual_pf=act_pf,
            perm_sharpe_p95=float(np.percentile(sharpe_list, 95)) if sharpe_list else None,
            perm_sharpe_median=float(np.median(sharpe_list)) if sharpe_list else None,
        )
        print(perm_out[vid])
        payload["permutation"] = perm_out
        OUT_JSON.write_text(json.dumps(json_safe(payload), indent=2))

    # Kraken robustness
    print("Kraken robustness...")
    kr_cal_start = max(kr[a].index.min() for a in ASSETS)
    ohlc_k = {}
    for a in ASSETS:
        df = ohlc_r[a].copy()
        kdf = kr[a].reindex(cal)
        overlap = kdf["c"].notna() & (cal >= kr_cal_start)
        for col in ["o", "h", "l", "c", "v"]:
            df.loc[overlap, col] = kdf.loc[overlap, col]
        ohlc_k[a] = df
    vpri = next(x for x in VARIANTS if x["id"] == primary_id)
    d_k, r_k = decided_for_variant(vpri, ohlc_k)
    book_k = Book(cal, {a: ohlc_k[a]["o"].to_numpy() for a in ASSETS},
                  {a: ohlc_k[a]["c"].to_numpy() for a in ASSETS}, r_k)
    run_k = book_k.run(d_k, rt_cost=COSTS["80bps"], start_equity=START_EQUITY,
                       start=max(TEST_START, kr_cal_start), end=END)
    kr_test = metrics_from_run(run_k, cal, start=max(TEST_START, kr_cal_start), end=END)
    payload["permutation"] = perm_out
    payload["kraken_robustness_primary_80bps"] = kr_test
    OUT_JSON.write_text(json.dumps(json_safe(payload), indent=2))
    print("Wrote", OUT_JSON)
    from report import write_report
    write_report(payload, VARIANTS)
    print("Wrote", OUT_REPORT)

if __name__ == "__main__":
    main()
