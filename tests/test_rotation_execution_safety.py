"""Offline rotation execution regressions; no exchange clients are constructed."""
from types import SimpleNamespace
from unittest.mock import Mock

from dublin_bot.models import Action, Signal
from dublin_bot.strategies.rotation_strategy import RotationStrategy


def settings(**overrides):
    values = dict(symbol="BTC/USD", paper_trading=False, dry_run=False,
                  cooldown_minutes=3, strategy_equity_usd=100,
                  risk_per_trade=.01, min_order_notional_usd=5,
                  universe_allowlist=["XRP/USD"], coin_basket=["XRP/USD"])
    values.update(overrides)
    return SimpleNamespace(**values)


def test_selection_is_not_position_ownership():
    strategy = RotationStrategy(settings())
    buy = Signal(Action.BUY, 80, "candidate", price=2, stop_price=1.9)
    assert strategy.evaluate_all_coins({"XRP/USD": buy}, {}) is buy
    assert strategy.get_position() is None
    assert strategy.get_entry_price() is None
    assert strategy.get_candidate() == "XRP/USD"



def agent_fixture(tmp_path, monkeypatch, **overrides):
    from dublin_bot.agents.trading_agent import TradingAgent
    monkeypatch.chdir(tmp_path)
    config = settings(session_state_path=str(tmp_path / "session.json"), **overrides)
    gateway = Mock()
    gateway.settings = config
    gateway.account_equity.return_value = 100
    risk = Mock()
    risk.evaluate.return_value = SimpleNamespace(approved=True, notional_usd=10)
    paper = Mock()
    paper.holdings.return_value = {}
    agent = TradingAgent(config, gateway=gateway, paper_portfolio=paper, risk_manager=risk)
    return agent, gateway, paper


def test_live_entry_fails_closed_without_reconciled_execution_owner(tmp_path, monkeypatch):
    agent, gateway, paper = agent_fixture(tmp_path, monkeypatch)
    buy = Signal(Action.BUY, 80, "candidate", price=2, stop_price=1.9)
    agent.strategy.evaluate_all_coins({"XRP/USD": buy}, {})
    result = agent._execute_buy(buy, {})
    assert result["executed"] is False
    assert result["symbol"] == "XRP/USD"
    assert "reconciliation" in result["error"].lower()
    gateway.buy_notional.assert_not_called()
    paper.record_trade.assert_not_called()
    assert agent.strategy.get_position() is None



def test_live_exit_never_sweeps_unknown_holdings(tmp_path, monkeypatch):
    agent, gateway, paper = agent_fixture(tmp_path, monkeypatch)
    agent.strategy._position = "XRP/USD"  # legacy in-memory claim is not ownership
    result = agent._execute_sell(Signal(Action.SELL, 90, "exit", price=2), {"XRP/USD": 999})
    assert result["executed"] is False
    assert "ownership" in result["error"].lower()
    gateway.close_position.assert_not_called()
    gateway.cancel_orders.assert_not_called()
    paper.record_trade.assert_not_called()
    assert agent.strategy.get_position() is None  # projection clears unowned legacy claim



def test_wait_scan_does_not_advance_trade_cooldown(tmp_path, monkeypatch):
    agent, gateway, _ = agent_fixture(tmp_path, monkeypatch)
    agent.gateway.get_bars_for = Mock(return_value=None)
    gateway.positions.return_value = []
    agent._cooldown_until = 123
    report = agent.scan_and_decide()
    assert report["executed"] is False
    assert agent._cooldown_until == 123



def test_market_data_scoping_never_mutates_shared_settings():
    from dublin_bot.market_data import MultiSymbolGateway
    config = settings()

    class Gateway:
        def __init__(self):
            self.settings = config
            self._meta = {"preserved": object()}

        def get_bars(self, **kwargs):
            assert config.symbol == "BTC/USD", "shared settings mutated mid-call"
            assert "preserved" in self._meta
            return self.settings.symbol

        def get_ticker(self):
            assert config.symbol == "BTC/USD", "shared settings mutated mid-call"
            return {"last": self.settings.symbol}

    gateway = Gateway()
    adapter = MultiSymbolGateway(gateway, config)
    assert adapter.get_bars_for("XRP/USD") == "XRP/USD"
    assert adapter.get_ticker_for("XRP/USD") == {"last": "XRP/USD"}
    assert config.symbol == "BTC/USD"
    assert "preserved" in gateway._meta



def test_live_account_inventory_is_never_adopted_as_owned(tmp_path, monkeypatch):
    agent, gateway, _ = agent_fixture(tmp_path, monkeypatch)
    gateway.positions.return_value = [{"asset": "XRP/USD", "quantity": 999}]
    assert agent._get_current_holdings() == {}
    assert agent.strategy.get_position() is None
    gateway.positions.assert_not_called()



def test_scan_reports_candidate_without_claiming_position(tmp_path, monkeypatch):
    agent, gateway, _ = agent_fixture(tmp_path, monkeypatch)
    agent.gateway.get_bars_for = Mock(return_value=[0] * 20)
    buy = Signal(Action.BUY, 80, "candidate", price=2, stop_price=1.9)
    agent.strategy.evaluate = Mock(return_value=buy)
    gateway.account_equity.side_effect = RuntimeError("read failed")
    gateway.positions.return_value = {"malformed": True}
    report = agent.scan_and_decide()
    assert report["best_symbol"] == "XRP/USD"
    assert report["current_position"] is None
    assert report["executed"] is False
    assert agent._cooldown_until == 0
    gateway.account_equity.assert_not_called()
    gateway.buy_notional.assert_not_called()



