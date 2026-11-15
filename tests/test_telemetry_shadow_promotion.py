"""Decision/equity telemetry, shadow trades, rich closed trades, promotion report."""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from dublin_bot import promotion
from dublin_bot.backtest_core import Costs, Spec
from dublin_bot.loop_telemetry import LoopTelemetry
from dublin_bot.scorecard import write_scorecard
from dublin_bot.shadow import ShadowLog, score_one, spec_for
from dublin_bot.telemetry import DecisionLog, equity_snapshot, excursion

from .test_d1_trendhold import _th, _world
from .test_meanrev_sleeve import NOW, TF, FakeGateway, _tick

H = 3600


def _ohlc(n=400, tf=TF, drop_last=0, start=1_700_000_000):
    x = np.arange(n, dtype=float)
    c = 100.0 * np.exp(0.002 * x)
    if drop_last:
        c[-drop_last:] = c[-drop_last - 1] * np.cumprod(np.full(drop_last, 0.97))
    return pd.DataFrame({"time": start + x.astype(int) * tf, "open": c, "high": c * 1.002,
                         "low": c * 0.998, "close": c, "volume": np.full(n, 1000.0)})


# ── shadow trades ──────────────────────────────────────────────
def test_spec_for_scores_raw_rules_without_d1():
    sp = spec_for("trendhold@240m", {"ema_fast": 20, "ema_slow": 100, "d1": True, "warm": 5})
    assert sp.name == "trendhold" and sp.params["d1"] is False and "warm" not in sp.params
    assert spec_for("meanrev_mk@240m", {"d1": True}).params["entry"] == "limit"
    assert spec_for("momentum@15m", {}) is None


def test_score_one_scored_open_and_missing():
    bars = _ohlc(400, drop_last=15)
    t0 = int(bars["time"].iloc[300])
    out = score_one(bars, Spec("trendhold", {"ema_fast": 20, "ema_slow": 100, "d1": False}), t0, 240, Costs())
    assert out["status"] == "scored" and out["exit_reason"] == "below_ema100"
    assert out["net_bps"] > 0 and out["hold_h"] > 0
    still = score_one(_ohlc(400), Spec("trendhold", {"d1": False}), t0, 240, Costs())
    assert still is None  # hypothetical position still open
    assert score_one(None, Spec("trendhold", {}), t0, 240, Costs()) is None


def test_shadow_log_records_once_and_scores_later(tmp_path):
    bars = _ohlc(400, drop_last=15)
    t0 = int(bars["time"].iloc[300])
    sh = ShadowLog(tmp_path / "shadow_signals.jsonl")
    kw = dict(strategy="trendhold@240m", symbol="BTC/USD", gate="max_positions", reason="max 3",
              signal_bar_open=t0, tf_minutes=240, signal_px=1.0, params={"ema_fast": 20, "ema_slow": 100})
    assert sh.record(**kw) is True
    assert sh.record(**kw) is False  # one row per strategy/symbol/bar
    assert sh.score(lambda s, tf: bars, now=t0 + 1) == []  # entry bar not closed yet
    done = sh.score(lambda s, tf: bars, now=float(bars["time"].iloc[-1]) + 10 * TF)
    assert len(done) == 1 and done[0]["status"] == "scored" and done[0]["gate"] == "max_positions"
    assert sh.open_signals() == []


# ── decision / equity telemetry ───────────────────────────────
def test_decision_log_dedups_per_bar_and_has_required_fields(tmp_path):
    dl = DecisionLog(tmp_path / "d.jsonl")
    kw = dict(strategy="regime@60m", symbol="BTC/USD", action="WAIT", reason="no entry", bar_open=1_000_000.0,
              tf_minutes=60, indicators={"close": 1.0}, d1={"enabled": True, "riskon": False},
              quote={"bid": 99.0, "ask": 101.0}, learner={"live_n": 3}, now=1_000_000.0 + 3700)
    assert dl.log(**kw) and not dl.log(**kw)
    assert dl.log(**{**kw, "action": "BUY"})
    row = json.loads((tmp_path / "d.jsonl").read_text().splitlines()[0])
    for k in ("bar_open", "bar_close", "indicators", "d1", "bid", "ask", "spread_bps", "data_age_s",
              "strategy", "learner", "git"):
        assert k in row
    assert row["spread_bps"] == 200.0 and row["data_age_s"] == 100.0


