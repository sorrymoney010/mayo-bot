#!/usr/bin/env python3
"""Run the three fee-surviving sleeves on Binance.US: paper or live.

PAPER (default): real Binance.US prices, simulated fills and balances. No keys
needed, no money at risk.

  python scripts/binanceus_runner.py --check          # prices, spreads, minimums, keys
  python scripts/binanceus_runner.py                  # paper loop (every 60 s)
  python scripts/binanceus_runner.py --report         # paper results per sleeve

LIVE (real money). Every one of these must be true or it refuses:
  .env: BINANCE_API_KEY / BINANCE_API_SECRET (trading enabled, withdrawals OFF)
        ALLOW_LIVE_TRADING=true  PAPER_TRADING=false  DRY_RUN=false
        LIVE_RISK_ACKNOWLEDGEMENT=I_ACCEPT_LIVE_TRADING_RISK
  flag: --live

  python scripts/binanceus_runner.py --test-order     # signed no-op order check (places nothing)
  python scripts/binanceus_runner.py --live --check
  python scripts/binanceus_runner.py --live           # live loop
  python scripts/binanceus_runner.py --live --flatten # cancel runner orders, sell runner positions

Kill switch: create the file data/binanceus_live/KILL (or data/binanceus_paper/KILL)
to stop new entries; existing positions keep their stops.
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dublin_bot.alpaca_gateway import live_locks_open  # noqa: E402
from dublin_bot.alpaca_paper import (  # noqa: E402
    DEFAULT_SYMBOLS, SLEEVES, RunnerConfig, SleeveRunner, report,
)
from dublin_bot.binanceus_gateway import BinanceUSGateway  # noqa: E402
from dublin_bot.config import Settings  # noqa: E402
from dublin_bot.instance import InstanceLock  # noqa: E402
from dublin_bot.sim_execution import SimulatedExecution  # noqa: E402

_running = True


def _stop(*_a) -> None:
    global _running
    _running = False


def mode_dir(live: bool) -> Path:
    return ROOT / "data" / ("binanceus_live" if live else "binanceus_paper")


def make_log(live: bool):
    path = ROOT / "logs" / ("binanceus_live.log" if live else "binanceus_paper.log")

    def log(msg: str) -> None:
        line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if sys.stdout.isatty():
            print(line, flush=True)
    return log


def build(args, settings: Settings):
    live = bool(args.live)
    if live:
        missing = [name for name, ok in [
            ("ALLOW_LIVE_TRADING=true", settings.allow_live_trading),
            ("PAPER_TRADING=false", not settings.paper_trading),
            ("DRY_RUN=false", not settings.dry_run),
            ("LIVE_RISK_ACKNOWLEDGEMENT", settings.live_risk_acknowledgement == "I_ACCEPT_LIVE_TRADING_RISK"),
            ("BINANCE_API_KEY/SECRET", bool(settings.binance_api_key and settings.binance_api_secret)),
        ] if not ok]
        if missing:
            sys.exit("Refusing LIVE: not set -> " + ", ".join(missing))
        assert live_locks_open(settings)
    gw = BinanceUSGateway(settings, allow_order_submission=live, quote=settings.binanceus_quote)
    d = mode_dir(live)
    budget = args.budget or settings.binanceus_budget_usd
    venue = gw if live else SimulatedExecution(
        gw, state_path=d / "sim_account.json", starting_cash=budget,
        taker_bps=settings.binanceus_taker_fee_bps, maker_bps=settings.binanceus_maker_fee_bps,
        slippage_bps=settings.binanceus_sim_slippage_bps)
    cfg = RunnerConfig(
        state_path=d / "state.json", journal_path=d / "trades.jsonl", events_path=d / "events.jsonl",
        symbols=tuple(args.symbols), sleeves=tuple(args.sleeves), budget_usd=budget,
        risk_per_trade=settings.risk_per_trade, max_daily_loss_fraction=settings.max_daily_loss_fraction,
        taker_bps=settings.binanceus_taker_fee_bps, maker_bps=settings.binanceus_maker_fee_bps,
        max_trade_usd=(args.max_trade or settings.binanceus_max_trade_usd) if live else None,
    )
    return gw, venue, cfg


def check(gw: BinanceUSGateway, cfg: RunnerConfig, live: bool, settings: Settings) -> int:
    print(f"mode: {'LIVE (real money)' if live else 'PAPER (simulated fills on real prices)'}")
    h = gw.health()
    print(f"Binance.US reachable: {h['reachable']} {h.get('error') or ''}")
    if not h["reachable"]:
        return 1
    off = gw.sync_time()
    print(f"clock offset vs server: {off} ms")
    n = len(cfg.sleeves) * len(cfg.symbols)
    per = cfg.budget_usd / n
    if cfg.max_trade_usd:
        per = min(per, cfg.max_trade_usd)
    print(f"budget ${cfg.budget_usd:.2f} over {n} slots -> about ${per:.2f} per trade")
    bad = 0
    for sym in cfg.symbols:
        try:
            m = gw.asset(sym)
        except Exception as exc:
            print(f"  {sym}: NOT LISTED ({exc})")
            bad += 1
            continue
        p = gw.latest_price(sym)
        spread = (p["ask"] - p["bid"]) / p["last"] * 1e4 if p["ask"] and p["bid"] else float("nan")
        min_usd = max(float(m.min_order_size) * p["last"], float(m.min_notional))
        ok = m.tradable and per >= min_usd * 1.05 and "STOP_LOSS_LIMIT" in m.order_types
        bad += not ok
        print(f"  {m.pair}: last {p['last']:,.4f}  spread {spread:.1f} bps  min ~${min_usd:.2f}  "
              f"stop-limit {'yes' if 'STOP_LOSS_LIMIT' in m.order_types else 'NO'}  "
              f"{'OK' if ok else 'PROBLEM'}")
        if spread > 30:
            print("    warning: wide spread; limit entries help, market exits will cost more")
        print(f"    1h bars {len(gw.fetch_bars(sym, 60, bars=300))}, 4h bars {len(gw.fetch_bars(sym, 240, bars=300))} (need 210+)")
    if settings.binance_api_key and settings.binance_api_secret:
        try:
            a = gw.account()
            fees = gw.commission_rates()
            print(f"keys: OK  canTrade={a.get('canTrade')} canWithdraw={a.get('canWithdraw')}  "
                  f"{gw.quote} free ${gw.cash():,.2f}  fees maker {fees['maker_bps']:.1f} / taker {fees['taker_bps']:.1f} bps")
            if a.get("canWithdraw"):
                print("  WARNING: this key can withdraw. Create a key with withdrawals disabled.")
                bad += 1
            if live and gw.cash() < per:
                print(f"  PROBLEM: free {gw.quote} below one trade (${per:.2f})")
                bad += 1
        except Exception as exc:
            print(f"keys: FAILED {exc}")
            bad += 1
    else:
        print("keys: none (fine for paper)")
    print("result:", "READY" if not bad else f"{bad} problem(s) above")
    return 0 if not bad else 1


def test_order(gw: BinanceUSGateway, cfg: RunnerConfig) -> int:
    """Signed no-op orders: proves keys, signing, permissions and filters."""
    gw.sync_time()
    for sym in cfg.symbols:
        m = gw.asset(sym)
        p = gw.latest_price(sym)
        px = m.floor_price(p["last"] * 0.5)
        qty = m.floor_qty(Decimal(str(max(float(m.min_notional), 12.0) * 1.2)) / px)
        params = gw.test_order(symbol=sym, side="buy", order_type="limit", qty=qty, limit_price=px,
                               client_order_id=f"dub-test-{int(time.time())}")
        print(f"  {sym}: test LIMIT BUY {params['quantity']} @ {params['price']} accepted (nothing placed)")
        stop = m.floor_price(p["last"] * 0.97)
        params = gw.test_order(symbol=sym, side="sell", order_type="stop_limit", qty=qty,
                               stop_price=stop, limit_price=float(stop) * 0.99,
                               client_order_id=f"dub-tsts-{int(time.time())}")
        print(f"  {sym}: test STOP_LOSS_LIMIT SELL {params['quantity']} stop {params['stopPrice']} accepted")
    print("result: keys, signing and order formats OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="REAL MONEY (needs every live lock)")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--test-order", action="store_true", help="signed no-op order check")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--flatten", action="store_true", help="cancel runner orders and sell runner positions")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--budget", type=float, default=None)
    ap.add_argument("--max-trade", type=float, default=None, help="live: hard cap USD per trade")
    ap.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    ap.add_argument("--sleeves", nargs="+", default=list(SLEEVES), choices=list(SLEEVES))
    args = ap.parse_args()

    if args.report:
        rows = report(mode_dir(args.live) / "trades.jsonl")
        print(f"[{'LIVE' if args.live else 'PAPER'}] {'sleeve':15} {'trades':>6} {'win%':>6} {'avg bps':>8} {'PF':>6} {'P&L $':>8}  gate")
        for r in rows:
            print(f"        {r['sleeve']:15} {r['trades']:>6} {r['win_rate'] * 100:>5.0f}% {r['avg_net_bps']:>+8.1f} "
                  f"{r['profit_factor']:>6} {r['pnl_usd']:>+8.2f}  {'PASS' if r['passes_gate'] else 'not yet'}")
        return 0

    settings = Settings()
    if args.test_order:
        if not (settings.binance_api_key and settings.binance_api_secret):
            sys.exit("--test-order needs BINANCE_API_KEY / BINANCE_API_SECRET")
        gw = BinanceUSGateway(settings, quote=settings.binanceus_quote)
        cfg = RunnerConfig(symbols=tuple(args.symbols))
        return test_order(gw, cfg)

    gw, venue, cfg = build(args, settings)
    if args.check:
        return check(gw, cfg, args.live, settings)

    log = make_log(args.live)
    lock = InstanceLock(ROOT / "logs" / ("binanceus_live.lock" if args.live else "binanceus_paper.lock"))
    if not lock.acquire(wait_seconds=5):
        log(f"another Binance.US runner is running (pid {lock.holder_pid()}); exiting")
        return 1
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        if args.live:
            gw.sync_time()
        runner = SleeveRunner(venue, cfg, log=log, live=args.live)
        if args.flatten:
            log("flatten requested")
            runner.flatten()
            return 0
        log(f"start {'LIVE' if args.live else 'PAPER'}: sleeves={list(cfg.sleeves)} symbols={list(cfg.symbols)} "
            f"budget=${cfg.budget_usd:.2f} max_trade={cfg.max_trade_usd}")
        last_sync = time.time()
        while _running:
            try:
                if args.live and time.time() - last_sync > 3600:
                    gw.sync_time()
                    last_sync = time.time()
                runner.cycle()
            except Exception as exc:
                log(f"cycle error: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            if args.once:
                break
            for _ in range(args.interval):
                if not _running:
                    break
                time.sleep(1)
    finally:
        log("stop")
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
