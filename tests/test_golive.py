from __future__ import annotations

from dublin_bot.config import Settings
from dublin_bot.errors import BrokerError
from dublin_bot.golive import FAIL, PASS, WARN, preflight, render


class FakeGw:
    def __init__(self, restr=None, bal=100.0, pos=None, restr_err=False):
        self.restr, self.bal, self._pos, self.restr_err = restr or {}, bal, pos or [], restr_err
    def health(self): return {"reachable": True, "latency_ms": 10, "error": None}
    def _public(self, path, params=None):
        import time; return {"serverTime": int(time.time() * 1000)}
    def _signed(self, path, params=None, **kw):
        if "apiRestrictions" in path:
            if self.restr_err: raise BrokerError("404")
            return self.restr
        return {"canTrade": True, "balances": [{"asset": "USDT", "free": str(self.bal), "locked": "0"}]}
    def positions(self): return self._pos
    def orders(self): return []
    def resolve_symbol(self):
        from decimal import Decimal
        from types import SimpleNamespace
        return SimpleNamespace(key="BTCUSDT", cost_min=Decimal("5"))


def S(**kw):
    base = dict(_env_file=None, broker="binance", binance_api_key="k", binance_api_secret="s",
                strategy_equity_usd=25.0, max_orders_per_day=3, timeframe_minutes=60)
    base.update(kw); return Settings(**base)


def by(checks): return {c.name: c for c in checks}


def test_withdrawals_enabled_is_a_hard_fail():
    r = by(preflight(S(), FakeGw({"enableWithdrawals": True, "enableSpotAndMarginTrading": True})))
    assert r["key: withdrawals"].status == FAIL


def test_clean_setup_has_no_fail_and_flags_report_locked(monkeypatch):
    monkeypatch.setenv("MAYO_LEDGER_OWNER", "1")
    ch = preflight(S(), FakeGw({"enableWithdrawals": False, "enableSpotAndMarginTrading": True}))
    assert not [c for c in ch if c.status == FAIL]
    assert by(ch)["live flags"].status == WARN      # still locked by default


def test_unlimited_orders_and_missing_keys_and_low_funds_fail():
    assert by(preflight(S(max_orders_per_day=0), FakeGw()))["daily order cap"].status == FAIL
    assert by(preflight(S(binance_api_key="", binance_api_secret=""), FakeGw()))["credentials"].status == FAIL
    assert by(preflight(S(), FakeGw({"enableWithdrawals": False}, bal=3.0)))["funds"].status == FAIL


def test_unverifiable_key_restrictions_warn_not_pass():
    r = by(preflight(S(), FakeGw(restr_err=True)))
    assert r["key: withdrawals"].status == WARN


def test_wrong_broker_and_render_exit_code():
    ch = preflight(S(broker="kraken"), FakeGw())
    assert by(ch)["broker"].status == FAIL
    text, code = render(ch)
    assert code == 1 and "NOT ready" in text


def test_subhour_timeframe_is_blocked_for_live(monkeypatch):
    monkeypatch.setenv("MAYO_LEDGER_OWNER", "1")
    r = by(preflight(S(timeframe_minutes=15), FakeGw({"enableWithdrawals": False})))
    assert r["strategy allowed live"].status == FAIL
