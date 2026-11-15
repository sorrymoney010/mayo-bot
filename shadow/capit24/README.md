# CAPIT-24 shadow signal log

**Signal log only.** It places no orders, uses no keys and reads public data only. It is a standalone process and does not touch `paper_trader_loop.py`, its lock, its book or any trading flag. Approved by the user on 2026-11-14 ("A and C"). The two extra variants (R2 market filter and OI-flush confirmation) were added 2026-11-15 so all three can build forward proof at once, still on paper only.

## Rule (frozen; see reports/edge_search_2026-11-14.md)

Shared by every variant:
- **Bars:** each closed 1h bar for BTC ETH SOL XRP DOGE ADA AVAX LINK LTC BNB DOT TRX on Binance.US.
- **Price-drop condition:** `z24 = 24h log return / (stdev of the previous 720 1h log returns * sqrt(24)) <= -2.5`.
- **Volume condition:** `24h quote volume >= 2 x (mean hourly quote volume over the 720h ending 24h earlier * 24)`.
- **Trade:** a hypothetical buy at the next 1h open, sold 48h after entry at that bar's open.
- **Limits:** at most 2 concurrent positions *per variant*, one per coin. When signals tie, the most negative z goes first.
- **Sizing:** each slot is 50% of a notional $466.81 ($233.405), non-compounded.
- **Seed:** 2026-11-14 00:00 UTC. The Oct 1–Nov 13 holdout trades do **not** count toward the 30.

### Variants (independent books)

| Key | Label | Extra filter |
| --- | --- | --- |
| `base` | CAPIT-24 | None. The original frozen rule. |
| `r2` | CAPIT-24\|R2 | Take the CAPIT-24 signal only when BTC's regime label is **not** `bear`. Definition is exactly `research/edge3/c3_capit_regime.py` / `gates.py`: on daily bars, raw = `flat` if ADX14 < 20; else `bull` if close > rising EMA200; else `bear` if close < falling EMA200; else `flat`. A new label takes effect only after 3 consecutive daily bars. The label at decision time (the close of signal bar t, i.e. t+1h) is the last daily bar that had already closed by then. |
| `oiflush` | CAPIT-24+OI-flush | CAPIT-24 plus the OI-flush confirmation from `research/edge3/c1_oi_liq.py`, frozen at **q=0.10, hold=48h** (the most common walk-forward pick in the capit+oiflush family, 188 forward-test trades / PF 3.72 in the family aggregate). 24h change in Binance USDT-M open-interest *value* must be at or below its trailing-90-day 10% quantile (`rq` window 2160h, min 720h, shifted one bar — no lookahead). |

Each variant keeps its own open positions, signal log, trade log and cursor. The base book's existing state (including the AVAX and ADA lots that entered 2026-11-14 18:00 / 19:00 UTC and exit 2026-11-16 18:00 / 19:00 UTC) is left intact.

## Data notes
- **Source (prices/volume):** Binance.US public REST. Price comes from `<COIN>USDT`; volume is the sum of the `USDT` and `USD` pairs' quote volume.
- **R2 regime:** Binance.US public daily BTCUSDT klines (enough history for the 200-day EMA warm-up).
- **OI-flush:** public daily USDT-M metrics zips from `data.binance.vision` (no keys). Cached under `metrics/` and, when present, `/workspace/edge2-data/metrics`. Binance futures REST (`fapi.binance.com`) is geo-blocked (HTTP 451) from this box, so there is no live OI stream. Vision files are published after the UTC day closes, so this variant can lag up to about one day and then catch up. Missing OI for a bar means no flush confirmation (the signal is logged as `skip_reason=no_oi_flush`); the cursor does not advance past the last complete OI day.
- **Fallback:** Binance global spot REST is also geo-blocked from this box. If Binance.US fails, the cycle aborts with no partial *price* decisions and catches up on the next run. An OI or regime fetch failure holds back only that variant; base keeps running.
- **Venue differences:** Binance.US volume is much thinner than Binance global, where the backtest was run. DOT, TRX and AVAX trade only tens of thousands of dollars a day. Signals therefore differ somewhat from the global-data backtest. Replaying Oct 1–Nov 14 on Binance.US data gave 3 closed trades (BNB, BTC, ETH; net −24 bps total), while global data gave BNB and LINK.

## Files
- `signals.jsonl` / `signals_r2.jsonl` / `signals_oiflush.jsonl`: every signal for that variant, with `taken` or `skip_reason` (`max_pos`, `coin_already_held`, `btc_regime_bear`, `no_oi_flush`, …).
- `trades.jsonl` / `trades_r2.jsonl` / `trades_oiflush.jsonl`: one line per closed hypothetical trade (gross/net bps at 10 bps Binance.US, $ P&L).
- `status.json`: last run, plus a `variants` object with open positions, closed count, win rate, PF at Binance.US 10 bps, PF at Kraken maker 80 bps and Kraken taker 170 bps, net bps and equity for each variant. Top-level fields stay backward-compatible (they mirror `base`).
- `state.json`: base `cursor` + `open` (unchanged meaning), plus `variants.r2` / `variants.oiflush` cursors and open lots.
- `run.log`: loop output.
- `metrics/`, `oi_1h.pkl`: local OI cache (gitignored).
- **Catch-up:** each run refetches about 1,100h of candles and replays every undecided bar after that variant's cursor, so pauses lose nothing. IDs are de-duplicated, so nothing is logged twice. New variants catch up from the seed; base continues from its existing cursor.

## Run
- Start: `/workspace/mayo-bot/shadow/capit24/start.sh`. It refuses to start a duplicate and runs a cycle every 15 min under `env -i`.
- Stop: `kill $(cat /workspace/mayo-bot/shadow/capit24/loop.pid)`.
- One manual cycle: `env -i PATH=/usr/bin:/bin HOME=/home/box python3 /workspace/mayo-bot/research/edge2/capit_shadow.py`.

## Pass / kill rules (applied to each variant on its own book)

Review is **per variant**, at ≥30 forward closed trades for that variant. All of these must hold:

- PF ≥ 1.10 after **Binance.US** costs (10 bps round trip)
- PF ≥ 1.10 after **Kraken maker** costs (80 bps round trip)
- Mean net per trade > 0 (after the same fee level being judged)
- Max drawdown no worse than −15% at 50% slots
- Beats buy-and-hold of the equal-weight 12-coin basket over the same period

**Kill early (per variant):** if that variant's first 15 forward trades have a net mean below −100 bps after Binance.US 10 bps.

Kraken **taker** costs (170 bps round trip) are reported in `status.json` for reference only. A variant that cannot clear 80 bps maker PF is not viable at Kraken taker fees either.

Even on a pass, going to paper or live execution needs the user's explicit OK. No flags change automatically. The three variants are scored separately; one passing does not promote the others.
