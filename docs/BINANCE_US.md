# Running on Binance.US

Set `BROKER=binance` (or `binance.us`) in `.env`. Everything else keeps working
the same way: the engine, the mean-reversion and trend-hold sleeves, the exit
watcher, the D1 regime filter, shadow scoring and telemetry, the dashboard and
the research scripts all read from Binance.US.

```env
BROKER=binance
BINANCE_API_KEY=...        # Read only at first; add Spot trading only for live
BINANCE_API_SECRET=...
BINANCE_HOST=binance.us    # default; binance.com or a full URL also accepted
BINANCE_USD_QUOTES=["USD","USDT"]
```

The safety locks are unchanged. In paper/dry-run mode every signed Binance
endpoint is refused before a request is built (`PAPER_BLOCK_PRIVATE_API`), so
paper trading needs no key at all. Live orders need the same three locks plus
the risk acknowledgement as on Kraken.

## How symbols map

| Canonical | Binance.US pair | Notes |
|---|---|---|
| `BTC/USD` | `BTCUSD` | real USD book |
| `ETH/USD` | `ETHUSD` | |
| `SOL/USD` | `SOLUSD` | |
| `XRP/USD` | `XRPUSD` | |
| `PUMP/USD` | `PUMPUSDT` | no USD book, so it falls back to USDT |

Resolution uses live `exchangeInfo`. Lot step, tick size and minimum notional
come from the pair's `LOT_SIZE` / `PRICE_FILTER` / `MIN_NOTIONAL` filters, and
every quantity and price rounds **down**. Equity counts USD, USDT and USDC as
cash.

## Differences from Kraken

* **Order ids** are `SYMBOL:orderId` (e.g. `BTCUSD:12345`), because Binance ids
  are unique only per symbol.
* **Idempotency.** `userref` is sent as `newClientOrderId=dublin-<userref>` and
  is recovered from open and historical orders after a crash.
* **Brackets.** Binance has no Kraken `close[...]` block. When the entry fills,
  the protective stop goes on as a separate `STOP_LOSS` sell
  (`dublin-<userref+1>`), and the take-profit is a resting `LIMIT`
  (`dublin-<userref+2>`). If the entry does not fill, no stop is armed and the
  engine fails closed as usual. There is no native trailing stop, so a trailing
  request becomes a fixed stop at the same distance.
* **Realized P&L** for the learner is rebuilt on an average-cost basis from
  `myTrades`. Binance does not report per-trade P&L.
* **Fees** come from `/sapi/v1/asset/query/trading-fee` (`fee_schedule()`). The
  paper fill model still uses `PAPER_TAKER_FEE_BPS` / `PAPER_MAKER_FEE_BPS`, so
  set those to your Binance.US tier.
* **Live prices for exits** come from the `<pair>@ticker` WebSocket on
  `wss://stream.binance.us:443`, with REST as the fallback. The Kraken tick-file
  pipeline is ignored on Binance, so venues' prices are never mixed.

## Not available on Binance.US (fails closed)

* **Futures short sleeve.** It models Kraken perpetuals, and Binance.US offers
  no futures. `sleeve_active()` reports it disabled.
* **Margin.** Binance.US is spot only; `margin_positions()` is always empty.
* **Live rotation executor** (`coordinator.py`, `STRATEGY=rotation` live). Its
  crash recovery depends on Kraken order semantics, so it refuses to start on
  Binance. Live trading runs through the main engine (`scripts/paper_trader_loop.py`).

## Liquidity note

Binance.US books are thinner than Kraken's. At the time of writing, 24h volume
was roughly BTCUSD $3.4M, ETHUSD $3.8M, XRPUSD $1.4M and SOLUSD $0.4M. With the
default `MIN_DOLLAR_VOLUME=1000000`, the market-quality gate will block SOL
entries on the USD book. That is the gate working as intended. Lower it
deliberately, or prefer the USDT book (`BINANCE_USD_QUOTES=["USDT","USD"]`),
only if you accept the thinner market.

## Verify

```bash
BROKER=binance python -m dublin_bot.cli health      # reachability, clock skew, freshness
BINANCE_LIVE_TESTS=1 pytest tests/test_binance_gateway.py -k live_binance
```
