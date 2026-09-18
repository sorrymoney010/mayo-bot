"""Offline restart regressions with real portfolios, never a broker."""
import json
import pytest
from types import SimpleNamespace
from unittest.mock import Mock
from dublin_bot.agents.trading_agent import TradingAgent
from dublin_bot.models import Action, Signal


def config(tmp_path):
    return SimpleNamespace(symbol="BTC/USD", paper_trading=True, dry_run=False,
        cooldown_minutes=3, strategy_equity_usd=100, risk_per_trade=.01,
        min_order_notional_usd=5, session_state_path=str(tmp_path / "session.json"),
        paper_portfolio_path=str(tmp_path / "portfolio.json"))


def build(s):
    return TradingAgent(s, gateway=Mock(settings=s), risk_manager=Mock())


def test_real_agent_restart_restores_portfolio_and_can_sell(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = config(tmp_path)
    agent = build(s)
    buy = Signal(Action.BUY, 80, "buy", price=2, stop_price=1.9)
    agent.strategy.evaluate_all_coins({"XRP/USD": buy}, {})
    assert agent._execute_buy(buy, {})["executed"]
    restarted = build(s)
    assert restarted._get_current_holdings() == {"XRP/USD": 2.5}
    assert restarted.paper_portfolio.snapshot().cash == 95
    assert restarted._execute_sell(Signal(Action.SELL, 90, "exit", price=2.1),
        restarted._get_current_holdings())["executed"]
    assert build(s)._get_current_holdings() == {}



@pytest.mark.parametrize("damage", ["missing", "corrupt", "quantity", "price"])
def test_restart_inconsistent_portfolio_fails_closed(tmp_path, monkeypatch, damage):
    monkeypatch.chdir(tmp_path)
    s = config(tmp_path)
    agent = build(s)
    buy = Signal(Action.BUY, 80, "buy", price=2, stop_price=1.9)
    agent.strategy.evaluate_all_coins({"XRP/USD": buy}, {})
    agent._execute_buy(buy, {})
    path = agent.paper_portfolio.path
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_text("not json")
    else:
        data = json.loads(path.read_text())
        data["positions"][0]["quantity" if damage == "quantity" else "entry_price"] = 99
        path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="paper.*reconciliation"):
        build(s)



def test_corrupt_flat_portfolio_cannot_reset_cash_to_budget(tmp_path):
    s = config(tmp_path)
    from pathlib import Path
    Path(s.paper_portfolio_path).write_text("broken")
    with pytest.raises(ValueError, match="paper.*reconciliation"):
        build(s)


def test_injected_unloaded_real_portfolio_is_restored(tmp_path):
    from dublin_bot.paper import PaperPortfolio
    s = config(tmp_path)
    a = build(s)
    buy = Signal(Action.BUY, 80, "buy", price=2, stop_price=1.9)
    a.strategy.evaluate_all_coins({"XRP/USD": buy}, {})
    a._execute_buy(buy, {})
    rebuilt = TradingAgent(s, gateway=Mock(settings=s), risk_manager=Mock(),
        paper_portfolio=PaperPortfolio(s.paper_portfolio_path))
    assert rebuilt._get_current_holdings() == {"XRP/USD": 2.5}



def test_loader_cannot_swallow_bad_position_metadata_and_reset_cash(tmp_path):
    from pathlib import Path
    s = config(tmp_path)
    Path(s.paper_portfolio_path).write_text(json.dumps({
        "equity": 10, "cash": 10,
        "positions": [{"symbol": "XRP/USD", "quantity": 2, "entry_price": 2,
                       "fees_paid": 0, "trades": "broken"}],
    }))
    with pytest.raises(ValueError, match="paper.*reconciliation"):
        build(s)
