"""Tests for the self-learning agent and autonomous (no manual coin section) selection."""

from __future__ import annotations

from unittest.mock import MagicMock

from dublin_bot.config import Settings
from dublin_bot.learner import LearningAgent
from dublin_bot.engine import TradingEngine
from dublin_bot.models import Action, Signal


def _fake_bars(rsi=70.0, price=1.0):
    import pandas as pd
    idx = pd.date_range("2026-01-01", periods=120, freq="15min", tz="UTC")
    slow = price * 1.2  # ensure price < slow EMA so MR entry gate can fire
    return pd.DataFrame(
        {"close": [price] * 120, "open": [price] * 120, "high": [price] * 120,
         "low": [price] * 120, "volume": [1.0] * 120, "vwap": [price] * 120,
         "rsi": [rsi] * 120, "ema_slow": [slow] * 120, "ema_fast": [price] * 120,
         "ema_regime": [price] * 120},
        index=idx,
    )


# ── learner ────────────────────────────────────────────────
def test_learner_biases_toward_positive_expectancy(tmp_path):
    la = LearningAgent(tmp_path / "learner.json", min_trades=2, enabled=True)
    # PUMP wins consistently -> should be favored (>1.0)
    for _ in range(4):
        la.record_trade("PUMP/USD", 0.5, "sideways")
    # SCAM bleeds -> should be avoided (<1.0)
    for _ in range(4):
        la.record_trade("SCAM/USD", -0.5, "sideways")
    assert la.bias("PUMP/USD") > 1.0
    assert la.bias("SCAM/USD") < 1.0


def test_learner_ignores_low_sample(tmp_path):
    la = LearningAgent(tmp_path / "learner.json", min_trades=3, enabled=True)
    la.record_trade("XRP/USD", 5.0, "sideways")  # 1 trade only
    assert la.bias("XRP/USD") == 1.0  # not enough evidence to bias


def test_learner_persists_and_reloads(tmp_path):
    p = tmp_path / "learner.json"
    la = LearningAgent(p, min_trades=2, enabled=True)
    for _ in range(3):
        la.record_trade("PUMP/USD", 0.4, "sideways")
    la2 = LearningAgent(p, min_trades=2, enabled=True)
    assert la2.coins["PUMP/USD"].trades == 3
    assert la2.bias("PUMP/USD") > 1.0


# ── autonomous selection (no manual pinning) ───────────────
def make_engine(universe="all_usd", symbols=None):
    s = Settings(_env_file=None, universe_mode=universe, symbol="XRP/USD",
                 learner_enabled=True, strategy="mean_reversion",
                 sentiment_enabled=False)
    gw = MagicMock()
    gw.account_equity.return_value = 20.0
    gw.has_position.return_value = False
    gw.market_quality.return_value = {"bid": 1.0, "ask": 1.0, "recent_dollar_volume": 1e6}
    gw.get_ticker_for.return_value = {"last": 1.0, "bid": 1.0, "ask": 1.0,
                                      "volume_24h": 1e6, "vwap_24h": 1.0, "trades_24h": 100}
    gw._can_size = lambda *a, **k: True
    # size_buy used by _can_size; we bypass via _can_size override above.
    gw.size_buy.return_value = MagicMock(price=1.0, volume=1.0, notional=1.0)
    gw.buy_notional.return_value = "ORDER-123"
    gw.close_position.return_value = "ORDER-456"
    gw.get_ticker.return_value = {"last": 1.0, "bid": 1.0, "ask": 1.0, "vwap": 1.0}
    # list_usd_pairs: return the candidate universe.
    pairs = []
    from decimal import Decimal
    from dublin_bot.kraken_gateway import SymbolMeta
    for sym in (symbols or ["XRP/USD", "PUMP/USD", "ADA/USD"]):
        pairs.append(SymbolMeta(key=sym.replace("/", ""), altname=sym, wsname=sym,
                                base=sym.split("/")[0], quote="USD", lot_decimals=8,
                                pair_decimals=5, order_min=Decimal("0"), cost_min=Decimal("0"), status="online"))
    gw.list_usd_pairs.return_value = pairs

    eng = TradingEngine(s, gateway=gw)

    # The selector ranks coins by the strategy's live signal. We stub the
    # strategy so PUMP reports a real MR BUY (score 60) and other coins WAIT
    # (score 10). This isolates the *selection* behaviour (auto-pick best setup)
    # from indicator math, which is covered by the strategy's own tests.
    def fake_evaluate(bars, in_position=False):
        sym = eng.settings.symbol
        if sym == "PUMP/USD" and not in_position:
            return Signal(Action.BUY, 60, "oversold reversion", 1.0, 1.0, 0.8)
        return Signal(Action.WAIT, 10, "no setup", 1.0, 1.0)
    eng.strategy.evaluate = fake_evaluate
    return eng


