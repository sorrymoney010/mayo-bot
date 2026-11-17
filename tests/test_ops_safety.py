"""Ops safety: single instance, ledger owner, no private calls in paper,
paper defaults, retired strategies, single log write."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
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
    assert (s.paper_taker_fee_bps, s.paper_maker_fee_bps, s.paper_slippage_bps) == (80.0, 40.0, 10.0)
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


# ── the loop script itself (run from a scratch copy, never the repo logs) ──
def _run_loop(tmp_path, env_extra):
    (tmp_path / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "paper_trader_loop.py", tmp_path / "scripts")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONPATH": str(REPO / "src"),
           "PAPER_LOCK_WAIT_SECONDS": "0", **env_extra}
    p = subprocess.run([sys.executable, str(tmp_path / "scripts" / "paper_trader_loop.py")],
                       env=env, capture_output=True, text=True, timeout=60)
    log = (tmp_path / "logs" / "paper_trader.log").read_text(encoding="utf-8").splitlines()
    return p, log


def test_loop_refuses_without_ledger_owner_and_logs_once(tmp_path):
    p, log = _run_loop(tmp_path, {})
    assert p.returncode == 3
    assert len(log) == 1 and "REFUSE: not the ledger owner" in log[0]
    assert p.stdout == ""  # no duplicate echo to (redirected) stdout


def test_loop_refuses_second_instance(tmp_path):
    held = InstanceLock(tmp_path / "logs" / "paper_trader.lock")
    assert held.acquire()
    try:
        p, log = _run_loop(tmp_path, {"MAYO_LEDGER_OWNER": "1"})
    finally:
        held.release()
    assert p.returncode == 3 and "another paper loop holds" in log[-1]


def test_loop_aborts_on_retired_strategy(tmp_path):
    p, log = _run_loop(tmp_path, {"MAYO_LEDGER_OWNER": "1", "STRATEGY": "momentum"})
    assert p.returncode == 2 and "retired" in log[-1]


def test_mac_wrapper_keeps_locks_and_sets_owner():
    sh = (REPO / "scripts" / "run_paper_mac.sh").read_text()
    for line in ("export PAPER_TRADING=true", "export DRY_RUN=true", "export ALLOW_LIVE_TRADING=false",
                 "export MAYO_LEDGER_OWNER=1", "export PAPER_USE_LEDGER_EQUITY=true",
                 "export PAPER_TAKER_FEE_BPS=80", "export PAPER_MAKER_FEE_BPS=40",
                 "export PAPER_SLIPPAGE_BPS=10"):
        assert line in sh
    assert "source .env" not in sh and ". .env" not in sh