def test_excursion_and_equity_snapshot(tmp_path):
    bars = pd.DataFrame({"time": [0, 3600, 7200], "high": [101.0, 104.0, 102.0], "low": [99.0, 97.0, 100.0]})
    assert excursion(bars, 3600, 100.0) == (-300.0, 400.0)
    book = tmp_path / "paper_portfolio.json"
    book.write_text(json.dumps({"cash": 300.0, "positions": [  # PaperPortfolio's list format
        {"symbol": "BTC/USD", "quantity": 2.0, "entry_price": 90.0}]}))
    snap = equity_snapshot(book, {"BTC/USD": 100.0}, seed=500.0, owners={"BTC/USD": "trendhold_4h"})
    assert snap["equity"] == 500.0 and snap["positions"]["BTC/USD"]["owner"] == "trendhold_4h"
    assert snap["exposure_pct"] == 40.0


def test_loop_telemetry_writes_all_files(tmp_path):
    from dublin_bot.config import Settings

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "paper_portfolio.json").write_text(json.dumps({"cash": 400.0, "positions": {
        "ETH/USD": {"quantity": 0.05, "entry_price": 2000.0}}}))
    bars = _ohlc(400, drop_last=15)
    t0 = int(bars["time"].iloc[300])
    now = float(bars["time"].iloc[-1]) + 10 * TF
    tel = LoopTelemetry(Settings(_env_file=None, strategy_equity_usd=500.0), logs_dir=logs, now_fn=lambda: now,
                        price_fn=lambda syms: {"ETH/USD": 2100.0}, bars_fn=lambda s, tf: bars)

    class Eng:
        decision_ctx = {"strategy": "regime@60m", "symbol": "BTC/USD", "action": "WAIT", "reason": "learner",
                        "bar_open": now - 7200, "tf_minutes": 60, "indicators": {}, "d1": {"enabled": True},
                        "quote": {"bid": 1.0, "ask": 1.0}, "learner": {}, "risk": {"approved": False}}
        shadow_candidates = [{"strategy": "trendhold@240m", "symbol": "BTC/USD", "gate": "learner",
                              "reason": "benched", "signal_px": 1.0, "signal_bar_open": t0, "tf_minutes": 240,
                              "params": {"ema_fast": 20, "ema_slow": 100}}]

    tel.engine(Eng())
    tel.sleeve({"sleeve": "meanrev_4h", "blocked": []})
    tel.tick()
    assert (logs / "decision_snapshots.jsonl").exists()
    eq = json.loads((logs / "equity.jsonl").read_text().splitlines()[-1])
    assert eq["equity"] == 505.0 and eq["marks"] == "public_ticker"
    rows = [json.loads(x) for x in (logs / "shadow_signals.jsonl").read_text().splitlines()]
    assert [r["type"] for r in rows] == ["signal", "outcome"]


# ── sleeves: shadow candidates, decisions and rich closed trades ──
def test_trendhold_blocked_entry_becomes_shadow_candidate_and_decision_row():
    bars, ticks = _world()
    Path("logs").mkdir(exist_ok=True)
    Path("logs/paper_bot_positions.json").write_text(json.dumps({"ETH/USD": 0.01, "SOL/USD": 0.01}))
    res = _th(FakeGateway(bars, ticks), max_concurrent_positions=2).run_cycle()
    assert res.blocked and res.blocked[0]["gate"] == "max_positions"
    assert res.blocked[0]["strategy"] == "trendhold@240m" and res.blocked[0]["signal_bar_open"]
    rows = [json.loads(x) for x in Path("logs/decision_snapshots.jsonl").read_text().splitlines()]
    btc = [r for r in rows if r["symbol"] == "BTC/USD" and r["strategy"] == "trendhold@240m"]
    assert btc and btc[-1]["action"] == "BUY" and btc[-1]["d1"]["riskon"] is True
    assert btc[-1]["indicators"].get("close") and btc[-1]["learner"]["min_sample"] >= 30


def test_trendhold_d1_block_is_a_shadow_candidate():
    bars, ticks = _world()
    res = _th(FakeGateway(bars, ticks), rising=False).run_cycle()
    assert [b["gate"] for b in res.blocked if b["symbol"] == "BTC/USD"] == ["d1"]


