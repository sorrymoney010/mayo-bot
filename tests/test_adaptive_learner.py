"""Adaptive learner gate: rolling expectancy, priors, bench/probation, regimes."""
from __future__ import annotations

import json
from pathlib import Path

from dublin_bot.learner import LearningAgent, PROBATION_SIZE

KEY = "regime@60m"


def _agent(tmp_path: Path, **kw) -> LearningAgent:
    return LearningAgent(tmp_path / "learner.json", min_trades=3, enabled=True,
                         strategy_key=KEY, **kw)


def _trades(la, sym, bps_list, regime="trend", notional=100.0, t0=1_000_000.0):
    for k, bps in enumerate(bps_list):
        la.record_trade(sym, notional * bps / 1e4, regime, notional=notional, ts=t0 + k)


def test_no_bench_below_min_sample_but_half_size(tmp_path):
    la = _agent(tmp_path)
    _trades(la, "BTC/USD", [-100] * 7)
    d = la.gate("BTC/USD", now=2_000_000)
    assert d.allow is True
    assert d.state == "negative" and d.size_mult == 0.5
    assert d.live_n == 7 and round(d.live_bps) == -100


def test_bench_at_min_sample_negative_expectancy(tmp_path):
    la = _agent(tmp_path)
    _trades(la, "BTC/USD", [-100] * 24 + [150, -50] * 3)  # mean -70 over 30, 90% UCB < 0
    d = la.gate("BTC/USD", now=2_000_000)
    assert d.allow is False and d.state == "benched" and d.size_mult == 0.0
    assert la.bench_for("BTC/USD") is not None
    # persists across reload
    la2 = _agent(tmp_path)
    assert la2.gate("BTC/USD", now=2_000_100).allow is False


def test_positive_and_weak_sizing(tmp_path):
    la = _agent(tmp_path)
    _trades(la, "SOL/USD", [200] * 8)
    _trades(la, "ETH/USD", [10] * 8)
    assert la.gate("SOL/USD").size_mult == 1.0
    weak = la.gate("ETH/USD")
    assert weak.state == "weak" and weak.size_mult == 0.75 and weak.allow


def test_bench_expiry_probation_then_rebench_or_clear(tmp_path):
    la = _agent(tmp_path, bench_hours=1.0)
    _trades(la, "BTC/USD", [-100] * 30)
    now = 2_000_000.0
    assert la.gate("BTC/USD", now=now).allow is False
    # still benched within the hour
    assert la.gate("BTC/USD", now=now + 1800).allow is False
    # expired → probation at reduced size (no new trade yet)
    d = la.gate("BTC/USD", now=now + 3700)
    assert d.allow and d.state == "probation" and d.size_mult == PROBATION_SIZE
    # probation trade loses → window still negative → re-benched
    _trades(la, "BTC/USD", [-20], t0=now + 3800)
    assert la.gate("BTC/USD", now=now + 3900).allow is False
    # after the new bench expires, a big winner flips the window positive → cleared
    later = now + 3900 + 3700
    assert la.gate("BTC/USD", now=later).state == "probation"
    _trades(la, "BTC/USD", [2000], t0=later + 1)
    d = la.gate("BTC/USD", now=later + 10)
    # mean still < 0 but the 90% upper bound is now > 0 → cleared, half size
    assert d.allow and d.size_mult >= 0.5 and la.bench_for("BTC/USD") is None


def test_regime_level_bench_only_blocks_that_regime(tmp_path):
    la = _agent(tmp_path)
    _trades(la, "ETH/USD", [300] * 10, regime="trend")
    _trades(la, "ETH/USD", [-80] * 30, regime="chop", t0=3_000_000)
    assert la.gate("ETH/USD", "trend").allow is True
    d = la.gate("ETH/USD", "chop")
    assert d.allow is False and d.key == "ETH/USD|chop"


def test_backtest_prior_shrinks_size_before_live_evidence(tmp_path):
    wf = tmp_path / "wf.json"
    wf.write_text(json.dumps({"results": [
        {"symbol": "BTC/USD", "tf": 60, "family": "regime",
         "oos": {"trades": 12, "avg_net_bps": -40.0}, "oos_by_regime": {}},
        {"symbol": "SOL/USD", "tf": 60, "family": "regime",
         "oos": {"trades": 12, "avg_net_bps": 120.0}, "oos_by_regime": {}},
        # different strategy key must be ignored
        {"symbol": "ETH/USD", "tf": 15, "family": "breakout",
         "oos": {"trades": 50, "avg_net_bps": -300.0}, "oos_by_regime": {}},
    ]}))
    la = _agent(tmp_path, priors_path=wf)
    btc = la.gate("BTC/USD")
    assert btc.allow and btc.size_mult == 0.5 and btc.prior_bps == -40.0
    assert la.gate("SOL/USD").size_mult == 1.0
    eth = la.gate("ETH/USD")
    assert eth.state == "neutral" and eth.prior_bps is None
    # A prior alone can never bench (needs live sample >= min_sample).
    assert la.bench_for("BTC/USD") is None