def test_paper_fill_commits_candidate_and_exact_quantity(tmp_path, monkeypatch):
    agent, _, paper = agent_fixture(tmp_path, monkeypatch, paper_trading=True)
    buy = Signal(Action.BUY, 80, "candidate", price=2, stop_price=1.9)
    agent.strategy.evaluate_all_coins({"XRP/USD": buy}, {})
    result = agent._execute_buy(buy, {})
    assert result["executed"] is True
    assert agent.strategy.get_position() == "XRP/USD"
    assert agent.strategy.get_entry_price() == 2
    assert agent.strategy.get_holdings() == {"XRP/USD": 2.5}



def test_paper_execution_uses_real_portfolio_api(tmp_path, monkeypatch):
    from dublin_bot.agents.trading_agent import TradingAgent
    from dublin_bot.paper import PaperPortfolio
    monkeypatch.chdir(tmp_path)
    config = settings(paper_trading=True, session_state_path=str(tmp_path / "session.json"))
    gateway = Mock(settings=config)
    portfolio = PaperPortfolio(tmp_path / "paper.json")
    portfolio.load(100, 100)
    agent = TradingAgent(config, gateway=gateway, paper_portfolio=portfolio, risk_manager=Mock())
    buy = Signal(Action.BUY, 80, "candidate", price=2, stop_price=1.9)
    agent.strategy.evaluate_all_coins({"XRP/USD": buy}, {})
    assert agent._execute_buy(buy, {})["executed"]
    assert agent._get_current_holdings() == {"XRP/USD": 2.5}
    sell = Signal(Action.SELL, 90, "exit", price=2.1)
    assert agent._execute_sell(sell, agent._get_current_holdings())["executed"]
    assert agent.strategy.get_position() is None
    assert agent._get_current_holdings() == {}
    assert agent.strategy.get_last_exit_time("XRP/USD") is not None



def test_confirmed_paper_position_and_exit_cooldowns_survive_restart(tmp_path, monkeypatch):
    agent, _, _ = agent_fixture(tmp_path, monkeypatch, paper_trading=True)
    buy = Signal(Action.BUY, 80, "candidate", price=2, stop_price=1.9)
    agent.strategy.evaluate_all_coins({"XRP/USD": buy}, {})
    agent._execute_buy(buy, {})
    restored = RotationStrategy(agent.settings)
    assert restored.get_position() == "XRP/USD"
    assert restored.get_holdings() == {"XRP/USD": 2.5}
    assert restored.get_entry_price() == 2
    restored.record_exit("XRP/USD")
    after_exit = RotationStrategy(agent.settings)
    assert after_exit.get_position() is None
    assert after_exit.get_last_exit_time("XRP/USD") == restored.get_last_exit_time("XRP/USD")
    assert after_exit.evaluate_all_coins({"XRP/USD": buy}, {}).action == Action.WAIT



def test_trade_cooldown_persists_and_blocks_only_entries(tmp_path, monkeypatch):
    agent, _, _ = agent_fixture(tmp_path, monkeypatch, paper_trading=True)
    buy = Signal(Action.BUY, 80, "candidate", price=2, stop_price=1.9)
    agent.strategy.evaluate_all_coins({"XRP/USD": buy}, {})
    assert agent._execute_buy(buy, {})["executed"]
    assert agent._cooldown_until > 0
    before = agent._cooldown_until
    assert agent._execute_sell(Signal(Action.SELL, 90, "exit", price=2.1), {"XRP/USD": 2.5})["executed"]
    rebuilt, gateway, paper = agent_fixture(tmp_path, monkeypatch, paper_trading=True)
    assert rebuilt._cooldown_until >= before
    rebuilt.strategy.evaluate_all_coins({"BTC/USD": buy}, {})
    assert rebuilt._execute_buy(buy, {})["executed"] is False
    paper.record_trade.assert_not_called()



def test_exit_cannot_clear_a_different_owned_symbol(tmp_path, monkeypatch):
    import pytest
    agent, _, _ = agent_fixture(tmp_path, monkeypatch, paper_trading=True)
    agent.strategy.record_entry("XRP/USD", 2, 2.5)
    with pytest.raises(ValueError):
        agent.strategy.record_exit("BTC/USD")
    assert agent.strategy.get_position() == "XRP/USD"
    assert agent.strategy.get_last_exit_time("BTC/USD") is None



def test_malformed_persisted_quantity_fails_closed(tmp_path, monkeypatch):
    import json
    import pytest
    monkeypatch.chdir(tmp_path)
    config = settings(paper_trading=True, session_state_path=str(tmp_path / "session.json"))
    (tmp_path / "rotation_paper_state.json").write_text(json.dumps({
        "version": 1, "position": "XRP/USD", "quantity": -2,
        "entry_price": 2, "entry_time": 1, "last_exit_time": {},
    }))
    with pytest.raises(ValueError):
        RotationStrategy(config)



def test_explicit_paper_reset_clears_persisted_candidate_and_quantity(tmp_path, monkeypatch):
    agent, _, _ = agent_fixture(tmp_path, monkeypatch, paper_trading=True)
    agent.strategy.record_entry("XRP/USD", 2, 2.5)
    agent.strategy.reset()
    assert RotationStrategy(agent.settings).get_position() is None
    assert agent.strategy.get_candidate() is None
    assert agent.strategy.get_holdings() == {}