def test_trendhold_exit_writes_rich_closed_trade():
    bars, ticks = _world()
    _th(FakeGateway(bars, ticks)).run_cycle()
    later = NOW + 2 * TF
    bars2 = {s: b.set_axis(b.index + pd.Timedelta(seconds=2 * TF)) for s, b in _world("break")[0].items()}
    ticks2 = {s: _tick(float(b["close"].iloc[-1])) for s, b in bars2.items()}
    _th(FakeGateway(bars2, ticks2), now=later).run_cycle()
    row = json.loads(Path("logs/closed_trades.jsonl").read_text().strip().splitlines()[-1])
    assert row["sleeve"] == "trendhold@240m" and row["exit_reason"] == "below_ema100"
    for k in ("entry_signal_px", "entry_fill_px", "exit_signal_px", "exit_fill_px", "fees_usd",
              "entry_maker", "exit_maker", "mae_bps", "mfe_bps", "hold_s", "net_bps", "trade_id"):
        assert row.get(k) is not None, k
    assert row["hold_s"] == 2 * TF and row["mae_bps"] < 0 and row["mae_bps"] <= row["mfe_bps"]
    card = write_scorecard(Path("logs"))
    det = card["sleeves"]["trendhold@240m"]["detail"]
    assert card["sleeves"]["trendhold@240m"]["trades"] == 1  # rich row + learner row deduplicated
    assert det["rich_trades"] == 1 and det["exit_reasons"] == {"below_ema100": 1}
    assert "shadow" in card and "equity" in card and "promotion" in card


def test_engine_learner_block_is_shadow_candidate(settings_factory, fake_session, tmp_path):
    from dublin_bot.models import Action, Signal

    from .conftest import ohlc_payload
    from .test_engine_pipeline import build_engine

    s = settings_factory(learner_path=tmp_path / "learner.json", learner_enabled=True,
                         adx_gate_enabled=False, learner_priors_path=tmp_path / "none.json")
    fake_session.routes["OHLC"] = ohlc_payload(bars=250, start_price=40_000.0)
    engine = build_engine(s, fake_session)
    for k in range(30):
        engine.learner.record_trade("BTC/USD", -1.0, "chop", notional=100.0,
                                    strategy=engine.strategy_key(), ts=1_000 + k)
    engine.strategy.evaluate = lambda bars, in_position=False: Signal(
        Action.BUY, 90, "forced test buy", float(bars["close"].iloc[-1]), 10.0,
        float(bars["close"].iloc[-1]) * 0.97)
    engine._select_symbol = lambda: None
    engine.run_cycle()
    assert engine.shadow_candidates and engine.shadow_candidates[0]["gate"] == "learner"
    assert engine.decision_ctx["raw_action"] == "BUY" and engine.decision_ctx["action"] == "WAIT"


# ── live-promotion bar (report only) ──────────────────────────
def _rows(bps, sleeve="regime@60m"):
    return [{"symbol": "BTC/USD", "ts": 1e9 + i, "pnl": b / 100, "net_bps": b, "sleeve": sleeve}
            for i, b in enumerate(bps)]


def test_promotion_bar_rules():
    ok = promotion.check_strategy(_rows([120, 80, 100, 90, 110, 95] * 5))
    assert ok["passes"] and ok["trades"] == 30
    assert ok["checks"]["profit_factor"]["pass"] and ok["checks"]["profit_factor"]["value"] == "inf"
    few = promotion.check_strategy(_rows([100] * 29))
    assert not few["passes"] and few["failed"] == ["min_trades"]
    noisy = promotion.check_strategy(_rows([900, -400, -300] * 10))  # mean +67, too noisy
    assert not noisy["passes"] and "lower_bound_positive" in noisy["failed"]
    lucky = promotion.check_strategy(_rows([-20] * 28 + [3000, 3000]))
    assert "ex_best_2_positive" in lucky["failed"]
    # PF = 2000/1900 = 1.05 < 1.10 (also fails some mean/lb checks; PF must be listed)
    weak_pf = promotion.check_strategy(_rows([100] * 20 + [-190] * 10))
    assert not weak_pf["passes"] and "profit_factor" in weak_pf["failed"]
    assert weak_pf["checks"]["profit_factor"]["value"] == 1.05


