"""Market-data pipeline: tick store, ticks->bars, technicals, collector, source.

Fully offline: the collector's WebSocket and REST client are fakes, and the
real-data check uses a small committed fixture (2.6k SOL/USD trades + the
matching Kraken OHLC rows).
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from dublin_bot.pipeline import SYMBOLS, canonical, fs_key
from dublin_bot.pipeline.backfill import backfill_range
from dublin_bot.pipeline.bars import BarBuilder, coverage_runs, resample, summarize, ticks_to_1m
from dublin_bot.pipeline.collector import TickCollector, parse_trade
from dublin_bot.pipeline.source import PipelineBarSource, merge_local_over_rest
from dublin_bot.pipeline.tickstore import Tick, TickStore, find_gaps
from dublin_bot.technicals import add_technicals

FIX = Path(__file__).parent / "fixtures"
T0 = 1_790_000_000 - (1_790_000_000 % 14400)  # a 4h-aligned epoch second


def mk(i: int, ts: float, price: float = 100.0, qty: float = 1.0, side: str = "b") -> Tick:
    return Tick(trade_id=i, ts=ts, price=f"{price:.2f}", qty=f"{qty:.4f}", side=side, ord_type="l")


def stream(n_minutes: int, *, start_id: int = 1000, start_ts: float = T0 - 30, per_min: int = 4,
           skip_ids: set[int] | None = None) -> list[Tick]:
    """Synthetic trades: per_min trades every minute, price walking up."""
    out, i = [], start_id
    for m in range(n_minutes):
        for k in range(per_min):
            ts = start_ts + m * 60 + k * (60 / per_min)
            if not (skip_ids and i in skip_ids):
                out.append(mk(i, ts, price=100 + m * 0.1 + k * 0.01, qty=1 + k,
                              side="b" if k % 2 == 0 else "s"))
            i += 1
    return out


# ── universe ────────────────────────────────────────────────────────

def test_universe_is_three_coins_and_aliases_resolve():
    assert SYMBOLS == ("BTC/USD", "ETH/USD", "SOL/USD")
    for alias in ("XBTUSD", "XXBTZUSD", "BTC/USD", "btcusd"):
        assert canonical(alias) == "BTC/USD"
    assert canonical("XETHZUSD") == "ETH/USD"
    assert canonical("PUMP/USD") is None
    with pytest.raises(ValueError):
        fs_key("PUMP/USD")


# ── tick store ──────────────────────────────────────────────────────

def test_tickstore_append_dedup_and_torn_line(tmp_path):
    st = TickStore(tmp_path)
    ticks = stream(3)
    st.append("BTC/USD", ticks[:6])
    st.append("BTC/USD", ticks[4:])          # overlap -> duplicates on disk
    day_file = st.day_files("BTC/USD")[0]
    with day_file.open("a") as fh:
        fh.write("1999,17900")                # torn final line (crash mid-write)
    df = st.read("BTC/USD")
    assert list(df["trade_id"]) == [t.trade_id for t in ticks]
    assert df["trade_id"].is_unique and df["trade_id"].is_monotonic_increasing
    assert st.last_tick("BTC/USD") == (ticks[-1].trade_id, pytest.approx(ticks[-1].ts))
    assert find_gaps(df) == []


def test_tickstore_splits_days_and_compacts_to_gzip(tmp_path):
    st = TickStore(tmp_path)
    day_end = T0 - (T0 % 86400) + 86400
    ticks = [mk(1, day_end - 10), mk(2, day_end - 1), mk(3, day_end + 5)]
    st.append("ETH/USD", ticks)
    st.append("ETH/USD", [ticks[0]])          # dup inside the closed day
    names = [p.name for p in st.day_files("ETH/USD")]
    assert len(names) == 2 and all(n.endswith(".csv") for n in names)
    done = st.compact("ETH/USD", now=day_end + 60)
    assert len(done) == 1
    names = sorted(p.name for p in st.day_files("ETH/USD"))
    assert names[0].endswith(".csv.gz") and names[1].endswith(".csv")
    with gzip.open(st.day_files("ETH/USD")[0], "rt") as fh:
        assert fh.read().count("\n") == 3      # header + 2 unique rows
    assert list(st.read("ETH/USD")["trade_id"]) == [1, 2, 3]
    # late backfill into an already-compacted day merges on the next compaction
    st.append("ETH/USD", [mk(0, day_end - 20)])
    st.compact("ETH/USD", now=day_end + 60)
    assert list(st.read("ETH/USD")["trade_id"]) == [0, 1, 2, 3]


def test_find_gaps_reports_missing_id_ranges(tmp_path):
    st = TickStore(tmp_path)
    st.append("SOL/USD", stream(5, skip_ids={1005, 1006}))
    gaps = find_gaps(st.read("SOL/USD"))
    assert [(g[0], g[1]) for g in gaps] == [(1005, 1006)]


# ── ticks -> bars ───────────────────────────────────────────────────

def test_ticks_to_1m_microstructure():
    ticks = pd.DataFrame([
        {"trade_id": 1, "ts": T0 + 1, "price": 10.0, "qty": 2.0, "side": "b", "ord_type": "m"},
        {"trade_id": 2, "ts": T0 + 2, "price": 12.0, "qty": 1.0, "side": "s", "ord_type": "l"},
        {"trade_id": 3, "ts": T0 + 3, "price": 9.0, "qty": 1.0, "side": "s", "ord_type": "l"},
        {"trade_id": 4, "ts": T0 + 61, "price": 11.0, "qty": 4.0, "side": "b", "ord_type": "m"},
    ])
    m1 = ticks_to_1m(ticks)
    first = m1.iloc[0]
    assert (first.open, first.high, first.low, first.close) == (10.0, 12.0, 9.0, 9.0)
    assert first.volume == 4.0 and first.trades == 3
    assert first.buy_vol == 2.0 and first.sell_vol == 2.0 and first.ofi == 0.0
    assert first.vwap == pytest.approx((20 + 12 + 9) / 4)
    assert m1.iloc[1].ofi == 1.0
    assert m1.index[0] == pd.Timestamp(T0, unit="s", tz="UTC")


def test_resample_only_emits_fully_covered_closed_bars():
    ticks = pd.DataFrame([t.__dict__ for t in stream(60)]).astype({"price": float, "qty": float})
    m1 = ticks_to_1m(ticks)
    runs = coverage_runs([summarize(ticks, "d")])
    # stream starts 30 s before T0 -> the 15m bar opening at T0 is covered.
    bars = resample(m1, 15, runs, now=T0 + 3600 + 600)
    assert list(bars.index.as_unit("ns").asi8 // 10**9) == [T0, T0 + 900, T0 + 1800]  # last bar ends after the final trade
    assert bars["trades"].tolist() == [60, 60, 60]
    assert bars.iloc[0].open == m1.loc[pd.Timestamp(T0, unit="s", tz="UTC")].open
    # "now" inside the second bar -> it is still forming -> not emitted
    assert len(resample(m1, 15, runs, now=T0 + 1000)) == 1


def test_bar_over_an_id_hole_is_dropped_and_runs_link_across_days():
    ticks = pd.DataFrame([t.__dict__ for t in stream(60, skip_ids={1000 + 4 * 20})])
    ticks = ticks.astype({"price": float, "qty": float})
    m1 = ticks_to_1m(ticks)
    runs = coverage_runs([summarize(ticks, "d")])
    assert len(runs) == 2
    bars = resample(m1, 15, runs, now=T0 + 7200)
    opens = list(bars.index.as_unit("ns").asi8 // 10**9)
    assert T0 + 900 not in opens          # the hole is at minute 20 -> 15m bar #2 dropped
    assert T0 in opens
    # day summaries with consecutive ids form one run
    a = summarize(ticks.iloc[:40], "d1")
    b = summarize(ticks.iloc[40:79], "d2")
    assert len(coverage_runs([a, b])) == 1


def test_live_through_extends_newest_run_only_when_in_sync():
    ticks = pd.DataFrame([t.__dict__ for t in stream(10)]).astype({"price": float, "qty": float})
    s = summarize(ticks, "d")
    last_id = int(ticks.trade_id.iloc[-1])
    base = coverage_runs([s])[-1][1]
    assert coverage_runs([s], live_through=base + 500, live_last_id=last_id)[-1][1] == base + 500
    # collector saw a newer trade than the store holds -> no extension
    assert coverage_runs([s], live_through=base + 500, live_last_id=last_id + 5)[-1][1] == base


def test_quiet_covered_bucket_is_flat_bar():
    t = [mk(1, T0 - 5, 100.0), mk(2, T0 + 10, 101.0), mk(3, T0 + 1900, 102.0)]
    ticks = pd.DataFrame([x.__dict__ for x in t]).astype({"price": float, "qty": float})
    bars = resample(ticks_to_1m(ticks), 15, coverage_runs([summarize(ticks, "d")]), now=T0 + 7200)
    quiet = bars.loc[pd.Timestamp(T0 + 900, unit="s", tz="UTC")]
    assert quiet.volume == 0 and quiet.open == quiet.close == 101.0


def test_real_ticks_rebuild_kraken_ohlc_exactly(tmp_path):
    """2.6k real SOL/USD trades -> 15m bars identical to Kraken's own OHLC."""
    d = tmp_path / "ticks" / "SOLUSD"
    d.mkdir(parents=True)
    (d / "2026-10-02.csv.gz").write_bytes((FIX / "sol_ticks_sample.csv.gz").read_bytes())
    ref = json.loads((FIX / "sol_ohlc15_sample.json").read_text())["rows"]
    bars = BarBuilder(TickStore(tmp_path)).build("SOL/USD", 15, ref[0][0], now=ref[-1][0] + 1800)
    assert len(bars) == len(ref) == 4
    for (ts, o, h, lo, c, _vwap, vol, cnt), (idx, row) in zip(ref, bars.iterrows(), strict=True):
        assert idx.timestamp() == ts
        assert (row.open, row.high, row.low, row.close) == (float(o), float(h), float(lo), float(c))
        assert row.volume == pytest.approx(float(vol), abs=1e-8)
        assert row.trades == cnt
        assert row.buy_vol + row.sell_vol == pytest.approx(row.volume)
        assert -1 <= row.ofi <= 1


