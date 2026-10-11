"""Order-flow features, the 15m overlay simulator, and the staged auto-promotion."""
from __future__ import annotations

import gzip
import json
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from dublin_bot import flow_promotion as fp
from dublin_bot.backtest_core import Costs, Spec, add_indicators, simulate
from dublin_bot.flow_research import OVERLAYS, SLEEVES, VARIANTS, forward_replay, improvement
from dublin_bot.flow_sim import Overlay, simulate_flow
from dublin_bot.orderflow import (
    ENTRY_FILTERS, aggressor_orders, build_flow_1m, continuous_1m, entry_filter_mask, fine_features,
    minute_features, resample_bars, tf_features, ticks_to_flow_1m,
)

COSTS = Costs(fee_bps=40.0, slippage_bps=10.0, maker_bps=25.0)


def _ticks(rows):
    return pd.DataFrame(rows, columns=["trade_id", "ts", "price", "qty", "side"])


def test_aggressor_orders_group_same_ts_and_side():
    t = _ticks([(1, 10.0, 100, 1, "b"), (2, 10.0, 101, 1, "b"), (3, 10.0, 100, 1, "s"), (4, 11.0, 100, 2, "s")])
    orders, gid = aggressor_orders(t)
    assert list(gid) == [0, 0, 1, 2]
    assert orders["usd"].tolist() == [201.0, 100.0, 200.0]


def test_flow_1m_large_orders_and_no_threshold():
    t = _ticks([(1, 60.0, 100, 1, "b"), (2, 60.0, 100, 9, "b"), (3, 61.0, 100, 1, "s"), (4, 125.0, 100, 1, "s")])
    m = ticks_to_flow_1m(t, 500.0)
    assert m.loc[60, "lbuy_usd"] == 1000.0 and m.loc[60, "lsell_usd"] == 0.0
    assert m.loc[60, "buy_usd"] == 1000.0 and m.loc[60, "sell_usd"] == 100.0
    assert ticks_to_flow_1m(t, None)["lbuy_usd"].sum() == 0.0


def _write_day(d: Path, day: str, t0: float, big: float):
    rows = ["trade_id,ts,price,qty,side,ord_type"]
    tid = int(t0)
    for k in range(200):
        rows.append(f"{tid + k},{t0 + k * 60},{100 + k * 0.01},{1.0},{'b' if k % 2 else 's'},m")
    rows.append(f"{tid + 500},{t0 + 300.5},100,{big},b,m")
    with gzip.open(d / f"{day}.csv.gz", "wt") as fh:
        fh.write("\n".join(rows) + "\n")


def test_large_threshold_uses_only_previous_days(tmp_path):
    _write_day(tmp_path, "2026-06-01", 1780272000.0, 50.0)
    _write_day(tmp_path, "2026-06-02", 1780358400.0, 50.0)
    m1 = build_flow_1m(tmp_path, cache_dir=tmp_path / "cache")
    day1 = m1[m1.index < 1780358400]
    day2 = m1[m1.index >= 1780358400]
    assert day1["lbuy_usd"].sum() == 0.0          # no history -> no large orders (no look-ahead)
    assert day2["lbuy_usd"].sum() >= 5000.0       # day-2 whale is above day-1's 99th percentile
    again = build_flow_1m(tmp_path, cache_dir=tmp_path / "cache")   # cache hit, same result
    pd.testing.assert_frame_equal(m1, again)


def test_entry_filters_fail_closed_without_flow_columns():
    d = pd.DataFrame({"close": [1.0, 2.0]})
    for name in ENTRY_FILTERS:
        assert not entry_filter_mask(d, name).any()
    assert entry_filter_mask(d, None).all()
    with pytest.raises(ValueError):
        entry_filter_mask(d, "nope")