def test_autonomous_selection_picks_mr_setup_without_manual_pin():
    eng = make_engine()
    eng.run_cycle()
    # The engine should have autonomously switched to PUMP/USD (the only coin
    # with a live mean-reversion setup) — no coin section / pin involved.
    assert eng.settings.symbol == "PUMP/USD", f"expected PUMP/USD, got {eng.settings.symbol}"


def test_autonomous_selection_stays_when_holding():
    eng = make_engine()
    # Disable rotation so this test isolates the churn-guard alone: with a
    # bot-owned lot open and no stronger setup to rotate into, the engine must
    # NOT abandon the position mid-trade. (Rotation itself is covered live.)
    eng.settings.rotate_positions = False
    # Simulate a BOT-OWNED open lot on the current symbol (the new position
    # model). Raw gateway.has_position() is no longer what gates churn — only
    # lots the bot actually acquired count, so external/pre-existing balances
    # can't freeze the engine into exit-only mode.
    eng._bot_qty[eng.settings.symbol] = 1.0
    eng.gateway.has_position.return_value = True  # belt-and-suspenders for SELL branch
    before = eng.settings.symbol
    eng.run_cycle()
    # Must not churn away from the open bot-owned position.
    assert eng.settings.symbol == before


def test_rotation_exits_weak_coin_for_stronger_setup():
    """When holding a bot-owned lot but another allowed coin has a clearly
    stronger momentum setup, the engine rotates: this cycle flags the exit and
    points at the stronger coin (the next cycle enters it)."""
    eng = make_engine()
    # Holding XRP/USD (bot-owned). PUMP/USD shows a strong setup; XRP does not.
    eng.settings.symbol = "XRP/USD"
    eng._bot_qty["XRP/USD"] = 1.0
    eng.gateway.has_position.return_value = True

    def fake_evaluate(bars, in_position=False):
        sym = eng.settings.symbol
        if sym == "PUMP/USD" and not in_position:
            return Signal(Action.BUY, 90, "momentum confirmed", 1.0, 1.0)
        if sym == "XRP/USD" and in_position:
            return Signal(Action.WAIT, 30, "holding", 1.0, 1.0)
        return Signal(Action.WAIT, 10, "no setup", 1.0, 1.0)

    eng.strategy.evaluate = fake_evaluate
    before = eng.settings.symbol
    eng.run_cycle()
    # Rotation detected: symbol repointed to the stronger coin for next cycle.
    assert eng.settings.symbol == "PUMP/USD", f"expected PUMP/USD, got {eng.settings.symbol}"
    assert before == "XRP/USD"  # didn't churn the hold symbol mid-cycle incorrectly


# ── gateway universe (root-cause regression for "not trading at all") ──
def test_list_usd_pairs_matches_kraken_zusd_quote():
    """Kraken reports the USD quote as 'ZUSD'; list_usd_pairs must still find it.

    Regression: an earlier filter checked quote == 'USD' and returned 0 pairs,
    which emptied the autonomous universe and stopped all MR trading.
    """
    from decimal import Decimal
    import time
    from dublin_bot.kraken_gateway import KrakenGateway, SymbolMeta

    gw = KrakenGateway.__new__(KrakenGateway)
    gw._meta = {
        "XRPUSD": SymbolMeta(key="XRPUSD", altname="XRPUSD", wsname="XRP/USD",
                             base="XXRP", quote="ZUSD", lot_decimals=8,
                             pair_decimals=5, order_min=Decimal("0"), cost_min=Decimal("0"),
                             status="online"),
        "BTCEUR": SymbolMeta(key="BTCEUR", altname="BTCEUR", wsname="BTC/EUR",
                             base="XXBT", quote="ZEUR", lot_decimals=8,
                             pair_decimals=5, order_min=Decimal("0"), cost_min=Decimal("0"),
                             status="online"),
        "DELISTEDUSD": SymbolMeta(key="DELISTEDUSD", altname="DELISTEDUSD",
                                  wsname="DEL/USD", base="DEL", quote="ZUSD",
                                  lot_decimals=8, pair_decimals=5,
                                  order_min=Decimal("0"), cost_min=Decimal("0"),
                                  status="delisted"),
    }
    gw._meta_loaded_at = time.time()  # cache hit -> load_metadata returns _meta
    gw._meta_ttl = 3600
    pairs = gw.list_usd_pairs()
    assert len(pairs) == 1, f"expected only XRPUSD, got {[p.altname for p in pairs]}"
    assert pairs[0].altname == "XRPUSD"
