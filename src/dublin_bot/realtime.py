"""Real-time market feed (WebSocket) with REST fallback.

Purpose: let the bot react in *seconds* instead of waiting for the next REST
poll.  The engine's safety pipeline stays REST-driven (authoritative), but the
real-time feed supplies a live price between cycles so the protective-stop and
exit checks can trigger intraday.  If the socket drops, callers fall back to
``gateway.get_ticker_for`` — nothing here is on the order-submission path.

Kraken public WS v1:
  wss://ws.kraken.com  — subscribe to ticker/<pair> for live last/bid/ask.

The feed is intentionally read-only.  It never places or cancels orders; the
engine's gated execution path remains the single source of truth for orders.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from typing import Callable

# `websockets` is an optional dependency used only by the real-time feed.  Import
# it lazily inside the feed class so the rest of the bot (and its tests) work
# without it installed; the feed degrades to REST-only when it is missing.


@dataclass
class FeedSnapshot:
    """Latest observed market state for one symbol."""

    symbol: str
    last: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    updated_at: float = 0.0
    source: str = "rest"  # "ws" once a socket tick arrives


class KrakenRealtimeFeed:
    """Live ticker feed for one or more Kraken pairs via WebSocket.

    Usage:
        feed = KrakenRealtimeFeed({"XBT/USD": "XBT/USD"})
        feed.start()                      # background WS reader
        snap = feed.latest("XBT/USD")     # FeedSnapshot or None
        # ... between REST cycles, check snap.last for stop breaches ...
        feed.stop()
    """

    WS_URL = "wss://ws.kraken.com"

    def __init__(self, pairs: dict[str, str], *, timeout: float = 10.0) -> None:
        # Map our canonical symbol -> Kraken wsname (e.g. "XBT/USD").
        self.pairs = dict(pairs)
        self._snapshots: dict[str, FeedSnapshot] = {
            sym: FeedSnapshot(symbol=sym) for sym in pairs
        }
        self._lock = threading.Lock()
        self._timeout = timeout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self.on_tick: Callable[[FeedSnapshot], None] | None = None

    # ── public API ──
    def latest(self, symbol: str) -> FeedSnapshot | None:
        with self._lock:
            return self._snapshots.get(symbol)

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="kraken-ws-feed"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ── internals ──
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._loop_once()
            except Exception:
                # Any socket/network failure → back off and retry. REST fallback
                # covers the gap; the feed is best-effort only.
                self._connected = False
                if self._stop.wait(self._timeout):
                    break
        self._connected = False

    def _loop_once(self) -> None:
        import websockets  # lazy: optional dependency; only used here

        async def _consume() -> None:
            async with websockets.connect(
                self.WS_URL, open_timeout=self._timeout, close_timeout=self._timeout
            ) as ws:
                self._connected = True
                sub = {
                    "event": "subscribe",
                    "subscription": {"name": "ticker"},
                    "pair": list(self.pairs.values()),
                }
                await ws.send(json.dumps(sub))
                async for raw in ws:
                    if self._stop.is_set():
                        break
                    self._ingest(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))

        asyncio.run(_consume())

    def _ingest(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        # Kraken ticker channel: [channelID, {c:[last], b:[bid], a:[ask]},
        #                          "ticker", "PAIR"]
        if not isinstance(msg, list) or len(msg) < 4:
            return
        payload = msg[1]
        pair_ws = msg[3]
        if not isinstance(payload, dict):
            return
        if "c" not in payload and "b" not in payload and "a" not in payload:
            return
        # Reverse-map wsname -> canonical symbol.
        canon = next((s for s, w in self.pairs.items() if w == pair_ws), None)
        if canon is None:
            return
        last = float(payload.get("c", [0])[0]) if payload.get("c") else 0.0
        bid = float(payload.get("b", [0])[0]) if payload.get("b") else 0.0
        ask = float(payload.get("a", [0])[0]) if payload.get("a") else 0.0
        with self._lock:
            snap = self._snapshots[canon]
            if last:
                snap.last = last
            if bid:
                snap.bid = bid
            if ask:
                snap.ask = ask
            snap.updated_at = time.time()
            snap.source = "ws"
        if self.on_tick is not None:
            self.on_tick(snap)
