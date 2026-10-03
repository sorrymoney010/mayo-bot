#!/usr/bin/env python3
"""One-shot paper exit check. Refuses without the ledger owner and the instance lock.

The long-running watcher is the thread inside scripts/paper_trader_loop.py,
which already holds that lock. This entry point is for a manual pass or a
supervisor that is the only writer. It will not start while the loop holds
logs/paper_trader.lock.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)


def main() -> int:
    from dublin_bot.config import Settings
    from dublin_bot.exit_watcher import ExitWatcher
    from dublin_bot.instance import InstanceLock, ledger_owner_ok

    if not ledger_owner_ok():
        print("REFUSE: not the ledger owner (MAYO_LEDGER_OWNER!=1)")
        return 3
    lock = InstanceLock(ROOT / "logs" / "paper_trader.lock")
    if not lock.acquire(wait_seconds=float(os.environ.get("PAPER_LOCK_WAIT_SECONDS", "0"))):
        print(f"REFUSE: another paper loop holds {lock.path}")
        return 3
    try:
        settings = Settings()
        watcher = ExitWatcher(settings, held_lock=lock)
        ok, why = watcher.allowed()
        if not ok:
            print(f"REFUSE: {why}")
            return 2
        # A single collect-and-evaluate pass. The loop thread is the continuous one.
        from dublin_bot.exit_watcher import _collect_prices
        now = __import__("time").time()
        prices = _collect_prices(settings, None, now)
        ticks = [(sym, now, px) for sym, px in prices.items()]
        watcher.run_once(ticks)
        print(f"exit-watcher pass prices={len(ticks)} closes={len(watcher.closes)}")
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
