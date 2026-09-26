#!/usr/bin/env python3
"""Mark-to-market close stale PAPER lots and (optionally) restart the paper book.

PAPER ONLY: touches logs/paper_portfolio.json, logs/paper_bot_positions.json
and logs/learner.json. Never calls a private/trading endpoint — prices come
from Kraken's PUBLIC Ticker. Each lot is sold through the bot's own paper
FillModel (spread + slippage + configured paper taker fee), the realized P&L
is booked honestly, and the closed trade is credited to the learner under the
strategy that OPENED it (e.g. breakout@15m), so it cannot pollute the stats of
a newly selected strategy.

    .venv/bin/python scripts/flatten_paper_lots.py --opened-by breakout@15m \\
        --reset-equity 500 --yes
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PAIRS = {"BTC/USD": "XBTUSD", "ETH/USD": "ETHUSD", "SOL/USD": "SOLUSD"}


def public_ticker(symbol: str) -> dict:
    pair = PAIRS.get(symbol, symbol.replace("/", ""))
    with urllib.request.urlopen(
        f"https://api.kraken.com/0/public/Ticker?pair={pair}", timeout=20
    ) as resp:
        payload = json.loads(resp.read().decode())
    if payload.get("error"):
        raise RuntimeError(payload["error"])
    row = next(iter(payload["result"].values()))
    return {"bid": float(row["b"][0]), "ask": float(row["a"][0]), "last": float(row["c"][0])}


def main() -> None:
    import os
    os.chdir(ROOT)
    from dublin_bot.config import Settings
    from dublin_bot.fills import FillModel
    from dublin_bot.learner import LearningAgent
    from dublin_bot.paper import PaperPortfolio

    ap = argparse.ArgumentParser()
    ap.add_argument("--opened-by", default="breakout@15m",
                    help="strategy key that opened these lots (learner bookkeeping)")
    ap.add_argument("--reset-equity", type=float, default=0.0,
                    help="after flattening, archive the book and restart it with this cash")
    ap.add_argument("--yes", action="store_true", help="actually write (default: dry preview)")
    a = ap.parse_args()

    settings = Settings()
    settings.paper_trading = True
    settings.dry_run = True
    settings.allow_live_trading = False
    fills = FillModel(settings)
    book_path = ROOT / "logs" / "paper_portfolio.json"
    book = PaperPortfolio(book_path)
    snap = book.load(equity=settings.strategy_equity_usd, cash=settings.strategy_equity_usd)
    now = datetime.now(timezone.utc).isoformat()
    report = {"at": now, "opened_by": a.opened_by, "closed": [],
              "cash_before": snap.cash, "fee_model": {
                  "taker_bps": fills.taker_fee_bps, "slippage_bps": fills.slippage_bps}}
    learner = LearningAgent(settings.learner_path, min_trades=settings.learner_min_trades,
                            enabled=True, strategy_key=a.opened_by)
    for sym, pos in list(snap.positions.items()):
        t = public_ticker(sym)
        fill = fills.sell(price=t["last"], volume=pos.quantity, bid=t["bid"], ask=t["ask"])
        basis = pos.quantity * pos.entry_price + pos.fees_paid
        row = {"symbol": sym, "qty": pos.quantity, "entry": pos.entry_price,
               "opened_at": pos.opened_at, "ticker": t, "fill_price": fill.price,
               "exit_fee": fill.fee, "cost_basis": basis}
        if a.yes:
            realized = book.record_sell(sym, pos.quantity, fill.price, fill.fee, now)
            learner.record_trade(sym, realized, "unknown", notional=basis, strategy=a.opened_by)
            learner.pop_entry(sym)
        else:
            realized = pos.quantity * fill.price - fill.fee - basis
        row["realized_pnl"] = realized
        row["net_bps"] = realized / basis * 1e4 if basis else None
        report["closed"].append(row)
        print(f"{sym}: qty {pos.quantity:.8g} entry {pos.entry_price:.2f} → fill {fill.price:.2f} "
              f"(last {t['last']:.2f}) realized ${realized:+.4f} "
              f"({row['net_bps']:+.0f} bps incl. fees)")
    after = book.snapshot()
    report["cash_after"] = after.cash
    print(f"paper cash before ${snap.cash:.2f} → after ${after.cash:.2f} "
          f"(open lots: {len(after.positions)})")
    if not a.yes:
        print("preview only — rerun with --yes to write")
        return
    (ROOT / "logs" / "paper_bot_positions.json").write_text("{}", encoding="utf-8")
    if a.reset_equity > 0:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = ROOT / "logs" / f"paper_portfolio.archived-{stamp}.json"
        shutil.copy2(book_path, archive)
        book_path.write_text(json.dumps({
            "equity": a.reset_equity, "cash": a.reset_equity, "updated_at": now,
            "positions": [],
        }, indent=2), encoding="utf-8")
        report["archived_book"] = str(archive)
        report["reset_equity"] = a.reset_equity
        print(f"archived old book → {archive.name}; new paper book cash ${a.reset_equity:.2f}")
    out = ROOT / "logs" / f"paper_flatten_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"wrote {out.name}")


if __name__ == "__main__":
    main()
