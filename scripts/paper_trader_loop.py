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
    """Append one line to logs/paper_trader.log (exactly once).

    launchd/nohup redirect stdout into the same file, so echoing to stdout
    wrote every line twice. Only echo when attached to a terminal.
    """
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    if sys.stdout.isatty():
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


LOCK = ROOT / "logs" / "paper_trader.lock"


def main() -> None:
    from dublin_bot.config import Settings, paper_live_choice_ok
    from dublin_bot.engine import TradingEngine
    from dublin_bot.instance import LEDGER_OWNER_ENV, InstanceLock, ledger_owner_ok
    from dublin_bot.meanrev_sleeve import MeanRevSleeve, sleeve_active
    from dublin_bot.trendhold_sleeve import TrendHoldSleeve
    from dublin_bot.trendhold_sleeve import sleeve_active as trendhold_active

    # Only the ledger-owner machine (the Mac wrapper) may write the paper book.
    if not ledger_owner_ok():
        log(f"REFUSE: not the ledger owner ({LEDGER_OWNER_ENV}!=1). Only the Mac "
            "(scripts/run_paper_mac.sh) writes the paper ledger; this copy will not start.")
        sys.exit(3)
    lock = InstanceLock(LOCK)
    if not lock.acquire(wait_seconds=float(os.environ.get("PAPER_LOCK_WAIT_SECONDS", "20"))):
        log(f"REFUSE: another paper loop holds {LOCK} (pid {lock.holder_pid()}); "
            "single instance only — exiting.")
        sys.exit(3)

    settings = Settings()
    ok_choice, why_choice = paper_live_choice_ok(settings)
    if not ok_choice:
        log(f"ABORT: {why_choice}")
        lock.release()
        sys.exit(2)
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
    mr_on, mr_why = sleeve_active(settings)
    log(
        f"SLEEVES primary={settings.strategy}@{settings.timeframe_minutes}m "
        f"meanrev_4h={'on' if mr_on else 'off'} ({mr_why}) "
        f"key=meanrev_mk@{settings.meanrev_timeframe_minutes}m "
        f"symbols={','.join(settings.meanrev_symbols)} rsi<={settings.meanrev_rsi_entry:g} "
        f"exit rsi>={settings.meanrev_rsi_exit:g}|close>=ema{settings.meanrev_ema_period} "
        f"stop={settings.meanrev_stop_pct:.2%} tp={settings.meanrev_take_profit_pct:.2%} "
        f"limit=-{settings.meanrev_limit_offset_pct:.2%} valid={settings.meanrev_limit_valid_bars}bar "
        f"max_pos={settings.max_concurrent_positions} (shared)"
    )

    th_on, th_why = trendhold_active(settings)
    log(
        f"SLEEVES trendhold_4h={'on' if th_on else 'off'} ({th_why}) "
        f"key=trendhold@{settings.trendhold_timeframe_minutes}m symbols={','.join(settings.trendhold_symbols)} "
        f"size={settings.trendhold_position_fraction:.0%}/coin ema{settings.trendhold_ema_fast}>"
        f"ema{settings.trendhold_ema_slow} exit=close<ema{settings.trendhold_ema_slow} stop=none "
        f"| D1 regime={settings.regime_daily_filter} meanrev={settings.meanrev_daily_filter} "
        f"trendhold={settings.trendhold_daily_filter} | caps max_pos={settings.max_concurrent_positions} "
        f"exposure<={settings.max_exposure_fraction:.0%} (shared, all sleeves)"
    )

    if getattr(settings, "pipeline_enabled", False):
        log(f"PIPELINE on data={settings.pipeline_data_dir} symbols={','.join(settings.pipeline_symbols)} "
            f"stale_after={settings.pipeline_stale_seconds:g}s (tick bars first, REST OHLC fallback)")
    else:
        log("PIPELINE off (REST OHLC bars)")

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
    if mr_on:
        try:
            mr_probe = MeanRevSleeve(settings)
            verdicts = mr_probe.learner.adaptive_summary(mr_probe.universe())
            log(
                f"LEARNER key={mr_probe.strategy_key} priors={len(mr_probe.learner.priors)} "
                + " | ".join(
                    f"{v['key']}:{v['state']} x{v['size_mult']:g} "
                    f"(live n={v['live_n']} bps={v['live_bps']}, prior n={v['prior_n']} bps={v['prior_bps']})"
                    for v in verdicts
                )
            )
            del mr_probe
        except Exception as exc:  # noqa: BLE001 — logging only
            log(f"LEARNER meanrev summary unavailable: {type(exc).__name__}: {exc}")

    from dublin_bot.loop_telemetry import LoopTelemetry
    telemetry = LoopTelemetry(settings, logs_dir=ROOT / "logs", log=log)
    log("TELEMETRY decision_snapshots.jsonl shadow_signals.jsonl equity.jsonl closed_trades.jsonl "
        "(public data only)")

    fu_on, fu_why = False, "not loaded"
    try:
        from dublin_bot.futures_sleeve import sleeve_active as futures_active
        fu_on, fu_why = futures_active(settings)
    except Exception as exc:  # noqa: BLE001
        fu_why = f"{type(exc).__name__}: {exc}"
    log(f"SLEEVES futures_short={'on' if fu_on else 'off'} ({fu_why}) "
        f"lev_cap=2 configured={getattr(settings, 'futures_leverage', 1)} "
        f"tf={getattr(settings, 'futures_timeframe_minutes', 240)}m "
        f"live_futures_orders={getattr(settings, 'allow_futures_live_orders', False)} "
        f"live_margin_orders={getattr(settings, 'allow_margin_live_orders', False)}")

    watcher = None
    if not getattr(settings, "exit_watcher_enabled", True):
        log("EXIT_WATCHER off (EXIT_WATCHER_ENABLED=false)")
    elif not live:
        try:
            from dublin_bot.exit_watcher import ExitWatcher
            watcher = ExitWatcher(settings, held_lock=lock)
            if watcher.start():
                log("EXIT_WATCHER on (public prices, 1m candles, stops/trailing/TP only; entries unchanged)")
            else:
                log(f"EXIT_WATCHER not started ({watcher.allowed()[1]})")
                watcher = None
        except Exception as exc:  # noqa: BLE001
            log(f"EXIT_WATCHER not started: {type(exc).__name__}: {exc}")
            watcher = None

    while _running:
        try:
            engine = TradingEngine(settings)
            result = engine.run_once()
            telemetry.engine(engine)
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
                    "CYCLE sleeve={sleeve} action={action} symbol={sym} dry_run={dry} order_id={oid} "
                    "score={score} reason={reason}".format(
                        sleeve=settings.strategy,
                        action=sig.get("action"),
                        sym=decision.get("symbol"),
                        dry=decision.get("dry_run"),
                        oid=decision.get("order_id"),
                        score=sig.get("score"),
                        reason=(sig.get("reason") or "")[:120],
                    )
                )
            else:
                log(f"CYCLE sleeve={settings.strategy} ok type={type(result).__name__}")
            gates = getattr(engine, "last_cycle_gates", None)
            if isinstance(gates, dict) and isinstance(gates.get("learner"), dict):
                lv = gates["learner"]
                log(f"LEARNER gate {lv.get('key')}: {lv.get('state')} x{lv.get('size_mult')} "
                    f"— {lv.get('reason')}")
            (ROOT / "logs" / "last_cycle.json").write_text(
                json.dumps(payload, default=str, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            log(f"ERROR sleeve={settings.strategy} {type(exc).__name__}: {exc}")
            traceback.print_exc()

        # Second PAPER sleeve (4h mean reversion, limit entries). Isolated so a
        # failure here can never block the primary sleeve, and vice versa.
        if mr_on:
            try:
                res = MeanRevSleeve(settings).run_cycle()
                d = res.to_dict()
                telemetry.sleeve(d)
                acts = ",".join(f"{a['event']}:{a['symbol']}" for a in d["actions"]) or "none"
                syms = " | ".join(f"{k}: {v}" for k, v in d["symbols"].items())
                log(f"CYCLE sleeve=meanrev_4h active={d['active']} actions={acts} errors={len(d['errors'])} "
                    f"| {syms}"[:900])
                for err in d["errors"]:
                    log(f"WARN sleeve=meanrev_4h {err}"[:300])
                (ROOT / "logs" / "last_cycle_meanrev.json").write_text(
                    json.dumps(d, default=str, indent=2), encoding="utf-8"
                )
            except Exception as exc:  # noqa: BLE001
                log(f"ERROR sleeve=meanrev_4h {type(exc).__name__}: {exc}")
                traceback.print_exc()

        # Third PAPER sleeve (4h trend-hold, market entries). Isolated like meanrev.
        if th_on:
            try:
                res = TrendHoldSleeve(settings).run_cycle()
                d = res.to_dict()
                telemetry.sleeve(d)
                acts = ",".join(f"{a['event']}:{a['symbol']}" for a in d["actions"]) or "none"
                syms = " | ".join(f"{k}: {v}" for k, v in d["symbols"].items())
                log(f"CYCLE sleeve=trendhold_4h active={d['active']} actions={acts} errors={len(d['errors'])} "
                    f"| {syms}"[:900])
                for err in d["errors"]:
                    log(f"WARN sleeve=trendhold_4h {err}"[:300])
                (ROOT / "logs" / "last_cycle_trendhold.json").write_text(
                    json.dumps(d, default=str, indent=2), encoding="utf-8"
                )
            except Exception as exc:  # noqa: BLE001
                log(f"ERROR sleeve=trendhold_4h {type(exc).__name__}: {exc}")
                traceback.print_exc()

        telemetry.tick()

        if fu_on:
            try:
                from dublin_bot.futures_sleeve import FuturesSleeve
                fres = FuturesSleeve(settings).run_cycle()
                fd = fres.to_dict()
                acts = ",".join(f"{a['event']}:{a['symbol']}" for a in fd["actions"]) or "none"
                log(f"CYCLE sleeve=futures_short active={fd['active']} actions={acts} "
                    f"errors={len(fd['errors'])}"[:900])
                (ROOT / "logs" / "last_cycle_futures.json").write_text(
                    json.dumps(fd, default=str, indent=2), encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                log(f"ERROR sleeve=futures_short {type(exc).__name__}: {exc}")
                traceback.print_exc()

        try:
            from dublin_bot.research import shadow_cycle
            logged = shadow_cycle(settings)
            if logged:
                log(f"SHADOW signals={len(logged)} (no paper fills)")
        except Exception as exc:  # noqa: BLE001
            log(f"SHADOW unavailable: {type(exc).__name__}: {exc}")

        if getattr(settings, "pipeline_enabled", False):
            try:
                from dublin_bot.pipeline.source import get_source, pipeline_summary

                src = get_source(settings.pipeline_data_dir,
                                 stale_seconds=float(settings.pipeline_stale_seconds),
                                 symbols=list(settings.pipeline_symbols))
                log(f"PIPELINE {pipeline_summary(src)}"[:900])
            except Exception as exc:  # noqa: BLE001 — logging only
                log(f"PIPELINE status unavailable: {type(exc).__name__}: {exc}")

        for _ in range(INTERVAL):
            if not _running:
                break
            time.sleep(1)

    if watcher is not None:
        watcher.stop()
    log("STOP paper loop")
    lock.release()


if __name__ == "__main__":
    main()
