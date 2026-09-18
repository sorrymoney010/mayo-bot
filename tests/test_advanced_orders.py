"""Tests for the advanced-order layer: bracket building, gateway add/cancel/edit, engine wiring."""

from __future__ import annotations

import json

from dublin_bot.orders import (
    BracketPlan,
    bracket_prices,
    fmt_price,
    limit_entry_price,
)
from dublin_bot.kraken_gateway import KrakenGateway
from dublin_bot.config import Settings
from dublin_bot.engine import TradingEngine
from dublin_bot.models import Action, Signal
from .conftest import default_routes, FakeResponse


def test_fmt_price_strips_zeros():
    assert fmt_price(1.5) == "1.5"
    assert fmt_price(0.003876) == "0.003876"
    assert fmt_price(100.0) == "100"


def test_limit_entry_price_buy_below_ask():
    # Buy posts below the ask to be a maker.
    assert limit_entry_price("buy", 100.0, 0.001) == 99.9
    # Sell posts above the bid.
    assert limit_entry_price("sell", 100.0, 0.001) == 100.1


def test_bracket_prices_long():
    sl, tp = bracket_prices("buy", 100.0, 0.04, 0.08)
    assert sl == 96.0
    assert tp == 108.0


def test_bracket_prices_short():
    sl, tp = bracket_prices("sell", 100.0, 0.04, 0.08)
    # Short: stop above, target below.
    assert sl == 104.0
    assert tp == 92.0


def test_bracket_plan_market_with_bracket_params():
    plan = BracketPlan(
        pair="PUMPUSD", side="buy", volume="2590", ordertype="market",
        stop_loss=0.96, userref=12345, pair_decimals=6,
    )
    p = plan.to_addorder_params()
    assert p["pair"] == "PUMPUSD"
    assert p["type"] == "buy"
    assert p["ordertype"] == "market"
    assert p["close[ordertype]"] == "stop-loss"
    assert p["close[price]"] == "0.960000"  # rounded to pair_decimals (6)
    assert p["close[userref]"] == "12346"
    assert "close[1]" not in p  # single-leg stop only (TP is a separate order)


def test_bracket_plan_trailing():
    plan = BracketPlan(
        pair="PUMPUSD", side="buy", volume="2590", ordertype="market",
        stop_loss=0.96, userref=5, trailing=True,
    )
    p = plan.to_addorder_params()
    assert p["close[trailing]"] == "4%"
    assert "close[price]" not in p  # trailing uses offset, not absolute price


def test_bracket_reference_survives_engine_rebuild(settings_factory):
    """A later exit retains the exact child txids it owns."""
    s = settings_factory(
        paper_trading=False,
        dry_run=False,
        allow_live_trading=True,
        live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK",
    )
    first = TradingEngine(s, gateway=object())
    first._record_bracket_ref("PUMP/USD", "ENTRY-1", ["STOP-1", "TARGET-1"])

    second = TradingEngine(s, gateway=object())
    assert second._bracket_ref_for("PUMP/USD") == {
        "entry_order_id": "ENTRY-1",
        "child_order_ids": ["STOP-1", "TARGET-1"],
        "complete": True,
    }
    second._clear_bracket_ref("PUMP/USD")

    third = TradingEngine(s, gateway=object())
    assert third._bracket_ref_for("PUMP/USD") is None


def test_phantom_lot_is_reconciled_against_exchange_balances(settings_factory, tmp_path):
    """A lot the exchange no longer holds must not freeze the bot in exit-only WAIT."""
    ledger = tmp_path / "bot_positions.json"
    ledger.write_text('{"PUMP/USD": 2693.6, "BTC/USD": 7.809e-05}', encoding="utf-8")

    class _Gateway:
        def balances(self):
            # Exchange holds neither asset: both lots were closed outside the bot.
            return {"PUMP": 0.0, "XXBT": 0.0, "ZUSD": 101.75}

        def resolve_symbol(self, symbol):
            class _Meta:
                base = "PUMP" if symbol.startswith("PUMP") else "XXBT"
            return _Meta()

    s = settings_factory(
        symbol="PUMP/USD",
        paper_trading=False,
        dry_run=False,
        allow_live_trading=True,
        live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK",
        session_state_path=str(tmp_path / "session.json"),
        journal_path=str(tmp_path / "journal.jsonl"),
        audit_log_path=str(tmp_path / "audit.jsonl"),
        idempotency_path=str(tmp_path / "orders.json"),
        learner_path=str(tmp_path / "learner.json"),
    )
    engine = TradingEngine(s, gateway=_Gateway())
    engine._bot_qty_path = ledger
    engine._bot_qty = engine._load_bot_qty()
    assert engine._bot_qty, "fixture should start with persisted lots"

    engine._reconcile_bot_qty()

    assert engine._bot_qty == {}
    assert json.loads(ledger.read_text(encoding="utf-8")) == {}


