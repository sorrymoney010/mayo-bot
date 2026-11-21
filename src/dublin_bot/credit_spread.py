"""Put credit spread sleeve on tastytrade — defined risk, paper-first.

Why this strategy
-----------------
The edge being harvested is the **volatility risk premium**: index-option
implied volatility has, on average, exceeded the volatility later realised, so
option sellers are paid for bearing crash risk.  A *vertical* put spread caps
that crash risk at ``width - credit`` per share, so no single move can do more
than a known, pre-sized loss.  The premium is not free money: losses cluster in
sell-offs, and the rules below exist to keep any one of those from mattering.

Rules (every number is a setting; defaults shown)
-------------------------------------------------
Entry, checked once per cycle during regular hours:
  * underlying in ``OPTIONS_UNDERLYINGS`` (SPY, QQQ, IWM: deep, penny-wide markets)
  * IV rank >= 0.30 (sell premium only when it is rich vs its own year)
  * liquidity rating >= 3, and no earnings before expiry (single stocks)
  * expiration 30-60 DTE, closest to 45
  * short put |delta| closest to 0.20 (within ±0.08), delta from mid-price IV
  * long put exactly ``width`` below; widest of 10/5/2 that fits the risk budget
  * round-trip fees + slippage <= 10% of the credit (kills fee-starved narrow spreads)
  * every leg's bid/ask <= 15% of mid; net credit >= 15% of width
  * limit price = mid credit, never worse than the natural (bid-ask) credit
Sizing: max loss per spread = (width - credit) * 100 + fees.  Contracts =
  floor(5% of sleeve equity / max loss); total open max loss <= 25%; <= 3 open.
Exit (first that fires):
  * take profit when 50% of the credit is captured
  * stop when the debit to close reaches credit * (1 + 2.0), i.e. a loss of 2x
  * time exit at <= 21 DTE (gamma risk grows fast near expiry)
  * expiration safety net: settle at intrinsic if ever held to expiry

Safety: nothing here can send a real order unless **all** of
``allow_order_submission``, ``ALLOW_LIVE_TRADING``, ``PAPER_TRADING=false``,
``DRY_RUN=false``, the risk acknowledgement, ``ALLOW_OPTIONS_LIVE_ORDERS`` and
``TASTYTRADE_ENV=production`` are set.  Default: paper ledger only.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .audit import AuditEvent, AuditLog
from .config import Settings
from .errors import BrokerError
from .options_math import bs_put_delta, implied_vol_put, parse_occ
from .tastytrade_client import TastytradeClient, UncertainSubmissionError

ET = ZoneInfo("America/New_York")
LIVE_ACK = "I_ACCEPT_LIVE_TRADING_RISK"
CONTRACT_MULTIPLIER = 100
_COMMISSION_CAP_PER_LEG = 10.0
_ETFS = {"SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "GLD", "TLT", "EEM", "SMH"}


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _cents_down(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def _cents_up(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_UP))


def normalize_iv_rank(raw: Any) -> float | None:
    """tastytrade returns IV rank as a decimal (0.31); tolerate percent (31.0)."""
    v = _f(raw, default=float("nan"))
    if math.isnan(v):
        return None
    return v / 100.0 if v > 1.0 else v


def regular_session_open(now: datetime) -> bool:
    """Weekday 09:45-15:45 ET: skip the open/close auctions' wide markets.

    Exchange holidays are not modelled; on a holiday quotes are stale and the
    leg-spread / freshness checks reject the trade anyway.
    """
    et = now.astimezone(ET)
    return et.weekday() < 5 and dtime(9, 45) <= et.time() <= dtime(15, 45)


# ── fees ────────────────────────────────────────────────────
def open_fees(contracts: int, s: Settings, legs: int = 2) -> float:
    commission = min(contracts * s.options_fee_open_per_contract, _COMMISSION_CAP_PER_LEG) * legs
    return round(commission + contracts * legs * s.options_clearing_fee_per_contract, 2)


def close_fees(contracts: int, s: Settings, legs: int = 2) -> float:
    return round(contracts * legs * s.options_clearing_fee_per_contract, 2)


def round_trip_cost(s: Settings) -> float:
    """Fees + slippage (both ways) for one 2-leg spread, in dollars."""
    return round(open_fees(1, s) + close_fees(1, s)
                 + 2 * s.options_slippage_per_spread * CONTRACT_MULTIPLIER, 2)


# ── data shapes ─────────────────────────────────────────────
@dataclass
class SpreadCandidate:
    underlying: str
    expiration: str
    dte: int
    short_symbol: str
    long_symbol: str
    short_strike: float
    long_strike: float
    width: float
    short_delta: float
    credit_mid: float
    credit_natural: float
    limit_credit: float
    spot: float

    @property
    def credit_ratio(self) -> float:
        return self.limit_credit / self.width

    @property
    def max_loss_per_spread(self) -> float:
        return (self.width - self.limit_credit) * CONTRACT_MULTIPLIER


@dataclass
class OpenSpread:
    id: str
    underlying: str
    expiration: str
    short_symbol: str
    long_symbol: str
    short_strike: float
    long_strike: float
    width: float
    contracts: int
    entry_credit: float
    entry_time: str
    entry_fees: float
    max_loss: float
    short_delta: float
    mode: str = "paper"          # paper | live
    status: str = "open"         # open | pending_open | closed
    order_id: str | None = None
    last_debit: float | None = None
    exit_time: str | None = None
    exit_debit: float | None = None
    exit_reason: str | None = None
    exit_fees: float = 0.0
    realized_pnl: float | None = None
    notes: list[str] = field(default_factory=list)

    def unrealized(self, debit: float) -> float:
        return round((self.entry_credit - debit) * CONTRACT_MULTIPLIER * self.contracts
                     - self.entry_fees, 2)


# ── pure rules ─────────────────────────────────────────────
def screen_underlying(symbol: str, metrics: dict, s: Settings, expiry: date | None = None) -> tuple[bool, str]:
    ivr = normalize_iv_rank(metrics.get("implied-volatility-index-rank"))
    if ivr is None:
        return False, "no IV rank"
    if ivr < s.options_min_iv_rank:
        return False, f"IV rank {ivr:.2f} < {s.options_min_iv_rank:.2f}"
    liq = metrics.get("liquidity-rating")
    if liq is not None and int(_f(liq)) < s.options_min_liquidity_rating:
        return False, f"liquidity rating {liq} < {s.options_min_liquidity_rating}"
    if symbol.upper() not in _ETFS:
        er = (metrics.get("earnings") or {}).get("expected-report-date")
        if er and expiry and date.fromisoformat(er) <= expiry:
            return False, f"earnings {er} before expiry"
    return True, f"IV rank {ivr:.2f}"


def pick_expiration(chain: dict, s: Settings) -> dict | None:
    exps = [e for e in chain.get("expirations", [])
            if s.options_dte_min <= int(e.get("days-to-expiration", -1)) <= s.options_dte_max]
    if not exps:
        return None
    # Closest to target; ties go to the standard monthly ("Regular") cycle.
    return min(exps, key=lambda e: (abs(int(e["days-to-expiration"]) - s.options_dte_target),
                                    e.get("expiration-type") != "Regular"))


def _leg_ok(q: dict, max_spread_pct: float) -> tuple[bool, float, float, float]:
    bid, ask = _f(q.get("bid")), _f(q.get("ask"))
    if bid <= 0 or ask <= 0 or ask < bid:
        return False, bid, ask, 0.0
    mid = (bid + ask) / 2
    return (ask - bid) / mid <= max_spread_pct, bid, ask, mid


def build_candidate(underlying: str, expiration: dict, spot: float, quotes: dict[str, dict],
                    s: Settings, today: date, max_width: float | None = None,
                    rate: float = 0.04) -> tuple[SpreadCandidate | None, str]:
    """Pick the short strike by delta, then the widest allowed long strike."""
    exp_date = date.fromisoformat(expiration["expiration-date"])
    dte = (exp_date - today).days
    t = max(dte, 1) / 365.0
    strikes = sorted((_f(k["strike-price"]), k["put"]) for k in expiration.get("strikes", []))
    by_strike = {k: sym for k, sym in strikes}

    best = None
    for k, sym in strikes:
        if k >= spot:
            continue
        q = quotes.get(sym)
        if not q:
            continue
        ok, _, _, mid = _leg_ok(q, s.options_max_leg_spread_pct)
        if not ok:
            continue
        iv = implied_vol_put(mid, spot, k, t, r=rate)
        if iv is None:
            continue
        d = abs(bs_put_delta(spot, k, t, iv, r=rate))
        err = abs(d - s.options_short_delta)
        if err <= s.options_delta_tolerance and (best is None or err < best[0]):
            best = (err, k, sym, d)
    if best is None:
        return None, "no short put near target delta with a tight market"
    _, short_k, short_sym, short_delta = best
    sq = quotes[short_sym]

    widths = sorted({w for w in s.options_width_candidates
                     if max_width is None or w <= max_width}, reverse=True)
    reasons = []
    for w in widths:
        long_k = round(short_k - w, 3)
        long_sym = by_strike.get(long_k)
        if not long_sym or long_sym not in quotes:
            reasons.append(f"w{w:g}: no long strike")
            continue
        ok, lbid, lask, lmid = _leg_ok(quotes[long_sym], s.options_max_leg_spread_pct)
        if not ok:
            reasons.append(f"w{w:g}: long leg market too wide")
            continue
        sbid, sask = _f(sq["bid"]), _f(sq["ask"])
        smid = (sbid + sask) / 2
        credit_mid = smid - lmid
        credit_nat = sbid - lask
        limit = max(_cents_down(credit_mid), _cents_up(max(credit_nat, 0.0)))
        if limit <= 0 or limit >= w:
            reasons.append(f"w{w:g}: credit {limit:.2f} invalid")
            continue
        if limit / w < s.options_min_credit_ratio:
            reasons.append(f"w{w:g}: credit {limit:.2f} < {s.options_min_credit_ratio:.0%} of width")
            continue
        cost = round_trip_cost(s)
        if cost > s.options_max_cost_to_credit * limit * CONTRACT_MULTIPLIER:
            reasons.append(f"w{w:g}: costs ${cost:.2f} > {s.options_max_cost_to_credit:.0%} "
                           f"of ${limit * CONTRACT_MULTIPLIER:.0f} credit")
            continue
        return SpreadCandidate(
            underlying=underlying, expiration=exp_date.isoformat(), dte=dte,
            short_symbol=short_sym, long_symbol=long_sym, short_strike=short_k,
            long_strike=long_k, width=w, short_delta=round(short_delta, 3),
            credit_mid=round(credit_mid, 4), credit_natural=round(credit_nat, 4),
            limit_credit=limit, spot=spot,
        ), "ok"
    return None, "; ".join(reasons) or "no width candidates"


def size_contracts(c: SpreadCandidate, equity: float, open_risk: float, s: Settings) -> int:
    per_spread = c.max_loss_per_spread + open_fees(1, s) + close_fees(1, s)
    if per_spread <= 0:
        return 0
    budget = min(equity * s.options_risk_per_trade,
                 equity * s.options_max_total_risk - open_risk)
    return max(0, int(budget // per_spread))


def exit_decision(sp: OpenSpread, debit_mid: float, today: date, s: Settings) -> str | None:
    exp = date.fromisoformat(sp.expiration)
    if today >= exp:
        return "expiration"
    captured = sp.entry_credit - debit_mid
    if captured >= s.options_take_profit_pct * sp.entry_credit:
        return "take_profit"
    if debit_mid >= sp.entry_credit * (1.0 + s.options_stop_loss_multiple):
        return "stop_loss"
    if (exp - today).days <= s.options_exit_dte:
        return "time_exit"
    return None


# ── order bodies ────────────────────────────────────────────
def open_order_body(c: SpreadCandidate, contracts: int, external_id: str) -> dict:
    return {
        "time-in-force": "Day", "order-type": "Limit",
        "price": f"{c.limit_credit:.2f}", "price-effect": "Credit",
        "external-identifier": external_id,
        "legs": [
            {"instrument-type": "Equity Option", "symbol": c.short_symbol,
             "quantity": str(contracts), "action": "Sell to Open"},
            {"instrument-type": "Equity Option", "symbol": c.long_symbol,
             "quantity": str(contracts), "action": "Buy to Open"},
        ],
    }


def close_order_body(sp: OpenSpread, debit: float, external_id: str) -> dict:
    return {
        "time-in-force": "Day", "order-type": "Limit",
        "price": f"{_cents_up(debit):.2f}", "price-effect": "Debit",
        "external-identifier": external_id,
        "legs": [
            {"instrument-type": "Equity Option", "symbol": sp.short_symbol,
             "quantity": str(sp.contracts), "action": "Buy to Close"},
            {"instrument-type": "Equity Option", "symbol": sp.long_symbol,
             "quantity": str(sp.contracts), "action": "Sell to Close"},
        ],
    }


# ── paper ledger ────────────────────────────────────────────
class SpreadBook:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.spreads: list[OpenSpread] = []
        if self.path.exists():
            raw = json.loads(self.path.read_text() or "[]")
            self.spreads = [OpenSpread(**r) for r in raw]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(x) for x in self.spreads], indent=2))
        tmp.replace(self.path)

    @property
    def open(self) -> list[OpenSpread]:
        return [x for x in self.spreads if x.status in ("open", "pending_open", "pending_close")]

    def open_risk(self) -> float:
        return sum(x.max_loss for x in self.open)

    def realized(self) -> float:
        return round(sum(x.realized_pnl or 0.0 for x in self.spreads if x.status == "closed"), 2)

    def stats(self) -> dict:
        closed = [x for x in self.spreads if x.status == "closed"]
        wins = [x for x in closed if (x.realized_pnl or 0) > 0]
        gross_win = sum(x.realized_pnl for x in wins)
        gross_loss = -sum(x.realized_pnl for x in closed if (x.realized_pnl or 0) <= 0)
        return {
            "closed": len(closed), "open": len(self.open),
            "win_rate": round(len(wins) / len(closed), 3) if closed else None,
            "realized_pnl": self.realized(),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "open_max_loss": round(self.open_risk(), 2),
        }


# ── the sleeve ──────────────────────────────────────────────
class CreditSpreadSleeve:
    def __init__(self, settings: Settings, *, client: TastytradeClient | None = None,
                 audit: AuditLog | None = None, book: SpreadBook | None = None,
                 allow_order_submission: bool = False):
        self.s = settings
        self.client = client or TastytradeClient(
            refresh_token=settings.tastytrade_refresh_token,
            client_secret=settings.tastytrade_client_secret,
            env=settings.tastytrade_env, timeout=settings.http_timeout_seconds,
            max_retries=settings.max_retries)
        self.audit = audit or AuditLog(Path(settings.audit_log_path))
        self.book = book or SpreadBook(settings.options_ledger_path)
        self._allow_order_submission = bool(allow_order_submission)

    @property
    def live_enabled(self) -> bool:
        s = self.s
        return (self._allow_order_submission and s.allow_live_trading and not s.paper_trading
                and not s.dry_run and s.live_risk_acknowledgement == LIVE_ACK
                and s.allow_options_live_orders and self.client.is_production
                and bool(s.tastytrade_account_number))

    def equity(self) -> float:
        base = self.s.options_equity_usd
        return round(base + self.book.realized(), 2)

    # ── data ──
    def _quotes(self, symbols: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(symbols), 100):
            out.update(self.client.market_data(equity_option=symbols[i:i + 100]))
        return out

    def _spot(self, underlying: str) -> float:
        q = self.client.market_data(equity=[underlying]).get(underlying) or {}
        spot = _f(q.get("mid")) or _f(q.get("mark")) or _f(q.get("last"))
        if spot <= 0:
            raise BrokerError(f"no usable quote for {underlying}")
        return spot

    def _debit_mid(self, sp: OpenSpread) -> float | None:
        q = self._quotes([sp.short_symbol, sp.long_symbol])
        a, b = q.get(sp.short_symbol), q.get(sp.long_symbol)
        if not a or not b:
            return None
        short_mid = (_f(a.get("bid")) + _f(a.get("ask"))) / 2
        long_mid = (_f(b.get("bid")) + _f(b.get("ask"))) / 2
        if short_mid <= 0:
            return None
        return max(0.0, short_mid - long_mid)

    # ── execution (paper or live) ──
    def _place(self, body: dict, kind: str) -> tuple[str, str]:
        ext = body["external-identifier"]
        if not self.live_enabled:
            if self.s.tastytrade_preview_orders and self.client.has_credentials \
                    and self.s.tastytrade_account_number:
                try:
                    res = self.client.dry_run_order(self.s.tastytrade_account_number, body)
                    self.audit.record(AuditEvent.SAFETY_CHECK, {
                        "sleeve": "credit_spread", "check": "broker_dry_run", "kind": kind,
                        "errors": [e.get("code") for e in res.get("errors") or []],
                        "bp_effect": (res.get("buying-power-effect") or {}).get("change-in-buying-power"),
                        "fees": (res.get("fee-calculation") or {}).get("total-fees")})
                except BrokerError as exc:
                    self.audit.record(AuditEvent.BROKER_ERROR, {"sleeve": "credit_spread",
                                                                "stage": "preview", "error": str(exc)},
                                      severity="warning")
            self.audit.record(AuditEvent.ORDER_INTENT, {"sleeve": "credit_spread", "kind": kind,
                                                        "mode": "paper", "body": body})
            return "paper", f"paper-{ext}"
        acct = self.s.tastytrade_account_number
        res = self.client.dry_run_order(acct, body)
        if res.get("errors"):
            codes = [e.get("code") for e in res["errors"]]
            self.audit.record(AuditEvent.ORDER_REJECTED, {"sleeve": "credit_spread",
                                                          "stage": "dry_run", "codes": codes},
                              severity="error")
            raise BrokerError(f"dry-run rejected {kind}: {codes}")
        try:
            placed = self.client.submit_order(acct, body)
        except UncertainSubmissionError:
            found = self.client.find_order_by_external_id(acct, ext)
            if not found:
                self.audit.record(AuditEvent.BROKER_ERROR, {
                    "sleeve": "credit_spread", "external_identifier": ext,
                    "error": "uncertain submission, not found, NOT resubmitted"}, severity="error")
                raise
            return "live", str(found.get("id"))
        oid = str((placed.get("order") or {}).get("id"))
        self.audit.record(AuditEvent.ORDER_SUBMITTED, {"sleeve": "credit_spread", "kind": kind,
                                                       "order_id": oid, "external_identifier": ext})
        return "live", oid

    # ── cycle ──
    def manage_exits(self, now: datetime) -> list[dict]:
        events = []
        today = now.astimezone(ET).date()
        for sp in self.book.open:
            if sp.status != "open":
                continue
            exp = date.fromisoformat(sp.expiration)
            if today >= exp:
                # Safety net: settle at intrinsic using the underlying quote.
                spot = self._spot(sp.underlying)
                debit = min(sp.width, max(0.0, sp.short_strike - spot))
                self._close(sp, debit, "expiration", now, paper_fill=True)
                events.append({"id": sp.id, "exit": "expiration", "pnl": sp.realized_pnl})
                continue
            debit = self._debit_mid(sp)
            if debit is None:
                events.append({"id": sp.id, "skip": "no quotes"})
                continue
            sp.last_debit = round(debit, 4)
            reason = exit_decision(sp, debit, today, self.s)
            if reason:
                self._close(sp, debit, reason, now)
                events.append({"id": sp.id, "exit": reason, "pnl": sp.realized_pnl})
        self.book.save()
        return events

    def _close(self, sp: OpenSpread, debit_mid: float, reason: str, now: datetime,
               paper_fill: bool = False) -> None:
        fill = debit_mid if paper_fill else min(sp.width, debit_mid + self.s.options_slippage_per_spread)
        mode = "paper"
        if not paper_fill:
            mode, oid = self._place(close_order_body(sp, fill, f"mayo-x-{sp.id}"), "close")
            if mode == "live":
                sp.notes.append(f"close order {oid} submitted at {fill:.2f}; awaiting fill")
                sp.order_id = oid
        fees = close_fees(sp.contracts, self.s)
        # Live closes stay at risk until the broker shows the legs gone.
        sp.status = "pending_close" if mode == "live" else "closed"
        sp.exit_time = now.isoformat()
        sp.exit_debit = round(fill, 4)
        sp.exit_reason = reason
        sp.exit_fees = fees
        sp.realized_pnl = round((sp.entry_credit - fill) * CONTRACT_MULTIPLIER * sp.contracts
                                - sp.entry_fees - fees, 2)
        self.audit.record(AuditEvent.ORDER_FILLED, {"sleeve": "credit_spread", "kind": "close",
                                                    "id": sp.id, "reason": reason,
                                                    "pnl": sp.realized_pnl})

    def scan_entries(self, now: datetime) -> list[dict]:
        events = []
        today = now.astimezone(ET).date()
        held = {x.underlying for x in self.book.open}
        for und in self.s.options_underlyings:
            if len(self.book.open) >= self.s.options_max_positions:
                events.append({"underlying": und, "skip": "max positions"})
                break
            if und in held:
                events.append({"underlying": und, "skip": "already holding a spread"})
                continue
            try:
                ev = self._try_enter(und, now, today)
            except BrokerError as exc:
                ev = {"underlying": und, "error": str(exc)}
            events.append(ev)
        self.book.save()
        return events

    def _try_enter(self, und: str, now: datetime, today: date) -> dict:
        metrics = self.client.market_metrics([und]).get(und, {})
        chain = self.client.nested_option_chain(und)
        exp = pick_expiration(chain, self.s)
        if exp is None:
            return {"underlying": und, "skip": "no expiration in DTE window"}
        ok, why = screen_underlying(und, metrics, self.s, date.fromisoformat(exp["expiration-date"]))
        if not ok:
            return {"underlying": und, "skip": why}
        spot = self._spot(und)
        band = [k["put"] for k in exp["strikes"]
                if spot * 0.75 <= _f(k["strike-price"]) < spot]
        quotes = self._quotes(band)
        equity, open_risk = self.equity(), self.book.open_risk()
        cand, why = build_candidate(und, exp, spot, quotes, self.s, today)
        # Step down in width until at least one contract fits the risk budget.
        smallest_viable = cand
        while cand is not None and size_contracts(cand, equity, open_risk, self.s) < 1:
            smallest_viable = cand
            smaller = [w for w in self.s.options_width_candidates if w < cand.width]
            cand = None
            if smaller:
                cand, why = build_candidate(und, exp, spot, quotes, self.s, today,
                                            max_width=max(smaller))
        if cand is None and smallest_viable is not None:
            return {"underlying": und, "skip": (
                f"risk budget too small: narrowest viable spread is "
                f"{smallest_viable.width:g}-wide with max loss "
                f"${smallest_viable.max_loss_per_spread:.0f}; budget is "
                f"${equity * self.s.options_risk_per_trade:.0f} per trade")}
        if cand is None:
            return {"underlying": und, "skip": why}
        contracts = size_contracts(cand, equity, open_risk, self.s)
        sid = uuid.uuid4().hex[:12]
        fill_credit = cand.limit_credit
        if fill_credit - self.s.options_slippage_per_spread >= max(cand.credit_natural, 0.01):
            fill_credit = round(fill_credit - self.s.options_slippage_per_spread, 4)
        mode, oid = self._place(open_order_body(cand, contracts, f"mayo-o-{sid}"), "open")
        fees = open_fees(contracts, self.s)
        sp = OpenSpread(
            id=sid, underlying=und, expiration=cand.expiration, short_symbol=cand.short_symbol,
            long_symbol=cand.long_symbol, short_strike=cand.short_strike,
            long_strike=cand.long_strike, width=cand.width, contracts=contracts,
            entry_credit=fill_credit, entry_time=now.isoformat(), entry_fees=fees,
            max_loss=round((cand.width - fill_credit) * CONTRACT_MULTIPLIER * contracts + fees, 2),
            short_delta=cand.short_delta, mode=mode,
            status="open" if mode == "paper" else "pending_open", order_id=oid,
        )
        self.book.spreads.append(sp)
        self.audit.record(AuditEvent.ORDER_FILLED if mode == "paper" else AuditEvent.ORDER_SUBMITTED,
                          {"sleeve": "credit_spread", "kind": "open", **asdict(sp)})
        return {"underlying": und, "entered": sid, "short": cand.short_strike,
                "long": cand.long_strike, "width": cand.width, "credit": fill_credit,
                "contracts": contracts, "max_loss": sp.max_loss, "dte": cand.dte,
                "delta": cand.short_delta, "mode": mode}

    def reconcile_live(self) -> list[dict]:
        """Promote pending live opens once both legs show up as broker positions."""
        pend = [x for x in self.book.open if x.status in ("pending_open", "pending_close")]
        if not pend or not self.client.has_credentials:
            return []
        held = {p.get("symbol"): p for p in self.client.positions(self.s.tastytrade_account_number,
                                                                    **{"instrument-type": "Equity Option"})}
        out = []
        for sp in pend:
            both = sp.short_symbol in held and sp.long_symbol in held
            neither = sp.short_symbol not in held and sp.long_symbol not in held
            if sp.status == "pending_open" and both:
                sp.status = "open"
                out.append({"id": sp.id, "status": "open"})
            elif sp.status == "pending_close" and neither:
                sp.status = "closed"
                out.append({"id": sp.id, "status": "closed"})
            elif not both and not neither:
                # One leg only: a legging risk the operator must see immediately.
                self.audit.record(AuditEvent.RECONCILIATION, {
                    "sleeve": "credit_spread", "id": sp.id,
                    "error": "only one leg held at broker"}, severity="error")
                out.append({"id": sp.id, "status": "LEG MISMATCH"})
        self.book.save()
        return out

    def run_cycle(self, now: datetime | None = None, *, force_hours: bool = False) -> dict:
        now = now or datetime.now(ET)
        if not self.s.options_sleeve_enabled:
            return {"sleeve": "credit_spread", "skipped": "OPTIONS_SLEEVE_ENABLED=false"}
        if not self.client.has_credentials:
            return {"sleeve": "credit_spread", "skipped": "no tastytrade credentials (market data needs OAuth)"}
        if not (force_hours or regular_session_open(now)):
            return {"sleeve": "credit_spread", "skipped": "outside 09:45-15:45 ET weekday session"}
        result = {"sleeve": "credit_spread", "mode": "live" if self.live_enabled else "paper",
                  "reconcile": self.reconcile_live(), "exits": self.manage_exits(now),
                  "entries": self.scan_entries(now)}
        result["book"] = self.book.stats()
        result["equity"] = self.equity()
        return result


def describe_open(book: SpreadBook) -> list[dict]:
    return [{"id": x.id, "underlying": x.underlying, "exp": x.expiration,
             "strikes": f"{x.short_strike:g}/{x.long_strike:g}", "contracts": x.contracts,
             "credit": x.entry_credit, "last_debit": x.last_debit, "max_loss": x.max_loss,
             "status": x.status, "mode": x.mode,
             "unrealized": x.unrealized(x.last_debit) if x.last_debit is not None else None,
             "occ_short": str(parse_occ(x.short_symbol))}
            for x in book.open]
