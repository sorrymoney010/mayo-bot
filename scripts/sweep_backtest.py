"""Walk-forward backtest sweep across strategies x coins x windows.

Reuses scripts/backtest_live.run_backtest (the REAL strategy+risk+fill
pipeline) but forces Settings.strategy per run via env, so we validate the
actual production code path for both live strategies.

For each (strategy, coin, window) we report in-sample metrics over the full
window AND an out-of-sample split (last 25% of trades by time) to check the
edge holds forward.

Usage:
    PYTHONPATH=src:. python scripts/sweep_backtest.py
"""
from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, "src")
sys.path.insert(0, ".")

import backtest_live as bl  # noqa: E402

COINS = ["PUMP/USD", "BTC/USD", "XRP/USD", "DOGE/USD", "SOL/USD"]
WINDOWS = [60, 120, 200]
STRATEGIES = ["momentum", "mean_reversion"]
# Maker entries pay half the taker fee (40 vs 80 bps on this tier), so entry
# style is a first-class sweep dimension — the live bot's ORDER_TYPE decides
# which of these rows describes reality.
ORDER_TYPES = ["market", "limit"]
EQUITY = 80.0


def _oos_expectancy(res: "bl.BacktestResult") -> float | None:
    """Expectancy of the last 25% of trades (by exit time) = out-of-sample."""
    if len(res.trades) < 4:
        return None
    ts = sorted(res.trades, key=lambda t: t.exit_time)
    cut = max(1, len(ts) // 4)
    oos = ts[-cut:]
    return sum(t.pnl for t in oos) / len(oos)


def main() -> None:
    rows = []
    for strat in STRATEGIES:
        os.environ["STRATEGY"] = strat
        for sym in COINS:
            for days in WINDOWS:
                for otype in ORDER_TYPES:
                    try:
                        cfg = bl.Settings(_env_file=None)
                        cfg.strategy = strat
                        cfg.order_type = otype
                        r = bl.run_backtest(symbol=sym, days=days,
                                            start_equity=EQUITY, settings=cfg)
                        oos = _oos_expectancy(r)
                        net_pct = (r.equity_curve[-1] / EQUITY - 1) * 100 if r.equity_curve else 0.0
                        rows.append({
                            "strategy": strat, "symbol": sym, "days": days,
                            "order_type": otype,
                            "trades": r.n, "win_rate": round(r.win_rate * 100, 1),
                            "expectancy": round(r.expectancy, 4),
                            "net_pct": round(net_pct, 2),
                            "oos_expectancy": (round(oos, 4) if oos is not None else None),
                            "max_dd": round(r.max_drawdown(EQUITY) * 100, 2),
                            "sharpe": round(r.sharpe(), 3),
                        })
                        print(f"{strat:12s} {sym:9s} {days:>4}d {otype:6s} "
                              f"trades={r.n:>3} "
                              f"win={r.win_rate*100:4.1f}% exp={r.expectancy:+.4f} "
                              f"net={net_pct:+6.2f}% oos={oos} dd={r.max_drawdown(EQUITY)*100:4.1f}%",
                              flush=True)
                    except Exception as e:  # noqa: BLE001
                        rows.append({"strategy": strat, "symbol": sym, "days": days,
                                     "order_type": otype, "error": str(e)[:60]})
                        print(f"{strat:12s} {sym:9s} {days:>4}d {otype:6s} ERR {e}",
                              flush=True)
    with open("backtest_sweep.json", "w") as f:
        json.dump(rows, f, indent=2)
    print("\nSaved backtest_sweep.json")


if __name__ == "__main__":
    main()
