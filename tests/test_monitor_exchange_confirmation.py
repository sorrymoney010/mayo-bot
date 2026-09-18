"""Offline regressions: a submission is never evidence of a fill."""
import json
import socket
from types import SimpleNamespace

import pytest

from dublin_bot.agents.monitoring_agent import MonitoringAgent


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Network forbidden in monitor tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)


class Gateway:
    def __init__(self, order):
        self.order = order
        self.queries = []

    def query_order(self, order_id):
        self.queries.append(order_id)
        if isinstance(self.order, Exception):
            raise self.order
        return dict(self.order)

    def resolve_symbol(self, symbol):
        assert symbol in {"XXBTZUSD", "XBTUSD", "XBT/USD"}
        return SimpleNamespace(wsname="XBT/USD")


def make_monitor(tmp_path, order):
    agent = SimpleNamespace(
        settings=SimpleNamespace(paper_trading=False, strategy_equity_usd=100),
        gateway=Gateway(order),
        scan_and_decide=lambda: {"executed": False},
    )
    return MonitoringAgent(agent, log_path=str(tmp_path / "monitor.json"))


def submitted():
    return {"order_id": "ORDER-1", "action": "BUY", "symbol": "WRONG/USD", "price": 999, "mode": "live"}


def exchange_order(**overrides):
    return {"status": "closed", "vol_exec": "0.2", "vol": "0.5", "price": "100", "cost": "20", "fee": "0.05", "descr": {"pair": "XXBTZUSD", "type": "buy"}, **overrides}


@pytest.mark.parametrize("order", [RuntimeError("offline"), {}, exchange_order(status="open", vol_exec="0"), exchange_order(status="closed", vol_exec="0"), exchange_order(status="mystery")])
def test_submission_is_not_confirmation(tmp_path, order):
    monitor = make_monitor(tmp_path, order)
    assert monitor._confirm_trade(submitted(), {}) is None
    assert monitor.trading_agent.gateway.queries == ["ORDER-1"]


@pytest.mark.parametrize("status", ["open", "closed", "canceled", "expired"])
def test_exchange_execution_fields_not_submission_estimates(tmp_path, status):
    monitor = make_monitor(tmp_path, exchange_order(status=status))
    # TradingAgent uses MultiSymbolGateway, not the raw KrakenGateway.
    monitor.trading_agent.gateway = SimpleNamespace(gateway=monitor.trading_agent.gateway)
    trade = monitor._confirm_trade(submitted(), {})
    assert trade is not None
    assert trade["symbol"] == "BTC/USD"
    assert trade["action"] == "BUY"
    assert trade["quantity"] == trade["fill_quantity"] == 0.2
    assert trade["price"] == trade["fill_price"] == 100
    assert trade["fee"] == 0.05
    assert trade["exchange_status"] == status
    assert trade["pnl"] is None


def test_pending_requery_partial_updates_deduplicate_without_extra_scans(tmp_path):
    monitor = make_monitor(tmp_path, exchange_order(status="open", vol_exec="0"))
    calls = []
    def scan():
        calls.append(1)
        return {"executed": True, "order_result": submitted()}
    monitor.trading_agent.scan_and_decide = scan
    report = monitor.check_and_report()
    assert not monitor.get_recent_trades()
    assert len(report["pending_trades"]) == 1
    monitor.trading_agent.gateway.order = exchange_order(status="open")
    monitor.check_and_report()
    assert len(monitor.get_recent_trades()) == 1
    assert len(monitor._pending_trades) == 1
    monitor.check_and_report()
    assert len(monitor.get_recent_trades()) == 1
    monitor.trading_agent.gateway.order = exchange_order(status="canceled", vol_exec="0.3", cost="30")
    report = monitor.check_and_report()
    assert monitor.get_recent_trades()[0]["quantity"] == 0.3
    assert len(monitor.get_recent_trades()) == 1
    assert not report["pending_trades"]
    assert len(calls) == 4
    assert len(monitor.trading_agent.gateway.queries) == 4


def test_reload_quarantines_unverified_live_history_and_persists_pending(tmp_path):
    historical = {**submitted(), "status": "SUBMITTED", "pnl": 42}
    path = tmp_path / "monitor.json"
    path.write_text(json.dumps({"confirmed_trades": [historical, historical], "wins": [historical]}))
    monitor = make_monitor(tmp_path, RuntimeError("offline"))
    assert monitor.get_recent_trades() == []
    assert monitor._wins == []
    assert len(monitor._pending_trades) == 1
    assert monitor.trading_agent.gateway.queries == []  # load is offline
    monitor._save_state()
    monitor = make_monitor(tmp_path, exchange_order())
    assert len(monitor._pending_trades) == 1
    report = monitor.check_and_report()
    assert len(monitor.get_recent_trades()) == 1
    assert not report["pending_trades"]
    assert monitor.trading_agent.gateway.queries == ["ORDER-1"]