def test_live_evidence_outweighs_prior(tmp_path):
    wf = tmp_path / "wf.json"
    wf.write_text(json.dumps({"results": [
        {"symbol": "BTC/USD", "tf": 60, "family": "regime",
         "oos": {"trades": 40, "avg_net_bps": 200.0}, "oos_by_regime": {}},
    ]}))
    la = _agent(tmp_path, priors_path=wf)
    _trades(la, "BTC/USD", [-60] * 30)
    assert la.gate("BTC/USD").allow is False  # live negative over 30 → bench despite rosy prior


def test_legacy_and_other_strategy_trades_do_not_count(tmp_path):
    la = _agent(tmp_path)
    for _ in range(18):
        la.record_trade("BTC/USD", -0.15, "sideways")  # legacy: no notional
    for _ in range(10):
        la.record_trade("BTC/USD", -1.0, "trend", notional=100.0, strategy="breakout@15m")
    d = la.gate("BTC/USD")
    assert d.allow and d.live_n == 0 and d.state == "neutral"
    assert la.coins["BTC/USD"].trades == 28  # still tracked for reporting


def test_decisions_logged_only_on_change(tmp_path):
    la = _agent(tmp_path)
    la.gate("BTC/USD")
    la.gate("BTC/USD")
    _trades(la, "BTC/USD", [-100] * 30)
    la.gate("BTC/USD", now=2_000_000)
    lines = (tmp_path / "learner_decisions.jsonl").read_text().strip().splitlines()
    states = [json.loads(x)["state"] for x in lines]
    assert states == ["neutral", "benched"]


def test_entry_meta_round_trip(tmp_path):
    la = _agent(tmp_path)
    la.note_entry("SOL/USD", regime="trend", notional=150.0)
    la2 = _agent(tmp_path)
    meta = la2.pop_entry("SOL/USD")
    assert meta["regime"] == "trend" and meta["notional"] == 150.0 and meta["strategy"] == KEY


def test_disabled_learner_never_gates(tmp_path):
    la = LearningAgent(tmp_path / "l.json", enabled=False, strategy_key=KEY)
    d = la.gate("BTC/USD")
    assert d.allow and d.size_mult == 1.0


def test_engine_blocks_buy_when_symbol_benched(settings_factory, fake_session, tmp_path):
    """Integration: a benched symbol turns a strategy BUY into a WAIT."""
    from dublin_bot.models import Action, Signal
    from .conftest import ohlc_payload
    from .test_engine_pipeline import build_engine

    s = settings_factory(learner_path=tmp_path / "learner.json", learner_enabled=True,
                         adx_gate_enabled=False, learner_priors_path=tmp_path / "none.json")
    fake_session.routes["OHLC"] = ohlc_payload(bars=250, start_price=40_000.0)
    engine = build_engine(s, fake_session)
    key = engine.strategy_key()
    for k in range(30):
        engine.learner.record_trade("BTC/USD", -1.0, "chop", notional=100.0,
                                    strategy=key, ts=1_000 + k)
    engine.strategy.evaluate = lambda bars, in_position=False: Signal(
        Action.BUY, 90, "forced test buy", float(bars["close"].iloc[-1]), 10.0,
        float(bars["close"].iloc[-1]) * 0.97)
    engine._select_symbol = lambda: None
    result = engine.run_cycle()
    assert result.record is not None
    assert result.record.signal.action is Action.WAIT
    assert "Learner bench" in result.record.signal.reason
    assert result.gates["learner"]["state"] == "benched"


# ── audit policy: n >= 30, 90% upper bound < 0, sizes in [0.5, 1.0], prior <= 2 ──
def test_no_bench_before_30_trades_even_if_all_lose(tmp_path):
    la = _agent(tmp_path, min_sample=30)
    _trades(la, "BTC/USD", [-150] * 29)
    d = la.gate("BTC/USD", now=2_000_000)
    assert d.allow and d.state == "negative" and d.size_mult == 0.5
    _trades(la, "BTC/USD", [-150], t0=1_500_000)
    assert la.gate("BTC/USD", now=2_000_001).allow is False