def test_phantom_lot_reconciliation_keeps_lots_when_balance_read_fails(
    settings_factory, tmp_path
):
    """An unverifiable balance read must NOT delete real lots (fail closed)."""
    ledger = tmp_path / "bot_positions.json"
    ledger.write_text('{"PUMP/USD": 2693.6}', encoding="utf-8")

    class _Gateway:
        def balances(self):
            raise RuntimeError("rate limited")

    s = settings_factory(
        symbol="PUMP/USD",
        paper_trading=False,
        dry_run=False,
        allow_live_trading=True,
        live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK",
        session_state_path=str(tmp_path / "session.json"),
        journal_path=str(tmp_path / "journal.jsonl"),
        audit_log_path=str(tmp_path / "audit.jsonl"),
        idempotency_path=str(tmp_path / "orders.json"),
        learner_path=str(tmp_path / "learner.json"),
    )
    engine = TradingEngine(s, gateway=_Gateway())
    engine._bot_qty_path = ledger
    engine._bot_qty = engine._load_bot_qty()

    engine._reconcile_bot_qty()

    assert engine._bot_qty == {"PUMP/USD": 2693.6}


def test_limit_price_respects_pair_decimals():
    """Kraken rejects over-precise prices (BTC/USD allows 1 decimal)."""
    assert fmt_price(77617.2, 1) == "77617.2"
    assert fmt_price(0.00378022, 6) == "0.00378"  # trailing zeros trimmed
    # No decimals given -> legacy trim behaviour, unchanged.
    assert fmt_price(1.5) == "1.5"
    assert fmt_price(0.003876) == "0.003876"
    # Never exceed the pair's precision.
    assert len(fmt_price(1234.56789, 1).split(".")[-1]) <= 1
    assert len(fmt_price(0.0012345678, 6).split(".")[-1]) <= 6


def test_limit_entry_price_rounds_to_pair_precision():
    """A rounded entry must still sit inside the spread to earn the maker fee."""
    # BTC: 1 decimal. Raw 0.1% off 77638.0 = 77560.362 -> 77560.4
    p = limit_entry_price("buy", 77638.0, 0.001, 1)
    assert round(p, 1) == p
    assert p < 77638.0
    # PUMP: 6 decimals.
    p2 = limit_entry_price("buy", 0.003784, 0.001, 6)
    assert p2 < 0.003784


def test_norm_pair_unifies_kraken_spellings():
    """XBTUSD / XXBTZUSD / BTC/USD are one pair; a mismatch must not reject it."""
    from dublin_bot.engine import _norm_pair

    assert _norm_pair("XBTUSD") == _norm_pair("XXBTZUSD") == _norm_pair("BTC/USD")
    assert _norm_pair("XXDGZUSD") == _norm_pair("DOGE/USD")
    assert _norm_pair("PUMPUSD") == _norm_pair("PUMP/USD")
    assert _norm_pair("PUMPUSD") != _norm_pair("XBTUSD")


