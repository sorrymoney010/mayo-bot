from dublin_bot import dashboard as d

class Gateway:
    def _private(self, name, params=None):
        if name == 'Balance': return {'XXBT': '0.00023', 'ZUSD': '3.5', 'PUMP': '0'}
        if name == 'TradeBalance': raise RuntimeError('permission denied')
        raise AssertionError(name)

def test_account_failure_is_null_not_budget_and_holdings_survive():
    data = d.load_live_portfolio(Gateway())
    assert data['equity'] is None
    assert data['cash'] == 3.5
    assert data['holdings'][0]['quantity'] == 0.00023
    assert data['holdings'][0]['cost_basis'] is None
    assert data['total_pnl'] is None
    assert 'permission denied' in data['errors']['TradeBalance']

class HistoryGateway:
    def _private(self, name, params=None):
        if name == 'TradesHistory':
            return {'count': 51, 'trades': {'T1': {'ordertxid': 'O1', 'pair': 'XXBTZUSD', 'type': 'buy', 'time': 100, 'vol': '0.1', 'price': '20', 'cost': '2', 'fee': '.01'}}}
        if name == 'OpenOrders':
            return {'open': {'O2': {'status': 'open', 'vol': '4', 'vol_exec': '0', 'descr': {'pair': 'XRPUSD', 'type': 'buy'}}}}
        if name == 'ClosedOrders': raise RuntimeError('history unavailable')
        raise AssertionError(name)

def test_history_partial_and_submitted_not_filled():
    data = d.load_exchange_history(HistoryGateway())
    assert data['fills'][0]['symbol'] == 'BTC/USD'
    assert data['fills'][0]['pnl'] is None
    assert data['fills'][0]['attribution'] == 'account / unverified bot ownership'
    assert data['orders'][0]['filled'] is False
    assert data['fills_total'] == 51
    assert data['fills_has_more'] is True
    assert data['errors']['ClosedOrders'] == 'history unavailable'
    assert all(p['realized_pnl'] is None for p in data['performance'])


def test_ohlc_normalized_for_local_chart():
    class Public:
        def _public(self, endpoint, params):
            assert endpoint == 'OHLC'
            assert params == {'pair': 'PUMPUSD', 'interval': 15}
            return {'PUMPUSD': [[100, '1', '3', '.5', '2', '1.5', '10', 2]], 'last': 100}
    data = d.load_chart('PUMP/USD', 15, Public())
    assert data['candles'][0] == {'time': 100, 'open': 1., 'high': 3., 'low': .5, 'close': 2., 'volume': 10.}


def test_readonly_http_surface_and_local_assets():
    import threading, urllib.request, urllib.error
    server = d.BoundedDashboardHTTPServer(('127.0.0.1', 0), d.SimpleDashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    try:
        html = urllib.request.urlopen(base+'/gunbot').read().decode()
        assert '/static/lightweight-charts.js' in html
        assert 'id="chart"' in html
        assert 'BTC/USD' in html and 'PUMP/USD' in html and 'XRP/USD' in html
        js = urllib.request.urlopen(base+'/static/dashboard.js').read().decode()
        assert 'AbortController' in js
        assert 'addCandlestickSeries' in js and 'addHistogramSeries' in js
        import pytest
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base+'/api/monitor/start', data=b''))
        assert e.value.code == 501
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base+'/static/../config.py')
        assert e.value.code == 404
    finally:
        server.shutdown(); server.server_close()


def test_private_collector_cooldown_and_stale_evidence():
    class Private:
        calls = 0
        def _private(self, name, params=None):
            self.calls += 1
            if self.calls == 1: return {'ZUSD': '3'}
            raise RuntimeError('EAPI:Rate limit exceeded')
    raw = Private()
    clock = [0.0]
    reader = d.DashboardPrivateReader(raw, clock=lambda: clock[0])
    assert reader._private('Balance') == {'ZUSD': '3'}
    assert reader._private('Balance') == {'ZUSD': '3'}
    assert raw.calls == 1
    clock[0] = 61
    stale = reader._private('Balance')
    assert stale['_dashboard_stale']['error'] == 'EAPI:Rate limit exceeded'
    import pytest
    with pytest.raises(RuntimeError, match='cooldown.*EAPI:Rate limit exceeded'):
        reader._private('OpenOrders')
    assert raw.calls == 2