def test_bar_builder_caches_closed_days(tmp_path):
    st = TickStore(tmp_path)
    st.append("BTC/USD", stream(30))
    bb = BarBuilder(st)
    now = T0 + 5 * 86400
    a = bb.build("BTC/USD", 1, T0 - 86400, now=now)
    assert any((tmp_path / "bars1m" / "BTCUSD").glob("*.json"))
    b = bb.build("BTC/USD", 1, T0 - 86400, now=now)
    pd.testing.assert_frame_equal(a, b, check_freq=False)


# ── technicals ──────────────────────────────────────────────────────

def _bars(n=300, flow=True):
    idx = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    rng = np.random.default_rng(7)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    df = pd.DataFrame({"open": close, "high": close * 1.004, "low": close * 0.996,
                       "close": close, "volume": rng.uniform(5, 15, n)}, index=idx)
    if flow:
        df["buy_vol"] = df["volume"] * rng.uniform(0.2, 0.8, n)
        df["sell_vol"] = df["volume"] - df["buy_vol"]
    return df


def test_technicals_no_lookahead_and_flow_columns():
    df = _bars()
    full = add_technicals(df)
    part = add_technicals(df.iloc[:200])
    for c in ("rsi", "adx", "ema50", "atr", "vol_z", "ofi", "ofi_z", "flow3"):
        np.testing.assert_allclose(full[c].iloc[:200].to_numpy(), part[c].to_numpy(), equal_nan=True)
    assert full["ofi"].between(-1, 1).all()
    ofi = (df.buy_vol - df.sell_vol) / df.volume
    np.testing.assert_allclose(full["ofi"], ofi)
    rest_only = add_technicals(_bars(flow=False))
    assert rest_only["ofi"].isna().all() and rest_only["rsi"].notna().all()


