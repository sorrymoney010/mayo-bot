#!/usr/bin/env python3
"""Continuous paper/dry-run trading loop. Never enables live trading."""
from __future__ import annotations

import json
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)

LOG = ROOT / "logs" / "paper_trader.log"
INTERVAL = int(os.environ.get("PAPER_LOOP_SECONDS", "300"))

_running = True


def _stop(*_args) -> None:
    global _running
    _running = False


signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _as_dict(result):
    if isinstance(result, dict):
        return result
    if hasattr(result, "to_dict"):
        try:
            return result.to_dict()
        except Exception:
            pass
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if hasattr(result, "dict"):
        return result.dict()
    return getattr(result, "__dict__", {"raw": str(result)})


def main() -> None:
    from dublin_bot.config import Settings
    from dublin_bot.engine import TradingEngine

    settings = Settings()
    live = (
        settings.allow_live_trading
        and not settings.paper_trading
        and not settings.dry_run
        and settings.live_risk_acknowledgement == "I_ACCEPT_LIVE_TRADING_RISK"
    )
    if live:
        log(
            "LIVE MODE enabled by settings "
            f"(ack present, equity={settings.strategy_equity_usd}, "
            f"risk={settings.risk_per_trade})"
        )
    else:
        # Default safe posture: force paper/dry locks.
        settings.paper_trading = True
        settings.dry_run = True
        settings.allow_live_trading = False
        if not settings.safety_locked:
            log("ABORT: safety_locked is False after force — refusing to run")
            sys.exit(2)

    mode = "live" if live else "paper"
    log(
        f"START {mode} loop interval={INTERVAL}s equity={settings.strategy_equity_usd} "
        f"symbol={settings.symbol} strategy={settings.strategy} "
        f"tf={settings.timeframe_minutes}m "
        f"stop={settings.stop_loss_pct:.2%} tp={settings.take_profit_pct:.2%} "
        f"locked={settings.safety_locked} "
        f"paper={settings.paper_trading} dry_run={settings.dry_run} "
        f"allow_live={settings.allow_live_trading}"
    )

    try:
        probe = TradingEngine(settings)
        universe = probe._breakout_universe() if probe._is_breakout_sleeve() else [settings.symbol]
        verdicts = probe.learner.adaptive_summary(universe)
        log(
            f"LEARNER key={probe.learner.strategy_key} priors={len(probe.learner.priors)} "
            f"min_sample={probe.learner.min_sample} bench_h={probe.learner.bench_hours:g} "
            + " | ".join(
                f"{v['key']}:{v['state']} x{v['size_mult']:g} "
                f"(live n={v['live_n']} bps={v['live_bps']}, prior n={v['prior_n']} bps={v['prior_bps']})"
                for v in verdicts
            )
        )
        del probe
    except Exception as exc:  # noqa: BLE001 — logging only
        log(f"LEARNER summary unavailable: {type(exc).__name__}: {exc}")

    while _running:
        try:
            engine = TradingEngine(settings)
            result = engine.run_once()
            payload = _as_dict(result)
            # run_once → DecisionRecord.to_dict(); run_cycle → CycleResult.to_dict()
            decision = payload if isinstance(payload, dict) and "signal" in payload else None
            if decision is None and isinstance(payload, dict):
                decision = payload.get("decision")
            if isinstance(decision, dict):
                sig = decision.get("signal") or {}
                if not isinstance(sig, dict):
                    sig = {}
                log(
                    "CYCLE action={action} symbol={sym} dry_run={dry} order_id={oid} "
                    "score={score} reason={reason}".format(
                        action=sig.get("action"),
                        sym=decision.get("symbol"),
                        dry=decision.get("dry_run"),
                        oid=decision.get("order_id"),
                        score=sig.get("score"),
                        reason=(sig.get("reason") or "")[:120],
                    )
                )
            else:
                log(f"CYCLE ok type={type(result).__name__}")
            gates = getattr(engine, "last_cycle_gates", None)
            if isinstance(gates, dict) and isinstance(gates.get("learner"), dict):
                lv = gates["learner"]
                log(f"LEARNER gate {lv.get('key')}: {lv.get('state')} x{lv.get('size_mult')} "
                    f"— {lv.get('reason')}")
            (ROOT / "logs" / "last_cycle.json").write_text(
                json.dumps(payload, default=str, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            log(f"ERROR {type(exc).__name__}: {exc}")
            traceback.print_exc()

        for _ in range(INTERVAL):
            if not _running:
                break
            time.sleep(1)

    log("STOP paper loop")


if __name__ == "__main__":
    main()