def test_engine_observation_is_not_trading_claim(tmp_path):
    log = tmp_path / 'coordinator.log'
    log.write_text('historical report')
    status = d.dashboard_engine_observation(process_text='63948 python3 -m dublin_bot.coordinator --live\n44 python grep dublin_bot.coordinator', log_path=log)
    assert status['coordinator_pids'] == [63948]
    assert status['state'] == 'process observed'
    assert status['trading_verified'] is False
    assert status['log_modified_at']
    assert status['log_source'] == str(log)


def test_engine_observation_prefers_live_log_without_claiming_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(d, '_DASH_BASE', tmp_path)
    logs = tmp_path / 'logs'
    logs.mkdir()
    coordinator = logs / 'coordinator.log'
    coordinator.write_text('old coordinator output')
    live = logs / 'live_trading.log'
    live.write_text('live coordinator output')
    status = d.dashboard_engine_observation(process_text='')
    assert status['log_source'] == str(live)
    assert status['log_modified_at']
    assert status['trading_verified'] is False
    assert 'heartbeat' not in status
    assert 'do not prove healthy heartbeat' in status['note']
    explicit = d.dashboard_engine_observation(process_text='', log_path=coordinator)
    assert explicit['log_source'] == str(coordinator)
    live.unlink()
    fallback = d.dashboard_engine_observation(process_text='')
    assert fallback['log_source'] == str(coordinator)
    missing = logs / 'explicit-missing.log'
    assert d.dashboard_engine_observation(process_text='', log_path=missing)['log_source'] == str(missing)


