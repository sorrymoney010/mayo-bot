"""tastytrade crypto spot gateway — implements the shared ``BrokerGateway``.

Drop-in peer of ``kraken_gateway`` / ``binance_gateway`` selected with
``BROKER=tastytrade``.

**Platform status (2026-11-21):** tastytrade disabled cryptocurrency order entry
through the Open API in release ``20260629``, "until further notice".  Quotes
still work.  Until ``TASTYTRADE_CRYPTO_API_ENABLED=true`` is set (do that only
after their release notes re-enable it) this adapter refuses real submission
even with every other lock open.

Design notes
------------
* **Bars** come from Kraken public OHLC for the same coin.  tastytrade's REST
  API has no candle endpoint (candles are DXLink-stream only), and the signal
  logic was validated on Kraken bars, so reusing them keeps the strategy
  identical.  Execution quality is checked on tastytrade's own quote.
* **Costs:** tastytrade charges 0.75% (BTC/ETH) or 1% (others) per side, which
  is 2-2.5x Kraken taker.  ``fee_bps_for`` exposes it so sizing / backtests stay
  honest.  See docs/TASTYTRADE.md for which strategies survive that.
* **Orders:** Notional Market + IOC to buy, Market + IOC ``Sell to Close`` to
  exit (one leg, per tastytrade crypto rules).  Every live order is
  broker-dry-run first, carries an ``external-identifier``, and is **never**
  auto-resubmitted after an uncertain failure.
"""

from __future__ import annotations

import time
import uuid
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from .audit import AuditEvent, AuditLog
from .config import Settings
from .errors import BrokerError, SafetyLockError
from .freshness import FreshnessVerdict
from .kraken_gateway import SymbolMeta
from .tastytrade_client import TastytradeClient, UncertainSubmissionError

LIVE_ACK = "I_ACCEPT_LIVE_TRADING_RISK"
_FIAT = {"USD"}
_MAJOR_FEE_BPS = 75.0   # BTC/USD, ETH/USD
_OTHER_FEE_BPS = 100.0  # every other coin


