from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings


@dataclass
class FilterAnalysis:
    """Analysis of which strategy filters fail most often and why."""
    filter_name: str
    failure_count: int
    total_evaluations: int
    failure_rate: float
    avg_score_when_failed: float
    correlated_with_buy: bool = False


@dataclass
class OptimizationSuggestion:
    """A suggested parameter change with confidence and rationale."""
    parameter: str
    current_value: Any
    suggested_value: Any
    confidence: float  # 0.0 to 1.0
    rationale: str
    backtest_winnable: bool = False  # Has been backtested
    requires_approval: bool = True


@dataclass
class MarketRegime:
    """Classified market regime with confidence."""
    regime: str  # "bull_trend", "bear_trend", "sideways", "range_bound"
    confidence: float
    description: str
    volatility: float
    volume_trend: str


@dataclass
class LearningReport:
    """Complete learning report — saved to disk, requires owner approval to act on."""
    timestamp: str
    total_evaluations: int
    filter_analysis: list[FilterAnalysis]
    regime: MarketRegime
    suggestions: list[OptimizationSuggestion]
    signal_quality: dict[str, float]
    win_rate_estimate: float
    notes: str = ""


def analyze_filters(decisions: list[dict]) -> list[FilterAnalysis]:
    """Analyze which strategy filters fail most often across recent decisions.

    For each filter, we count:
    - How many times it appears in the "Filters failed:" list (failure_count)
    - The score values recorded during those failures (for avg_score_when_failed)
    """
    all_filters = {"regime", "trend", "momentum", "breakout", "volume"}
    filter_failures: dict[str, list[int]] = {f: [] for f in all_filters}
    total = len(decisions)
    buy_when_pass: dict[str, list[bool]] = {f: [] for f in all_filters}

    for entry in decisions:
        reason: str = entry.get("signal", {}).get("reason", "")
        score: int = entry.get("signal", {}).get("score", 0)
        action: str = entry.get("signal", {}).get("action", "WAIT")

        if "Filters failed:" in reason:
            failed_str = reason.replace("Filters failed:", "").strip()
            failed_filters = {f.strip() for f in failed_str.split(",")}
            for f in all_filters:
                if f in failed_filters:
                    filter_failures[f].append(score)
                else:
                    buy_when_pass[f].append(action == "BUY")
        else:
            # All filters passed
            for f in all_filters:
                buy_when_pass[f].append(action == "BUY")

    results: list[FilterAnalysis] = []
    for f in sorted(all_filters):
        failure_count = len(filter_failures[f])
        failure_rate = failure_count / max(total, 1)
        avg_score = statistics.mean(filter_failures[f]) if filter_failures[f] else 0.0
        buy_rate_when_pass = (sum(buy_when_pass[f]) / len(buy_when_pass[f])
                              if buy_when_pass[f] else 0.0)

        results.append(FilterAnalysis(
            filter_name=f,
            failure_count=failure_count,
            total_evaluations=total,
            failure_rate=round(failure_rate, 3),
            avg_score_when_failed=round(avg_score, 1),
            correlated_with_buy=buy_rate_when_pass > 0.3,
        ))

    return results


def classify_regime(decisions: list[dict], bars: list[float] | None = None) -> MarketRegime:
    """Classify the current market regime from recent price action."""
    if not decisions or not bars or len(bars) < 20:
        return MarketRegime(
            regime="unknown",
            confidence=0.0,
            description="Insufficient data for regime classification",
            volatility=0.0,
            volume_trend="unknown",
        )

    # Price trend: compare recent avg to older avg
    recent = bars[-20:]
    older = bars[-60:-20] if len(bars) >= 60 else bars[:20]

    recent_avg = statistics.mean(recent) if recent else 0
    older_avg = statistics.mean(older) if older else 0

    if older_avg > 0:
        trend = (recent_avg / older_avg - 1) * 100
    else:
        trend = 0

    # Volatility
    volatility = statistics.stdev(recent) / recent_avg * 100 if recent_avg and len(recent) > 1 else 0

    # Classify
    if trend > 2:
        regime = "bull_trend"
        confidence = min(abs(trend) / 10, 0.95)
        desc = f"Uptrend detected ({trend:+.1f}% vs 20-bar ago)"
    elif trend < -2:
        regime = "bear_trend"
        confidence = min(abs(trend) / 10, 0.95)
        desc = f"Downtrend detected ({trend:+.1f}% vs 20-bar ago)"
    elif volatility < 1.5:
        regime = "sideways"
        confidence = min((1.5 - volatility) / 1.5, 0.95)
        desc = f"Sideways/range-bound (low volatility {volatility:.1f}%)"
    else:
        regime = "range_bound"
        confidence = min(volatility / 5, 0.95)
        desc = f"Range-bound with {volatility:.1f}% volatility"

    return MarketRegime(
        regime=regime,
        confidence=round(confidence, 3),
        description=desc,
        volatility=round(volatility, 2),
        volume_trend="recent" if len(decisions) > 10 else "unknown",
    )


