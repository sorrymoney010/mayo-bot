#!/usr/bin/env python3
"""Run the three fee-surviving sleeves on Alpaca's PAPER account.

Simulated money only. Refuses to start unless ALPACA_PAPER is true.

  python scripts/alpaca_paper_runner.py            # loop (every 60 s)
  python scripts/alpaca_paper_runner.py --once     # one cycle, then exit
  python scripts/alpaca_paper_runner.py --report   # forward results per sleeve
  python scripts/alpaca_paper_runner.py --check    # keys, endpoint, prices, sizing

Needs ALPACA_API_KEY / ALPACA_API_SECRET from a *paper* account (Alpaca
dashboard → Paper Trading → API keys) in .env or the environment.
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dublin_bot.alpaca_gateway import AlpacaGateway  # noqa: E402
from dublin_bot.alpaca_paper import (  # noqa: E402
    DEFAULT_SYMBOLS, SLEEVES, AlpacaPaperRunner, RunnerConfig, report,
)
from dublin_bot.config import Settings  # noqa: E402
from dublin_bot.instance import InstanceLock  # noqa: E402

LOG = ROOT / "logs" / "alpaca_paper.log"
_running = True


def _stop(*_a) -> None:
    global _running
    _running = False


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    if sys.stdout.isatty():
        print(line, flush=True)


def build(args) -> tuple[Settings, AlpacaGateway, RunnerConfig]:
    settings = Settings()
    if not settings.alpaca_paper:
        sys.exit("Refusing: ALPACA_PAPER is false. This runner only trades Alpaca PAPER.")
    if not (settings.alpaca_api_key and settings.alpaca_api_secret):
        sys.exit("Missing ALPACA_API_KEY / ALPACA_API_SECRET (paper account keys) in .env")
    gw = AlpacaGateway(settings, allow_order_submission=True)
    data = ROOT / "data" / "alpaca_paper"
    cfg = RunnerConfig(
        state_path=data / "state.json", journal_path=data / "trades.jsonl",
        events_path=data / "events.jsonl",
        symbols=tuple(args.symbols), sleeves=tuple(args.sleeves),
        budget_usd=args.budget or settings.alpaca_paper_budget_usd,
        risk_per_trade=settings.risk_per_trade,
        max_daily_loss_fraction=settings.max_daily_loss_fraction,
        taker_bps=settings.alpaca_taker_fee_bps, maker_bps=settings.alpaca_maker_fee_bps,
    )
    return settings, gw, cfg


def check(gw: AlpacaGateway, cfg: RunnerConfig) -> int:
    print(f"endpoint: {gw.host} (paper={gw.paper})")
    h = gw.health()
    print(f"reachable: {h['reachable']} {h.get('error') or ''}")
    if not h["reachable"]:
        return 1
    acct = gw.account()
    print(f"account: status={acct.get('status')} equity=${float(acct.get('equity') or 0):,.2f} "
          f"crypto_status={acct.get('crypto_status')}")
    n = len(cfg.sleeves) * len(cfg.symbols)
    slot_usd = cfg.budget_usd / n
    print(f"budget: ${cfg.budget_usd:.2f} over {n} slots -> about ${slot_usd:.2f} per trade")
    for sym in cfg.symbols:
        p = gw.latest_price(sym)
        a = gw.asset(sym)
        min_usd = float(a.min_order_size) * p["last"]
        flag = "OK" if slot_usd >= min_usd * 1.05 else "TOO SMALL - raise --budget"
        print(f"  {sym}: last {p['last']:,.4f} spread {(p['ask'] - p['bid']) / p['last'] * 1e4:.1f} bps, "
              f"min order {a.min_order_size} (~${min_usd:.2f}) {flag}")
        bars = gw.fetch_bars(sym, 240, bars=300)
        print(f"    4h bars: {len(bars)} closed (need 210+)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--interval", type=int, default=60, help="seconds between cycles")
    ap.add_argument("--budget", type=float, default=None, help="USD to size against (default ALPACA_PAPER_BUDGET_USD)")
    ap.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    ap.add_argument("--sleeves", nargs="+", default=list(SLEEVES), choices=list(SLEEVES))
    args = ap.parse_args()

    if args.report:
        rows = report(ROOT / "data" / "alpaca_paper" / "trades.jsonl")
        print(f"{'sleeve':15} {'trades':>6} {'win%':>6} {'avg bps':>8} {'PF':>6} {'P&L $':>8}  gate (30+ trades, PF>=1.10)")
        for r in rows:
            print(f"{r['sleeve']:15} {r['trades']:>6} {r['win_rate'] * 100:>5.0f}% {r['avg_net_bps']:>+8.1f} "
                  f"{r['profit_factor']:>6} {r['pnl_usd']:>+8.2f}  {'PASS' if r['passes_gate'] else 'not yet'}")
        return 0

    _settings, gw, cfg = build(args)
    if args.check:
        return check(gw, cfg)

    lock = InstanceLock(ROOT / "logs" / "alpaca_paper.lock")
    if not lock.acquire(wait_seconds=5):
        log(f"another alpaca paper runner is running (pid {lock.holder_pid()}); exiting")
        return 1
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    runner = AlpacaPaperRunner(gw, cfg, log=log)
    log(f"start: paper endpoint {gw.host}, sleeves={list(cfg.sleeves)}, symbols={list(cfg.symbols)}, "
        f"budget=${cfg.budget_usd:.2f}")
    try:
        while _running:
            try:
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