def test_backtest_core_uses_the_shared_technicals():
    from dublin_bot.backtest_core import add_indicators
    df = _bars()
    pd.testing.assert_frame_equal(add_indicators(df), add_technicals(df))


# ── REST backfill & collector ───────────────────────────────────────

class FakeRest:
    def __init__(self, ticks: list[Tick]):
        self.ticks = sorted(ticks, key=lambda t: t.trade_id)
        self.calls = []

    def trades(self, pair, since, count=1000):
        self.calls.append((pair, since))
        s = float(since)
        s = s / 1e9 if s > 1e12 else s
        page = [t for t in self.ticks if t.ts >= s][:3]   # tiny pages to exercise paging
        cursor = str(int(page[-1].ts * 1e9) + 1) if page else str(since)
        return page, cursor


def test_backfill_pages_until_live_id(tmp_path):
    st = TickStore(tmp_path)
    src = stream(4)
    rest = FakeRest(src)
    res = backfill_range(st, rest, "BTC/USD", src[0].ts, until_id=src[10].trade_id)
    got = st.read("BTC/USD")["trade_id"].tolist()
    assert got == [t.trade_id for t in src[:10]]
    assert res["stop"] == "reached_live" and rest.calls[0][0] == "XBTUSD"


class FakeWS:
    def __init__(self, msgs):
        self.msgs = list(msgs)
        self.sent = []

    def send(self, m):
        self.sent.append(json.loads(m))

    def recv(self, timeout=None):
        if not self.msgs:
            raise ConnectionError("socket closed")
        m = self.msgs.pop(0)
        if isinstance(m, Exception):
            raise m
        return m

    def close(self):
        pass