def test_promotion_profit_factor():
    assert promotion.profit_factor([]) is None
    assert promotion.profit_factor([10.0, 5.0, 0.0]) == float("inf")
    assert promotion.profit_factor([-1.0, -2.0]) == 0.0
    assert abs(promotion.profit_factor([100.0, 50.0, -80.0]) - 150 / 80) < 1e-12
    # pnl fallback when net_bps is missing
    rows = [{"symbol": "BTC/USD", "ts": 1e9 + i, "pnl": p, "sleeve": "x"}
            for i, p in enumerate([2.0, 1.0, -1.5])]
    r = promotion.check_strategy(rows)
    assert r["trades"] == 0  # no net_bps → other checks see n=0
    assert r["checks"]["profit_factor"]["value"] == round((2 + 1) / 1.5, 2)
    assert r["checks"]["profit_factor"]["pass"] is True  # 2.0 >= 1.10


def test_dedup_trades_prefers_stable_id():
    # Same legacy symbol|ts|pnl, two distinct ids → two trades
    a = {"symbol": "ETH/USD", "ts": 1000.0, "pnl": -0.147467, "net_bps": -10, "trade_id": "aaa",
         "source": "fill"}
    b = {"symbol": "ETH/USD", "ts": 1000.0, "pnl": -0.147467, "net_bps": -11, "trade_id": "bbb",
         "source": "fill"}
    out = promotion.dedup_trades([a, b])
    assert {r["trade_id"] for r in out} == {"aaa", "bbb"}

    # No-id lean row + identified fill with the same legacy key → one row, fill wins
    lean = {"symbol": "BTC/USD", "ts": 50.0, "pnl": 1.5, "net_bps": 150, "sleeve": "s"}
    fill = {"symbol": "BTC/USD", "ts": 50.0, "pnl": 1.5, "net_bps": 150, "sleeve": "s",
            "source": "fill", "trade_id": "tid-1", "exit_reason": "trail"}
    out = promotion.dedup_trades([lean, fill])
    assert len(out) == 1 and out[0]["trade_id"] == "tid-1" and out[0]["source"] == "fill"
    out = promotion.dedup_trades([fill, lean])
    assert len(out) == 1 and out[0]["trade_id"] == "tid-1" and out[0]["source"] == "fill"

    # Two no-id rows, same legacy key: fill wins (old behavior)
    out = promotion.dedup_trades([
        {"symbol": "SOL/USD", "ts": 9.0, "pnl": 0.1, "net_bps": 1},
        {"symbol": "SOL/USD", "ts": 9.0, "pnl": 0.1, "net_bps": 1, "source": "fill", "fees_usd": 0.2},
    ])
    assert len(out) == 1 and out[0]["source"] == "fill" and out[0]["fees_usd"] == 0.2

    # Different id fields (order_id / txid / lot_id) also count
    out = promotion.dedup_trades([
        {"symbol": "X/USD", "ts": 1, "pnl": 1, "order_id": "o1"},
        {"symbol": "X/USD", "ts": 1, "pnl": 1, "source": "fill", "order_id": "o1"},
    ])
    assert len(out) == 1 and out[0]["source"] == "fill"

    # Unrelated trades stay separate
    out = promotion.dedup_trades([
        {"symbol": "BTC/USD", "ts": 1, "pnl": 1, "trade_id": "t1"},
        {"symbol": "ETH/USD", "ts": 2, "pnl": 2, "id": "t2"},
        {"symbol": "SOL/USD", "ts": 3, "pnl": 3},
    ])
    assert len(out) == 3


def test_promotion_report_is_report_only(tmp_path):
    before = dict(os.environ)
    logs = tmp_path / "logs"
    logs.mkdir()
    with (logs / "closed_trades.jsonl").open("w") as fh:
        for r in _rows([100] * 31):
            fh.write(json.dumps(r) + "\n")
    rep = promotion.write_report(logs)
    assert rep["report_only"] is True and rep["strategies"]["regime@60m"]["passes"] is True
    assert rep["bar"]["min_profit_factor"] == 1.10
    assert "profit factor >= 1.10" in " ".join(rep["bar"]["rules"])
    assert sorted(p.name for p in logs.iterdir()) == ["closed_trades.jsonl", "promotion_report.json"]
    assert dict(os.environ) == before
    src = Path(promotion.__file__).read_text()
    assert "Settings(" not in src and ".env" not in src.replace("env files", "")
    assert "PASS" in promotion.render(rep) and "never changes a lock" in promotion.render(rep)