def test_negative_mean_but_upper_bound_above_zero_is_not_benched(tmp_path):
    la = _agent(tmp_path, min_sample=30)
    _trades(la, "BTC/USD", [-400, 350] * 15)  # mean -25, very noisy
    d = la.gate("BTC/USD", now=2_000_000)
    assert d.live_bps < 0 and d.ucb90_bps > 0
    assert d.allow and la.bench_for("BTC/USD") is None and d.size_mult == 0.5


def test_default_policy_constants():
    from dublin_bot import learner
    from dublin_bot.config import Settings

    assert learner.MIN_SAMPLE == 30 and learner.PRIOR_MAX_WEIGHT <= 2.0
    assert learner.ROLLING_WINDOW >= learner.MIN_SAMPLE
    assert (learner.SIZE_MIN, learner.SIZE_MAX) == (0.5, 1.0) and learner.PROBATION_SIZE >= 0.5
    assert Settings(_env_file=None).learner_min_sample == 30
    # an old env value below 30 is raised to the floor, never crashes the loop
    assert Settings(_env_file=None, learner_min_sample=8).learner_min_sample == 30


def test_size_multiplier_always_within_bounds(tmp_path):
    la = _agent(tmp_path, bench_hours=1.0, min_sample=30)
    for sym, bps in (("A/USD", [500] * 40), ("B/USD", [10] * 40), ("C/USD", [-30, 20] * 20)):
        _trades(la, sym, bps)
        d = la.gate(sym, now=2_000_000)
        assert d.allow and 0.5 <= d.size_mult <= 1.0
    _trades(la, "D/USD", [-100] * 30)
    assert la.gate("D/USD", now=2_000_000).allow is False
    assert la.gate("D/USD", now=2_000_000 + 3700).size_mult == 0.5  # probation


def test_prior_weight_capped_at_two(tmp_path):
    wf = tmp_path / "wf.json"
    wf.write_text(json.dumps({"results": [{"symbol": "BTC/USD", "tf": 60, "family": "regime",
                                           "oos": {"trades": 500, "avg_net_bps": 300.0}, "oos_by_regime": {}}]}))
    la = _agent(tmp_path, priors_path=wf)
    _trades(la, "BTC/USD", [-30] * 4)
    d = la.gate("BTC/USD")
    assert d.blended_bps == (300.0 * 2 + -30 * 4) / 6


def test_trade_detail_writes_rich_closed_trade_row(tmp_path):
    la = _agent(tmp_path)
    la.record_trade("BTC/USD", 1.5, "trend", notional=100.0, ts=1_800_000_000.0,
                    detail={"entry_signal_px": 100.0, "entry_fill_px": 100.1, "entry_fee": 0.4,
                            "exit_signal_px": 102.0, "exit_fill_px": 101.9, "exit_fee": 0.41,
                            "entry_maker": False, "exit_maker": False, "exit_reason": "trail",
                            "mae_bps": -80.0, "mfe_bps": 250.0, "hold_s": 7200})
    row = json.loads((tmp_path / "closed_trades.jsonl").read_text().strip())
    assert row["net_bps"] == 150.0 and row["fees_usd"] == 0.81 and row["exit_reason"] == "trail"
    assert row["entry_slip_bps"] == 10.0 and round(row["exit_slip_bps"], 1) == 9.8
    assert row["sleeve"] == KEY and row["source"] == "fill" and "git" in row
    assert row.get("trade_id")
    # detail-supplied id is preserved
    la.record_trade("ETH/USD", 0.5, "trend", notional=100.0, ts=1_800_000_001.0,
                    detail={"trade_id": "custom-id-1", "entry_signal_px": 1.0, "entry_fill_px": 1.0,
                            "exit_signal_px": 1.0, "exit_fill_px": 1.0, "entry_fee": 0.0, "exit_fee": 0.0})
    rows = [json.loads(l) for l in (tmp_path / "closed_trades.jsonl").read_text().splitlines() if l.strip()]
    assert rows[-1]["trade_id"] == "custom-id-1"


def test_old_small_sample_benches_are_dropped_on_load(tmp_path):
    p = tmp_path / "learner.json"
    p.write_text(json.dumps({"strategy_key": KEY, "benches": {
        "BTC/USD": {"since": 1.0, "until": 9e12, "trades_at_bench": 8, "reason": "old policy"},
        "ETH/USD": {"since": 1.0, "until": 9e12, "trades_at_bench": 31, "reason": "real"}}}))
    la = LearningAgent(p, strategy_key=KEY, min_sample=30)
    assert la.bench_for("BTC/USD") is None and la.bench_for("ETH/USD") is not None
