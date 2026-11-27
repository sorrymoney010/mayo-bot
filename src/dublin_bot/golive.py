"""Read-only Binance go-live preflight. Never places, modifies or cancels an order."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .config import Settings
from .errors import BrokerError

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
FIRST_BUDGET_USD = 25.0


@dataclass
class Check:
    name: str
    status: str
    detail: str


def preflight(settings: Settings, gateway: Any, audit: AuditLog | None = None) -> list[Check]:
    c: list[Check] = []
    add = lambda n, s, d: c.append(Check(n, s, d))  # noqa: E731

    add("broker", PASS if settings.broker == "binance" else FAIL, f"BROKER={settings.broker}")
    placeholder = "PASTE" in (settings.binance_api_key + settings.binance_api_secret).upper()
    has_keys = bool(settings.binance_api_key and settings.binance_api_secret) and not placeholder
    add("credentials", PASS if has_keys else FAIL,
        "key+secret present" if has_keys else
        ("key/secret are still the PASTE_ placeholders" if placeholder
         else "BINANCE_API_KEY / BINANCE_API_SECRET missing"))
    add("budget", PASS if settings.strategy_equity_usd <= FIRST_BUDGET_USD else WARN,
        f"STRATEGY_EQUITY_USD={settings.strategy_equity_usd} (first live run should be {FIRST_BUDGET_USD})")
    add("daily order cap", PASS if settings.max_orders_per_day > 0 else FAIL,
        f"MAX_ORDERS_PER_DAY={settings.max_orders_per_day} (0 = unlimited; set 3 for the first run)")
    if settings.broker == "binance":
        unsup = settings.use_bracket or settings.order_type != "market"
        add("order style", FAIL if unsup else PASS,
            f"USE_BRACKET={settings.use_bracket}, ORDER_TYPE={settings.order_type} "
            + ("(Binance adapter has no bracket/limit support: set USE_BRACKET=false ORDER_TYPE=market)" if unsup
               else "(market entries)"))
        add("exchange stop", PASS if settings.binance_exchange_stop else FAIL,
            f"BINANCE_EXCHANGE_STOP={settings.binance_exchange_stop} "
            f"(OCO stop {settings.stop_loss_pct:.1%} / take-profit {settings.take_profit_pct:.1%} placed after each entry; "
            "if it cannot be placed the entry is sold straight back)")
    add("no leverage", PASS if settings.max_leverage <= 1.0 and settings.margin_exposure_fraction == 0 else FAIL,
        f"MAX_LEVERAGE={settings.max_leverage}, MARGIN_EXPOSURE_FRACTION={settings.margin_exposure_fraction}")

    # Which of the four live flags are still locked (informational: flipping them is the operator's act).
    locks = {"PAPER_TRADING": settings.paper_trading, "DRY_RUN": settings.dry_run,
             "ALLOW_LIVE_TRADING=false": not settings.allow_live_trading,
             "LIVE_RISK_ACKNOWLEDGEMENT": settings.live_risk_acknowledgement != "I_ACCEPT_LIVE_TRADING_RISK"}
    still = [k for k, v in locks.items() if v]
    add("live flags", WARN if still else PASS,
        ("still locked: " + ", ".join(still)) if still else "all four live flags set - orders WILL be sent")

    from .config import paper_live_choice_ok
    from .instance import ledger_owner_ok
    ok_choice, why_choice = paper_live_choice_ok(settings)
    add("strategy allowed live", PASS if ok_choice else FAIL, f"{settings.strategy}: {why_choice}")
    add("ledger owner", PASS if ledger_owner_ok() else FAIL,
        "this host owns the ledger" if ledger_owner_ok() else "MAYO_LEDGER_OWNER is not 1 in THIS shell: run `export MAYO_LEDGER_OWNER=1` first (read from the environment, not .env)")

    if audit is not None:
        ok, why = audit.verify_chain()
        add("audit chain", PASS if ok else FAIL, str(why))

    if not has_keys:
        return c

    # Connectivity + clock skew
    try:
        h = gateway.health()
        add("reachable", PASS if h.get("reachable") else FAIL, f"latency {h.get('latency_ms')} ms, err={h.get('error')}")
        t0 = time.time()
        srv = gateway._public("/api/v3/time")["serverTime"] / 1000.0
        skew = abs(srv - (t0 + time.time()) / 2)
        add("clock skew", PASS if skew < 2.0 else FAIL, f"{skew:.2f}s (recvWindow is 5s)")
    except BrokerError as e:
        add("reachable", FAIL, str(e))
        return c

    # Key permissions: withdrawals must be OFF (best effort - endpoint availability differs on .us)
    try:
        r = gateway._signed("/sapi/v1/account/apiRestrictions")
        fields = sorted(r.keys()) if isinstance(r, dict) else []
        if "enableWithdrawals" not in (r or {}):
            add("key: withdrawals", WARN,
                f"exchange response had fields {fields} but no withdrawal flag: NOT verified. "
                "Open Binance.us API Management and confirm withdrawals are OFF for this key")
        elif r["enableWithdrawals"]:
            add("key: withdrawals", FAIL, "WITHDRAWALS ENABLED on this key - create a new key without it")
        else:
            add("key: withdrawals", PASS, "withdrawals disabled (explicit flag from exchange)")
        if "enableSpotAndMarginTrading" in (r or {}):
            add("key: trading", PASS if r["enableSpotAndMarginTrading"] else WARN,
                f"spot trading enabled={r['enableSpotAndMarginTrading']}")
        else:
            add("key: trading", WARN, "trading permission not reported by the exchange; confirm Spot trading is ON")
    except BrokerError as e:
        add("key: withdrawals", WARN, f"could not verify ({e}); confirm in Binance.us API settings that withdrawals are OFF")

    # Account state
    try:
        acct = gateway._signed("/api/v3/account")
        if acct.get("canTrade") is False:
            add("account canTrade", FAIL, "Binance reports canTrade=false")
        quote = gateway._quote
        free_usd = sum(float(b["free"]) for b in acct.get("balances", []) if b["asset"] == quote)
        others = {b["asset"]: float(b["free"]) for b in acct.get("balances", [])
                  if b["asset"] in ("USDT", "USD", "USDC") and b["asset"] != quote and float(b["free"]) > 0}
        need = settings.strategy_equity_usd
        hint = f"; you hold {others} - set BINANCE_QUOTE to that asset or convert" if others and free_usd < need else ""
        add("funds", PASS if free_usd >= need else FAIL,
            f"free {quote} ${free_usd:.2f}, budget ${need:.2f} (the bot trades BTC/{quote}){hint}")
        pos = gateway.positions()
        add("flat at start", PASS if not pos else WARN, "no open positions" if not pos else f"holding {[p['symbol'] for p in pos]}")
        oo = gateway.orders()
        add("no open orders", PASS if not oo else WARN, f"{len(oo)} open order(s)")
        meta = gateway.resolve_symbol()
        ok = meta.cost_min <= need
        add("min notional", PASS if ok else FAIL, f"{meta.key} min notional ${meta.cost_min} vs budget ${need}")
    except BrokerError as e:
        add("account", FAIL, str(e))
    return c


def render(checks: list[Check]) -> tuple[str, int]:
    lines = [f"{k.status:<5} {k.name:<20} {k.detail}" for k in checks]
    fails = sum(k.status == FAIL for k in checks)
    warns = sum(k.status == WARN for k in checks)
    lines.append(f"\n{fails} FAIL, {warns} WARN. " + ("NOT ready." if fails else "No blockers; read the WARNs."))
    return "\n".join(lines), (1 if fails else 0)


def run(settings: Settings) -> int:
    from .engine import build_gateway
    gw = build_gateway(settings)
    text, code = render(preflight(settings, gw, AuditLog(Path(settings.audit_log_path))))
    print(text)
    return code