def ws_trade(t: Tick, sym="BTC/USD"):
    return {"symbol": sym, "side": "buy" if t.side == "b" else "sell", "price": float(t.price),
            "qty": float(t.qty), "ord_type": "limit", "trade_id": t.trade_id,
            "timestamp": pd.Timestamp(t.ts, unit="s", tz="UTC").isoformat().replace("+00:00", "Z")}


def test_collector_dedups_fills_gaps_and_writes_status(tmp_path):
    st = TickStore(tmp_path)
    src = stream(5)                     # ids 1000..1019
    st.append("BTC/USD", src[:5])       # store already has 1000..1004
    rest = FakeRest(src)
    clock = {"t": src[-1].ts + 1}
    msgs = [
        json.dumps({"channel": "heartbeat"}),
        # snapshot replays 1003..1005: 1003/1004 are duplicates
        json.dumps({"channel": "trade", "type": "snapshot",
                    "data": [ws_trade(t) for t in src[3:6]]}),
        # jump to 1012 -> 1006..1011 must be REST-filled first
        json.dumps({"channel": "trade", "type": "update", "data": [ws_trade(src[12])]}),
        json.dumps({"channel": "ticker", "data": [{"symbol": "BTC/USD", "bid": 99.5, "ask": 100.5}]}),
        json.dumps({"channel": "ticker", "data": [{"symbol": "BTC/USD", "bid": 99.6, "ask": 100.6}]}),
        json.dumps({"channel": "trade", "data": [ws_trade(mk(5, src[0].ts), sym="PUMP/USD")]}),
    ]
    ws = FakeWS(msgs)
    c = TickCollector(st, symbols=["BTC/USD"], client=rest, connect=lambda url: ws,
                      now_fn=lambda: clock["t"], flush_every=0, log=lambda m: None)
    with pytest.raises(ConnectionError):
        c.run_once()
    ids = st.read("BTC/USD")["trade_id"].tolist()
    assert ids == list(range(1000, 1013))
    assert find_gaps(st.read("BTC/USD")) == []
    status = st.read_status("BTC/USD")
    assert status["last_trade_id"] == 1012 and status["gaps_filled"] == 1
    assert status["in_sync"] is False      # socket died -> not live any more
    q = st.read_quotes("BTC/USD")
    assert len(q) == 1                     # second quote throttled (same clock)
    subs = {m["params"]["channel"] for m in ws.sent}
    assert subs == {"trade", "ticker"}
    assert ws.sent[0]["params"]["symbol"] == ["BTC/USD"]


def test_parse_trade_v2_payload():
    sym, t = parse_trade({"symbol": "ETH/USD", "side": "sell", "price": "2500.1", "qty": "0.5",
                          "ord_type": "market", "trade_id": 42,
                          "timestamp": "2026-10-02T20:00:00.123456Z"})
    assert sym == "ETH/USD" and t.side == "s" and t.ord_type == "m" and t.trade_id == 42
    assert t.ts == pytest.approx(pd.Timestamp("2026-10-02T20:00:00.123456Z").timestamp())