def test_unknown_pnl_is_unavailable_and_excluded_from_denominator(tmp_path):
    monitor = make_monitor(tmp_path, exchange_order())
    monitor._record_confirmation(monitor._confirm_trade(submitted(), {}))
    stats = monitor.get_stats()
    assert stats["total_pnl"] is None
    assert stats["win_rate"] is None
    assert stats["average_win"] is None
    assert stats["average_loss"] is None
    monitor._save_state()
    assert json.loads(monitor.log_path.read_text())["win_rate"] is None
    monitor._record_confirmation({"mode": "paper", "timestamp": "a", "pnl": 10})
    monitor._record_confirmation({"mode": "paper", "timestamp": "b", "pnl": -2})
    monitor._record_confirmation({"mode": "paper", "timestamp": "c", "pnl": 0})
    stats = monitor.get_stats()
    assert stats["total_pnl"] == 8
    assert stats["win_rate"] == pytest.approx(1 / 3)
    assert stats["pnl_known_trades"] == 3
    assert stats["pnl_unknown_trades"] == 1


def test_checks_serialize_trading_invocations(tmp_path):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    monitor = make_monitor(tmp_path, exchange_order())
    entered = threading.Event()
    release = threading.Event()
    active = 0
    maximum = 0
    calls = 0
    guard = threading.Lock()
    def scan():
        nonlocal active, maximum, calls
        with guard:
            active += 1
            calls += 1
            maximum = max(maximum, active)
        entered.set()
        release.wait(2)
        with guard:
            active -= 1
        return {"executed": False}
    monitor.trading_agent.scan_and_decide = scan
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(monitor.check_and_report)
        assert entered.wait(1)
        second = pool.submit(monitor.force_check)
        time.sleep(0.05)
        release.set()
        first.result(timeout=2)
        second.result(timeout=2)
    assert maximum == 1
    assert calls == 2


@pytest.mark.parametrize("pnl", [None, 0, 2, -2])
def test_coordinator_formats_unknown_performance_without_even(tmp_path, capsys, pnl):
    from dublin_bot.coordinator import TradingSystem
    system = TradingSystem.__new__(TradingSystem)
    monitor = make_monitor(tmp_path, exchange_order())
    stats = monitor.get_stats()
    stats["total_confirmed_trades"] = 5
    report = {"last_confirmed_trade": {"action": "BUY", "symbol": "BTC/USD", "pnl": pnl, "return_pct": None}, "stats": stats}
    system._print_report(report)
    system._print_summary(report)
    output = capsys.readouterr().out
    assert "unavailable" in output.lower()
    if pnl is None:
        assert "EVEN" not in output
        assert "WIN" not in output
        assert "LOSS" not in output
    elif pnl == 0:
        assert "EVEN" in output


def test_failed_scan_does_not_prevent_readonly_pending_verification(tmp_path):
    monitor = make_monitor(tmp_path, exchange_order())
    monitor._pending_trades["ORDER-1"] = submitted()
    def failed_scan():
        raise RuntimeError("scan failed")
    monitor.trading_agent.scan_and_decide = failed_scan
    report = monitor.check_and_report()
    assert len(monitor.get_recent_trades()) == 1
    assert any("scan failed" in item for item in report["anomalies"])


@pytest.mark.parametrize("overrides", [{"vol_exec": None}, {"vol_exec": "NaN"}, {"vol_exec": "-1"}, {"price": "NaN"}, {"price": "0"}, {"fee": "NaN"}])
def test_invalid_execution_evidence_stays_pending(tmp_path, overrides):
    monitor = make_monitor(tmp_path, exchange_order(**overrides))
    assert monitor._confirm_trade(submitted(), {}) is None
    assert "ORDER-1" in monitor._pending_trades


def test_missing_volume_never_becomes_terminal_zero_fill(tmp_path):
    order = exchange_order()
    order.pop("vol_exec")
    monitor = make_monitor(tmp_path, order)
    assert monitor._confirm_trade(submitted(), {}) is None
    assert "ORDER-1" in monitor._pending_trades


@pytest.mark.parametrize("failure", ["query", "scan", "metadata"])
def test_error_report_is_dispatched_once_per_check(tmp_path, failure):
    monitor = make_monitor(tmp_path, RuntimeError("offline") if failure == "query" else exchange_order())
    if failure != "scan":
        monitor._pending_trades["ORDER-1"] = submitted()
    def unavailable(*args):
        raise RuntimeError("unavailable")
    if failure == "scan":
        monitor.trading_agent.scan_and_decide = unavailable
    elif failure == "metadata":
        monitor.trading_agent.gateway.resolve_symbol = unavailable
    received = []
    monitor.report_callback = received.append
    for _ in range(2):
        report = monitor.check_and_report()
        assert report["anomalies"]
        assert received[-1:] == [report]
    assert len(received) == 2