def test_bracket_children_recovered_when_never_captured(settings_factory, tmp_path):
    """A bracket with no child IDs must adopt its own stop by userref + pair."""
    brackets = tmp_path / "bot_brackets.json"
    brackets.write_text(
        json.dumps({"BTC/USD": {"entry_order_id": "ENTRY-1",
                                "child_order_ids": [], "complete": False}}),
        encoding="utf-8",
    )
    lots = tmp_path / "bot_positions.json"
    lots.write_text('{"BTC/USD": 0.00025955}', encoding="utf-8")

    class _Gateway:
        def find_children_by_userref(self, userref):
            assert userref == 1501986096
            # Kraken reports the altname; resolve_symbol returns the key.
            return [{"id": "STOP-1", "symbol": "XBTUSD"}]

        def resolve_symbol(self, symbol=None):
            class _Meta:
                key = "XXBTZUSD"
                altname = "XBTUSD"
            return _Meta()

    s = settings_factory(
        symbol="BTC/USD",
        paper_trading=False,
        dry_run=False,
        allow_live_trading=True,
        live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK",
        session_state_path=str(tmp_path / "session.json"),
        journal_path=str(tmp_path / "journal.jsonl"),
        audit_log_path=str(tmp_path / "audit.jsonl"),
        idempotency_path=str(tmp_path / "orders.json"),
        learner_path=str(tmp_path / "learner.json"),
    )
    engine = TradingEngine(s, gateway=_Gateway())
    engine._bracket_refs_path = brackets
    engine._bot_qty_path = lots
    engine._bracket_refs = engine._load_bracket_refs()
    engine._bot_qty = engine._load_bot_qty()
    engine._userref_for_order = lambda oid: 1501986096

    engine._reconcile_bracket_refs()

    ref = engine._bracket_ref_for("BTC/USD")
    assert ref["child_order_ids"] == ["STOP-1"]
    assert ref["complete"] is True


def test_fee_model_defaults_to_real_kraken_schedule():
    """Backtest cost assumption must match the exchange, not an optimistic guess."""
    from dublin_bot.fills import FillModel

    s = Settings(_env_file=None)
    fm = FillModel(s)
    assert fm.taker_fee_bps == 80.0
    assert fm.maker_fee_bps == 40.0


def test_maker_fill_is_cheaper_than_taker():
    """A passive limit fill must not be charged taker fee + spread crossing."""
    from dublin_bot.fills import FillModel

    s = Settings(_env_file=None)
    fm = FillModel(s)
    taker = fm.buy(price=100.0, volume=1.0, bid=99.9, ask=100.1)
    maker = fm.buy(price=100.0, volume=1.0, bid=99.9, ask=100.1, maker=True)
    assert maker.fee < taker.fee
    assert maker.price < taker.price
    assert "maker" in maker.model_note
    assert "taker" in taker.model_note


def test_fee_schedule_reads_live_kraken_tier():
    """TradeVolume must yield real bps; it returns fees:null without a pair."""
    gw = KrakenGateway(Settings(_env_file=None, broker="kraken",
                                kraken_api_key="test-key-not-real",
                                kraken_api_secret="dGVzdC1zZWNyZXQtbm90LXJlYWw="))
    calls = {}

    def _fake_private(endpoint, params=None):
        calls["endpoint"] = endpoint
        calls["params"] = params or {}
        assert params and params.get("pair"), "TradeVolume needs a pair or fees are null"
        return {
            "fees": {"XXBTZUSD": {"fee": "0.8000"}},
            "fees_maker": {"XXBTZUSD": {"fee": "0.4000"}},
        }

    gw._private = _fake_private
    gw.resolve_symbol = lambda *a, **k: type("M", (), {"key": "XXBTZUSD"})()
    assert gw.fee_schedule() == {"taker_bps": 80.0, "maker_bps": 40.0}


def test_exit_is_blocked_when_owned_bracket_cancellation_is_unconfirmed(settings_factory):
    """A reserved stop may not race an ordinary SELL."""
    from unittest.mock import MagicMock

    s = settings_factory(
        symbol="PUMP/USD",
        paper_trading=False,
        dry_run=False,
        allow_live_trading=True,
        live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK",
    )
    gateway = MagicMock()
    gateway.cancel_orders.return_value = False
    engine = TradingEngine(s, gateway=gateway)
    engine._record_bracket_ref("PUMP/USD", "ENTRY-1", ["STOP-1"])

    assert engine._cancel_attached_bracket(None) is False
    gateway.cancel_orders.assert_called_once_with(["STOP-1"])
    assert engine._bracket_ref_for("PUMP/USD") is not None


def test_exit_is_blocked_when_bracket_child_discovery_is_incomplete(settings_factory):
    """A partial child list must not permit a SELL past an unknown stop."""
    from unittest.mock import MagicMock

    s = settings_factory(
        symbol="PUMP/USD",
        paper_trading=False,
        dry_run=False,
        allow_live_trading=True,
        live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK",
    )
    gateway = MagicMock()
    engine = TradingEngine(s, gateway=gateway)
    engine._record_bracket_ref("PUMP/USD", "ENTRY-1", ["TARGET-1"], complete=False)

    assert engine._cancel_attached_bracket(None) is False
    gateway.cancel_orders.assert_not_called()