def fee_bps_for(symbol: str) -> float:
    base = symbol.split("/")[0].upper()
    return _MAJOR_FEE_BPS if base in ("BTC", "ETH") else _OTHER_FEE_BPS


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class TastytradeGateway:
    broker_name = "tastytrade"

    def __init__(
        self,
        settings: Settings,
        *,
        audit: AuditLog | None = None,
        client: TastytradeClient | None = None,
        bar_source: Any | None = None,
        allow_order_submission: bool = False,
    ) -> None:
        self.settings = settings
        self.audit = audit or AuditLog(Path(settings.audit_log_path))
        self.client = client or TastytradeClient(
            refresh_token=settings.tastytrade_refresh_token,
            client_secret=settings.tastytrade_client_secret,
            env=settings.tastytrade_env,
            timeout=settings.http_timeout_seconds,
            max_retries=settings.max_retries,
        )
        self._bar_source = bar_source
        self._allow_order_submission = bool(allow_order_submission)
        self._account = settings.tastytrade_account_number or ""

    # ── locks ──────────────────────────────────────────────
    @property
    def order_submission_enabled(self) -> bool:
        s = self.settings
        return (
            self._allow_order_submission
            and s.allow_live_trading
            and not s.paper_trading
            and not s.dry_run
            and s.live_risk_acknowledgement == LIVE_ACK
            and s.tastytrade_crypto_api_enabled
        )

    def lock_report(self) -> dict:
        s = self.settings
        return {
            "allow_order_submission": self._allow_order_submission,
            "allow_live_trading": s.allow_live_trading,
            "paper_trading": s.paper_trading,
            "dry_run": s.dry_run,
            "acknowledgement": s.live_risk_acknowledgement == LIVE_ACK,
            "crypto_api_enabled": s.tastytrade_crypto_api_enabled,
            "environment": "production" if self.client.is_production else "sandbox",
            "order_submission_enabled": self.order_submission_enabled,
        }

    # ── account ────────────────────────────────────────────
    def account_number(self) -> str:
        if not self._account:
            nums = self.client.account_numbers()
            if not nums:
                raise BrokerError("tastytrade: no accounts on this login")
            if len(nums) > 1:
                raise BrokerError(
                    "tastytrade: several accounts found; set TASTYTRADE_ACCOUNT_NUMBER")
            self._account = nums[0]
        return self._account

    # ── market data ────────────────────────────────────────
    def _bars_gateway(self):
        if self._bar_source is None:
            from .kraken_gateway import KrakenGateway
            self._bar_source = KrakenGateway(self.settings, audit=self.audit,
                                             allow_order_submission=False)
        return self._bar_source

    def get_bars(self, *, validate: bool = True) -> pd.DataFrame:
        return self._bars_gateway().get_bars(validate=validate)

    def check_freshness(self, bars: pd.DataFrame | None = None) -> FreshnessVerdict:
        return self._bars_gateway().check_freshness(bars)

    def get_ticker(self) -> dict:
        return self.get_ticker_for(self.settings.symbol)

    def get_ticker_for(self, symbol: str) -> dict:
        q = self.client.market_data(cryptocurrency=[symbol]).get(symbol)
        if not q:
            raise BrokerError(f"tastytrade returned no quote for {symbol}")
        return {
            "symbol": symbol,
            "last": _f(q.get("last") or q.get("mark") or q.get("mid")),
            "bid": _f(q.get("bid")),
            "ask": _f(q.get("ask")),
            "volume": _f(q.get("volume")),
        }

    def market_quality(self) -> dict:
        t = self.get_ticker()
        # tastytrade's crypto volume is its own venue's, not the global market.
        # Dollar-volume gating uses the bar source's figure when available.
        vol = t["volume"] * t["last"]
        bars_mq = getattr(self._bars_gateway(), "market_quality", None)
        if callable(bars_mq):
            try:
                vol = max(vol, _f(bars_mq().get("recent_dollar_volume")))
            except Exception:
                pass
        return {"bid": t["bid"], "ask": t["ask"], "recent_dollar_volume": vol,
                "fee_bps_per_side": fee_bps_for(self.settings.symbol)}

    def resolve_symbol(self, symbol: str | None = None) -> SymbolMeta:
        sym = symbol or self.settings.symbol
        inst = self.client.cryptocurrency(sym)
        venues = [v for v in inst.get("destination-venue-symbols", []) if v.get("routable")]
        qty_prec = max((int(v.get("max-quantity-precision") or 0) for v in venues), default=8)
        tick = Decimal(str(inst.get("tick-size") or "0.01"))
        price_dec = max(0, -tick.normalize().as_tuple().exponent)
        base, _, quote = sym.partition("/")
        active = inst.get("active", True) and not inst.get("is-closing-only", False)
        return SymbolMeta(
            key=sym, altname=sym, wsname=sym, base=base, quote=quote,
            lot_decimals=qty_prec, pair_decimals=price_dec,
            order_min=Decimal(1).scaleb(-qty_prec), cost_min=Decimal("1"),
            status="online" if active else "closing-only",
        )

    def fee_bps(self) -> float:
        return fee_bps_for(self.settings.symbol)

    # ── positions ──────────────────────────────────────────
    def _paper_or_unconfigured(self) -> bool:
        return not self.client.has_credentials

    def account_equity(self) -> float:
        if self._paper_or_unconfigured():
            return float(self.settings.strategy_equity_usd)
        bal = self.client.balances(self.account_number())
        return round(_f(bal.get("net-liquidating-value")), 2)

    def positions(self) -> list[dict]:
        if self._paper_or_unconfigured():
            return []
        out = []
        for p in self.client.positions(self.account_number(), **{"instrument-type": "Cryptocurrency"}):
            qty = _f(p.get("quantity"))
            if p.get("quantity-direction") == "Short":
                qty = -qty
            if qty == 0:
                continue
            mark = _f(p.get("mark") or p.get("close-price"))
            out.append({
                "symbol": p.get("symbol"),
                "quantity": qty,
                "market_value": round(qty * mark, 2),
                "average_entry": _f(p.get("average-open-price")),
            })
        return out

    def has_position(self) -> bool:
        return any(p["symbol"] == self.settings.symbol for p in self.positions())

    def orders(self) -> list[dict]:
        if self._paper_or_unconfigured():
            return []
        out = []
        for o in self.client.live_orders(self.account_number()):
            legs = o.get("legs") or [{}]
            out.append({
                "order_id": str(o.get("id")),
                "symbol": legs[0].get("symbol"),
                "side": "buy" if "Buy" in str(legs[0].get("action")) else "sell",
                "status": str(o.get("status", "")).lower(),
                "external_identifier": o.get("external-identifier"),
            })
        return out

    def find_order_by_userref(self, userref: int) -> dict | None:
        if self._paper_or_unconfigured():
            return None
        ext = self._external_id(userref)
        for o in self.orders():
            if o.get("external_identifier") == ext:
                return o
        return None

    # ── order bodies ───────────────────────────────────────
    @staticmethod
    def _external_id(userref: int | None) -> str:
        return f"mayo-{userref}" if userref is not None else f"mayo-{uuid.uuid4().hex[:20]}"

    def build_buy_body(self, notional_usd: float, userref: int | None) -> dict:
        notional = Decimal(str(notional_usd)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if notional < Decimal("1"):
            raise BrokerError("tastytrade crypto minimum opening notional is $1")
        if notional > Decimal("75000"):
            raise BrokerError("tastytrade crypto maximum notional is $75,000")
        return {
            "time-in-force": "IOC",
            "order-type": "Notional Market",
            "value": format(notional, "f"),
            "value-effect": "Debit",
            "external-identifier": self._external_id(userref),
            "legs": [{"instrument-type": "Cryptocurrency", "symbol": self.settings.symbol,
                      "action": "Buy to Open"}],
        }

    def build_close_body(self, quantity: float, userref: int | None, decimals: int = 8) -> dict:
        q = Decimal(str(quantity)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)
        if q <= 0:
            raise BrokerError("nothing to close")
        return {
            "time-in-force": "IOC",
            "order-type": "Market",
            "external-identifier": self._external_id(userref),
            "legs": [{"instrument-type": "Cryptocurrency", "symbol": self.settings.symbol,
                      "action": "Sell to Close", "quantity": format(q, "f")}],
        }

    # ── execution ──────────────────────────────────────────
    def _preview(self, body: dict) -> dict | None:
        """Broker-side dry-run (never executes). Opt-in via TASTYTRADE_PREVIEW_ORDERS."""
        if not (self.settings.tastytrade_preview_orders and self.client.has_credentials):
            return None
        res = self.client.dry_run_order(self.account_number(), body)
        errs = res.get("errors") or []
        self.audit.record(AuditEvent.SAFETY_CHECK, {
            "broker": "tastytrade", "check": "broker_dry_run",
            "errors": [e.get("code") for e in errs],
            "warnings": [w.get("code") for w in res.get("warnings") or []],
            "fees": (res.get("fee-calculation") or {}).get("total-fees"),
        })
        return res

    def _execute(self, body: dict, side: str) -> str:
        ext = body["external-identifier"]
        if not self.order_submission_enabled:
            preview = None
            try:
                preview = self._preview(body)
            except BrokerError as exc:
                self.audit.record(AuditEvent.BROKER_ERROR,
                                  {"broker": "tastytrade", "stage": "preview", "error": str(exc)},
                                  severity="warning")
            self.audit.record(AuditEvent.ORDER_INTENT, {
                "broker": "tastytrade", "side": side, "external_identifier": ext,
                "body": body, "locks": self.lock_report(),
                "preview_errors": [e.get("code") for e in (preview or {}).get("errors") or []],
            })
            return f"tastytrade-dry-{side}-{ext}"

        # Live path: broker dry-run is mandatory, regardless of preview setting.
        account = self.account_number()
        res = self.client.dry_run_order(account, body)
        if res.get("errors"):
            codes = [e.get("code") for e in res["errors"]]
            self.audit.record(AuditEvent.ORDER_REJECTED,
                              {"broker": "tastytrade", "stage": "dry_run", "codes": codes},
                              severity="error")
            raise BrokerError(f"tastytrade dry-run rejected the order: {codes}")
        self.audit.record(AuditEvent.ORDER_INTENT, {"broker": "tastytrade", "side": side,
                                                    "external_identifier": ext})
        try:
            placed = self.client.submit_order(account, body)
        except UncertainSubmissionError:
            time.sleep(1.0)
            found = self.client.find_order_by_external_id(account, ext)
            if found:
                self.audit.record(AuditEvent.RECOVERY, {"broker": "tastytrade",
                                                        "external_identifier": ext,
                                                        "order_id": found.get("id")})
                return str(found.get("id"))
            self.audit.record(AuditEvent.BROKER_ERROR, {
                "broker": "tastytrade", "external_identifier": ext,
                "error": "uncertain submission; order not found; NOT resubmitted"},
                severity="error")
            raise
        except BrokerError as exc:
            self.audit.record(AuditEvent.ORDER_REJECTED, {"broker": "tastytrade",
                                                          "error": str(exc)}, severity="error")
            raise
        order = placed.get("order") or {}
        self.audit.record(AuditEvent.ORDER_SUBMITTED, {"broker": "tastytrade",
                                                       "order_id": order.get("id"),
                                                       "external_identifier": ext, "side": side})
        return str(order.get("id"))

    def buy_notional(self, notional_usd: float, *, userref: int | None = None,
                     leverage: float | None = None) -> str:
        if leverage not in (None, 0, 1, 1.0):
            raise SafetyLockError("tastytrade crypto is spot only; leverage refused")
        return self._execute(self.build_buy_body(notional_usd, userref), "buy")

    def close_position(self, *, userref: int | None = None, quantity: float | None = None) -> str:
        if quantity is None:
            if not self.order_submission_enabled:
                # Paper book holds no broker position: log the close-all intent.
                self.audit.record(AuditEvent.ORDER_INTENT, {
                    "broker": "tastytrade", "side": "sell", "userref": userref,
                    "note": "close-all intent (paper)", "locks": self.lock_report()})
                return f"tastytrade-dry-sell-{self._external_id(userref)}"
            held = [p for p in self.positions() if p["symbol"] == self.settings.symbol]
            quantity = sum(p["quantity"] for p in held)
            if quantity <= 0:
                return "tastytrade-no-position"
        return self._execute(self.build_close_body(quantity, userref), "sell")

    def margin_positions(self) -> list[dict]:
        return []

    # ── health ─────────────────────────────────────────────
    def health(self, *, include_freshness: bool = True) -> dict:
        out: dict[str, Any] = {"broker": "tastytrade", "locks": self.lock_report(),
                               "crypto_api_enabled": self.settings.tastytrade_crypto_api_enabled}
        t0 = time.time()
        try:
            q = self.get_ticker()
            out.update(reachable=True, error=None, latency_ms=round((time.time() - t0) * 1000, 1),
                       quote={"bid": q["bid"], "ask": q["ask"]})
        except Exception as exc:  # health must never raise
            out.update(reachable=False, error=str(exc))
        return out