def generate_suggestions(
    settings: Settings,
    filter_analysis: list[FilterAnalysis],
    regime: MarketRegime,
    decisions: list[dict],
) -> list[OptimizationSuggestion]:
    """Generate parameter optimization suggestions based on analysis."""
    suggestions: list[OptimizationSuggestion] = []

    # 1. Check if regime filter is failing most — suggest regime EMA adjustment
    regime_filter = next((f for f in filter_analysis if f.filter_name == "regime"), None)
    if regime_filter and regime_filter.failure_rate > 0.3:
        suggestions.append(OptimizationSuggestion(
            parameter="regime_ema",
            current_value=settings.regime_ema,
            suggested_value=settings.regime_ema if regime.confidence < 0.5 else max(settings.regime_ema - 20, 50),
            confidence=round(regime_filter.failure_rate, 3),
            rationale=f"Regime filter failing {regime_filter.failure_rate:.0%} of the time. "
                      f"Market regime: {regime.description}. Suggests regime EMA period adjustment.",
        ))

    # 2. Check volume filter failures
    vol_filter = next((f for f in filter_analysis if f.filter_name == "volume"), None)
    if vol_filter and vol_filter.failure_rate > 0.6:
        suggestions.append(OptimizationSuggestion(
            parameter="min_volume_ratio",
            current_value=settings.min_volume_ratio,
            suggested_value=round(settings.min_volume_ratio - 0.05, 2),
            confidence=round(vol_filter.failure_rate, 3),
            rationale=f"Volume filter failing {vol_filter.failure_rate:.0%} of evaluations. "
                      "Lowering minimum volume ratio would increase trade frequency.",
        ))

    # 3. Check breakout failures
    br_filter = next((f for f in filter_analysis if f.filter_name == "breakout"), None)
    if br_filter and br_filter.failure_rate > 0.6:
        suggestions.append(OptimizationSuggestion(
            parameter="breakout_lookback",
            current_value=settings.breakout_lookback,
            suggested_value=settings.breakout_lookback,
            confidence=round(br_filter.failure_rate, 3),
            rationale=f"Breakout filter failing {br_filter.failure_rate:.0%} of evaluations. "
                      "Consider shortening lookback period during high-volatility regimes.",
        ))

    # 4. Risk adjustment based on regime
    if regime.regime in ("bull_trend", "sideways") and regime.confidence > 0.6:
        suggestions.append(OptimizationSuggestion(
            parameter="risk_per_trade",
            current_value=settings.risk_per_trade,
            suggested_value=round(settings.risk_per_trade * 1.5, 3),
            confidence=regime.confidence,
            rationale=f"Current regime ({regime.regime}) shows {regime.confidence:.0%} confidence. "
                      "Increasing risk per trade may be appropriate in trending markets.",
        ))

    # 5. Cooldown adjustment if many WAIT signals
    wait_count = len([d for d in decisions if d.get("signal", {}).get("action") == "WAIT"])
    total = len(decisions)
    if total > 5 and wait_count / total > 0.9:
        suggestions.append(OptimizationSuggestion(
            parameter="cooldown_minutes",
            current_value=settings.cooldown_minutes,
            suggested_value=max(settings.cooldown_minutes - 30, 15),
            confidence=round(wait_count / total, 3),
            rationale=f"{wait_count}/{total} decisions are WAIT. Strategy is too conservative. "
                      "Reducing cooldown may capture more opportunities.",
        ))

    return suggestions


def compute_signal_quality(decisions: list[dict]) -> dict[str, float]:
    """Compute signal quality metrics."""
    if not decisions:
        return {"avg_score": 0, "score_variance": 0, "buy_hit_rate": 0, "wait_ratio": 1.0}

    scores = [d.get("signal", {}).get("score", 0) for d in decisions]
    buy_signals = len([d for d in decisions if d.get("signal", {}).get("action") == "BUY"])
    sell_signals = len([d for d in decisions if d.get("signal", {}).get("action") == "SELL"])
    wait_signals = len([d for d in decisions if d.get("signal", {}).get("action") == "WAIT"])
    total = len(decisions)

    return {
        "avg_score": round(statistics.mean(scores), 1),
        "score_variance": round(statistics.variance(scores) if len(scores) > 1 else 0, 1),
        "buy_hit_rate": round(buy_signals / total, 3) if total else 0,
        "sell_hit_rate": round(sell_signals / total, 3) if total else 0,
        "wait_ratio": round(wait_signals / total, 3) if total else 1.0,
    }


