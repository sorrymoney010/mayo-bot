"""Ops safety: single instance, ledger owner, no private calls in paper,
paper defaults, retired strategies, single log write."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from dublin_bot.config import Settings, paper_live_choice_ok
from dublin_bot.errors import PaperPrivateCallBlocked
from dublin_bot.instance import InstanceLock, ledger_owner_ok
from dublin_bot.kraken_gateway import KrakenGateway

REPO = Path(__file__).resolve().parents[1]


def test_paper_defaults():
    s = Settings(_env_file=None)
    assert s.strategy == "regime_trend"
    assert (s.paper_taker_fee_bps, s.paper_maker_fee_bps, s.paper_slippage_bps) == (40.0, 25.0, 10.0)
    assert s.paper_use_ledger_equity is True and s.paper_block_private_api is True
    assert s.safety_locked  # locks unchanged: paper + dry-run + no live by default


@pytest.mark.parametrize("over,ok", [
    ({}, True),
    ({"strategy": "regime"}, True),
    ({"strategy": "momentum"}, False),
    ({"strategy": "breakout"}, False),
    ({"strategy": "regime_trend", "timeframe_minutes": 15}, False),
    ({"meanrev_timeframe_minutes": 15}, False),
    ({"strategy": "momentum", "allow_retired_strategies": True}, True),
])
def test_retired_strategies_not_a_paper_live_choice(over, ok):
    s = Settings(_env_file=None, **{"timeframe_minutes": 60, **over})
    assert paper_live_choice_ok(s)[0] is ok


def test_single_instance_lock(tmp_path):
    a, b = InstanceLock(tmp_path / "x.lock"), InstanceLock(tmp_path / "x.lock")
    assert a.acquire()
    assert a.holder_pid() == os.getpid()
    assert not b.acquire(wait_seconds=0.2, poll=0.05)
    a.release()
    assert b.acquire()
    b.release()


def test_ledger_owner_env():
    assert ledger_owner_ok({"MAYO_LEDGER_OWNER": "1"})
    assert not ledger_owner_ok({})
    assert not ledger_owner_ok({"MAYO_LEDGER_OWNER": "0"})


def test_private_endpoints_blocked_in_paper(settings, fake_session):
    gw = KrakenGateway(settings, session=fake_session, sleep_fn=lambda _s: None)
    for call in (gw.account_equity, lambda: gw.closed_trade_pnl(0)):
        try:
            call()
        except PaperPrivateCallBlocked:
            pass
        except Exception:  # some wrappers swallow/convert errors; the HTTP call is what matters
            pass
    assert not [c for c in fake_session.calls if c["kind"] == "POST"]


def test_paper_cycle_never_touches_private_endpoints(settings, fake_session):
    from tests.test_engine_pipeline import build_engine
    build_engine(settings, fake_session).run_cycle()
    private = [c["endpoint"] for c in fake_session.calls if c["kind"] == "POST"]
    assert private == []


# ── the old main paper loop is retired (archived 2026-11-15) ──
def test_old_paper_loop_is_archived_not_runnable():
    assert not (REPO / "scripts" / "paper_trader_loop.py").exists()
    assert (REPO / "archive" / "2026-11-pre-capit" / "scripts" / "paper_trader_loop.py").exists()
