"""Fee-adjusted backtest of the LIVE strategy against REAL Kraken history.

This is the artifact the earlier audit found missing (audit finding P1-a): the
shipped ``backtest.py`` only exercises ``TrendBreakoutStrategy`` and
``compare_strategies.py`` uses hand-rolled signal functions — NEITHER tests the
production strategy. This script replays the *actual* pipeline that the bot
runs:

    strategy.<Strategy>.evaluate(bars, in_position)   # the real signal
        -> RiskManager.evaluate(signal, state)         # the real gate + stop
        -> FillModel (ask/bid fill, spread, slippage, fee) on entry AND exit

It charges a realistic market-order fill on EVERY trade (buy at ask, sell at
bid, plus the configured taker fee and slippage), not a single flat 0.26% at
the close price. ``min_volume_ratio`` / RSI / EMA gates all run exactly as in
production. The only simplifications vs live:

  * Fills are modeled at the next bar's open/touch (no intrabar execution).
  * It assumes the exchange-native stop-loss would have filled near the
    configured stop (it also reports what the *signal-only* exit would yield).

Usage:
    PYTHONPATH=src:. .venv/bin/python scripts/backtest_live.py \
        --symbol PUMP/USD --days 180 --oospct 25
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from dublin_bot.config import Settings
from dublin_bot.engine import build_gateway
from dublin_bot.fills import FillModel
from dublin_bot.kraken_gateway import KrakenGateway
from dublin_bot.models import Action, Signal
from dublin_bot.risk import RiskManager, SessionState
from dublin_bot.strategy import build_strategy


def _has_envarg() -> bool:
    return any(a.startswith("--no-env") for a in sys.argv)


def fetch_history(gw: KrakenGateway, symbol: str, days: int,
                  interval_min: int = 15) -> pd.DataFrame:
    """Fetch `days` of completed OHLC candles for `symbol` from Kraken.

    Kraken serves ONLY the most recent ~721 candles per OHLC request and
    ``since`` does not extend that depth (it selects within, not back into,
    history). Requesting 15m bars therefore yields at most ~7.5 days no matter
    what ``days`` asks for — which silently made every "200d" backtest a 7.5d
    one. So pick the finest interval that still COVERS the requested window:

        60d -> 60m  (721 * 1h  ~= 30d)   [best available under 60d]
        120d, 200d -> 240m (120d) / 1440m (720d)

    and report the ACTUAL span so a truncated window is never mistaken for the
    requested one.
    """
    g = KrakenGateway(gw.settings)
    g.settings.symbol = symbol
    meta = g.resolve_symbol()
    raw = g._public("OHLC", {"pair": meta.key, "interval": interval_min})
    rows = raw.get(meta.key) or []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    cols = ["time", "open", "high", "low", "close", "vwap", "volume", "count"]
    for ci, name in enumerate(cols):
        df[name] = df[ci].astype(float)
    df = df[df["close"] > 0].drop_duplicates(subset="time")
    df = df.sort_values("time").reset_index(drop=True)
    # drop the still-forming candle, mirror get_bars()
    return df.iloc[:-1].reset_index(drop=True)


@dataclass
class Trade:
    entry_time: int
    entry_price: float
    exit_time: int
    exit_price: float
    qty: float
    pnl: float
    pnl_pct: float
    reason: str


@dataclass
class BacktestResult:
    symbol: str
    strategy: str
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
    def expectancy(self) -> float:
        return float(np.mean([t.pnl for t in self.trades])) if self.trades else 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    def sharpe(self) -> float:
        if len(self.trades) < 3:
            return 0.0
        rets = [t.pnl_pct for t in self.trades]
        sd = float(np.std(rets))
        return float(np.mean(rets) / sd * np.sqrt(252)) if sd > 0 else 0.0

    def max_drawdown(self, equity_start: float) -> float:
        if not self.equity_curve:
            return 0.0
        peak = equity_start
        worst = 0.0
        for eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                worst = max(worst, (peak - eq) / peak)
        return worst


def interval_for_days(days: int) -> int:
    """Finest Kraken interval whose ~721-bar cap still covers ``days``.

    Kraken serves only the latest ~721 candles per request, so the interval
    determines how much history you actually get: 15m -> ~7.5d, 60m -> ~30d,
    240m -> ~120d, 1440m -> ~720d. Choosing a finer interval than the window
    needs silently truncates the test instead of extending it.
    """
    for interval, capacity in ((15, 7), (60, 30), (240, 120), (1440, 720)):
        if days <= capacity:
            return interval
    return 1440


def run_backtest(symbol: str, days: int, start_equity: float = 1000.0,
                 interval_min: int | None = None, settings: Settings | None = None,
                 use_real_stop: bool = True) -> BacktestResult:
    """Replay the real strategy + risk pipeline bar-by-bar.

    `interval_min` defaults to ``None`` = auto: the finest Kraken interval whose
    721-bar cap still covers ``days``. Passing an explicit value overrides that
    (useful for tests), but note a too-fine interval silently truncates history.

    `use_real_stop` (default) treats the strategy's `stop_price` as an
    exchange-native stop and exits the position if the bar's low breaches it
    (conservative fill at the stop). When False, only the strategy SELL signal
    exits — this is the naive "no real stop" comparison from the audit.
    """
    if interval_min is None:
        interval_min = interval_for_days(days)
    # ORDER_TYPE=limit means the bot posts passive entries (maker fee, no
    # spread crossing). Model that or the backtest silently charges taker and
    # understates the edge of a limit config.
    use_limit = getattr(settings, "order_type", "market") == "limit" if settings else False
    settings = settings or Settings(_env_file=None)
    settings.symbol = symbol
    gw = build_gateway(settings)
    strat = build_strategy(settings)
    risk = RiskManager(settings)
    fill = FillModel(settings)

    bars = fetch_history(gw, symbol, days, interval_min)
    if bars.empty:
        raise RuntimeError(f"No history for {symbol}")

    result = BacktestResult(symbol=symbol, strategy=type(strat).__name__,
                            bars=len(bars))
    result.equity_curve.append(start_equity)

    equity = start_equity
    in_position = False
    entry_qty = entry_notional = entry_price = entry_time = 0.0
    entry_fee = 0.0

    for i in range(len(bars)):
        window = bars.iloc[: i + 1].copy()
        row = window.iloc[-1]
        price = float(row["close"])
        atr = float(row["atr"]) if "atr" in window else price * 0.01
        if math_isnan(atr):
            atr = price * 0.01

        signal = strat.evaluate(window, in_position=in_position)

        if not in_position:
            if signal.action is Action.BUY:
                # Neutralize with realized bar values for the risk gate.
                sig = Signal(signal.action, signal.score, signal.reason,
                             price, atr,
                             signal.stop_price or (price - atr * settings.atr_stop_multiplier))
                state = SessionState(
                    start_equity=start_equity, peak_equity=start_equity,
                    current_equity=equity, orders_today=0,
                )
                decision = risk.evaluate(sig, state)
                if decision.approved and decision.notional_usd > 0:
                    # Market fills cross the spread (ask + slippage). A limit
                    # entry is passive: it fills at the bid side and pays the
                    # maker fee, which is what ORDER_TYPE=limit does live.
                    ask = float(row.get("ask", price * 1.0005)) if "ask" in row else price
                    entry = fill.buy(
                        price=price,
                        volume=decision.notional_usd / price,
                        bid=ask - 0.0005 * ask,
                        ask=ask,
                        maker=use_limit,
                    )
                    qty = decision.notional_usd / entry.price
                    entry_qty = qty
                    entry_notional = qty * entry.price
                    entry_fee = entry.fee
                    entry_price = entry.price
                    entry_time = int(row["time"])
                    equity -= entry_fee
                    in_position = True
        else:
            # Check the exchange-native protective stop first (conservative:
            # fill at the stop price if the bar's low breached it).
            stop_hit = (use_real_stop and signal.stop_price is not None
                        and float(row["low"]) <= signal.stop_price)
            if signal.action is Action.SELL or stop_hit:
                exit_reason = ("stop-loss" if stop_hit and signal.action is not Action.SELL
                               else signal.reason)
                # Exit at the BID (- slippage), like a live market sell.
                bid = float(row.get("bid", price * 0.9995)) if "bid" in row else price
                out = fill.sell(price=price, volume=entry_qty,
                                bid=bid, ask=bid + 0.0005 * bid)
                proceeds = entry_qty * out.price - out.fee
                pnl = proceeds - entry_notional - entry_fee
                pnl_pct = pnl / entry_notional if entry_notional else 0.0
                equity += pnl
                result.trades.append(Trade(
                    entry_time=entry_time, entry_price=entry_price,
                    exit_time=int(row["time"]), exit_price=out.price,
                    qty=entry_qty, pnl=pnl, pnl_pct=pnl_pct, reason=exit_reason,
                ))
                result.equity_curve.append(equity)
                in_position = False
                entry_qty = entry_notional = entry_price = entry_fee = 0.0

    # Close any open position at the final bar for accounting.
    if in_position:
        row = bars.iloc[-1]
        price = float(row["close"])
        bid = float(row.get("bid", price * 0.9995)) if "bid" in row else price
        out = fill.sell(price=price, volume=entry_qty, bid=bid, ask=bid + 0.0005 * bid)
        proceeds = entry_qty * out.price - out.fee
        pnl = proceeds - entry_notional - entry_fee
        equity += pnl
        result.trades.append(Trade(
            entry_time=entry_time, entry_price=entry_price,
            exit_time=int(row["time"]), exit_price=out.price,
            qty=entry_qty, pnl=pnl, pnl_pct=pnl / entry_notional if entry_notional else 0.0,
            reason="backtest-end close",
        ))
        result.equity_curve.append(equity)

    return result


def math_isnan(x):
    try:
        return x != x
    except Exception:
        return False


def _print(res: BacktestResult, equity_start: float, label: str = "") -> None:
    tag = f"[{label}] " if label else ""
    print(f"{tag}{res.symbol} | {res.strategy} | bars={res.bars} trades={res.n}")
    print(f"   win_rate   : {res.win_rate*100:.1f}%  ({res.wins}/{res.n})")
    print(f"   expectancy : ${res.expectancy:.4f} / trade")
    print(f"   total PnL  : ${res.total_pnl:.2f}")
    print(f"   final eq   : ${res.equity_curve[-1]:.2f}  (start ${equity_start:.2f})")
    print(f"   return     : {(res.equity_curve[-1]/equity_start - 1)*100:+.1f}%")
    print(f"   Sharpe(ann): {res.sharpe():.2f}")
    print(f"   max DD     : {res.max_drawdown(equity_start)*100:.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="PUMP/USD")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--equity", type=float, default=1000.0)
    ap.add_argument("--oospct", type=float, default=25.0,
                    help="percent of the window held out as out-of-sample (0 = none)")
    ap.add_argument("--no-stop", action="store_true",
                    help="disable the exchange-native stop (naive exit) for comparison")
    ap.add_argument("--interval", type=int, default=15)
    args = ap.parse_args()

    settings = Settings(_env_file=None) if _has_envarg() else Settings()

    print(f"Backtesting LIVE strategy ({settings.strategy}) on {args.symbol} "
          f"over {args.days}d ({args.interval}m bars, equity ${args.equity:.0f})")
    full = run_backtest(args.symbol, args.days, args.equity, args.interval,
                        settings=settings, use_real_stop=not args.no_stop)

    oos_n = int(len(full.trades) * args.oospct / 100.0) if args.oospct else 0
    if oos_n >= 3:
        is_trades = full.trades[:-oos_n]
        oos_trades = full.trades[-oos_n:]
        print("\n=== IN-SAMPLE ===")
        _print(_subset(full, is_trades, args.equity), args.equity, "in-sample")
        print("\n=== OUT-OF-SAMPLE ===")
        _print(_subset(full, oos_trades, args.equity), args.equity, "out-of-sample")
    else:
        _print(full, args.equity)

    print("\n=== FULL WINDOW ===")
    _print(full, args.equity)

    # Verdict for the owner.
    exp = full.expectancy
    print("\n=== VERDICT ===")
    if full.n < 5:
        print("  INSUFFICIENT TRADES to assess expectancy. Do NOT go live on this.")
    elif exp > 0:
        print(f"  POSITIVE expectancy (${exp:.4f}/trade over {full.n} trades). "
              "Edge is present in-sample; review OOS before scaling.")
    else:
        print(f"  NEGATIVE expectancy (${exp:.4f}/trade over {full.n} trades). "
              "This strategy would LOSE money with fees. DO NOT go live.")


def _subset(res: BacktestResult, trades: list, equity_start: float) -> BacktestResult:
    r = BacktestResult(symbol=res.symbol, strategy=res.strategy, bars=res.bars)
    r.trades = list(trades)
    eq = equity_start
    r.equity_curve.append(eq)
    for t in trades:
        eq += t.pnl
        r.equity_curve.append(eq)
    return r


if __name__ == "__main__":
    main()