def test_live_pnl_sync_failure_is_not_treated_as_a_clean_ledger(settings_factory):
    """Live BUYs must not use stale daily-loss data after a broker read error."""
    from unittest.mock import MagicMock
    from dublin_bot.errors import BrokerError
    from dublin_bot.risk import SessionState

    s = settings_factory(
        paper_trading=False,
        dry_run=False,
        allow_live_trading=True,
        live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK",
    )
    gateway = MagicMock()
    gateway.closed_trade_pnl.side_effect = BrokerError("TradesHistory unavailable")
    engine = TradingEngine(s, gateway=gateway)

    assert engine._refresh_live_realized_pnl(
        SessionState(start_equity=100.0, peak_equity=100.0, current_equity=100.0)
    ) is False


def _make_gw():
    """Gateway whose AddOrder/CancelOrder we stub to capture params."""
    gw = KrakenGateway.__new__(KrakenGateway)
    gw.__dict__["_submitted"] = []
    gw.__dict__["_cancelled"] = []

    def add_order(params):
        gw.__dict__["_submitted"].append(params)
        return f"TX{len(gw.__dict__['_submitted'])}"

    def cancel_order(txid):
        gw.__dict__["_cancelled"].append(txid)
        return True

    gw.add_order = add_order
    gw.cancel_order = cancel_order
    gw.cancel_attached = lambda userref: 0
    return gw


def test_gateway_add_order_records_params():
    gw = _make_gw()
    params = {"pair": "PUMPUSD", "type": "buy", "ordertype": "market", "volume": "2590"}
    oid = gw.add_order(params)
    assert oid == "TX1"
    assert gw.__dict__["_submitted"][0] is params


def test_cancel_orders_confirms_only_explicit_child_ids():
    """Bracket cleanup must never sweep adjacent userref orders."""
    gw = _make_gw()
    open_ids = {"STOP-1", "USER-OWNED"}
    gw.orders = lambda: [{"id": txid} for txid in open_ids]

    def cancel_order(txid):
        gw.__dict__["_cancelled"].append(txid)
        open_ids.discard(txid)
        return True

    gw.cancel_order = cancel_order

    assert gw.cancel_orders(["STOP-1"]) is True
    assert gw.__dict__["_cancelled"] == ["STOP-1"]
    assert open_ids == {"USER-OWNED"}


def test_engine_buy_uses_bracket_when_configured():
    from dublin_bot.audit import AuditLog
    from dublin_bot.ratelimit import KrakenRateLimiter, RateLimitTier

    s = Settings(_env_file=None, use_bracket=True, order_type="market",
                 stop_loss_pct=0.04, take_profit_pct=0.08,
                 paper_trading=True, dry_run=True, allow_live_trading=False,
                 broker="kraken", kraken_api_key="k", kraken_api_secret="test",
                 symbol="BTC/USD", sentiment_enabled=False, timeframe_minutes=60,
                 lookback_bars=250, universe_mode="basket")
    # Capture the AddOrder request body.
    captured = {}

    class CaptureSession:
        def __init__(self):
            self.routes = default_routes()
            self.headers = {}
        def get(self, url, params=None, timeout=None, headers=None):
            ep = url.rstrip("/").split("/")[-1]
            if ep == "OHLC":
                return FakeResponse(self.routes["OHLC"])
            if ep == "Ticker":
                return FakeResponse(self.routes["Ticker"])
            if ep == "AssetPairs":
                return FakeResponse(self.routes["AssetPairs"])
            if ep == "Time":
                return FakeResponse(self.routes["Time"])
            raise AssertionError(ep)
        def post(self, url, data=None, timeout=None, headers=None):
            ep = url.rstrip("/").split("/")[-1]
            if ep == "AddOrder":
                captured.update(data or {})
                return FakeResponse({"error": [], "result": {"txid": ["TX1"], "descr": {"order": "buy"}}})
            if ep in self.routes:
                return FakeResponse(self.routes[ep])
            raise AssertionError(ep)

    clock = {"t": 0.0}
    def _sleep(seconds):
        clock["t"] += seconds
    audit = AuditLog(s.audit_log_path)
    limiter = KrakenRateLimiter(RateLimitTier.pro(), time_fn=lambda: clock["t"], sleep_fn=_sleep)
    gw = KrakenGateway(s, session=CaptureSession(), rate_limiter=limiter,
                       audit=audit, sleep_fn=lambda _s: None)
    eng = TradingEngine(s, gateway=gw, audit=audit)
    eng.learner.enabled = False

    def fake_evaluate(bars, in_position=False):
        return Signal(Action.BUY, 60, "test setup", 1.0, 1.0, 0.9)
    eng.strategy.evaluate = fake_evaluate
    # BTC/USD at ~$50k: a $2 notional order → tiny volume, but bracket math still runs.
    res = eng.run_cycle()
    # In dry-run, add_order logs the bracket params as an ORDER_INTENT audit
    # event (no network call). Assert the protective stop-loss was built.
    bracket_logged = False
    for ev in audit.entries():
        if ev.get("event") == "order_intent" and ev.get("payload", {}).get("close[ordertype]") == "stop-loss":
            bracket_logged = True
            break
    assert bracket_logged, "protective stop-loss not found in audit"
    assert res.record.signal.action is Action.BUY


