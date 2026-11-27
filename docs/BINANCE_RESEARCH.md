# Binance.us strategy research (real data, out-of-sample)

Reproduce:

    python scripts/fetch_binance_history.py --symbols BTC ETH SOL --tfs 60 240
    python scripts/backtest_walkforward.py --cache-dir data/binance_us --prefix "" --tfs 60 240 \
        --symbols BTC/USD ETH/USD SOL/USD --fee-bps 10 --slip-bps 5 --maker-bps 10 --folds 5 \
        --max-gap-bars 24 --out /tmp/wf_base.json        # --fee-bps 20 --slip-bps 10 for the stress case
    python scripts/study_binance_regime.py               # robustness of the one survivor

Data: Binance.us public klines, BTC/ETH since 2019-09, SOL since 2020-09 (hourly and 4h, ~7y / ~6y).
Costs are ASSUMPTIONS (10 bps/side fee + 5 bps slippage; stress = double). Verify against your fee tier.
Method: the repo's walk-forward (tune on one segment, score on the next unseen one; 5 folds).

## Result (out-of-sample, all symbols pooled)
| tf | family | trades | net bps/trade (base) | (2x costs) |
|---|---|---|---|---|
| 1h | breakout / momentum / meanrev | 1500-4300 each | -24 / -27 / -49 | -52 / -56 / -79 |
| 1h | regime | 700 | -1 | -31 |
| 4h | breakout / momentum / meanrev | 630-1580 | -12 / -20 / -63 | -40 / -48 / -92 |
| **4h** | **regime** | **201** | **+135** | **+106** |

Almost everything loses after fees. The mean-reversion strategies lose the most on real data.

## The one survivor: regime_trend on 4h
* 201 OOS trades: +135 bps/trade, win rate 36%, profit factor 1.57. Bootstrap 95% CI [+19, +256] bps
  (P(mean<=0) 1.1%); at double costs CI [-10, +226].
* BTC +101, ETH +215, **SOL +87 only via walk-forward selection; fixed-parameter runs put SOL at ~0 or negative**
  -> trade BTC and ETH only.
* Years: positive 2021, 2023, 2024, 2025; **negative 2022 and 2026 YTD** (a trend strategy loses in chop).
* Robust to parameters: four neighbouring settings all give ~+90..99 bps/trade over ~350 trades.

## Caveats (read these)
* **Profits come from a few big winners.** Median trade is -219 bps. Without the 10 best trades (5%) the
  mean falls to +13 bps (base) / -16 bps (stress). Expect long losing stretches.
* **Selection bias.** This is the best of ~14 family/timeframe combinations; adjusting for that, the
  1.1% P-value is closer to ~15%. Treat the edge as plausible, not proven.
* Costs are assumed; real fills (esp. the exchange-side stop) will be worse than backtest.
* **Size of the prize:** ~35 trades/yr, ~10%/yr on capital (1% risk, 40% cap), worst closed-trade drawdown
  ~11-12%. That is ~$2.60/yr on $25 and ~$52/yr on $500. This is not a path to $100k by itself: the
  return is roughly a percentage of capital, so capital size dominates.
* Long-only spot: no profit when the market is falling; it just sits flat.
