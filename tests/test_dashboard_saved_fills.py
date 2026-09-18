import sqlite3
from dublin_bot import dashboard


def test_saved_fills_are_historical_not_live(tmp_path):
    path = tmp_path / 'executions.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE executions (order_id TEXT, symbol TEXT, side TEXT, status TEXT, signal_price REAL, fill_price REAL, volume REAL, cost REAL, fee REAL, filled_at TEXT)')
        db.execute('INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,?,?)', ('order-1', 'XXBTZUSD', 'buy', 'closed', 1.4, 76000, .0005, 38, .3, '2026-09-15T16:00:00+00:00'))
    result = dashboard.load_saved_fills(path)
    assert result['live_verified'] is False
    assert result['source'] == 'local exchange execution capture'
    assert result['fills'][0]['symbol'] == 'BTC/USD'
    assert result['fills'][0]['price'] == 76000
    assert result['fills'][0]['pnl'] is None
    assert result['fills'][0]['quantity'] == .0005


def test_missing_saved_database_is_unavailable_and_not_created(tmp_path):
    path = tmp_path / 'absent.sqlite3'
    result = dashboard.load_saved_fills(path)
    assert result['fills'] is None
    assert result['error']
    assert not path.exists()


def test_saved_history_route_and_visible_provenance():
    from pathlib import Path
    import inspect
    assert '/gunbot/saved-fills' in inspect.getsource(dashboard.SimpleDashboardHandler.do_GET)
    static = Path(dashboard.__file__).parent / 'static'
    html = (static / 'dashboard.html').read_text()
    js = (static / 'dashboard.js').read_text()
    assert 'id="saved-fills"' in html
    assert 'Historical local captures — not a live exchange read' in html
    assert '/gunbot/saved-fills' in js
