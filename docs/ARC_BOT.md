# arc_bot: futures eval bot (separate from Mayo)

Targets the DayTraders 25K EOD evaluation (MES/MNQ micros). Independent of `dublin_bot`.
**Backtest/research only. No broker connection, no live orders.**

## Rules modelled (`rules.py`, verify against the firm's current page)
$25k start, $1,000 target, $800 EOD trailing drawdown (lock at start balance is an
*assumption*), 60 micros max, 2 qualifying days (>= $100), 50% consistency, $1,500 daily limit.

## Strategy (`strategy.py`)
Opening-range breakout: 15-min range, range-size filter vs 10-day avg range, VWAP-side filter,
stop at range midpoint, 2R target, breakeven at +1R, flat 15:55 ET, one trade/day.
Sizing: $120 risk/trade, further capped to half of remaining drawdown cushion.
Own daily stop $300 / daily target $450 (keeps the consistency rule satisfiable).

## Usage
    arc-bot --csv mes_1m.csv --symbol MES --mc 5000
CSV: `timestamp,open,high,low,close,volume`, 1-minute bars (UTC if naive).

## What must happen before real money
1. Get >= 2-3 years of real 1m MES/MNQ data (e.g. Databento, Rithmic history).
2. Split in/out-of-sample; tune on the first part only; report the out-of-sample result.
3. Feed OOS daily P&L to `monte_carlo_pass` for a pass/blow probability.
4. Forward-test on the free ArcTrader demo before paying for an eval.
5. Only then build execution (Rithmic R|Protocol or ArcTrader) behind a kill switch.

Synthetic data (`--synthetic-days`) is a random walk: it has no edge by construction and
exists only to test plumbing. No result here implies a profitable strategy.

## Strategies (pluggable)
- `orb` momentum breakout (above). `meanrev` fades |z|>=2.2 stretches from VWAP after a
  reversal bar, target = VWAP, stop = 3x ATR (<=12 pts), skips trend days, max 2 trades/day.

## Comparing
    arc-bot --regimes --synthetic-days 250      # walk / revert / trend synthetic regimes
    arc-bot --csv mes_1m.csv --strategy compare # in-sample vs OUT-OF-SAMPLE, both strategies
Judge only the OUT-OF-SAMPLE block on real data.