def _synthetic(days=40, seed=3):
    rng = np.random.default_rng(seed)
    n = days * 1440
    start = 1780272000
    drift = np.where((np.arange(n) // 7000) % 2 == 0, 4e-5, -3e-5)
    px = 100 * np.exp(np.cumsum(drift + rng.normal(0, 8e-4, n)))
    vol = rng.lognormal(0, 1, n)
    buy = vol * rng.uniform(0.2, 0.8, n)
    m1 = pd.DataFrame({
        "open": px, "high": px * (1 + np.abs(rng.normal(0, 4e-4, n))), "low": px * (1 - np.abs(rng.normal(0, 4e-4, n))),
        "close": px, "volume": vol, "buy_vol": buy, "sell_vol": vol - buy, "usd": vol * px,
        "buy_usd": buy * px, "sell_usd": (vol - buy) * px, "lbuy_usd": np.where(rng.random(n) < .01, buy * px, 0),
        "lsell_usd": np.where(rng.random(n) < .01, (vol - buy) * px, 0), "orders": 5.0,
    }, index=np.arange(start, start + n * 60, 60))
    c1 = continuous_1m(m1, start, start + n * 60)
    mf = minute_features(c1)
    fb = fine_features(c1, mf)
    return c1, mf, fb


@pytest.fixture(scope="module")
def synth():
    return _synthetic()


@pytest.mark.parametrize("sleeve", ["regime@60m", "meanrev_mk@240m", "trendhold@240m"])
def test_baseline_matches_backtest_core(synth, sleeve):
    c1, mf, fb = synth
    cfg = SLEEVES[sleeve]
    tf = cfg["tf"]
    spec = Spec(cfg["spec"].name, {**cfg["spec"].params, "d1": False})
    d = add_indicators(tf_features(c1, mf, fb, tf))
    warm = 210 if tf == 60 else 60
    ref = simulate(d, Spec(spec.name, {**spec.params, "warm": warm}), COSTS)
    mine = simulate_flow(d, fb, spec, COSTS, Overlay(), tf_minutes=tf, warm=warm)
    assert len(ref) > 0
    assert [t.entry_i for t in mine] == [t.entry_i for t in ref]
    for a, b in zip(mine, ref):
        if b.reason in ("chandelier", "regime_chop", "below_ema100", "reverted", "eod_mark"):
            assert a.net == pytest.approx(b.net, abs=1e-9)


def test_partial_exit_legs_sum_to_one(synth):
    c1, mf, fb = synth
    cfg = SLEEVES["trendhold@240m"]
    spec = Spec("trendhold", {"ema_fast": 20, "ema_slow": 100})
    d = add_indicators(tf_features(c1, mf, fb, 240))
    for ov in (Overlay("p", ptp_atr=1.0), Overlay("h", exit_trigger="buy_climax", exit_frac=0.5, min_gain=-1)):
        for t in simulate_flow(d, fb, spec, COSTS, ov, tf_minutes=240, warm=110):
            assert sum(leg[0] for leg in t.legs) == pytest.approx(1.0)


def test_entry_filter_only_removes_trades(synth):
    c1, mf, fb = synth
    spec = Spec("regime", {**SLEEVES["regime@60m"]["spec"].params, "d1": False})
    d = add_indicators(tf_features(c1, mf, fb, 60))
    base = {t.entry_i for t in simulate_flow(d, fb, spec, COSTS, Overlay(), tf_minutes=60)}
    for f in ENTRY_FILTERS:
        filt = simulate_flow(d, fb, spec, COSTS, Overlay(f, entry_filter=f), tf_minutes=60)
        assert len(filt) <= len(base) + 2   # removing a trade can free a later re-entry


def test_variant_registry_is_small_and_named():
    total = sum(len(v) - 1 for v in VARIANTS.values())
    assert total == 35
    assert all(v[0].name == "baseline" for v in VARIANTS.values())


def test_improvement_gate():
    base = {"mean": 10, "oos_return_pct": 1.0, "by_fold": {1: {"mean": 5}, 2: {"mean": 5}, 3: {"mean": 5}, 4: {"mean": 5}}}
    var = {"mean": 20, "oos_return_pct": 2.0, "by_fold": {1: {"mean": 6}, 2: {"mean": 6}, 3: {"mean": 6}, 4: {"mean": 1}},
           "by_coin": {"A": {"mean": 1}, "B": {"mean": 2}, "C": {"mean": -1}}}
    assert improvement(var, base, 5.0)["passes"]
    assert not improvement(var, base, -1.0)["passes"]


# ── staged promotion ───────────────────────────────────────────

def _report(cand: bool, variant="of_no_dist", sleeve="regime@60m"):
    return {"sleeves": {sleeve: {"variants": {
        "baseline": {"n": 30, "mean": 10},
        variant: {"n": 31, "mean": 20, "lb90": 1, "bar": {"passes": cand},
                  "improvement": {"passes": cand}, "shadow_candidate": cand}}}}}


def _paper_settings(**kw):
    return SimpleNamespace(paper_trading=True, dry_run=True, allow_live_trading=False, **kw)


def test_backtest_moves_only_candidates_to_shadow(tmp_path):
    reg_p = tmp_path / "reg.json"
    reg = fp.update_from_backtest(_report(False), path=reg_p, now=1000)
    assert reg["regime@60m|of_no_dist"]["stage"] == "candidate"
    reg = fp.update_from_backtest(_report(True), path=reg_p, now=2000)
    rec = reg["regime@60m|of_no_dist"]
    assert rec["stage"] == "shadow" and rec["shadow_since"] == 2000
    assert rec["overlay"]["entry_filter"] == "of_no_dist"


def _fwd_rows(n, net, t0=3000, sym="BTC/USD"):
    return [{"symbol": sym, "entry_time": t0 + i * 10, "exit_time": t0 + i * 10 + 5, "net_bps": net + (i % 3),
             "pnl": net / 100, "reason": "chandelier"} for i in range(n)]


def test_forward_trades_only_after_shadow_start_and_dedup(tmp_path):
    reg_p, fwd = tmp_path / "reg.json", tmp_path / "fwd.jsonl"
    reg = fp.update_from_backtest(_report(True), path=reg_p, now=3000)
    rows = _fwd_rows(5, 50, t0=2990) + [{**_fwd_rows(1, 50)[0], "reason": "eod_mark", "entry_time": 9999}]
    n = fp.record_forward_trades(reg, {"regime@60m|of_no_dist": {"variant": rows, "baseline": []}}, path=fwd)
    assert n == 4            # 2990 is before shadow start; eod_mark is not a closed trade
    assert fp.record_forward_trades(reg, {"regime@60m|of_no_dist": {"variant": rows, "baseline": []}}, path=fwd) == 0


def test_shadow_to_paper_needs_30_forward_trades_flag_and_paper_mode(tmp_path):
    reg_p, fwd = tmp_path / "reg.json", tmp_path / "fwd.jsonl"
    reg = fp.update_from_backtest(_report(True), path=reg_p, now=3000)
    rid = "regime@60m|of_no_dist"
    fp.record_forward_trades(reg, {rid: {"variant": _fwd_rows(29, 60), "baseline": _fwd_rows(29, 10)}}, path=fwd)
    fp.advance(path=reg_p, forward_path=fwd, settings=_paper_settings(), auto_paper=True, now=5000)
    assert fp.load(reg_p)[rid]["stage"] == "shadow"                     # 29 < 30
    fp.record_forward_trades(fp.load(reg_p), {rid: {"variant": _fwd_rows(31, 60), "baseline": []}}, path=fwd)
    fp.advance(path=reg_p, forward_path=fwd, settings=_paper_settings(), auto_paper=False, now=5000)
    rec = fp.load(reg_p)[rid]
    assert rec["stage"] == "shadow" and rec["paper_ready"] and "off" in rec["paper_blocked"]
    live = SimpleNamespace(paper_trading=False, dry_run=False, allow_live_trading=True)
    fp.advance(path=reg_p, forward_path=fwd, settings=live, auto_paper=True, now=5000)
    assert fp.load(reg_p)[rid]["stage"] == "shadow"                     # never in live mode
    fp.advance(path=reg_p, forward_path=fwd, settings=_paper_settings(), auto_paper=True, now=6000)
    assert fp.load(reg_p)[rid]["stage"] == "paper"


def test_forward_must_beat_baseline(tmp_path):
    reg_p, fwd = tmp_path / "reg.json", tmp_path / "fwd.jsonl"
    reg = fp.update_from_backtest(_report(True), path=reg_p, now=3000)
    rid = "regime@60m|of_no_dist"
    fp.record_forward_trades(reg, {rid: {"variant": _fwd_rows(31, 20), "baseline": _fwd_rows(31, 90)}}, path=fwd)
    fp.advance(path=reg_p, forward_path=fwd, settings=_paper_settings(), auto_paper=True, now=5000)
    assert fp.load(reg_p)[rid]["stage"] == "shadow"


def test_exit_overlays_never_auto_paper(tmp_path):
    reg_p, fwd = tmp_path / "reg.json", tmp_path / "fwd.jsonl"
    reg = fp.update_from_backtest(_report(True, variant="x_climax_all"), path=reg_p, now=3000)
    rid = "regime@60m|x_climax_all"
    fp.record_forward_trades(reg, {rid: {"variant": _fwd_rows(31, 60), "baseline": []}}, path=fwd)
    fp.advance(path=reg_p, forward_path=fwd, settings=_paper_settings(), auto_paper=True, now=5000)
    rec = fp.load(reg_p)[rid]
    assert rec["stage"] == "shadow" and rec["paper_ready"] and "entry filters" in rec["paper_blocked"]


def test_paper_overlay_retires_when_forward_edge_disappears(tmp_path):
    reg_p, fwd = tmp_path / "reg.json", tmp_path / "fwd.jsonl"
    reg = fp.update_from_backtest(_report(True), path=reg_p, now=3000)
    rid = "regime@60m|of_no_dist"
    fp.record_forward_trades(reg, {rid: {"variant": _fwd_rows(31, 60), "baseline": []}}, path=fwd)
    fp.advance(path=reg_p, forward_path=fwd, settings=_paper_settings(), auto_paper=True, now=5000)
    assert fp.load(reg_p)[rid]["stage"] == "paper"
    fp.record_forward_trades(fp.load(reg_p), {rid: {"variant": _fwd_rows(31, -80, t0=6000), "baseline": []}},
                             path=fwd)
    fp.advance(path=reg_p, forward_path=fwd, settings=_paper_settings(), auto_paper=True, now=9000)
    assert fp.load(reg_p)[rid]["stage"] == "retired"


def test_paper_entry_veto(tmp_path, monkeypatch):
    reg_p = tmp_path / "reg.json"
    assert fp.paper_entry_veto("regime@60m", "BTC/USD", settings=_paper_settings(), path=reg_p) is None
    fp.save({"regime@60m|of_no_dist": {"id": "x", "sleeve": "regime@60m", "variant": "of_no_dist",
                                       "stage": "paper", "overlay": {"entry_filter": "of_no_dist"}}}, reg_p)
    now = 1_800_000_000.0
    last_closed = (int(now) // 3600) * 3600 - 3600
    frame = pd.DataFrame({"time": [last_closed], "lnet_z_240": [-2.5]})
    monkeypatch.setattr(fp, "_live_tf_frame", lambda *a, **k: frame)
    assert "vetoed" in fp.paper_entry_veto("regime@60m", "BTC/USD", settings=_paper_settings(), path=reg_p, now=now)
    frame2 = frame.assign(lnet_z_240=[0.5])
    monkeypatch.setattr(fp, "_live_tf_frame", lambda *a, **k: frame2)
    assert fp.paper_entry_veto("regime@60m", "BTC/USD", settings=_paper_settings(), path=reg_p, now=now) is None
    # not in paper mode -> overlay ignored entirely
    live = SimpleNamespace(paper_trading=False, dry_run=False, allow_live_trading=True)
    monkeypatch.setattr(fp, "_live_tf_frame", lambda *a, **k: frame)
    assert fp.paper_entry_veto("regime@60m", "BTC/USD", settings=live, path=reg_p, now=now) is None
    # errors fail open
    monkeypatch.setattr(fp, "_live_tf_frame", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert fp.paper_entry_veto("regime@60m", "BTC/USD", settings=_paper_settings(), path=reg_p, now=now) is None


def test_forward_replay_runs_frozen_overlays(synth):
    c1, mf, fb = synth
    frames = {"BTC/USD": {60: (add_indicators(tf_features(c1, mf, fb, 60)), fb)}}
    reg = {"regime@60m|of_no_dist": {"stage": "shadow", "sleeve": "regime@60m",
                                     "overlay": {"name": "of_no_dist", "entry_filter": "of_no_dist"}},
           "regime@60m|ptp_2atr": {"stage": "candidate", "sleeve": "regime@60m", "overlay": {}}}
    out = forward_replay(reg, frames, COSTS)
    assert set(out) == {"regime@60m|of_no_dist"}
    assert set(out["regime@60m|of_no_dist"]) == {"variant", "baseline"}


def test_overlay_modules_never_touch_locks_or_env():
    root = Path(__file__).resolve().parents[1] / "src" / "dublin_bot"
    for name in ("orderflow.py", "flow_sim.py", "flow_research.py", "flow_promotion.py"):
        src = (root / name).read_text()
        for bad in ("venue_locks", "os.environ[", "putenv", ".env\"", "place_order", "kraken_gateway",
                    "write_text(\"true"):
            assert bad not in src, (name, bad)


def test_pipeline_auto_run_falls_back_to_longest(monkeypatch, tmp_path):
    from dublin_bot.pipeline import history
    idx = pd.DatetimeIndex(pd.to_datetime(
        list(range(0, 100 * 3600, 3600)) + list(range(200 * 3600, 203 * 3600, 3600)), unit="s", utc=True))
    bars = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}, index=idx)

    class FakeBuilder:
        def __init__(self, *_a, **_k):
            pass

        def build(self, *_a, **_k):
            return bars

    monkeypatch.setattr(history, "BarBuilder", FakeBuilder)
    monkeypatch.setattr(history, "TickStore", lambda *_a, **_k: None)
    latest, info = history.load_pipeline_bars(tmp_path, "BTC/USD", 60, now=1e9)
    assert len(latest) == 3 and info["run"] == "latest"
    auto, info = history.load_pipeline_bars(tmp_path, "BTC/USD", 60, now=1e9, run="auto")
    assert len(auto) == 100 and info["run"] == "longest"


def test_research_cycle_daily_with_empty_registry(tmp_path):
    import subprocess
    import sys
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / ".research_cycle_state.json").write_text(json.dumps({"weekly": time.time()}))
    script = Path(__file__).resolve().parents[1] / "scripts" / "research_cycle.py"
    r = subprocess.run([sys.executable, str(script), "--logs", str(logs), "--data", str(tmp_path / "nodata"),
                        "--force-daily"], capture_output=True, text=True, timeout=120,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    assert "daily live=0" in (logs / "research_cycle.log").read_text()
