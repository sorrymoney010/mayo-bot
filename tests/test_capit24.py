"""CAPIT-24 paper runner: frozen rule, fills, fee books, kill rules, idempotency. No network."""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd

from dublin_bot import capit24 as cp
from dublin_bot.promotion import build_report, dedup_trades, load_jsonl

T0 = pd.Timestamp("2026-01-01T00:00:00Z")
NBARS = 1000


def _world(crashes: dict[str, list[tuple[int, float]]], seed=1):
    """crashes: coin -> [(end_bar_index, per-bar log return over 24 bars)]"""
    rng = np.random.default_rng(seed)
    out = {}
    for i, c in enumerate(cp.COINS):
        r = rng.normal(0, 0.004, NBARS)
        qv = np.full(NBARS, 1000.0)
        for end, step in crashes.get(c, []):
            r[end - 23:end + 1] = step
            qv[end - 23:end + 1] = 6000.0
        close = 1.0 * np.exp(np.cumsum(r))
        opn = np.r_[close[0], close[:-1]]
        idx = pd.date_range(T0, periods=NBARS, freq="1h", tz="UTC")
        ct = (idx.astype("int64") // 10**6) + 3_599_999
        out[c] = pd.DataFrame({"o": opn, "h": np.maximum(opn, close), "l": np.minimum(opn, close),
                               "c": close, "qv": qv, "ct": ct.astype(float)}, index=idx)
    return out


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _runner(tmp_path, world, clock):
    def fetch(c):
        d = world[c]
        return d[d.index <= clock()]
    return cp.CapitPaper(tmp_path, fetch=fetch, fetch_r1=lambda: None, now=clock)


def bar(i):
    return T0 + pd.Timedelta(f"{int(i)}h")


def test_features_match_frozen_formula():
    w = _world({"VET": [(900, -0.02)]})
    k = w["VET"]
    f = cp.features(k)
    lv = np.log(k.c)
    sd = lv.diff().rolling(720, min_periods=360).std().shift(1)
    z = lv.diff(24) / (sd * math.sqrt(24))
    np.testing.assert_allclose(f["z"].dropna(), z.dropna())
    assert cp.is_signal(f.at[bar(900), "z"], f.at[bar(900), "volr"])
    assert not cp.is_signal(f.at[bar(800), "z"], f.at[bar(800), "volr"])
    assert (cp.Z, cp.VM, cp.N, cp.HOLD_H, cp.MAX_POS) == (2.5, 2.0, 24, 48, 2)
    assert cp.COINS == ("VET", "HBAR", "ALGO", "XLM")


def test_seed_no_backfill_then_entry_next_open_exit_48h(tmp_path):
    w = _world({"HBAR": [(860, -0.02)]})
    clock = Clock(bar(850) + pd.Timedelta(minutes=5))      # bar 849 is the last closed bar
    r = _runner(tmp_path, w, clock)
    st = r.run_cycle()
    assert st["ok"] and st["seed_cursor"] == cp._iso(bar(849)) and st["open_positions"] == []
    clock.t = bar(862) + pd.Timedelta(minutes=2)
    st = r.run_cycle()
    pos = [p for p in st["open_positions"] if p["coin"] == "HBAR"]
    assert len(pos) >= 1
    p = pos[0]
    sig_bar = cp._ts(p["signal_t"])
    assert cp._ts(p["entry_t"]) == sig_bar + pd.Timedelta("1h")
    assert p["entry_px"] == w["HBAR"].loc[cp._ts(p["entry_t"]), "o"]
    assert cp._ts(p["exit_t"]) == cp._ts(p["entry_t"]) + pd.Timedelta(hours=48)
    clock.t = cp._ts(p["exit_t"]) + pd.Timedelta(minutes=1)
    st = r.run_cycle()
    rows = load_jsonl(tmp_path / "closed_trades.jsonl")
    mine = [x for x in rows if x["coin"] == "HBAR" and x["entry_t"] == p["entry_t"]]
    assert {x["fee_book"] for x in mine} == {"binanceus", "kraken80"}
    bus = next(x for x in mine if x["fee_book"] == "binanceus")
    krk = next(x for x in mine if x["fee_book"] == "kraken80")
    assert bus["exit_px"] == w["HBAR"].loc[cp._ts(p["exit_t"]), "o"]
    assert round(bus["net_bps"] - krk["net_bps"], 2) == 70.0       # 10 bps vs 80 bps
    assert bus["net_bps"] == round(bus["gross_bps"] - 10.0, 2)
    assert bus["trade_id"] and krk["trade_id"] == bus["trade_id"] + ":kraken80"
    assert bus["notional_usd"] == 50.0 and bus["source"] == "fill"
    assert json.loads((tmp_path / "status.json").read_text())["orders_placed"] == 0


def test_idempotent_rerun_and_promotion_compatible(tmp_path):
    w = _world({"XLM": [(860, -0.02)]})
    clock = Clock(bar(850) + pd.Timedelta(minutes=5))
    r = _runner(tmp_path, w, clock)
    r.run_cycle()
    clock.t = bar(990) + pd.Timedelta(minutes=5)
    r.run_cycle()
    n1 = len(load_jsonl(tmp_path / "closed_trades.jsonl"))
    s1 = len(load_jsonl(tmp_path / "signals.jsonl"))
    r.run_cycle()
    r.run_cycle()
    rows = load_jsonl(tmp_path / "closed_trades.jsonl")
    assert len(rows) == n1 > 0 and len(load_jsonl(tmp_path / "signals.jsonl")) == s1
    assert len(dedup_trades(rows)) == len(rows)
    rep = build_report(tmp_path)
    assert set(rep["strategies"]) == {"capit24_binanceus", "capit24_kraken80_shadow"}
    assert rep["paper_book"]["seed"] == 100.0


def test_max_two_slots_ties_most_negative_z(tmp_path):
    w = _world({"VET": [(860, -0.020)], "HBAR": [(860, -0.030)], "ALGO": [(860, -0.025)]})
    clock = Clock(bar(850) + pd.Timedelta(minutes=5))
    r = _runner(tmp_path, w, clock)
    r.run_cycle()
    clock.t = bar(862) + pd.Timedelta(minutes=5)
    st = r.run_cycle()
    assert len(st["open_positions"]) == 2
    assert {p["coin"] for p in st["open_positions"]} == {"HBAR", "ALGO"}
    sigs = load_jsonl(tmp_path / "signals.jsonl")
    assert any(s["coin"] == "VET" and not s["taken"] and s["reason"] == "max 2 positions" for s in sigs)


def _rows(n, net_bps, ts):
    return [{"net_bps": net_bps, "pnl": 0.5 * net_bps / 1e4 * 100, "ts": ts} for _ in range(n)]


def test_kill_rules():
    now = pd.Timestamp("2026-11-15T00:00:00Z").timestamp()
    ok = cp.kill_check(_rows(29, -50, now), now_ts=now)
    assert not ok["killed"]                                     # < 30 trades: K1 not armed
    k1 = cp.kill_check(_rows(20, 100, now) + _rows(20, -100, now), now_ts=now)
    assert k1["k1_pf_below_1_10_after_30"] and k1["killed"]     # PF 1.0 < 1.10
    q2 = pd.Timestamp("2026-05-01T00:00:00Z").timestamp()
    q3 = pd.Timestamp("2026-08-01T00:00:00Z").timestamp()
    k2 = cp.kill_check(_rows(3, -100, q2) + _rows(3, -100, q3), now_ts=now)
    assert k2["k2_two_neg_quarters_and_below_cash"] and k2["killed"]
    nk = cp.kill_check(_rows(3, -100, q2) + _rows(3, -100, q3) + _rows(1, 9000, now), now_ts=now)
    assert not nk["killed"]                                     # above cash -> no K2 kill


def test_primary_kill_blocks_new_entries(tmp_path):
    w = _world({"VET": [(860, -0.02)]})
    clock = Clock(bar(850) + pd.Timedelta(minutes=5))
    r = _runner(tmp_path, w, clock)
    r.run_cycle()
    st = json.loads((tmp_path / "state.json").read_text())
    st["killed"]["binanceus"] = True
    (tmp_path / "state.json").write_text(json.dumps(st))
    clock.t = bar(862) + pd.Timedelta(minutes=5)
    out = r.run_cycle()
    assert out["open_positions"] == []
    assert all(not s["taken"] for s in load_jsonl(tmp_path / "signals.jsonl"))


def test_fetch_failure_aborts_without_partial_state(tmp_path):
    w = _world({})
    clock = Clock(bar(850) + pd.Timedelta(minutes=5))
    r = _runner(tmp_path, w, clock)
    r.run_cycle()
    before = (tmp_path / "state.json").read_text()

    def boom(c):
        raise RuntimeError("binance.us down")
    r.fetch = boom
    clock.t = bar(900)
    try:
        r.run_cycle()
    except RuntimeError:
        pass
    assert (tmp_path / "state.json").read_text() == before
    assert json.loads((tmp_path / "status.json").read_text())["ok"] is False


def test_module_has_no_order_path():
    src = open(cp.__file__).read()
    for bad in ("api/v3/order", "X-MBX-APIKEY", "signature", "PAPER_TRADING", "ALLOW_LIVE_TRADING", ".env"):
        assert bad not in src.replace("never reads keys or .env", "").replace(
            "PAPER_TRADING / DRY_RUN / ALLOW_LIVE_TRADING", "")