# ── source (what the strategies read) ───────────────────────────────

def _rest_frame(n, tf, end_open):
    idx = pd.DatetimeIndex([pd.Timestamp(end_open - (n - 1 - i) * tf * 60, unit="s", tz="UTC")
                            for i in range(n)], name="timestamp")
    return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "vwap": 1.0,
                         "volume": 1.0, "trades": 1}, index=idx)


def _live_store(tmp_path, minutes, now):
    st = TickStore(tmp_path)
    ticks = stream(minutes, start_ts=now - minutes * 60 - 30)
    st.append("SOL/USD", ticks)
    st.write_status("SOL/USD", live_through=now - 1, in_sync=True,
                    last_trade_id=ticks[-1].trade_id)
    return st


def test_source_serves_local_bars_without_rest_when_fresh(tmp_path):
    now = float(T0 + 3 * 86400 + 5)
    _live_store(tmp_path, 200, now)
    src = PipelineBarSource(tmp_path, now_fn=lambda: now)

    def boom():
        raise AssertionError("REST must not be called")
    bars = src.get_bars("SOLUSD", 1, 150, rest_fetch=boom)
    assert len(bars) == 150 and (bars["src"] == "ticks").all()
    assert bars.index[-1].timestamp() == now - (now % 60) - 60     # last CLOSED minute
    assert bars.attrs["pipeline"]["source"] == "ticks"


