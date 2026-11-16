# mayo-bot — Operations Runbook (updated 2026-11-16)

**State: PAPER ONLY. Live is locked.** `PAPER_TRADING=true`, `DRY_RUN=true`,
`ALLOW_LIVE_TRADING=false`, futures/margin live flags off. No process here holds exchange keys or
reads `.env`. Changing any flag needs the owner's explicit OK **per flag**, plus his own keys.
Read [SAFETY.md](SAFETY.md) first.

> **MAIN PLAN (2026-11-16): [docs/MAIN_PLAN.md](docs/MAIN_PLAN.md)**: Binance.US is the only strategy/paper venue,
> Kraken is the vault (reference fee columns only), no Coinbase/Kraken execution. TREND-6 paper runner
> `paper/trend6_binanceus/` (`scripts/trend6_paper.py`) is **running** and in `scripts/capit_autostart.sh`.
> CAPIT-24L rules: [docs/MAIN_PLAN_CAPIT24L_BINANCEUS.md](docs/MAIN_PLAN_CAPIT24L_BINANCEUS.md)
> CAPIT-24L on Binance.US (VET, HBAR, ALGO, XLM; limit entry). Its runner `paper/capit24l_binanceus/`
> (`scripts/capit24l_paper.py`) is built and tested but **NOT started** and **NOT** in
> `scripts/capit_autostart.sh`. Start it only with the owner's OK: `paper/capit24l_binanceus/start.sh`.
> Until it checks out, the two processes below keep running untouched.

## 1. What is running

There is exactly **one active strategy, CAPIT-24 crash-buy**, plus a research shadow log.

| Process | Location | What it does |
|---|---|---|
| **Paper runner** | `paper/capit_binanceus/` (`scripts/capit_paper.py --loop`, rule in `src/dublin_bot/capit24.py`) | Hourly. **VET, HBAR, ALGO, XLM**: buy at the next 1h open when the 24h z-score is ≤ −2.5 with 24h volume ≥ 2× normal, then sell 48 h later. At most two $50 slots on $100 of paper equity. Public Binance.US data. Primary fee book is **Binance.US at 10 bps** round trip (0.02% taker + 3 bps slip per side). A **Kraken 80 bps** maker shadow book tracks the same fills. Details: [paper/capit_binanceus/README.md](paper/capit_binanceus/README.md) |
| **TREND-6 paper** | `paper/trend6_binanceus/` (`scripts/trend6_paper.py --loop`, rule in `src/dublin_bot/trend6.py`) | Hourly cycle; decides once per day after the 00:00 UTC close and fills at the next Binance.US daily open. **BTC, ETH**: weight = 50% × (signals on / 6) per coin from close > SMA50/100/200 and 7/14/28-day return > 0. $100 paper. Primary book Binance.US 8 bps RT, conservative 10 bps, Kraken 80/160 reference only. Kills: DD > 45% any time; after 30 round trips, behind 50/50 B&H AND > 10% off peak AND below 30 days ago (K-REL). Details: [paper/trend6_binanceus/README.md](paper/trend6_binanceus/README.md) |
| **Shadow loop** | `shadow/capit24/` (`loop.sh` → `research/edge2/capit_shadow.py` every 15 min) | Signal log only, no book of record. Runs CAPIT-24 on the old 12-coin set in three variants: `base`, `r2` (BTC regime filter) and `oiflush` (OI-flush confirmation). Details: [shadow/capit24/README.md](shadow/capit24/README.md) |

Both run under `env -i` (clean environment, no secrets), place **no orders**, and were approved by
the owner (shadow 2026-11-14, paper runner 2026-11-15). Nothing else should be running.

## 2. Start / stop / status

```bash
# paper runner
paper/capit_binanceus/start.sh      # idempotent: prints "already running pid N" if up
paper/capit_binanceus/stop.sh       # stops only this process; writes NO_AUTOSTART
python3 -m json.tool paper/capit_binanceus/status.json | less   # last cycle, open positions, books, kill state
tail -f paper/capit_binanceus/run.log
env -i PATH=/usr/bin:/bin HOME=$HOME /workspace/mayo-runtime/pyenv/bin/python scripts/capit_paper.py --once  # one cycle

# shadow loop
shadow/capit24/start.sh             # idempotent
shadow/capit24/stop.sh              # stops only this loop; writes NO_AUTOSTART
python3 -m json.tool shadow/capit24/status.json | less
tail -f shadow/capit24/run.log
```

Is it up? `pgrep -af 'capit_paper.py --loop|capit24/loop.sh'` should show exactly one of each.

Runtime files (`status.json`, `state.json`, `*.jsonl`, `run.log`, pid/lock files) are gitignored.
Forward trades for the promotion check are in `paper/capit_binanceus/closed_trades.jsonl` and
`promotion_report.json`.

## 3. Auto-restart after a box reboot

The box has **no cron and no systemd**: PID 1 is the platform's `tini`/pod-daemon, and `crontab`
is not installed. A `@reboot` crontab line would never fire. Instead:

- `scripts/capit_autostart.sh` checks **only** the two processes above and calls each `start.sh --auto`.
  It is idempotent (each `start.sh` refuses to launch a second copy: live pid file + cmdline check,
  a `pgrep` fallback, and a `flock`). It skips a process whose directory contains `NO_AUTOSTART`,
  which `stop.sh` writes, so a deliberate stop survives a reboot. A manual `start.sh` clears it.
  Every action is logged to `logs/capit_autostart.log`.
- **Hook:** `~/.profile` and `~/.bashrc` (top of each, outside the repo) run
  `scripts/capit_autostart.sh --boot`, which does the check **once per box boot**, keyed on
  `/tmp/sand-box-boot-id` and remembered in `~/.cache/capit_autostart.boot`. It fires on the first
  interactive or login shell after a reboot, for example a desktop terminal.
- **Backstop:** the agent's tool shells are non-interactive and do not read those files. The update
  routine (section 5) therefore runs `scripts/capit_autostart.sh` on every check. Worst case after a
  reboot, the runner is down until the next routine check, which can be up to about 10 h overnight
  (10 pm → 8 am CT). The runner catches up on missed closed bars from its cursor when restarted.
- It never starts the old main paper loop, supervisors, health loop or `ensure_box` (those hooks
  in `~/.profile`/`~/.bashrc` stay commented out).

## 4. Kill rules (paper runner, checked every cycle, per fee book)

1. **K1:** after **30** closed trades, profit factor < **1.10** at that book's fee → KILL.
2. **K2:** the last **two completed calendar quarters** are both net negative **and** the
   book is below cash (equity < $100) → KILL.

A primary-book (Binance.US) kill latches: no new entries, and open positions run to their 48 h exit.
The state is in `status.json` → `killed`. A kill is final for this frozen config; do not retune and
restart.

**Promotion bar** (needed before any talk of real money, along with the owner's per-flag OK):
≥ 30 closed forward trades, PF ≥ 1.10 after fees, majority of quarterly folds positive,
permutation p < 0.01, beats cash and buy-and-hold of the same coins, with a regime split reported.
As of 2026-11-16 there are **zero** closed forward trades.

## 5. Updates to the owner

A Grok Bot routine (not in this repo) checks the paper run **every 2 hours from 8 am to 10 pm CT,
every day**. It messages only when a paper trade opens or closes (results at Binance.US and Kraken
fees, plus running totals) or a kill rule trips. If the runner is down, it restarts it via
`scripts/capit_autostart.sh`. When nothing happens, it stays quiet.

## 6. Hard rules

- Paper only. Do not edit `PAPER_TRADING`, `DRY_RUN`, `ALLOW_LIVE_TRADING` or the futures/margin
  live flags.
- No keys or `.env` in the repo; never `source .env`; run under `env -i`. The Binance.US key, if
  linked, is **read-only** (trading and withdrawals off) and lives as a bot secret, never on disk.
- Never `chmod` `logs/` or `data/`. Never create `logs/BOX_STOP`.
- Do not restart anything from the retired stack (main paper loop, supervisors, health loop,
  `ensure_box`, `deploy/box/*`) without the owner's OK. The Mac `kraken_ema_bot` stays stopped.
  Dublin-local is off-limits unless asked.
- `deploy/box/box_env.sh` carries an uncommitted "USER URGENT STOP" line: keep it, never commit it.
- Git: stage files explicitly (no `git add -A`), no force-push, no history rewrite.

## 7. Research and history

- Reports: `reports/` (edge search, venue fees, low-price study, the OI-flush look-ahead fix
  `reports/oiflush_lookahead_fix_2026-11-16.md`). Research code: `research/edge2/`, `research/edge3/`.
- **Retired strategies** (EMA/breakout sleeves, multi-sleeve paper loop, supervisors, PUMP/memecoin
  work, etc.) are in [archive/2026-11-pre-capit/](archive/2026-11-pre-capit/README.md). They are not
  running and are not to be restarted.
- The legacy gated Kraken engine (`src/dublin_bot/engine.py`, CLI, dashboard) is dormant shared
  infrastructure: safety locks, exchange clients and tests. No strategy runs on it.

## 8. Verification

```bash
env -i PATH=/usr/bin:/bin HOME=$HOME PYTHONPATH=$HOME/.local/lib/python3.13/site-packages \
  /workspace/mayo-runtime/pyenv/bin/python -m pytest -q -p no:cacheprovider   # 662 passed (2026-11-16)
```

(The runtime venv has no pytest of its own, so the PYTHONPATH line borrows the user-site copy.)

The suite is offline. `tests/test_safety_locks.py` fails loudly if the trading locks are relaxed;
`tests/test_capit24.py` covers the CAPIT-24 rule and runner; `tests/test_capit24l.py` covers CAPIT-24L (strict-below limit fills, 60-min expiry, 48 h exit, slots, $70 floor, K1/K2/K4); `tests/test_trend6_paper.py` covers TREND-6 (signals, sizing, next-open fills, no look-ahead, band/min notional, fee books, kills, Binance global warm-up, idempotency, no order code).