@pytest.mark.parametrize("printer", ["_print_report", "_print_summary"])
def test_coordinator_prints_errors_and_blocked_reasons(tmp_path, capsys, printer):
    from dublin_bot.coordinator import TradingSystem
    system = TradingSystem.__new__(TradingSystem)
    report = {
        "anomalies": ["Exchange verification unavailable", "Exchange verification unavailable"],
        "agent_decision": {"executed": False, "order_result": {
            "executed": False,
            "error": "Live rotation entry blocked: authoritative balance/risk evidence required",
        }},
    }
    getattr(system, printer)(report)
    output = capsys.readouterr().out
    assert output.count("Exchange verification unavailable") == 1
    assert output.count("Live rotation entry blocked: authoritative balance/risk evidence required") == 1


@pytest.mark.parametrize("printer", ["_print_report", "_print_summary"])
def test_coordinator_surfaces_blocked_decision_and_risk_reason(capsys, printer):
    from dublin_bot.coordinator import TradingSystem
    system = TradingSystem.__new__(TradingSystem)
    report = {"agent_decision": {
        "executed": False, "best_action": "HALT", "best_reason": "Cooldown active",
        "risk": {"allowed": False, "reason": "Daily loss limit reached"},
        "decisions": [{"symbol": "BTC/USD", "signal": "ERROR", "error": "Market evidence unavailable"}],
    }}
    getattr(system, printer)(report)
    output = capsys.readouterr().out
    for reason in ["Cooldown active", "Daily loss limit reached", "Market evidence unavailable"]:
        assert output.count(reason) == 1


def test_coordinator_loop_dispatches_and_prints_error_once(tmp_path, capsys, monkeypatch):
    from dublin_bot.coordinator import TradingSystem
    import dublin_bot.coordinator as coordinator
    system = TradingSystem.__new__(TradingSystem)
    system._report_callbacks = []
    system.check_interval = 0
    system._running = True
    system.monitoring_agent = make_monitor(tmp_path, RuntimeError("offline"))
    system.monitoring_agent._pending_trades["ORDER-1"] = submitted()
    system.monitoring_agent.report_callback = system._on_report
    received = []
    system.add_report_callback(received.append)
    monkeypatch.setattr(coordinator.time, "sleep", lambda _: setattr(system, "_running", False))
    system._run_loop()
    assert len(received) == 1
    assert capsys.readouterr().out.count("Exchange verification unavailable") == 1


@pytest.mark.parametrize("target", ["monitor_report", "monitor_trade", "coordinator"])
def test_callback_failure_is_logged_without_stopping_delivery(tmp_path, caplog, target):
    from dublin_bot.coordinator import TradingSystem
    monitor = make_monitor(tmp_path, exchange_order())
    received = []
    def broken(report):
        received.append(report)
        raise RuntimeError("private callback detail")
    if target == "coordinator":
        system = TradingSystem.__new__(TradingSystem)
        delivered = []
        system._report_callbacks = [broken, delivered.append]
        system._dispatch_report({"anomalies": ["scan unavailable"]})
        assert len(delivered) == 1
    else:
        if target == "monitor_trade":
            monitor._pending_trades["ORDER-1"] = submitted()
        monitor.report_callback = broken
        report = monitor.check_and_report()
        assert received[-1] is report
        assert len(received) == (2 if target == "monitor_trade" else 1)
    assert "callback failed (RuntimeError)" in caplog.text
    assert "private callback detail" not in caplog.text


@pytest.mark.parametrize("previous_seconds,expected", [(None, False), (10, False), (1, True)])
def test_rapid_detection_uses_previous_distinct_fill(tmp_path, previous_seconds, expected):
    from datetime import datetime, timezone, timedelta
    monitor = make_monitor(tmp_path, exchange_order())
    if previous_seconds is not None:
        previous = {"order_id": "OLDER", "timestamp": (datetime.now(timezone.utc) - timedelta(seconds=previous_seconds)).isoformat(), "mode": "live"}
        monitor._record_confirmation(previous)
    monitor.trading_agent.scan_and_decide = lambda: {"executed": True, "order_result": submitted()}
    report = monitor.check_and_report()
    assert any("Rapid trading" in a for a in report["anomalies"]) is expected
    # Re-observing the same execution (or a cumulative partial update) is not a new order.
    monitor.trading_agent.gateway.order = exchange_order(vol_exec="0.3")
    assert not any("Rapid trading" in a for a in monitor.check_and_report()["anomalies"])


