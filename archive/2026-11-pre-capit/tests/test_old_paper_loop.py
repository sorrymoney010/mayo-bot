"""Archived from tests/test_ops_safety.py: tests of the retired scripts/paper_trader_loop.py
and scripts/run_paper_mac.sh (both now under archive/2026-11-pre-capit/scripts/)."""
import shutil
import subprocess
import sys
from pathlib import Path

from dublin_bot.instance import InstanceLock

REPO = Path(__file__).resolve().parents[3]


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
                 "export PAPER_TAKER_FEE_BPS=40", "export PAPER_MAKER_FEE_BPS=25",
                 "export PAPER_SLIPPAGE_BPS=10"):
        assert line in sh
    assert "source .env" not in sh and ". .env" not in sh
