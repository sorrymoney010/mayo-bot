"""Live-strategy backtest — uses the REAL trading code, REAL Kraken history.

No synthetic data. Pulls historical OHLC from Kraken's public API, then replays
the actual ``TrendBreakoutStrategy`` + ``RiskManager`` (vol-Kelly sizer) bar by
bar, charging realistic taker fees, and reports expectancy / win-rate / Sharpe.

Run:
    PYTHONPATH=src:. .venv/bin/python scripts/backtest.py --symbol XRP/USD --days 120
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from dublin_bot.config import Settings
from dublin_bot.engine import build_gateway
from dublin_bot.kraken_gateway import KrakenGateway
from dublin_bot.models import Action, Signal
from dublin_bot.risk import RiskManager, SessionState
from dublin_bot.strategy import build_strategy


# ── real historical bars from Kraken public OHLC ────────────────

def fetch_history(gateway: KrakenGateway, symbol: str, days: int, interval_min: int = 15) -> pd.DataFrame:
    """Fetch `days` of completed OHLC candles for `symbol` (e.g. XRP/USD)."""
    # Map our symbol to Kraken pair key.
    gw = KrakenGateway(gateway.settings)
    gw.settings.symbol = symbol
    meta = gw.resolve_symbol()
    since = int(time.time()) - days * 86400
    raw = gw._public(
        "OHLC",
        {"pair": meta.key, "interval": interval_min, "since": str(since)},
    )
    # Response: { "<pair>": [[time, o,h,l,c,v,vwap,trades,status], ...], "last": ... }
    pair_key = next(k for k in raw if k != "last")
    rows = raw[pair_key]
    df = pd.DataFrame(rows)
    # Kraken OHLC returns [time, open, high, low, close, vwap, volume, count]
    # (8 cols) — some pairs append a 9th status field; slice the first 8 safely.
    cols = ["time", "open", "high", "low", "close", "vwap", "volume", "count"]
    for ci, name in enumerate(cols):
        df[name] = df[ci].astype(float)
    df["time"] = df["time"].astype(float)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df = df[df["close"] > 0].sort_values("time").reset_index(drop=True)
    # drop the final, still-forming candle
    return df.iloc[:-1].reset_index(drop=True)


# ── replay ──────────────────────────────────────────────────────

@dataclass
class Trade:
    entry_time: float
    entry_price: float
    exit_time: float
    exit_price: float
    qty: float
    pnl: float          # net of fees, in quote (USD)
    pnl_pct: float     # net return on notional
    reason: str


@dataclass
class BacktestResult:
    symbol: str
    bars: int
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def expectancy_usd(self) -> float:
        return float(np.mean([t.pnl for t in self.trades])) if self.trades else 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    def sharpe(self, per_trade: bool = True) -> float:
        if len(self.trades) < 3:
            return 0.0
        rets = [t.pnl_pct for t in self.trades]
        sd = float(np.std(rets))
        return float(np.mean(rets) / sd * np.sqrt(252)) if sd > 0 else 0.0


def run_backtest(symbol: str, days: int, fee_rate: float = 0.0026,
                 start_equity: float = 1000.0, interval_min: int = 60,
                 strategy_name: str | None = None) -> BacktestResult:
    """Replay the REAL live strategy/risk pipeline bar-by-bar.

    Defaults to the strategy the bot is actually running (build_strategy), so the
    validation matches what is risking money. ``--strategy`` can pin a specific
    variant for comparison. Charges realistic taker fees on both legs.
    """
    settings = Settings(_env_file=None) if _has_envarg() else Settings()
    settings.symbol = symbol
    settings.timeframe_minutes = interval_min
    if strategy_name:
        settings.strategy = strategy_name
    gw = build_gateway(settings)
    bars = fetch_history(gw, symbol, days, interval_min)
    if bars.empty:
        raise RuntimeError(f"No history for {symbol}")

    strat = build_strategy(settings)  # live strategy factory
    risk = RiskManager(settings)

    equity = start_equity
    in_position = False
    entry_price = entry_qty = entry_notional = 0.0
    entry_time = 0.0
    result = BacktestResult(symbol=symbol, bars=len(bars))
    result.equity_curve.append(equity)

    # Walk bars; never peek ahead. Strategy sees up to bar i.
    for i in range(len(bars)):
        window = bars.iloc[: i + 1].copy()
        price = float(window.iloc[-1]["close"])
        atr = float(window.iloc[-1].get("high", price) - window.iloc[-1].get("low", price)) or price * 0.01

        signal = strat.evaluate(window, in_position=in_position)
        # Neutralize price/atr with realized values for the risk gate.
        signal = Signal(
            signal.action, signal.score, signal.reason, price,
            atr, signal.stop_price or (price - atr * settings.atr_stop_multiplier),
        )

        if not in_position:
            if signal.action is Action.BUY:
                state = SessionState(
                    start_equity=start_equity, peak_equity=start_equity,
                    current_equity=equity, orders_today=0,
                )
                decision = risk.evaluate(signal, state)
                if decision.approved and decision.notional_usd > 0:
                    notional = decision.notional_usd
                    qty = notional / price
                    entry_price, entry_qty, entry_notional = price, qty, notional
                    entry_time = float(window.iloc[-1]["time"])
                    in_position = True
        else:
            if signal.action is Action.SELL:
                # exit at this bar's close
                exit_price = price
                gross = entry_qty * exit_price
                fees = (entry_notional + gross) * fee_rate
                pnl = gross - entry_notional - fees
                pnl_pct = pnl / entry_notional
                equity += pnl
                result.trades.append(Trade(
                    entry_time=entry_time, entry_price=entry_price,
                    exit_time=float(window.iloc[-1]["time"]), exit_price=exit_price,
                    qty=entry_qty, pnl=pnl, pnl_pct=pnl_pct, reason=signal.reason,
                ))
                result.equity_curve.append(equity)
                in_position = False

    # Close any open position at the final bar for accounting.
    if in_position:
        price = float(bars.iloc[-1]["close"])
        gross = entry_qty * price
        fees = (entry_notional + gross) * fee_rate
        pnl = gross - entry_notional - fees
        equity += pnl
        result.trades.append(Trade(
            entry_time=entry_time, entry_price=entry_price,
            exit_time=float(bars.iloc[-1]["time"]), exit_price=price,
            qty=entry_qty, pnl=pnl, pnl_pct=pnl / entry_notional, reason="backtest-end close",
        ))
        result.equity_curve.append(equity)

    return result


def _has_envarg() -> bool:
    return any(a.startswith("--no-env") for a in sys.argv)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XRP/USD")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--fee", type=float, default=0.0026)
    ap.add_argument("--equity", type=float, default=1000.0)
    ap.add_argument("--interval", type=int, default=60,
                    help="candle interval in minutes (live bot uses 60)")
    ap.add_argument("--strategy", default=None,
                    help="override strategy (mean_reversion|momentum). "
                         "Default = the live bot's strategy (build_strategy).")
    args = ap.parse_args()

    print(f"Backtesting {args.symbol} over {args.days}d ({args.interval}m bars, "
          f"fee {args.fee*100:.2f}%) using the LIVE strategy pipeline ...")
    res = run_backtest(args.symbol, args.days, fee_rate=args.fee,
                       start_equity=args.equity, interval_min=args.interval,
                       strategy_name=args.strategy)

    print(f"\n=== {res.symbol} | {res.bars} bars | {res.n} trades ===")
    print(f"Win rate      : {res.win_rate*100:.1f}%  ({res.wins}/{res.n})")
    print(f"Expectancy    : ${res.expectancy_usd:.2f} / trade")
    print(f"Total net PnL : ${res.total_pnl:.2f}")
    print(f"Final equity  : ${res.equity_curve[-1]:.2f}  (start ${args.equity:.2f})")
    print(f"Sharpe (ann.) : {res.sharpe():.2f}")
    if res.trades:
        last = res.trades[-1]
        print(f"Last trade    : {last.reason} @ {last.entry_price:.4f} -> {last.exit_price:.4f}  pnl ${last.pnl:.2f}")


if __name__ == "__main__":
    main()