def test_source_stitches_rest_history_when_local_is_short(tmp_path):
    now = float(T0 + 3 * 86400 + 5)
    _live_store(tmp_path, 120, now)       # ~2h of ticks -> a few full 15m bars
    src = PipelineBarSource(tmp_path, now_fn=lambda: now)
    last_closed = int(now // 900) * 900 - 900
    bars = src.get_bars("SOL/USD", 15, 50, rest_fetch=lambda: _rest_frame(60, 15, last_closed))
    assert len(bars) == 50
    n_local = bars.attrs["pipeline"]["local_used"]
    assert 5 <= n_local <= 8
    assert set(bars["src"]) == {"ticks", "ohlc"}
    assert (bars["src"].iloc[-n_local:] == "ticks").all()
    assert bars["ofi"].iloc[:-n_local].isna().all() and bars["ofi"].iloc[-n_local:].notna().all()
    assert bars.index.is_monotonic_increasing and not bars.index.has_duplicates


def test_source_falls_back_to_rest_when_feed_stale(tmp_path):
    now = float(T0 + 3 * 86400 + 5)
    st = _live_store(tmp_path, 200, now)
    st.write_status("SOL/USD", live_through=now - 5000, in_sync=True, last_trade_id=1)
    src = PipelineBarSource(tmp_path, now_fn=lambda: now, stale_seconds=600)
    calls = []

    def rest():
        calls.append(1)
        return _rest_frame(200, 1, int(now // 60) * 60 - 60)
    bars = src.get_bars("SOL/USD", 1, 150, rest_fetch=rest)
    assert calls and bars.attrs["pipeline"]["reason"] == "feed stale"
    assert len(bars) == 150


def test_source_rest_failure_uses_local_or_reraises(tmp_path):
    now = float(T0 + 3 * 86400 + 5)
    _live_store(tmp_path, 30, now)
    src = PipelineBarSource(tmp_path, now_fn=lambda: now)

    def down():
        raise RuntimeError("kraken down")
    bars = src.get_bars("SOL/USD", 1, 500, rest_fetch=down)
    assert len(bars) > 0 and bars.attrs["pipeline"]["source"].startswith("ticks_only")
    empty = PipelineBarSource(tmp_path / "nothing", now_fn=lambda: now)
    with pytest.raises(RuntimeError):
        empty.get_bars("BTC/USD", 60, 300, rest_fetch=down)


def test_merge_local_over_rest_prefers_local():
    rest = _rest_frame(5, 60, T0)
    local = rest.iloc[-2:].copy()
    local["close"] = 2.0
    local["buy_vol"] = 1.0
    m = merge_local_over_rest(rest, local)
    assert list(m["src"]) == ["ohlc"] * 3 + ["ticks"] * 2
    assert m["close"].tolist() == [1, 1, 1, 2, 2]


# ── gateway integration ─────────────────────────────────────────────

def test_gateway_default_is_plain_rest(settings, gateway):
    assert settings.pipeline_enabled is False
    assert gateway.pipeline_source() is None
    bars = gateway.get_bars()
    assert "src" not in bars.columns


def test_gateway_pipeline_enabled_serves_pipeline_frame(settings_factory, fake_session, tmp_path):
    from dublin_bot.kraken_gateway import KrakenGateway
    s = settings_factory(pipeline_enabled=True, pipeline_data_dir=tmp_path / "pipe")
    gw = KrakenGateway(s, session=fake_session, max_retries=1, sleep_fn=lambda _s: None)
    assert gw.pipeline_source() is not None
    bars = gw.get_bars()                       # no local ticks -> REST via the pipeline
    assert len(bars) == s.lookback_bars
    assert (bars["src"] == "ohlc").all() and "ofi" in bars.columns
    s.symbol = "DOGE/USD"
    assert gw.pipeline_source() is None        # outside the 3-coin universe


# ── order-flow filters (off by default, fail closed) ────────────────

def test_flow_filters_default_off_and_live_config_unchanged(settings):
    assert settings.regime_flow_filter == "" and settings.meanrev_flow_filter == ""
    assert settings.pipeline_symbols == ["BTC/USD", "ETH/USD", "SOL/USD"]
    from dublin_bot.strategies.meanrev4h_strategy import MeanReversion4hStrategy
    from dublin_bot.strategies.regime_strategy import RegimeTrendStrategy
    assert RegimeTrendStrategy(settings).params()["flt"] == ""
    assert MeanReversion4hStrategy(settings).params()["flt"] == ""


def test_flow_filter_masks_and_fail_closed():
    from dublin_bot.backtest_core import meanrev_signals, regime_signals
    from dublin_bot.technicals import FLOW_FILTERS, flow_filter_mask
    d = add_technicals(_bars())
    assert flow_filter_mask(d, "").all()
    np.testing.assert_array_equal(flow_filter_mask(d, "ofi_pos"), d["ofi"].to_numpy() > 0)
    with pytest.raises(ValueError):
        flow_filter_mask(d, "nope")
    no_flow = add_technicals(_bars(flow=False))
    for name in FLOW_FILTERS:
        assert not flow_filter_mask(no_flow, name).any()      # NaN order flow never passes
    p = {"rsi_os": 60.0}
    base = meanrev_signals(d, p)["entry"]
    filt = meanrev_signals(d, {**p, "flt": "ofi_pos"})["entry"]
    assert filt.sum() < base.sum() and not (filt & ~base).any()
    r0 = regime_signals(d, {})["entry"]
    r1 = regime_signals(d, {"flt": "flow3_pos"})["entry"]
    assert not (r1 & ~r0).any()


def test_walkforward_pipeline_source_never_overwrites_learner_priors():
    src = (Path(__file__).parents[1] / "scripts" / "backtest_walkforward.py").read_text()
    assert 'walkforward_pipeline_results.json' in src
    assert 'SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"]' in src


def test_exchange_side_hole_is_verified_and_does_not_break_coverage(tmp_path):
    from dublin_bot.pipeline.backfill import fill_gaps
    st = TickStore(tmp_path)
    src = stream(30, skip_ids={1040, 1041})          # Kraken never published 1040-1041
    st.append("BTC/USD", src[:20] + src[25:])        # we also lost 1020..1024 (fillable)
    res = fill_gaps(st, FakeRest(src), "BTC/USD")
    assert res == {"symbol": "BTC/USD", "gaps": 2, "filled": 1, "exchange_holes": 1, "open": 0}
    assert st.verified_holes("BTC/USD") == {(1040, 1041)}
    bb = BarBuilder(st)
    runs = bb.coverage("BTC/USD", now=T0 + 7200)
    assert len(runs) == 1                           # continuous despite the exchange hole