@pytest.mark.parametrize("failure", ["serialize", "replace"])
def test_atomic_save_preserves_pending_recovery_on_failure(tmp_path, monkeypatch, failure):
    import dublin_bot.agents.monitoring_agent as module
    monitor = make_monitor(tmp_path, exchange_order())
    monitor._pending_trades["ORDER-1"] = submitted()
    monitor._save_state()
    original = monitor.log_path.read_bytes()
    received = []
    monitor.report_callback = received.append
    with monkeypatch.context() as patcher:
        if failure == "serialize":
            def broken_dump(data, stream, **kwargs):
                stream.write('{"incomplete":')
                raise OSError("disk full")
            patcher.setattr(module.json, "dump", broken_dump)
        else:
            import os
            def broken_replace(*args):
                raise OSError("replace failed")
            patcher.setattr(os, "replace", broken_replace)
        report = monitor.check_and_report()
        assert any("persist" in issue.lower() for issue in report["anomalies"])
        assert received[-1] is report
        assert monitor.log_path.read_bytes() == original
        assert list(tmp_path.iterdir()) == [monitor.log_path]
        restored = make_monitor(tmp_path, exchange_order())
        assert "ORDER-1" in restored._pending_trades
    # Confirmed in-memory evidence is retained and can be saved on retry.
    assert monitor.get_stats()["total_confirmed_trades"] == 1
    monitor._save_state()
    restored = make_monitor(tmp_path, exchange_order())
    assert restored.get_stats()["total_confirmed_trades"] == 1
    assert not restored._pending_trades


def test_paper_verification_error_still_delivers_full_report(tmp_path):
    monitor = make_monitor(tmp_path, RuntimeError("must not query"))
    monitor.trading_agent.settings.paper_trading = True
    monitor.trading_agent.scan_and_decide = lambda: {"executed": True, "order_result": {"mode": "paper"}}
    def unreadable():
        raise OSError("ledger unavailable")
    monitor.trading_agent._load_paper_trades = unreadable
    received = []
    monitor.report_callback = received.append
    report = monitor.check_and_report()
    assert received == [report]
    assert any("verification" in issue.lower() for issue in report["anomalies"])
    assert report["stats"]["total_confirmed_trades"] == 0


def test_query_failure_is_reported_and_retryable(tmp_path):
    monitor = make_monitor(tmp_path, RuntimeError("query unavailable"))
    monitor._pending_trades["ORDER-1"] = submitted()
    report = monitor.check_and_report()
    assert any("ORDER-1" in item for item in report["anomalies"])
    assert report["pending_trades"]
    assert not monitor.get_recent_trades()


def test_symbol_metadata_failure_remains_pending(tmp_path):
    monitor = make_monitor(tmp_path, exchange_order())
    def unavailable(symbol):
        raise RuntimeError("metadata unavailable")
    monitor.trading_agent.gateway.resolve_symbol = unavailable
    assert monitor._confirm_trade(submitted(), {}) is None
    assert monitor._pending_trades


def test_real_kraken_gateway_query_shape_and_symbol_metadata(tmp_path, gateway, fake_session):
    fake_session.routes["QueryOrders"] = {"error": [], "result": {"ORDER-1": exchange_order()}}
    monitor = make_monitor(tmp_path, {})
    monitor.trading_agent.gateway = SimpleNamespace(gateway=gateway)
    trade = monitor._confirm_trade(submitted(), {})
    assert trade["symbol"] == "BTC/USD"
    assert trade["price"] == 100
    assert trade["fee"] == 0.05
    assert {call["endpoint"] for call in fake_session.calls} <= {"QueryOrders", "AssetPairs"}
    assert len(fake_session.endpoint_calls("QueryOrders")) == 1


@pytest.mark.parametrize("status", ["closed", "canceled", "expired"])
def test_terminal_zero_execution_is_not_a_trade(tmp_path, status):
    monitor = make_monitor(tmp_path, exchange_order(status=status, vol_exec="0"))
    assert monitor._confirm_trade(submitted(), {}) is None
    assert not monitor._pending_trades
    assert not monitor.get_recent_trades()


def test_paper_confirmation_stays_offline_and_deduplicates(tmp_path):
    monitor = make_monitor(tmp_path, RuntimeError("must not query"))
    monitor.trading_agent.settings.paper_trading = True
    paper = {"timestamp": "2026-01-01T00:00:00+00:00", "action": "SELL", "symbol": "BTC/USD", "price": 100, "quantity": 0.2, "entry_price": 90, "mode": "paper", "status": "FILLED"}
    monitor.trading_agent._load_paper_trades = lambda: [paper]
    monitor.trading_agent.scan_and_decide = lambda: {"executed": True, "order_result": paper}
    monitor.check_and_report()
    monitor.check_and_report()
    assert monitor.get_stats()["total_confirmed_trades"] == 1
    assert monitor.get_stats()["total_pnl"] == 2
    assert monitor.trading_agent.gateway.queries == []
