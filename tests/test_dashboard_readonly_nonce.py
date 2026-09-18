import pytest
from dublin_bot import dashboard as d


def test_read_only_nonce_race_gets_one_fresh_attempt(monkeypatch):
    class Reader:
        calls = 0
        def _private(self, endpoint, params=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError('EAPI:Invalid nonce')
            return {'ZUSD': '19.30'}
    raw = Reader()
    monkeypatch.setattr(d.time, 'sleep', lambda seconds: None)
    result = d.DashboardPrivateReader(raw)._private('Balance')
    assert result == {'ZUSD': '19.30'}
    assert raw.calls == 2


def test_repeated_nonce_failure_is_bounded_and_cools_down(monkeypatch):
    class Reader:
        calls = 0
        def _private(self, endpoint, params=None):
            self.calls += 1
            raise RuntimeError('EAPI:Invalid nonce')
    raw = Reader()
    monkeypatch.setattr(d.time, 'sleep', lambda seconds: None)
    reader = d.DashboardPrivateReader(raw)
    with pytest.raises(RuntimeError, match='Invalid nonce'):
        reader._private('Balance')
    assert raw.calls == 2
    with pytest.raises(RuntimeError, match='cooldown'):
        reader._private('OpenOrders')
    assert raw.calls == 2