def test_cli_dashboard_cannot_start_monitor(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('dashboard must not construct a trading monitor')
    monkeypatch.setattr(d, 'TradingMonitor', forbidden)
    monkeypatch.setattr(d, 'TradingEngine', forbidden)
    servers = []
    class Server:
        def __init__(self, address, handler):
            self.address, self.handler = address, handler
            self.served = self.closed = False
            servers.append(self)
        def serve_forever(self): self.served = True
        def shutdown(self): pass
        def server_close(self): self.closed = True
    monkeypatch.setattr(d, 'BoundedDashboardHTTPServer', Server)
    from dublin_bot.config import Settings
    assert d.serve_dashboard(Settings(_env_file=None, auto_start_monitor=True)) == 0
    assert [(s.address, s.handler) for s in servers] == [
        (('127.0.0.1', 8766), d.SimpleDashboardHandler),
        (('127.0.0.1', 8765), d.DashboardRedirectHandler),
    ]
    assert all(s.served and s.closed for s in servers)




def test_cli_dashboard_second_bind_failure_closes_primary(monkeypatch):
    import pytest
    servers = []
    real_server = d.BoundedDashboardHTTPServer
    def bind(address, handler):
        if servers:
            raise OSError('compatibility port occupied')
        server = real_server(('127.0.0.1', 0), handler)
        servers.append(server)
        return server
    monkeypatch.setattr(d, 'BoundedDashboardHTTPServer', bind)
    from dublin_bot.config import Settings
    try:
        with pytest.raises(OSError, match='compatibility port occupied'):
            d.serve_dashboard(Settings(_env_file=None))
        assert servers[0].socket.fileno() == -1
    finally:
        for server in servers:
            server.server_close()


def test_cli_dashboard_listener_error_propagates_and_closes_both(monkeypatch):
    import pytest
    servers = []
    class Server:
        def __init__(self, address, handler):
            self.handler, self.closed = handler, False
            servers.append(self)
        def serve_forever(self):
            if self.handler is d.DashboardRedirectHandler:
                raise RuntimeError('listener failed')
        def shutdown(self): pass
        def server_close(self): self.closed = True
    monkeypatch.setattr(d, 'BoundedDashboardHTTPServer', Server)
    from dublin_bot.config import Settings
    with pytest.raises(RuntimeError, match='listener failed'):
        d.serve_dashboard(Settings(_env_file=None))
    assert len(servers) == 2 and all(s.closed for s in servers)


def test_cli_dashboard_listener_failure_stops_running_sibling(monkeypatch):
    import threading
    import pytest
    from dublin_bot.config import Settings
    servers = []
    started = threading.Event()
    stopped = threading.Event()
    class Server:
        def __init__(self, address, handler):
            self.handler, self.closed = handler, False
            servers.append(self)
        def serve_forever(self):
            if self.handler is d.SimpleDashboardHandler:
                started.set()
                assert stopped.wait(3), 'sibling was not shut down promptly'
            else:
                assert started.wait(3)
                raise RuntimeError('compatibility listener failed')
        def shutdown(self): stopped.set()
        def server_close(self): self.closed = True
    monkeypatch.setattr(d, 'BoundedDashboardHTTPServer', Server)
    with pytest.raises(RuntimeError, match='compatibility listener failed'):
        d.serve_dashboard(Settings(_env_file=None))
    assert stopped.is_set()
    assert all(s.closed for s in servers)
    assert not any(t.name == 'dashboard-listener' for t in threading.enumerate())


def test_cli_dashboard_thread_start_failure_closes_both(monkeypatch):
    import threading
    import pytest
    from dublin_bot.config import Settings
    servers = []
    stopped = threading.Event()
    class Server:
        def __init__(self, address, handler):
            self.closed = False
            servers.append(self)
        def serve_forever(self): assert stopped.wait(3)
        def shutdown(self): stopped.set()
        def server_close(self): self.closed = True
    threads = []
    def thread_factory(*args, **kwargs):
        thread = threading.Thread(*args, **kwargs)
        if threads:
            def fail(): raise RuntimeError('thread start failed')
            thread.start = fail
        threads.append(thread)
        return thread
    monkeypatch.setattr(d, 'BoundedDashboardHTTPServer', Server)
    monkeypatch.setattr(d, 'Thread', thread_factory)
    with pytest.raises(RuntimeError, match='thread start failed'):
        d.serve_dashboard(Settings(_env_file=None))
    assert all(s.closed for s in servers)
    assert not any(t.is_alive() for t in threads)


def test_cli_dashboard_keyboard_interrupt_closes_both(monkeypatch):
    from dublin_bot.config import Settings
    servers = []
    class Server:
        def __init__(self, address, handler):
            self.closed = False
            servers.append(self)
        def serve_forever(self): raise KeyboardInterrupt
        def shutdown(self): pass
        def server_close(self): self.closed = True
    monkeypatch.setattr(d, 'BoundedDashboardHTTPServer', Server)
    assert d.serve_dashboard(Settings(_env_file=None)) == 0
    assert len(servers) == 2 and all(s.closed for s in servers)


def test_cli_dashboard_run_false_preserves_learner_initialization(monkeypatch, tmp_path):
    from dublin_bot.config import Settings
    from dublin_bot import learner
    loaded = []
    class Learner:
        def __init__(self, path, min_trades, enabled):
            self.values = (path, min_trades, enabled)
        def load(self): loaded.append(self.values)
    def forbidden(*args, **kwargs):
        raise AssertionError('run=False must not construct servers or trading components')
    monkeypatch.setattr(learner, 'LearningAgent', Learner)
    monkeypatch.setattr(d, 'BoundedDashboardHTTPServer', forbidden)
    monkeypatch.setattr(d, 'TradingMonitor', forbidden)
    monkeypatch.setattr(d, 'TradingEngine', forbidden)
    settings = Settings(_env_file=None, learner_enabled=True,
                        learner_path=tmp_path / 'learner.json', auto_start_monitor=True)
    assert d.serve_dashboard(settings, run=False) == 0
    assert loaded == [(settings.learner_path, settings.learner_min_trades, True)]


def test_legacy_redirect_does_not_collect_or_accept_posts():
    import http.client, threading
    server = d.BoundedDashboardHTTPServer(('127.0.0.1', 0), d.DashboardRedirectHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
        conn.request('GET', '/api/status')
        response = conn.getresponse()
        assert response.status == 302
        assert response.getheader('Location') == 'http://127.0.0.1:8766/gunbot'
        response.read()
        conn.request('POST', '/api/monitor/start', body='')
        response = conn.getresponse()
        assert response.status == 405
        response.read(); conn.close()
    finally:
        server.shutdown(); server.server_close()
