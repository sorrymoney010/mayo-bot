# MAIN PLAN: CAPIT-24L on Binance.US (VET, HBAR, ALGO, XLM)

> **Umbrella plan: [MAIN_PLAN.md](MAIN_PLAN.md)** (venue rule: Binance.US only, Kraken = vault; TREND-6;
> live gates; 2026-11-16 snapshot). This file keeps the frozen CAPIT-24L rules.

**Approved by the owner 2026-11-16 (voice call). PAPER ONLY. Live is locked.**
`PAPER_TRADING=true`, `DRY_RUN=true`, `ALLOW_LIVE_TRADING=false`, futures/margin live flags off.
Nothing in this plan changes a flag, holds a key, reads `.env`, or calls an order endpoint.

Why this strategy: [reports/strategy_proposal_2026-11-16.md](../reports/strategy_proposal_2026-11-16.md)
(research in `research/edge4/`). It is the frozen CAPIT-24 crash-buy signal with one change, a
post-only **limit** entry, which was the only refinement that helped on both coin sets once fees were
counted honestly. **It is not proven** (deflated Sharpe ≤ 0.22 after ~529 variants tried). Forward
paper must confirm it.

## 1. Rules (frozen; do not retune mid-test)

| Item | Rule |
|---|---|
| Coins | **VET, HBAR, ALGO, XLM** (Binance.US `<COIN>USDT` price; volume = USDT + USD pair quote volume). No coins added mid-test |
| Signal | On each **closed 1h bar** t: z24 = 24h log return / (trailing-720h σ of 1h log returns, excluding bar t, × √24) ≤ **−2.5**, AND 24h quote volume ≥ **2×** its trailing-30-day average 24h volume |
| Entry | At t+1h place a **post-only limit buy at 0.995 × close(t)** (0.5% under the signal bar's close), **valid 60 minutes**. Not filled → **skip** the signal (a later bar can signal again) |
| Exit | **Market sell 48 h after the fill.** No profit target, no price stop |
| Book | **$100**, **2 slots × $50**, one per coin, spot long only, no leverage/margin/futures. Ties → most negative z. A slot is free for a new order if its exit is at or before the order time |
| Equity floor | If book equity (cash + realized + open positions marked at the last 1h close, primary fees) is **≤ $70**, **no new entries** (latched until the owner reviews) |
| Kill K1 | ≥ 30 closed trades and profit factor < **1.10** after fees |
| Kill K2 | The last **two completed calendar quarters** both net negative **and** equity below $100 (cash) |
| Kill K4 | The **first 15** closed trades average worse than **−1% (−100 bps)** net |

A kill latches: no new entries; open positions run to their 48 h exit. Kills are final for this
frozen config: do not retune and restart.

## 2. Paper fill rule (stricter than the backtest)

The backtest counted a fill whenever a 1h low touched the limit. The paper runner uses
**public 1m bars** for the 60-minute window and counts a fill **only when price trades strictly
below the limit**:

- **Placement price P** = open of the first 1m bar of the window. If P is already ≤ the limit, a
  post-only order at the limit would cross and be rejected, so the paper rule rests the bid at P
  instead (`limit_eff = min(limit, P)`).
- **Filled ("strict")**: some 1m low in [t+1h, t+2h) is **< limit_eff**. Fill price = limit_eff;
  fill time = the close of that minute.
- **Touch ("touch")**: lows reach limit_eff but never go below it. **Logged separately, NOT filled.**
  A touch is the case where a real resting order may or may not have filled, depending on queue
  position.
- **Unfilled**: the limit was never reached. Skipped.
- Every signal row in `signals.jsonl` records `fill` (`strict` / `touch` / `unfilled` / `no_data`),
  the limit, the placement price and the window low; `status.json → fill_counts` totals them.

A signal bar is only decided once its whole 60-minute window has closed, so the runner never
peeks: in live, the order would be placed at t+1h and the result known at t+2h.

## 3. Fees (round trip; Binance.US is the venue)

Binance.US spot, read 2026-11-16 from [binance.us/fees](https://www.binance.us/fees):
**0% maker / 0.02% taker** on all pairs except the Tier 0 pair (BNB/USD), no volume requirement.
Kraken Pro tier 1 is shown only as a shadow ([kraken.com/pro/fees](https://www.kraken.com/pro/fees):
0.40% maker / 0.80% taker).

| Fee book | Model | Round trip |
|---|---|---|
| `binanceus_l` (**primary**, kill rules use this) | 0% maker entry + 0.02% taker exit + 3 bps exit slip | **5 bps** |
| `binanceus10` (conservative shadow) | the old CAPIT-24 primary model | 10 bps |
| `kraken125` (shadow) | 0.40% maker entry + 0.80% taker exit + 5 bps slip | 125 bps |

Binance.US `exchangeInfo` (2026-11-16): VETUSDT, HBARUSDT, ALGOUSDT, XLMUSDT all `TRADING`,
min notional $1. The Binance.US books are thin (VETUSDT 1m bars often show a single trade), which is
exactly why touches are not counted as fills.

## 4. Confirmation (needed before asking the owner about $100 live)

From the proposal, on forward paper only:

- **≥ 30 closed trades** (pooled with the 12-coin research shadow is allowed for the count, but this
  four-coin book itself needs **≥ 12**).
- **PF ≥ 1.30** after the primary fee, **and** PF ≥ 1.10 even at the 125 bps Kraken shadow if Kraken
  is the intended live venue.
- **Mean ≥ +75 bps** per trade after fees.
- **A majority of forward quarters positive.**
- **Strict fill rate ≥ 70%** of attempted orders (touches do not count). Below **50%** = the
  limit-entry premise failed → stop.
- **Book drawdown < 30%**, no K1/K2/K4 trip, no floor halt.
- The promotion bar still applies: beats cash and buy-and-hold of the same four coins over the
  same window, with a regime split reported.

Expected time: about 12–14 signals a year on four coins, so roughly **9–12 months** (pooled) to
reach 30. There is no honest way to make that faster.

## 5. Path to live (all still locked)

1. Confirmation above is met and written up.
2. **Always-on host.** This box hibernates when idle; a missed 60-minute window is a missed trade.
   The live runner must run on a host that does not sleep.
3. **Owner's explicit OK per flag** (`PAPER_TRADING`, `DRY_RUN`, `ALLOW_LIVE_TRADING`); futures and
   margin stay off.
4. **Owner's own Binance.US keys**, trade-only (withdrawals off), stored as a secret, never in the
   repo or `.env` on the box. The current linked key, if any, stays read-only.
5. A separate live-order module would be written and reviewed then; this runner has **no** order
   code by design (a test fails if order endpoints or key handling appear in it).

## 6. How to run (paper)

Code: `src/dublin_bot/capit24l.py` (rule + runner), `scripts/capit24l_paper.py` (CLI / hourly
loop), `paper/capit24l_binanceus/` (start/stop, runtime files), tests `tests/test_capit24l.py`.

```bash
# NOT started yet. Start only with the owner's OK:
paper/capit24l_binanceus/start.sh          # idempotent; nohup setsid env -i ... --loop
paper/capit24l_binanceus/stop.sh           # stops only this runner; writes NO_AUTOSTART
python3 -m json.tool paper/capit24l_binanceus/status.json | less
# dry replay into a scratch dir (never the real paper dir):
env -i PATH=/usr/bin:/bin HOME=$HOME /workspace/mayo-runtime/pyenv/bin/python \
  scripts/capit24l_paper.py --once --out /tmp/capit24l_dry --seed-cursor 2026-11-02T15:00:00Z
```

It is **not** in `scripts/capit_autostart.sh` yet (that hook only restarts the two approved
processes). Add it there only after the owner OKs starting it.

## 7. Relationship to what is already running

- `paper/capit_binanceus/` (CAPIT-24, market entry at the next open) **keeps running untouched**
  until this runner checks out. The two use the same signal on the same coins, so their
  side-by-side results show what the limit entry adds or costs.
- `shadow/capit24/` (12-coin research signal log) keeps running untouched.
- The `chore/lock-regime-trend-20261116` branch / PR 18 (BTC/ETH/SOL hourly regime-trend) is **not**
  part of this plan and stays unmerged.