def test_budget_cap_limits_sizing_to_strategy_equity():
    """Sizing must never exceed the advertised budget even if the real Kraken
    balance is larger (audit finding #1)."""
    from dublin_bot.risk import RiskManager, SessionState

    s = Settings(_env_file=None, strategy_equity_usd=25.0, max_position_fraction=0.40,
                 stop_loss_pct=0.04, take_profit_pct=0.08, risk_per_trade=0.02,
                 adaptive_risk=False)
    rm = RiskManager(s)
    # Real account equity is $1000, but the budget is $25.
    state = SessionState(start_equity=1000.0, peak_equity=1000.0, current_equity=1000.0)
    signal = Signal(Action.BUY, 60, "setup", price=100.0, atr=2.0, stop_price=96.0)
    decision = rm.evaluate(signal, state, open_exposure_usd=0.0)
    assert decision.approved
    # Hard ceiling: budget ($25) * max_position_fraction (0.40) = $10.
    assert decision.notional_usd <= 25.0 * 0.40 + 1e-6
    assert decision.notional_usd <= 10.0 + 1e-6


def test_bot_exposure_aggregates_every_owned_symbol():
    from unittest.mock import MagicMock

    s = Settings(_env_file=None)
    gateway = MagicMock()
    prices = {"BTC/USD": 80_000.0, "PUMP/USD": 0.005}
    gateway.get_ticker_for.side_effect = lambda symbol: {"last": prices[symbol]}
    engine = TradingEngine(s, gateway=gateway)
    engine._bot_qty = {"BTC/USD": 0.0001, "PUMP/USD": 2_000.0}

    assert engine._bot_open_exposure_usd() == 18.0


def test_bot_exposure_fails_closed_when_a_lot_cannot_be_valued():
    from unittest.mock import MagicMock

    s = Settings(_env_file=None)
    gateway = MagicMock()
    gateway.get_ticker_for.side_effect = RuntimeError("ticker unavailable")
    engine = TradingEngine(s, gateway=gateway)
    engine._bot_qty = {"BTC/USD": 0.0001}

    assert engine._bot_open_exposure_usd() == float("inf")


def test_universe_allowlist_defaults_to_positive_expectancy_coins():
    """Audit #8: the live MR edge is coin-specific, so the default universe
    must be restricted to coins with positive backtested expectancy — not the
    entire Kraken market (which bleeds on XRP/SOL)."""
    s = Settings(_env_file=None)
    assert s.universe_allowlist == ["PUMP/USD", "BTC/USD"]
    # The engine applies the allowlist on top of all_usd discovery.
    candidates = ["PUMP/USD", "BTC/USD", "XRP/USD", "SOL/USD"]
    if s.universe_allowlist:
        allowed = {x.upper() for x in s.universe_allowlist}
        candidates = [c for c in candidates if c.upper() in allowed]
    assert "XRP/USD" not in candidates
    assert "SOL/USD" not in candidates
