from __future__ import annotations

import argparse
import json
from pathlib import Path

from .audit import AuditEvent, AuditLog
from .config import Settings
from .dashboard import serve_dashboard
from .engine import TradingEngine, build_gateway


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dublin-bot")
    parser.add_argument(
        "command",
        choices=[
            "doctor",       # config + safety summary
            "health",       # broker connectivity, clock skew, rate budget
            "freshness",    # market-data freshness verdict
            "pairs",        # resolved symbol metadata (precision, minimums)
            "run-once",     # one full gated cycle
            "recover",      # resolve pending order intents after a crash
            "audit-verify", # verify the audit hash chain
            "safety-report",# full pre-live safety report
            "dashboard",
        ],
    )
    return parser


def doctor(settings: Settings) -> int:
    print(json.dumps(settings.safety_report(), indent=2, default=str))
    if not settings.safety_locked:
        print("\nWARNING: safety locks are NOT fully engaged.")
    if not settings.has_credentials:
        print("\nRead-only mode: no broker credentials configured.")
    return 0


def health(settings: Settings) -> int:
    gateway = build_gateway(settings)
    if not hasattr(gateway, "health"):
        print(json.dumps({"error": "adapter does not implement health()"}))
        return 1
    print(json.dumps(gateway.health(), indent=2, default=str))
    return 0


def freshness(settings: Settings) -> int:
    gateway = build_gateway(settings)
    verdict = gateway.check_freshness()
    print(json.dumps(verdict.to_dict(), indent=2, default=str))
    return 0 if verdict.fresh else 2


def pairs(settings: Settings) -> int:
    gateway = build_gateway(settings)
    meta = gateway.resolve_symbol()
    print(json.dumps(meta.to_dict(), indent=2, default=str))
    return 0


def recover(settings: Settings) -> int:
    print(json.dumps(TradingEngine(settings).recover(), indent=2, default=str))
    return 0


def audit_verify(settings: Settings) -> int:
    intact, reason = AuditLog(Path(settings.audit_log_path)).verify_chain()
    print(json.dumps({"chain_intact": intact, "status": reason}, indent=2))
    return 0 if intact else 3


def safety_report(settings: Settings) -> int:
    """Everything a human needs to review before considering live activation."""
    audit = AuditLog(Path(settings.audit_log_path))
    intact, chain_status = audit.verify_chain()
    gateway = build_gateway(settings)

    report: dict[str, object] = {
        "config": settings.safety_report(),
        "audit_chain": {"intact": intact, "status": chain_status},
        "locks_engaged": settings.safety_locked,
        "order_submission_enabled": getattr(gateway, "order_submission_enabled", False),
    }
    try:
        report["broker_health"] = gateway.health()
    except Exception as exc:
        report["broker_health"] = {"error": str(exc)}
    try:
        report["freshness"] = gateway.check_freshness().to_dict()
    except Exception as exc:
        report["freshness"] = {"error": str(exc)}

    engine = TradingEngine(settings)
    report["pending_order_intents"] = [r.to_dict() for r in engine.ledger.pending()]
    audit.record(AuditEvent.CONFIG_SNAPSHOT, {"safety_report": True})
    print(json.dumps(report, indent=2, default=str))
    return 0 if settings.safety_locked else 4


def main() -> int:
    args = build_parser().parse_args()
    settings = Settings()
    handlers = {
        "doctor": doctor,
        "health": health,
        "freshness": freshness,
        "pairs": pairs,
        "recover": recover,
        "audit-verify": audit_verify,
        "safety-report": safety_report,
        "dashboard": serve_dashboard,
    }
    if args.command in handlers:
        return handlers[args.command](settings)

    result = TradingEngine(settings).run_cycle()
    print(json.dumps(result.to_dict(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
