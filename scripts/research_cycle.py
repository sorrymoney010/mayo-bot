#!/usr/bin/env python3
"""Self-running research loop (paper only). Safe to call every few minutes; it decides
what is due and exits fast otherwise. Started detached by deploy/box/box_health.py.

* WEEKLY (every 7 days): ``scripts/strategy_research.py --register`` which also runs the
  order-flow overlay study and auto-moves passing overlays candidate -> shadow
  (``logs/flow_overlays.json``).
* DAILY: replays shadow/paper overlays with frozen rules on the latest contiguous tick run,
  appends forward trades (``logs/flow_shadow_trades.jsonl``) and applies the forward gates
  (shadow -> paper only with >= 30 forward trades passing the bar, ``flow_auto_paper`` on,
  and the process in paper mode; paper -> retired when the forward edge disappears).

Never reads or writes a safety lock, env file or key; never places an order; never
restarts the bot. Single instance via ``logs/research_cycle.lock``.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

WEEK = 7 * 86400
DAY = 86400


def log(logs: Path, msg: str) -> None:
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / "research_cycle.log").open("a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}\n")


def _settings():
    try:
        from dublin_bot.config import Settings
        return Settings()
    except Exception:  # noqa: BLE001 — no settings -> paper promotion stays off
        return None


def run(args) -> int:
    logs, data = Path(args.logs), Path(args.data)
    logs.mkdir(parents=True, exist_ok=True)
    lock = (logs / "research_cycle.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0
    state_p = logs / ".research_cycle_state.json"
    try:
        st = json.loads(state_p.read_text())
    except (OSError, ValueError):
        st = {}
    now = time.time()
    did = []
    if args.force_weekly or now - float(st.get("weekly", 0)) >= WEEK:
        cmd = [sys.executable, str(ROOT / "scripts" / "strategy_research.py"), "--tfs", "60", "240",
               "--data", str(data), "--register", "--shadow-path", str(logs / "shadow_sleeves.json"),
               "--flow-registry", str(logs / "flow_overlays.json"), "--flow-cache", str(logs / "flow_cache")]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3 * 3600, cwd=str(ROOT))
        st["weekly"] = now
        did.append(f"weekly rc={r.returncode}")
        if r.returncode != 0:
            log(logs, "weekly research FAILED: " + (r.stderr or "")[-600:].replace("\n", " | "))
    if args.force_daily or now - float(st.get("daily", 0)) >= DAY:
        from dublin_bot import flow_promotion as fp
        from dublin_bot.backtest_core import Costs
        from dublin_bot.flow_research import forward_replay, load_flow_sleeve_frames
        reg_p, fwd_p = logs / "flow_overlays.json", logs / "flow_shadow_trades.jsonl"
        reg = fp.load(reg_p)
        live = {k: v for k, v in reg.items() if v.get("stage") in ("shadow", "paper")}
        added = 0
        if live:
            frames = load_flow_sleeve_frames(data, ["BTC/USD", "ETH/USD", "SOL/USD"],
                                             cache=str(logs / "flow_cache"), which="latest")
            costs = Costs(fee_bps=40.0, slippage_bps=10.0, maker_bps=25.0)
            added = fp.record_forward_trades(reg, forward_replay(live, frames, costs), path=fwd_p)
        changes = fp.advance(path=reg_p, forward_path=fwd_p, settings=_settings())
        st["daily"] = now
        did.append(f"daily live={len(live)} forward_added={added} changes={changes}")
    tmp = state_p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(state_p)
    if did:
        log(logs, "; ".join(did))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--logs", default=str(ROOT / "logs"))
    ap.add_argument("--force-weekly", action="store_true")
    ap.add_argument("--force-daily", action="store_true")
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