def build_learning_report(
    settings: Settings,
    decisions: list[dict] | None = None,
    market_closes: list[float] | None = None,
) -> LearningReport:
    """Build a complete learning report from session data."""
    if decisions is None:
        from .dashboard import recent_activity
        decisions = recent_activity(settings.journal_path, limit=200)

    # Get market data for regime classification
    if market_closes is None:
        try:
            from .engine import build_gateway
            gateway = build_gateway(settings)
            bars = gateway.get_bars().tail(60)
            market_closes = [round(float(v), 2) for v in bars["close"].tolist()]
        except Exception:
            market_closes = []

    filter_analysis = analyze_filters(decisions)
    regime = classify_regime(decisions, market_closes)
    suggestions = generate_suggestions(settings, filter_analysis, regime, decisions)
    signal_quality = compute_signal_quality(decisions)

    # Win rate estimate: in dry-run we approximate from buy approvals vs total signals
    buy_signals = len([d for d in decisions if d.get("signal", {}).get("action") == "BUY"])
    win_estimate = (buy_signals / len(decisions) * 100) if decisions else 0

    return LearningReport(
        timestamp=datetime.now(timezone.utc).isoformat(),
        total_evaluations=len(decisions),
        filter_analysis=filter_analysis,
        regime=regime,
        suggestions=suggestions,
        signal_quality=signal_quality,
        win_rate_estimate=round(win_estimate, 1),
        notes="Report generated in paper-trading mode. All suggestions require owner "
              "approval and backtest validation before application.",
    )


def save_report(settings: Settings, report: LearningReport) -> Path:
    """Save learning report to disk. Never modifies live config."""
    report_path = Path("logs/learning_report.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "timestamp": report.timestamp,
        "total_evaluations": report.total_evaluations,
        "signal_quality": report.signal_quality,
        "win_rate_estimate": report.win_rate_estimate,
        "regime": {
            "regime": report.regime.regime,
            "confidence": report.regime.confidence,
            "description": report.regime.description,
            "volatility": report.regime.volatility,
            "volume_trend": report.regime.volume_trend,
        },
        "filter_analysis": [
            {
                "filter_name": f.filter_name,
                "failure_count": f.failure_count,
                "total_evaluations": f.total_evaluations,
                "failure_rate": f.failure_rate,
                "avg_score_when_failed": f.avg_score_when_failed,
                "correlated_with_buy": f.correlated_with_buy,
            }
            for f in report.filter_analysis
        ],
        "suggestions": [
            {
                "parameter": s.parameter,
                "current_value": s.current_value,
                "suggested_value": s.suggested_value,
                "confidence": s.confidence,
                "rationale": s.rationale,
                "backtest_winnable": s.backtest_winnable,
                "requires_approval": s.requires_approval,
            }
            for s in report.suggestions
        ],
        "notes": report.notes,
    }
    tmp = report_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(report_path)
    return report_path


def generate_brief(
    settings: Settings,
    decisions: list[dict] | None = None,
    market_closes: list[float] | None = None,
) -> str:
    """Generate a human-readable daily brief from the learning report.

    Callers may provide already-loaded decisions and closes. Besides making
    batch use cheaper, this keeps tests and other offline workflows from
    silently reaching a market-data service.
    """
    report = build_learning_report(
        settings,
        decisions=decisions,
        market_closes=market_closes,
    )
    lines = [
        f"=== Dublin Daily Brief ({report.timestamp[:10]}) ===",
        f"Market Regime: {report.regime.description}",
        f"Confidence: {report.regime.confidence:.0%}",
        f"Evaluations: {report.total_evaluations}",
        f"Avg Score: {report.signal_quality.get('avg_score', 0)}",
        f"Wait Ratio: {report.signal_quality.get('wait_ratio', 0):.0%}",
    ]
    if report.suggestions:
        lines.append("\nSuggested parameter changes:")
        for s in report.suggestions[:3]:
            lines.append(f"  • {s.parameter}: {s.current_value} → {s.suggested_value} "
                        f"(confidence {s.confidence:.0%}) — {s.rationale}")
    else:
        lines.append("No optimization suggestions at this time.")
    lines.append(f"\nNOTE: {report.notes}")
    return "\n".join(lines)
