"""Paper lot ledger + protective exits so paper trading can close trades."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from dublin_bot.audit import AuditLog
from dublin_bot.engine import TradingEngine
from dublin_bot.kraken_gateway import KrakenGateway
from dublin_bot.models import Action, Signal
from dublin_bot.paper import PaperPortfolio
from dublin_bot.ratelimit import KrakenRateLimiter, RateLimitTier


def _build_engine(settings, fake_session):
    clock = {"t": 0.0}

    def sleep(seconds):
        clock["t"] += seconds

    audit = AuditLog(settings.audit_log_path)
    limiter = KrakenRateLimiter(
        RateLimitTier.pro(), time_fn=lambda: clock["t"], sleep_fn=sleep
    )
    gateway = KrakenGateway(
        settings, session=fake_session, rate_limiter=limiter,
        audit=audit, sleep_fn=lambda _s: None,
    )
    return TradingEngine(settings, gateway=gateway, audit=audit)


def _bars_at(price: float) -> pd.DataFrame:
    return pd.DataFrame({"close": [price * 0.99, price]})


def test_paper_record_buy_persists_and_reloads(settings_factory, fake_session):
    settings = settings_factory(paper_trading=True, dry_run=True)
    engine = _build_engine(settings, fake_session)
    engine._record_bot_buy("BTC/USD", 0.01)

    paper_path = Path("logs/paper_bot_positions.json")
    live_path = Path("logs/bot_positions.json")
    assert paper_path.exists()
    assert not live_path.exists()
    assert json.loads(paper_path.read_text())["BTC/USD"] == pytest.approx(0.01)

    engine2 = _build_engine(settings, fake_session)
    assert engine2._bot_qty_path == paper_path
    assert engine2._bot_qty.get("BTC/USD") == pytest.approx(0.01)


def test_live_mode_uses_bot_positions_not_paper(settings_factory, fake_session):
    settings = settings_factory(
        paper_trading=False,
        dry_run=False,
        allow_live_trading=True,
        live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK",
    )
    engine = _build_engine(settings, fake_session)
    engine._record_bot_buy("BTC/USD", 0.02)

    paper_path = Path("logs/paper_bot_positions.json")
    live_path = Path("logs/bot_positions.json")
    assert live_path.exists()
    assert not paper_path.exists()
    assert json.loads(live_path.read_text())["BTC/USD"] == pytest.approx(0.02)
    assert engine._bot_qty_path == live_path


def test_hydrate_from_paper_portfolio_when_ledger_empty(settings_factory, fake_session):
    settings = settings_factory(paper_trading=True, dry_run=True)
    portfolio = PaperPortfolio(Path("logs/paper_portfolio.json"))
    portfolio.load(equity=1000.0, cash=1000.0)
    portfolio.record_buy("BTC/USD", 0.05, fill_price=50_000.0, fee=1.0, when="2026-09-19T00:00:00+00:00")
    portfolio.record_buy("PUMP/USD", 1000.0, fill_price=0.01, fee=0.5, when="2026-09-19T00:00:00+00:00")

    assert not Path("logs/paper_bot_positions.json").exists()
    engine = _build_engine(settings, fake_session)
    assert engine._bot_qty.get("BTC/USD") == pytest.approx(0.05)
    assert engine._bot_qty.get("PUMP/USD") == pytest.approx(1000.0)
    assert Path("logs/paper_bot_positions.json").exists()


def test_paper_protective_stop_and_take_profit(settings_factory, fake_session):
    settings = settings_factory(
        paper_trading=True,
        dry_run=True,
        stop_loss_pct=0.04,
        take_profit_pct=0.08,
        symbol="BTC/USD",
    )
    engine = _build_engine(settings, fake_session)
    engine._bot_qty["BTC/USD"] = 0.01
    # Seed paper portfolio entry at 100 so stop/TP math is obvious.
    engine.paper_portfolio.load(equity=1000.0, cash=900.0)
    engine.paper_portfolio.record_buy(
        "BTC/USD", 0.01, fill_price=100.0, fee=0.0, when="2026-09-19T00:00:00+00:00"
    )

    wait = Signal(Action.WAIT, 0, "no signal", 100.0)
    stop_hit = engine._apply_paper_protective_exit(wait, _bars_at(95.0))  # -5% < -4%
    assert stop_hit.action is Action.SELL
    assert "Paper stop loss hit" in stop_hit.reason

    tp_hit = engine._apply_paper_protective_exit(wait, _bars_at(109.0))  # +9% > +8%
    assert tp_hit.action is Action.SELL
    assert "Paper take profit hit" in tp_hit.reason

    hold = engine._apply_paper_protective_exit(wait, _bars_at(101.0))
    assert hold.action is Action.WAIT


def test_select_symbol_switches_to_held_lot(settings_factory, fake_session):
    settings = settings_factory(
        paper_trading=True,
        dry_run=True,
        symbol="ETH/USD",
        rotate_positions=False,
    )
    engine = _build_engine(settings, fake_session)
    engine._bot_qty = {"BTC/USD": 0.01}
    engine._select_symbol()
    assert engine.settings.symbol == "BTC/USD"
