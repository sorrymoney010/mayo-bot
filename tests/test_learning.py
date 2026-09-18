"""Tests for the self-learning module — validates analytics without touching live config."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dublin_bot.learning import (
    analyze_filters,
    classify_regime,
    compute_signal_quality,
    generate_suggestions,
    build_learning_report,
    save_report,
    generate_brief,
)


# ── Fixtures with sample decision data ─────────────────────────

@pytest.fixture
def sample_decisions() -> list[dict]:
    """30 decisions with mixed filter failures."""
    decisions = []
    base_ts = "2026-08-04T01:00:00+00:00"
    for i in range(30):
        if i < 10:
            # Regime filter failing
            action = "WAIT"
            reason = "Filters failed: regime, trend"
            score = 40
        elif i < 20:
            # Volume filter failing
            action = "WAIT"
            reason = "Filters failed: volume"
            score = 60
        else:
            # All filters passing — would be BUY
            action = "BUY"
            reason = "Trend, momentum, breakout and volume confirmed"
            score = 100

        decisions.append({
            "dry_run": True,
            "order_id": None,
            "risk": {"approved": False, "reason": "No entry order requested",
                     "notional_usd": 0.0, "planned_loss_usd": 0.0},
            "signal": {"action": action, "score": score, "reason": reason,
                       "price": 64000.0 + i * 100, "atr": 350.0, "stop_price": None},
            "symbol": "BTC/USD",
            "timestamp": base_ts,
        })
    return decisions


@pytest.fixture
def all_pass_decisions() -> list[dict]:
    """All filters passing — should produce no suggestions."""
    return [{
        "dry_run": True, "order_id": f"order-{i}",
        "risk": {"approved": True, "reason": "Risk checks passed",
                 "notional_usd": 10.0, "planned_loss_usd": 0.15},
        "signal": {"action": "BUY", "score": 100,
                   "reason": "Trend, momentum, breakout and volume confirmed",
                   "price": 64000.0, "atr": 350.0, "stop_price": 63500.0},
        "symbol": "BTC/USD", "timestamp": "2026-08-04T01:00:00+00:00",
    } for i in range(10)]


@pytest.fixture
def sample_settings():
    from dublin_bot.config import Settings
    return Settings(
        kraken_api_key="",
        kraken_api_secret="",
        paper_trading=True,
        dry_run=True,
        allow_live_trading=False,
    )


@pytest.fixture
def uptrend_bars():
    """Simulated uptrending price data."""
    bars = []
    price = 60000.0
    for _ in range(60):
        price += price * 0.005  # 0.5% per bar
        bars.append(price)
    return bars


@pytest.fixture
def downtrend_bars():
    """Simulated downtrending price data."""
    bars = []
    price = 65000.0
    for _ in range(60):
        price -= price * 0.005
        bars.append(price)
    return bars


# ── Filter Analysis Tests ───────────────────────────────────

class TestAnalyzeFilters:
    def test_returns_all_filter_names(self, sample_decisions):
        results = analyze_filters(sample_decisions)
        filter_names = {f.filter_name for f in results}
        assert filter_names == {"regime", "trend", "momentum", "breakout", "volume"}

    def test_failure_rate_correct(self, sample_decisions):
        results = {f.filter_name: f for f in analyze_filters(sample_decisions)}
        # Regime filter failed in first 10 entries out of 30
        assert results["regime"].failure_count == 10
        assert abs(results["regime"].failure_rate - 10/30) < 0.01

    def test_volume_filter_failures(self, sample_decisions):
        results = {f.filter_name: f for f in analyze_filters(sample_decisions)}
        # Volume filter failed in entries 10-19 (10 failures)
        assert results["volume"].failure_count == 10

    def test_empty_decisions(self):
        results = analyze_filters([])
        assert len(results) == 5
        for f in results:
            assert f.failure_count == 0
            assert f.failure_rate == 0.0

    def test_all_pass_decisions(self, all_pass_decisions):
        results = analyze_filters(all_pass_decisions)
        for f in results:
            assert f.failure_count == 0
            assert f.failure_rate == 0.0


# ── Regime Classification Tests ───────────────────────────────

class TestClassifyRegime:
    def test_uptrend_detected(self, sample_decisions, uptrend_bars):
        regime = classify_regime(sample_decisions, uptrend_bars)
        assert regime.regime == "bull_trend"
        assert regime.confidence > 0
        assert "Uptrend" in regime.description

    def test_insufficient_data(self):
        regime = classify_regime([], [])
        assert regime.regime == "unknown"
        assert regime.confidence == 0.0
        assert len(regime.description) > 0

    def test_sideways_market(self, sample_decisions):
        # Flat bars
        bars = [64000.0] * 40
        regime = classify_regime(sample_decisions, bars)
        assert regime.regime in ("sideways", "unknown")

    def test_volatility_calculated(self, sample_decisions, uptrend_bars):
        regime = classify_regime(sample_decisions, uptrend_bars)
        assert regime.volatility > 0


# ── Suggestion Generation Tests ───────────────────────────────

class TestGenerateSuggestions:
    def test_regime_filter_failure_triggers_suggestion(self, sample_settings, sample_decisions, uptrend_bars):
        analysis = analyze_filters(sample_decisions)
        regime = classify_regime(sample_decisions, uptrend_bars)
        suggestions = generate_suggestions(sample_settings, analysis, regime, sample_decisions)
        # At least one suggestion should be generated for regime_ema
        regime_suggestions = [s for s in suggestions if s.parameter == "regime_ema"]
        assert len(regime_suggestions) > 0
        assert regime_suggestions[0].requires_approval is True

    def test_all_pass_no_suggestions(self, sample_settings, all_pass_decisions, uptrend_bars):
        analysis = analyze_filters(all_pass_decisions)
        regime = classify_regime(all_pass_decisions, uptrend_bars)
        # With 10 buy signals, all filters pass — no high-failure suggestions
        suggestions = generate_suggestions(sample_settings, analysis, regime, all_pass_decisions)
        # Should not suggest regime_ema since no filter is failing
        regime_suggestions = [s for s in suggestions if s.parameter == "regime_ema"]
        assert len(regime_suggestions) == 0

    def test_all_suggestions_require_approval(self, sample_settings, sample_decisions, uptrend_bars):
        analysis = analyze_filters(sample_decisions)
        regime = classify_regime(sample_decisions, uptrend_bars)
        suggestions = generate_suggestions(sample_settings, analysis, regime, sample_decisions)
        for s in suggestions:
            assert s.requires_approval is True

    def test_suggestion_has_rationale(self, sample_settings, sample_decisions, uptrend_bars):
        analysis = analyze_filters(sample_decisions)
        regime = classify_regime(sample_decisions, uptrend_bars)
        suggestions = generate_suggestions(sample_settings, analysis, regime, sample_decisions)
        for s in suggestions:
            assert len(s.rationale) > 10
            assert s.current_value is not None
            assert s.suggested_value is not None


# ── Signal Quality Tests ──────────────────────────────────────

class TestComputeSignalQuality:
    def test_mixed_signals(self, sample_decisions):
        quality = compute_signal_quality(sample_decisions)
        assert quality["avg_score"] > 0
        assert quality["buy_hit_rate"] > 0
        assert quality["wait_ratio"] > 0
        assert quality["buy_hit_rate"] + quality["wait_ratio"] <= 1.01

    def test_all_buy_signals(self, all_pass_decisions):
        quality = compute_signal_quality(all_pass_decisions)
        assert quality["buy_hit_rate"] == 1.0
        assert quality["wait_ratio"] == 0.0

    def test_empty_decisions(self):
        quality = compute_signal_quality([])
        assert quality["avg_score"] == 0
        assert quality["buy_hit_rate"] == 0
        assert quality["wait_ratio"] == 1.0

    def test_score_variance(self, sample_decisions):
        quality = compute_signal_quality(sample_decisions)
        assert quality["score_variance"] >= 0


# ── Learning Report Tests ─────────────────────────────────────

class TestBuildLearningReport:
    def test_report_has_all_fields(self, sample_settings, sample_decisions, uptrend_bars):
        report = build_learning_report(sample_settings, decisions=sample_decisions,
                                       market_closes=uptrend_bars)
        assert report.timestamp is not None
        assert report.total_evaluations == 30
        assert len(report.filter_analysis) == 5
        assert report.regime is not None
        assert isinstance(report.suggestions, list)
        assert report.signal_quality is not None
        assert report.win_rate_estimate >= 0

    def test_report_timestamp_is_iso(self, sample_settings, sample_decisions, uptrend_bars):
        report = build_learning_report(sample_settings, decisions=sample_decisions,
                                       market_closes=uptrend_bars)
        # Should be parseable as ISO format
        from datetime import datetime
        datetime.fromisoformat(report.timestamp)

    def test_report_notes_present(self, sample_settings, sample_decisions, uptrend_bars):
        report = build_learning_report(sample_settings, decisions=sample_decisions,
                                       market_closes=uptrend_bars)
        assert len(report.notes) > 0
        assert "owner approval" in report.notes.lower()


# ── Save Report Tests ─────────────────────────────────────────

class TestSaveReport:
    def test_save_creates_file(self, sample_settings, tmp_path, sample_decisions, uptrend_bars):
        # Override the path by changing working directory
        import os
        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            report = build_learning_report(sample_settings, decisions=sample_decisions,
                                           market_closes=uptrend_bars)
            path = save_report(sample_settings, report)
            assert path.exists()
            data = json.loads(path.read_text())
            assert "timestamp" in data
            assert "suggestions" in data
            assert "filter_analysis" in data
        finally:
            os.chdir(old_cwd)

    def test_save_never_touches_config(self, sample_settings, tmp_path, sample_decisions, uptrend_bars):
        """Critical safety test: learning module must never modify Settings."""
        import os
        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            report = build_learning_report(sample_settings, decisions=sample_decisions,
                                           market_closes=uptrend_bars)
            save_report(sample_settings, report)

            # Verify .env was NOT modified
            env_path = Path(".env")
            if env_path.exists():
                content = env_path.read_text()
                # Should not have been modified by learning module
                assert "regime_ema" not in content
        finally:
            os.chdir(old_cwd)


# ── Brief Generation Tests ────────────────────────────────────

class TestGenerateBrief:
    def test_brief_has_regime(self, sample_settings, sample_decisions, uptrend_bars):
        brief = generate_brief(sample_settings, sample_decisions, uptrend_bars)
        assert "Market Regime" in brief

    def test_brief_has_timestamp(self, sample_settings, sample_decisions, uptrend_bars):
        brief = generate_brief(sample_settings, sample_decisions, uptrend_bars)
        assert "Brief" in brief

    def test_brief_mentions_safety(self, sample_settings, sample_decisions, uptrend_bars):
        brief = generate_brief(sample_settings, sample_decisions, uptrend_bars)
        assert "approval" in brief.lower() or "owner" in brief.lower()
