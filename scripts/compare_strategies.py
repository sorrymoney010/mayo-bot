"""Strategy comparison harness — same REAL Kraken data, identical fees/risk.

Replays several signal variants through the SAME risk model and fee schedule so
the only variable is the entry/exit logic. Surfaces which variant (if any) has
positive expectancy before we commit it to live trading.

Run:
    PYTHONPATH=src:. .venv/bin/python scripts/compare_strategies.py --symbol XRP/USD --days 120
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import numpy as np
import pandas as pd

from dublin_bot.config import Settings
from dublin_bot.indicators import enrich
from dublin_bot.kraken_gateway import KrakenGateway
from dublin_bot.risk import RiskManager, SessionState


# ── real history (reuse backtest fetcher) ───────────────────────

def fetch_history(gw: KrakenGateway, symbol: str, days: int, interval_min: int = 15) -> pd.DataFrame:
    g = KrakenGateway(gw.settings)
    g.settings.symbol = symbol
    meta = g.resolve_symbol()
    import time
    since = int(time.time()) - days * 86400
    raw = g._public("OHLC", {"pair": meta.key, "interval": interval_min, "since": str(since)})
    pair_key = next(k for k in raw if k != "last")
    rows = raw[pair_key]
    df = pd.DataFrame(rows)
    cols = ["time", "open", "high", "low", "close", "vwap", "volume", "count"]
    for ci, name in enumerate(cols):
        df[name] = df[ci].astype(float)
    df = df[df["close"] > 0].sort_values("time").reset_index(drop=True)
    return df.iloc[:-1].reset_index(drop=True)


# ── signal variants (pure functions over an enriched window) ────
# Each returns (action, reason). action in {"BUY","SELL","WAIT"}.

def sig_current(df: pd.DataFrame, in_position: bool) -> tuple[str, str]:
    """Live strategy: momentum RSI band gate, SELL below slow EMA."""
    row = df.iloc[-1]
    price = float(row["close"])
    if in_position:
        if price < float(row["ema_slow"]):
            return "SELL", "below slow EMA"
        return "WAIT", "hold"
    rsi = float(row["rsi"])
    if 45.0 <= rsi <= 68.0:
        return "BUY", f"momentum rsi={rsi:.0f}"
    return "WAIT", f"rsi={rsi:.0f}"


def sig_meanreversion(df: pd.DataFrame, in_position: bool) -> tuple[str, str]:
    """Buy when oversold (RSI<32) and price stretched below slow EMA; sell on reversion."""
    row = df.iloc[-1]
    price = float(row["close"])
    rsi = float(row["rsi"])
    slow = float(row["ema_slow"])
    if in_position:
        if rsi > 55 or price >= slow:
            return "SELL", f"reversion rsi={rsi:.0f}"
        return "WAIT", "hold"
    if rsi < 32 and price < slow:
        return "BUY", f"oversold rsi={rsi:.0f}"
    return "WAIT", f"rsi={rsi:.0f}"


def sig_meanreversion_aggressive(df: pd.DataFrame, in_position: bool) -> tuple[str, str]:
    """Aggressive MR: wider entry band (RSI<40) but still requires price<slow EMA."""
    row = df.iloc[-1]
    price = float(row["close"])
    rsi = float(row["rsi"])
    slow = float(row["ema_slow"])
    if in_position:
        if rsi > 55 or price >= slow:
            return "SELL", f"reversion rsi={rsi:.0f}"
        return "WAIT", "hold"
    if rsi < 40 and price < slow:
        return "BUY", f"oversold rsi={rsi:.0f}"
    return "WAIT", f"rsi={rsi:.0f}"


def sig_regime_trend(df: pd.DataFrame, in_position: bool) -> tuple[str, str]:
    """Buy only when in uptrend (price>regime EMA) AND momentum gate; sell below slow EMA."""
    row = df.iloc[-1]
    price = float(row["close"])
    if in_position:
        if price < float(row["ema_slow"]):
            return "SELL", "below slow EMA"
        return "WAIT", "hold"
    rsi = float(row["rsi"])
    if 45.0 <= rsi <= 68.0 and price > float(row["ema_regime"]):
        return "BUY", f"trend+momentum rsi={rsi:.0f}"
    return "WAIT", f"rsi={rsi:.0f} regime={'up' if price>float(row['ema_regime']) else 'down'}"


def sig_trend_follow(df: pd.DataFrame, in_position: bool) -> tuple[str, str]:
    """Pure fast>slow EMA crossover; sell on cross-back."""
    row = df.iloc[-1]
    price = float(row["close"])
    if in_position:
        if float(row["ema_fast"]) < float(row["ema_slow"]):
            return "SELL", "fast<slow cross"
        return "WAIT", "hold"
    if float(row["ema_fast"]) > float(row["ema_slow"]) and price > float(row["ema_regime"]):
        return "BUY", "fast>slow cross"
    return "WAIT", "no cross"


def sig_meanreversion_sentiment(df, in_position, sentiment=None):
    """Mean-reversion entry gated by live sentiment: skip BUY when bearish.

    NOTE: real historical sentiment requires an archived news feed we don't have,
    so this applies the *current* sentiment reading as a constant overlay to
    demonstrate the filter's effect on entry frequency (not a true backtest).
    """
    row = df.iloc[-1]
    price = float(row["close"])
    rsi = float(row["rsi"])
    slow = float(row["ema_slow"])
    if in_position:
        if rsi > 55 or price >= slow:
            return "SELL", f"reversion rsi={rsi:.0f}"
        return "WAIT", "hold"
    if rsi < 32 and price < slow:
        if sentiment is not None and sentiment <= -0.15:
            return "WAIT", f"sentiment block ({sentiment:+.2f})"
        return "BUY", f"oversold rsi={rsi:.0f}"
    return "WAIT", f"rsi={rsi:.0f}"


def sig_meanreversion_loose(df, in_position) -> tuple[str, str]:
    """Looser MR: enter at RSI<=38 (was 32) to trade more often."""
    row = df.iloc[-1]
    price = float(row["close"])
    rsi = float(row["rsi"])
    slow = float(row["ema_slow"])
    if in_position:
        if rsi > 55 or price >= slow:
            return "SELL", f"reversion rsi={rsi:.0f}"
        return "WAIT", "hold"
    if rsi < 38 and price < slow:
        return "BUY", f"oversold rsi={rsi:.0f}"
    return "WAIT", f"rsi={rsi:.0f}"


def sig_combined(df, in_position) -> tuple[str, str]:
    """MR entry OR momentum breakout entry; same exits. More activity."""
    row = df.iloc[-1]
    price = float(row["close"])
    rsi = float(row["rsi"])
    slow = float(row["ema_slow"])
    if in_position:
        if rsi > 55 or price < slow:
            return "SELL", f"exit rsi={rsi:.0f}"
        return "WAIT", "hold"
    # MR: deep oversold
    if rsi < 32 and price < slow:
        return "BUY", f"MR rsi={rsi:.0f}"
    # Momentum: RSI in band + price reclaiming slow EMA (uptrend)
    if 45.0 <= rsi <= 68.0 and price > slow:
        return "BUY", f"momentum rsi={rsi:.0f}"
    return "WAIT", f"rsi={rsi:.0f}"


STRATEGIES = {
    "current": sig_current,
    "mean_reversion": sig_meanreversion,
    "mean_reversion_loose": sig_meanreversion_loose,
    "mean_reversion_aggressive": sig_meanreversion_aggressive,
    "mean_reversion_sentiment": ("sig", sig_meanreversion_sentiment),
    "combined_mr_momentum": sig_combined,
    "regime_trend": sig_regime_trend,
    "trend_follow": sig_trend_follow,
}


@dataclass
class VariantResult:
    name: str
    trades: int = 0
    wins: int = 0
    expectancy: float = 0.0
    total_pnl: float = 0.0
    final_equity: float = 0.0
    sharpe: float = 0.0
    max_dd: float = 0.0


def replay(symbol: str, days: int, fee_rate: float, start_equity: float,
           strat_fn, settings: Settings, sentiment=None) -> VariantResult:
    gw = KrakenGateway(settings)
    bars = fetch_history(gw, symbol, days)
    if bars.empty:
        return VariantResult(strat_fn.__name__)
    risk = RiskManager(settings)
    equity = start_equity
    peak = start_equity
    in_pos = False
    eq = en = 0.0
    res = VariantResult(strat_fn.__name__)
    pnls = []

    for i in range(len(bars)):
        window = enrich(
            bars.iloc[: i + 1].copy(),
            fast_ema=settings.fast_ema, slow_ema=settings.slow_ema,
            regime_ema=settings.regime_ema, rsi_period=settings.rsi_period,
            atr_period=settings.atr_period,
            breakout_lookback=settings.breakout_lookback,
            volume_lookback=settings.volume_lookback,
        ).dropna()
        if window.empty:
            continue
        price = float(window.iloc[-1]["close"])
        action, reason = strat_fn(window, in_pos, sentiment) if sentiment is not None else strat_fn(window, in_pos)
        if not in_pos:
            if action == "BUY":
                state = SessionState(start_equity=start_equity, peak_equity=start_equity,
                                     current_equity=equity, orders_today=0)
                # Build a minimal signal for the risk gate's notional sizing.
                from dublin_bot.models import Action, Signal
                sig = Signal(Action.BUY, 50, reason, price,
                             price * 0.01, price - price * 0.015)
                dec = risk.evaluate(sig, state)
                if dec.approved and dec.notional_usd > 0:
                    en = dec.notional_usd
                    eq = en / price
                    in_pos = True
        else:
            if action == "SELL":
                gross = eq * price
                fees = (en + gross) * fee_rate
                pnl = gross - en - fees
                equity += pnl
                pnls.append(pnl / en)
                peak = max(peak, equity)
                res.trades += 1
                res.total_pnl += pnl
                if pnl > 0:
                    res.wins += 1
                in_pos = False
    if in_pos:  # close at end
        price = float(bars.iloc[-1]["close"])
        gross = eq * price
        fees = (en + gross) * fee_rate
        pnl = gross - en - fees
        equity += pnl
        pnls.append(pnl / en)
        res.trades += 1
        res.total_pnl += pnl
        if pnl > 0:
            res.wins += 1
    res.final_equity = equity
    res.expectancy = res.total_pnl / res.trades if res.trades else 0.0
    res.sharpe = float(np.mean(pnls) / np.std(pnls) * np.sqrt(252)) if len(pnls) > 2 and np.std(pnls) > 0 else 0.0
    res.max_dd = (peak - equity) / peak if peak > 0 else 0.0
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XRP/USD")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--fee", type=float, default=0.0026)
    ap.add_argument("--equity", type=float, default=1000.0)
    args = ap.parse_args()

    settings = Settings()
    from dublin_bot.sentiment import SentimentAgent, SentimentConfig
    agent = SentimentAgent(SentimentConfig())
    coin = args.symbol.split("/")[0].upper()
    cur_sent = agent.index_for(args.symbol).score
    print(f"Comparing strategies on {args.symbol} | {args.days}d | 15m | fee {args.fee*100:.2f}%")
    print(f"Live sentiment overlay for {coin}: {cur_sent:+.2f} (applied to *sentiment variants only)\n")
    print(f"{'strategy':<26}{'trades':>7}{'win%':>7}{'exp/t':>9}{'net$':>10}{'final$':>10}{'sharpe':>8}{'maxDD':>7}")
    for name, entry in STRATEGIES.items():
        fn = entry[1] if isinstance(entry, tuple) else entry
        sent = cur_sent if isinstance(entry, tuple) else None
        r = replay(args.symbol, args.days, args.fee, args.equity, fn, settings, sentiment=sent)
        wr = f"{r.wins/r.trades*100:.0f}%" if r.trades else "-"
        print(f"{name:<26}{r.trades:>7}{wr:>7}{r.expectancy:>9.2f}{r.total_pnl:>10.2f}"
              f"{r.final_equity:>10.2f}{r.sharpe:>8.2f}{r.max_dd*100:>6.1f}%")


if __name__ == "__main__":
    main()
