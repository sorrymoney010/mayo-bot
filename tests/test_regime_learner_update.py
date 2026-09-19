"""E3: regime detector updates learner.last_regime and regime_penalty varies."""

from __future__ import annotations

from pathlib import Path

from dublin_bot.learner import LearningAgent
from dublin_bot.learning import classify_regime


def _uptrend(n: int = 60) -> list[float]:
    return [100.0 + i * 0.5 for i in range(n)]


def _downtrend(n: int = 60) -> list[float]:
    return [100.0 - i * 0.5 for i in range(n)]


def test_classify_regime_works_with_bars_only():
    regime = classify_regime([], _uptrend())
    assert regime.regime != "unknown"
    assert regime.confidence > 0


def test_update_regime_persists(tmp_path: Path):
    agent = LearningAgent(tmp_path / "learner.json", min_trades=2, enabled=True)
    assert agent.last_regime == "unknown"
    agent.update_regime("bull_trend")
    assert agent.last_regime == "bull_trend"
    reloaded = LearningAgent(tmp_path / "learner.json", min_trades=2, enabled=True)
    assert reloaded.last_regime == "bull_trend"


def test_regime_penalty_differs_across_regimes(tmp_path: Path):
    agent = LearningAgent(tmp_path / "learner.json", min_trades=3, enabled=True)
    for _ in range(4):
        agent.record_trade("PUMP/USD", -1.0, "bear_trend")
        agent.record_trade("PUMP/USD", 0.5, "bull_trend")
    # Bleeds in bear, profitable in bull → penalty only in bear.
    assert agent.regime_penalty("PUMP/USD", "bear_trend") < 1.0
    assert agent.regime_penalty("PUMP/USD", "bull_trend") == 1.0
    assert agent.regime_penalty("PUMP/USD", "bear_trend") != agent.regime_penalty(
        "PUMP/USD", "bull_trend"
    )


def test_engine_cycle_updates_last_regime(settings, fake_session, tmp_path):
    """Offline engine cycle must classify from bars and stamp the learner."""
    from .test_engine_pipeline import build_engine
    from .conftest import ohlc_payload

    settings.learner_path = tmp_path / "learner.json"
    settings.learner_enabled = True
    fake_session.routes["OHLC"] = ohlc_payload(bars=250, start_price=40_000.0)
    engine = build_engine(settings, fake_session)
    assert engine.learner.last_regime == "unknown"
    result = engine.run_cycle()
    assert "regime" in result.gates
    assert engine.learner.last_regime != "unknown"
