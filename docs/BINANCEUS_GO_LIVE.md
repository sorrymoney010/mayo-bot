# Binance.US: from backtest to live

Everything runs through one script, `scripts/binanceus_runner.py`, plus
`scripts/binanceus_backtest.py` for history. Strategies (long-only spot):

| Sleeve | Timeframe | Entry | Protection |
|---|---|---|---|
| `regime_1h` | 1h | market | 3% stop resting on Binance.US, 25% target, chandelier/chop exit |
| `regime_4h_mk` | 4h | limit 0.1% under close, one bar | same |
| `meanrev_4h_mk` | 4h | limit 0.1% under close, one bar | 3% resting stop, 25% target, reverted exit |

Coins default to BTC/USD, ETH/USD, SOL/USD (`--symbols` to change).

## Step 1: backtest on Binance.US prices (10 minutes)

```bash
python scripts/binanceus_backtest.py                 # 2 years, BTC/ETH/SOL
python scripts/binanceus_backtest.py --days 1095     # 3 years
```

The verdict line per sleeve says **PASS** only with 30+ out-of-sample trades,
profit factor ≥ 1.10, and a positive result with heavy slippage. **Only take
PASS sleeves live** (`--sleeves meanrev_4h_mk`, for example). If nothing
passes, stop here; live trading would just pay for the lesson.

## Step 2: paper on Binance.US (no keys, no money)

```bash
python scripts/binanceus_runner.py --check
python scripts/binanceus_runner.py --sleeves <the PASS sleeves>
python scripts/binanceus_runner.py --report
```

Real Binance.US prices, simulated fills (real fees, spreads and stop-limit gap
behavior). Let it run at least 2–4 weeks and confirm the trades look like the
backtest: similar win rate, similar average, no surprise errors in
`logs/binanceus_paper.log`.

## Step 3: API key (Binance.US → Profile → API Management)

- Enable **trading**. Keep **withdrawals OFF**. `--check` warns if they're on.
- Restrict the key to your home IP if you can.
- Put it in `.env` as `BINANCE_API_KEY` / `BINANCE_API_SECRET`. Never paste it
  into chat, email or a commit.

```bash
python scripts/binanceus_runner.py --test-order
```

This sends signed **test** orders (a limit buy and a stop-loss-limit sell) to
`/api/v3/order/test`. Binance.US checks the key, signature, permissions and
order format, and places nothing.

## Step 4: go live small

Fund the account with what you're willing to lose while proving it (the
defaults assume about $100). In `.env`:

```
ALLOW_LIVE_TRADING=true
PAPER_TRADING=false
DRY_RUN=false
LIVE_RISK_ACKNOWLEDGEMENT=I_ACCEPT_LIVE_TRADING_RISK
BINANCEUS_BUDGET_USD=100
BINANCEUS_MAX_TRADE_USD=25
```

```bash
python scripts/binanceus_runner.py --live --check     # must end "READY"
python scripts/binanceus_runner.py --live --sleeves <PASS sleeves>
```

Note: those four lock lines also unlock the Kraken engine. Don't run
`paper_trader_loop.py` or the coordinator on the same machine while they're set.

### What happens on each trade

1. Signal on a closed bar → buy (market, or a one-bar limit).
2. As soon as it fills, a **STOP_LOSS_LIMIT sell rests on Binance.US** at −3%
   (limit 1% below the trigger). It protects the position even if your Mac is
   off or offline.
3. The runner checks every 60 s. A signal exit or the 25% target cancels the
   stop and sells at market. If price gaps below the stop's limit (the stop
   can't fill), the runner cancels it and sells at market.
4. It only ever sells the coins it bought. BTC/ETH/SOL you already hold on the
   account are never touched.

### Limits that always apply

- `BINANCEUS_MAX_TRADE_USD` caps every live trade.
- At most 6 new entries a day; new entries stop for the day after a 3% loss of
  budget.
- Orders have deterministic ids, so a crash or restart can't double an order,
  and entries only fire within 15 minutes of a bar closing.
- One runner per machine (lock file).

### Stop buttons

- **Pause new trades:** `touch data/binanceus_live/KILL` (delete it to resume).
  Open positions keep their exchange stops.
- **Get out of everything the bot holds:**
  `python scripts/binanceus_runner.py --live --flatten`
- **Hard stop:** Ctrl-C the runner, then cancel orders in the Binance.US app.
  Resting stops stay on the exchange until you cancel them.

## Step 5: judge it

```bash
python scripts/binanceus_runner.py --live --report
```

After 30+ live trades, compare with the paper and backtest numbers. Raise the
budget only if live matches paper. If live is clearly worse (fills, spreads,
slippage), go back to paper.

## Known limits

- Binance.US volume is thin, especially outside BTC/ETH. `--check` prints each
  pair's spread; above ~30 bps, market exits cost real money.
- Stop-limits can be skipped in a fast crash; the runner's gap fallback sells
  at market, which can fill far below −3%.
- Binance.US fees "may change". `--check` prints the account's actual rates;
  update `BINANCEUS_*_FEE_BPS` if they differ.
- Built and tested against a simulated exchange; it has not yet sent a real
  order. Step 3's `--test-order` is the first real contact.
