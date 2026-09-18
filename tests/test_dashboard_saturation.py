"""Listener admission must not make shutdown depend on idle clients."""

import socket
import threading

import pytest

from dublin_bot import dashboard as d


class ObservedServer(d.BoundedDashboardHTTPServer):
    """Observe real admission/handler activity without changing its behavior."""

    def __init__(self, *args, **kwargs):
        self.condition = threading.Condition()
        self.active = 0
        self.peak = 0
        self.accepted = 0
        self.excess_accepted = threading.Event()
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        self.accepted += 1
        if self.accepted > self.max_request_threads:
            self.excess_accepted.set()
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        with self.condition:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.condition.notify_all()
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()


def saturate(server, clients):
    assert server.max_request_threads == 24
    for _ in range(24):
        clients.append(socket.create_connection(server.server_address, timeout=2))
    with server.condition:
        assert server.condition.wait_for(lambda: server.active == 24, timeout=2)
    clients.append(socket.create_connection(server.server_address, timeout=2))
    assert server.excess_accepted.wait(2), "listener did not accept client 25"


def close_clients(server, clients):
    for client in clients:
        client.close()
    with server.condition:
        assert server.condition.wait_for(lambda: server.active == 0, timeout=3)


@pytest.mark.parametrize("handler", [d.SimpleDashboardHandler, d.DashboardRedirectHandler])
def test_saturated_listener_shuts_down_before_idle_clients_close(handler):
    server = ObservedServer(("127.0.0.1", 0), handler)
    listener = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    clients = []
    shutdown = threading.Thread(target=server.shutdown, daemon=True)
    listener.start()
    try:
        saturate(server, clients)
        shutdown.start()
        shutdown.join(1)
        assert not shutdown.is_alive(), "shutdown blocked by 25 idle clients"
        listener.join(1)
        assert not listener.is_alive()
        assert server.active == server.peak == 24
        # No test client has been closed: only the excess client gets server EOF.
        assert all(client.fileno() >= 0 for client in clients)
        assert clients[-1].recv(1) == b""
        server.server_close()
        assert server.socket.fileno() == -1
    finally:
        # Even RED must unblock and clean up its own ephemeral test listener.
        close_clients(server, clients)
        if shutdown.ident is None:
            shutdown.start()
        shutdown.join(3)
        listener.join(3)
        server.server_close()
        assert not shutdown.is_alive()
        assert not listener.is_alive()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_thread_start_failure_releases_exactly_one_slot(monkeypatch, failure):
    server = d.BoundedDashboardHTTPServer(("127.0.0.1", 0), d.SimpleDashboardHandler)
    client = socket.create_connection(server.server_address, timeout=2)
    request, address = server.get_request()

    def fail_start(self):
        raise failure("thread start failed")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    acquired = 0
    try:
        with pytest.raises(failure, match="thread start failed"):
            server.process_request(request, address)
        for _ in range(server.max_request_threads):
            assert server._request_slots.acquire(blocking=False), "request slot leaked"
            acquired += 1
        assert not server._request_slots.acquire(blocking=False)
    finally:
        for _ in range(acquired):
            server._request_slots.release()
        server.shutdown_request(request)
        client.close()
        server.server_close()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("saturated_handler", [d.SimpleDashboardHandler, d.DashboardRedirectHandler])
def test_cli_failure_closes_both_listeners_while_sibling_saturated(
    monkeypatch, failure, saturated_handler
):
    from dublin_bot.config import Settings

    servers = []
    ready = threading.Event()
    trigger_failure = threading.Event()
    outcomes = []
    clients = []

    class FailingSibling(ObservedServer):
        def serve_forever(self, poll_interval=0.01):
            if self.RequestHandlerClass is saturated_handler:
                super().serve_forever(poll_interval=poll_interval)
            else:
                assert trigger_failure.wait(5), "test did not trigger sibling failure"
                raise failure("sibling listener failed")

    def ephemeral_server(address, handler):
        server = FailingSibling(("127.0.0.1", 0), handler)
        servers.append(server)
        if len(servers) == 2:
            ready.set()
        return server

    def run_cli():
        try:
            outcomes.append(d.serve_dashboard(Settings(_env_file=None)))
        except BaseException as exc:
            outcomes.append(exc)

    monkeypatch.setattr(d, "BoundedDashboardHTTPServer", ephemeral_server)
    cli = threading.Thread(target=run_cli, daemon=True)
    cli.start()
    saturated = None
    try:
        assert ready.wait(2)
        saturated = next(s for s in servers if s.RequestHandlerClass is saturated_handler)
        saturate(saturated, clients)
        trigger_failure.set()
        cli.join(2)
        assert not cli.is_alive(), "CLI cleanup blocked by saturated sibling"
        assert all(client.fileno() >= 0 for client in clients)
        assert all(server.socket.fileno() == -1 for server in servers)
        assert saturated.active == saturated.peak == 24
        assert not any(t.name == "dashboard-listener" for t in threading.enumerate())
        if failure is KeyboardInterrupt:
            assert outcomes == [0]
        else:
            assert len(outcomes) == 1 and isinstance(outcomes[0], failure)
            assert str(outcomes[0]) == "sibling listener failed"
    finally:
        trigger_failure.set()
        if saturated is not None:
            close_clients(saturated, clients)
        cli.join(3)
        for server in servers:
            server.server_close()
        assert not cli.is_alive()
