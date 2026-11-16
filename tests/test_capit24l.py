"""CAPIT-24L paper runner: signal, strict-below limit fill, touch logging, 60-min expiry, 48h exit,
slots, $70 floor, kill rules (K1/K2/K4), idempotency, no order endpoints. No network."""
from __future__ import annotations

import inspect

import numpy as np
import pandas as pd

from dublin_bot import capit24l as L

T0 = pd.Timestamp("2026-01-01T00:00:00Z")
NB = 1000


def bar(i):
    return T0 + pd.Timedelta(f"{int(i)}h")


def world(crashes, seed=1):
    rng = np.random.default_rng(seed)
    out = {}
    for c in L.COINS:
        r = rng.normal(0, 0.004, NB)
        qv = np.full(NB, 1000.0)
        for end, step in crashes.get(c, []):
            r[end - 23:end + 1] = step
            qv[end - 23:end + 1] = 6000.0
        close = np.exp(np.cumsum(r))
        opn = np.r_[close[0], close[:-1]]
        idx = pd.date_range(T0, periods=NB, freq="1h", tz="UTC")
        ct = (idx.astype("int64") // 10**6) + 3_599_999
        out[c] = pd.DataFrame({"o": opn, "h": np.maximum(opn, close), "l": np.minimum(opn, close),
                               "c": close, "qv": qv, "ct": ct.astype(float)}, index=idx)
    return out


def m1_flat(start, n, price, low=None, low_at=None):
    idx = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    df = pd.DataFrame({"o": price, "h": price, "l": price, "c": price}, index=idx)
    if low is not None:
        df.iloc[low_at, df.columns.get_loc("l")] = low
    return df


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def runner(tmp_path, w, clock, m1fn, **kw):
    def f1h(c):
        d = w[c]
        return d[d.index <= clock()]
    return L.Capit24LPaper(tmp_path, fetch_1h=f1h, fetch_m1=m1fn, now=clock, **kw)


# ── pure pieces ──

def test_constants_frozen():
    assert L.COINS == ("VET", "HBAR", "ALGO", "XLM")
    assert (L.LIMIT_OFFSET, L.WINDOW_MIN, L.HOLD_H, L.MAX_POS, L.SLOT_USD, L.FLOOR_USD) == (0.005, 60, 48, 2, 50.0, 70.0)
    assert round(L.COSTS["binanceus_l"] * 1e4) == 5 and round(L.COSTS["kraken125"] * 1e4) == 125
    assert L.limit_price(1.0) == 0.995


def test_strict_below_fills_touch_does_not():
    t = pd.Timestamp("2026-02-01T10:00:00Z")
    lim = 0.995
    touch = L.evaluate_limit(m1_flat(t, 60, 1.0, low=0.995, low_at=10), t, lim)
    assert touch["status"] == "touch" and touch["touched"] and "fill_px" not in touch
    fill = L.evaluate_limit(m1_flat(t, 60, 1.0, low=0.9949, low_at=10), t, lim)
    assert fill["status"] == "filled" and fill["fill_px"] == lim
    assert fill["fill_t"] == t + pd.Timedelta(minutes=11)        # close of the minute that traded through
    none = L.evaluate_limit(m1_flat(t, 60, 1.0), t, lim)
    assert none["status"] == "unfilled" and not none["touched"]


def test_60_minute_expiry():
    t = pd.Timestamp("2026-02-01T10:00:00Z")
    df = m1_flat(t, 90, 1.0, low=0.90, low_at=60)                # trades through at minute 60 = too late
    assert L.evaluate_limit(df, t, 0.995)["status"] == "unfilled"
    df = m1_flat(t, 90, 1.0, low=0.90, low_at=59)                # last minute of the window
    assert L.evaluate_limit(df, t, 0.995)["status"] == "filled"
    assert L.evaluate_limit(m1_flat(t - pd.Timedelta("2h"), 60, 1.0), t, 0.995)["status"] == "no_data"


def test_gap_down_reprices_post_only_to_placement_price():
    t = pd.Timestamp("2026-02-01T10:00:00Z")
    df = m1_flat(t, 60, 0.98)                                    # opens below the limit
    r = L.evaluate_limit(df, t, 0.995)
    assert r["repriced"] and r["limit_eff"] == 0.98 and r["status"] == "touch"
    df.iloc[5, df.columns.get_loc("l")] = 0.979
    r = L.evaluate_limit(df, t, 0.995)
    assert r["status"] == "filled" and r["fill_px"] == 0.98


def test_exit_48h_after_fill():
    f = pd.Timestamp("2026-02-01T10:17:00Z")
    assert L.exit_time(f) == pd.Timestamp("2026-02-03T10:17:00Z")


def test_floor():
    assert L.floor_hit(70.0) and L.floor_hit(69.99) and not L.floor_hit(70.01)


def rows(nets, start="2026-01-05"):
    t0 = pd.Timestamp(start, tz="UTC")
    return [{"net_bps": n, "pnl": 50 * n / 1e4, "ts": (t0 + pd.Timedelta(days=i)).timestamp()} for i, n in enumerate(nets)]


def test_kill_k1():
    now = pd.Timestamp("2026-03-01", tz="UTC").timestamp()
    bad = rows([100] * 10 + [-100] * 20)        # n=30, PF 0.5
    k = L.kill_check(bad, now_ts=now)
    assert k["k1_pf_below_1_10_after_30"] and k["killed"]
    ok = rows([100] * 20 + [-100] * 10)
    assert not L.kill_check(ok, now_ts=now)["k1_pf_below_1_10_after_30"]
    assert not L.kill_check(bad[:29], now_ts=now)["k1_pf_below_1_10_after_30"]


def test_kill_k4_first15():
    now = pd.Timestamp("2026-03-01", tz="UTC").timestamp()
    k = L.kill_check(rows([-150] * 15), now_ts=now)
    assert k["k4_first15_mean_below_minus_100bps"] and k["first15_mean_bps"] == -150
    assert not L.kill_check(rows([-150] * 14), now_ts=now)["k4_first15_mean_below_minus_100bps"]
    assert not L.kill_check(rows([-90] * 15), now_ts=now)["k4_first15_mean_below_minus_100bps"]


def test_kill_k2_two_negative_quarters_below_cash():
    r = rows([-50, -50], start="2026-01-10") + rows([-50, -50], start="2026-04-10")
    now = pd.Timestamp("2026-07-15", tz="UTC").timestamp()
    assert L.kill_check(r, now_ts=now)["k2_two_neg_quarters_and_below_cash"]
    assert not L.kill_check(r, now_ts=pd.Timestamp("2026-05-15", tz="UTC").timestamp())["k2_two_neg_quarters_and_below_cash"]


def test_no_order_endpoints_or_keys():
    src = inspect.getsource(L)
    for bad in ("/api/v3/order", "signature", "X-MBX-APIKEY", "API_SECRET", "dotenv", "ALLOW_LIVE_TRADING ="):
        assert bad not in src


# ── runner end to end ──

def make_m1(w, through=True, touch_only=False):
    """1m bars synthesised from the 1h bar: flat at the 1h open; one dip in minute 7."""
    def fn(c, start, n):
        k = w[c]
        h0 = start.floor("1h")
        if h0 not in k.index:
            return pd.DataFrame(columns=["o", "h", "l", "c"])
        o = float(k.loc[h0, "o"])
        df = m1_flat(start, n, o)
        prev_close = float(k.loc[h0 - pd.Timedelta("1h"), "c"])
        lim = min(prev_close * 0.995, o)
        if n <= 7:
            return df
        if through:
            df.iloc[7, df.columns.get_loc("l")] = lim * 0.999
        elif touch_only:
            df.iloc[7, df.columns.get_loc("l")] = lim
        return df
    return fn


def test_runner_seed_fill_exit_and_idempotent(tmp_path):
    w = world({"HBAR": [(860, -0.02)]})
    clock = Clock(bar(850) + pd.Timedelta(minutes=5))
    r = runner(tmp_path, w, clock, make_m1(w))
    st = r.run_cycle()
    assert st["ok"] and st["open_positions"] == [] and st["orders_placed"] == 0
    clock.t = bar(866)
    st = r.run_cycle()
    pos = [p for p in st["open_positions"] if p["coin"] == "HBAR"]
    assert len(pos) == 1
    p = pos[0]
    place = pd.Timestamp(p["place_t"])
    sig_close = float(w["HBAR"].loc[pd.Timestamp(p["signal_t"]), "c"])
    assert place == pd.Timestamp(p["signal_t"]) + pd.Timedelta("1h")
    assert abs(p["entry_px"] - min(0.995 * sig_close, float(w["HBAR"].loc[place, "o"]))) < 1e-12
    assert pd.Timestamp(p["entry_t"]) == place + pd.Timedelta(minutes=8)
    assert pd.Timestamp(p["exit_t"]) == pd.Timestamp(p["entry_t"]) + pd.Timedelta("48h")
    clock.t = bar(930)
    st = r.run_cycle()
    assert not any(q["coin"] == "HBAR" and q["trade_id"] == p["trade_id"] for q in st["open_positions"])
    tr = [x for x in L._jsonl(tmp_path / "closed_trades.jsonl") if x["trade_id"].startswith(p["trade_id"])]
    assert {x["fee_book"] for x in tr} == set(L.COSTS)
    prim = [x for x in tr if x["fee_book"] == "binanceus_l"][0]
    assert prim["exit_t"] == L._iso(pd.Timestamp(p["exit_t"]))
    assert abs(prim["net_bps"] - (prim["gross_bps"] - 5)) < 0.02
    n1 = len(L._jsonl(tmp_path / "closed_trades.jsonl"))
    s1 = len(L._jsonl(tmp_path / "signals.jsonl"))
    r.run_cycle()
    assert len(L._jsonl(tmp_path / "closed_trades.jsonl")) == n1 and len(L._jsonl(tmp_path / "signals.jsonl")) == s1


def test_runner_touch_only_is_skipped_and_logged(tmp_path):
    w = world({"HBAR": [(860, -0.02)]})
    clock = Clock(bar(850) + pd.Timedelta(minutes=5))
    r = runner(tmp_path, w, clock, make_m1(w, through=False, touch_only=True))
    r.run_cycle()
    clock.t = bar(866)
    st = r.run_cycle()
    assert st["open_positions"] == []
    sig = [s for s in L._jsonl(tmp_path / "signals.jsonl") if s["coin"] == "HBAR"]
    assert sig and all(s["fill"] == "touch" and not s["taken"] for s in sig)
    assert st["fill_counts"]["touch"] >= 1 and st["fill_counts"]["strict"] == 0


def test_runner_waits_for_full_window(tmp_path):
    w = world({"HBAR": [(860, -0.02)]})
    clock = Clock(bar(850) + pd.Timedelta(minutes=5))
    r = runner(tmp_path, w, clock, make_m1(w))
    r.run_cycle()
    first = None
    for s in range(851, 866):          # find the first HBAR signal bar
        clock.t = bar(s + 1) + pd.Timedelta(minutes=5)   # bar s closed, its window not done yet
        r.run_cycle()
        if any(x["coin"] == "HBAR" for x in L._jsonl(tmp_path / "signals.jsonl")):
            first = s
            break
    assert first is not None
    sig = [x for x in L._jsonl(tmp_path / "signals.jsonl") if x["coin"] == "HBAR"][0]
    # decided only once now >= signal bar + 2h (window [t+1h, t+2h) fully closed)
    assert clock.t >= pd.Timestamp(sig["signal_t"]) + pd.Timedelta("2h")


def test_runner_max_two_slots_one_per_coin(tmp_path):
    w = world({"VET": [(860, -0.02)], "HBAR": [(860, -0.021)], "ALGO": [(860, -0.022)]})
    clock = Clock(bar(850) + pd.Timedelta(minutes=5))
    r = runner(tmp_path, w, clock, make_m1(w))
    r.run_cycle()
    clock.t = bar(866)
    st = r.run_cycle()
    coins = [p["coin"] for p in st["open_positions"]]
    assert len(coins) == 2 and len(set(coins)) == 2
    reasons = {s.get("reason") for s in L._jsonl(tmp_path / "signals.jsonl") if not s["taken"]}
    assert "max 2 positions" in reasons


def test_runner_floor_and_kill_block_entries(tmp_path):
    w = world({"HBAR": [(860, -0.02)]})
    clock = Clock(bar(850) + pd.Timedelta(minutes=5))
    r = runner(tmp_path, w, clock, make_m1(w))
    r.run_cycle()
    st = L._read_json(tmp_path / "state.json", {})
    st["floor_halt"] = True
    L._write_json(tmp_path / "state.json", st)
    clock.t = bar(866)
    st = r.run_cycle()
    assert st["open_positions"] == [] and st["floor_halt"]
    assert any("floor" in (s.get("reason") or "") for s in L._jsonl(tmp_path / "signals.jsonl"))


def test_runner_floor_trips_on_equity(tmp_path):
    # seed $100 but pretend realized losses of $31 -> equity $69 -> FLOOR_HALT event
    w = world({})
    clock = Clock(bar(850) + pd.Timedelta(minutes=5))
    r = runner(tmp_path, w, clock, make_m1(w))
    r.run_cycle()
    L._append(tmp_path / "closed_trades.jsonl", {"trade_id": "x", "fee_book": "binanceus_l", "net_bps": -6200,
                                                "pnl": -31.0, "ts": bar(851).timestamp(), "sleeve": "capit24l_binanceus"})
    clock.t = bar(856)
    st = r.run_cycle()
    assert st["floor_halt"] and any(e["event"] == "FLOOR_HALT" for e in L._jsonl(tmp_path / "events.jsonl"))
