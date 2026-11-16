"""OI-flush look-ahead re-test (2026-11-16).
Binance USDT-M 'metrics' snapshots are stamped ~5 min early (create_time of the print that reflects OI at HH:00 is HH:55
of the previous hour). load_metrics.py took the last print with create_time in [H, H+1h) as 'known at the close of bar H',
which may include a snapshot that is only published at/after H+1h (the entry open). Re-run the EXACT c1 family
'OI|capit+oiflush|q{0.10,0.25}|h{12,24,48}' (same CAPIT mask, same rq, same simulate, same purged walk-forward, same
DSR N and SR variance as wf_r2.py) under three OI timing assumptions:
  orig : last print with create_time in [H, H+1h)               (as published, for reference)
  p5m  : create_time + 5 min, then last print in [H, H+1h)      (i.e. original stamp in [H-5m, H+55m))
  p1h  : last print with create_time in [H-1h, H)               (conservative: OI fully known before bar H even opens)
Also reports the frozen shadow variant q=0.10/h48 on the full dev window. Research only; nothing is wired anywhere."""
import glob, zipfile, io, os, json, itertools
from concurrent.futures import ProcessPoolExecutor
from r2common import *
from gates import permutation_test
CACHE = "/workspace/edge2-data/oi_5m_raw.pkl"
def _one(c):
    out = []
    for f in sorted(glob.glob(f"/workspace/edge2-data/metrics/{c}USDT-metrics-*.zip")):
        z = zipfile.ZipFile(f); d = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])), usecols=["create_time", "sum_open_interest_value"]); out.append(d)
    d = pd.concat(out); d["t"] = pd.to_datetime(d.create_time, utc=True)
    return c, d.set_index("t").sort_index().sum_open_interest_value.astype(float)
def raw():
    if os.path.exists(CACHE): return pickle.load(open(CACHE, "rb"))
    with ProcessPoolExecutor(6) as ex: R = dict(ex.map(_one, COINS))
    pickle.dump(R, open(CACHE, "wb")); return R
def hourly(s, mode):
    if mode == "p5m": s = s.copy(); s.index = s.index + pd.Timedelta("5min")
    h = s.resample("1h", label="left", closed="left").last()
    return h.shift(1, freq="1h") if mode == "p1h" else h
if __name__ == "__main__":
    P = panel(); K1 = P["k1h"]; R = raw()
    N_TOTAL = json.load(open("wf_r2.json"))["n_total_variants"]; SRVAR = json.load(open("wf_r2.json"))["sr_var"]
    def capit(c):
        k = K1[c]; lv = np.log(k.c); sd = lv.diff().rolling(720, min_periods=360).std().shift(1)
        z = lv.diff(24) / (sd * np.sqrt(24)); vr = k.qv.rolling(24).sum() / (k.qv.rolling(720, min_periods=360).mean().shift(24) * 24)
        return (z <= -2.5) & (vr >= 2.0)
    CAP = {c: capit(c) for c in COINS}
    # sanity: orig mode must reproduce metrics_1h.pkl used in round 2
    M0 = pickle.load(open("/workspace/edge2-data/metrics_1h.pkl", "rb"))
    chk = max(float((hourly(R[c], "orig").reindex(M0[c].index) - M0[c].oi).abs().max()) for c in COINS)
    print("orig vs metrics_1h.pkl max abs diff:", chk, flush=True)
    START = pd.Timestamp("2023-02-01", tz="UTC"); out = {"sanity_maxdiff_vs_round2": chk}
    for mode in ["orig", "p5m", "p1h"]:
        OI = {c: hourly(R[c], mode).reindex(K1[c].index) for c in COINS}
        fam = {}
        for q, hold in itertools.product([0.10, 0.25], [12, 24, 48]):
            f = lambda c, q=q: (lambda d: CAP[c] & (d <= rq(d, q)))(OI[c].pct_change(24, fill_method=None))
            t, _ = run_family(f, K1, hold); fam[f"OI|capit+oiflush|q{q}|h{hold}"] = t
        res = {}
        for cost in ["bus_base", "krk_maker"]:
            oos, picks = walk_forward(fam, cost); s = wf_summary(oos, cost)
            dc = (oos.gross - COSTS[cost]).groupby(oos.entry.dt.floor("D")).mean()
            s["dsr"] = deflated_sharpe(dc.values, N_TOTAL, SRVAR)
            oos2 = oos.copy(); oos2["exit"] = pd.to_datetime(oos2.exit, utc=True); oos2["entry"] = pd.to_datetime(oos2.entry, utc=True)
            s["perm"] = permutation_test(oos2, FOLDS[4].start_time.tz_localize("UTC"), DEV_END, cost)
            s["y2026"] = [int((oos.entry >= "2026-01-01").sum()), round(1e4 * (oos[oos.entry >= "2026-01-01"].gross - COSTS[cost]).mean(), 1)]
            s["picks"] = [p[1] for p in picks]; res[f"wf|{cost}"] = s
            # frozen shadow variant, full dev window
            fz = fam["OI|capit+oiflush|q0.1|h48"]; fz = fz[(fz.entry >= START) & (fz.entry < DEV_END)]
            st = stats_of(fz.gross - COSTS[cost]); fs = (fz.gross - COSTS[cost]).groupby(quarter(fz.entry)).sum().reindex(FOLDS).fillna(0)
            dcz = (fz.gross - COSTS[cost]).groupby(fz.entry.dt.floor("D")).mean()
            res[f"frozen_q0.1_h48|{cost}"] = dict(n=st["n"], pf=st["pf"], win=st["win"], mean_bps=st["mean_bps"], folds=f"{int((fs > 0).sum())}/15",
                                                 perm=permutation_test(fz, START, DEV_END, cost), dsr=deflated_sharpe(dcz.values, N_TOTAL, SRVAR))
            print(mode, cost, "WF n", s["n"], "pf %.2f" % s["pf"], "win %.3f" % s["win"], s["folds"], "dsr %.3f" % s["dsr"], "p", s["perm"]["p_value"],
                  "| frozen n", st["n"], "pf %.2f" % st["pf"], res[f"frozen_q0.1_h48|{cost}"]["folds"], flush=True)
        out[mode] = res
        fam_ids = {k: set(zip(v.coin, v.entry.astype(str))) for k, v in fam.items()}; out[mode]["fam_ids_q0.1_h48"] = len(fam_ids["OI|capit+oiflush|q0.1|h48"])
        if mode == "orig": base_ids = fam_ids
        else: out[mode]["overlap_q0.1_h48_vs_orig"] = len(fam_ids["OI|capit+oiflush|q0.1|h48"] & base_ids["OI|capit+oiflush|q0.1|h48"])
    json.dump(out, open("oi_lookahead_fix.json", "w"), indent=1, default=str)
