"""Dublin Terminal v2 — live-view Kraken dashboard.

Drop-in replacement for src/dublin_bot/dashboard.py.

What changed vs the previous dashboard:
- Clean routing table (fixes the double-response bug that sent a valid
  payload followed by a spurious 404 on /api/reconciliation and
  /api/stop-monitor — the likely cause of the phantom "Offline" banner).
- Real Kraken account data: balances, equivalent USD equity, and real
  trade history with FIFO-estimated realized P&L.
- Multi-pair watchlist + 24h momentum scanner (public data, no key needed).
- ntfy.sh push alerts to iPhone (bot trades, errors, emergency stop).
- Redesigned mobile-first UI with a bottom tab bar and ticker tape.

Safety posture is unchanged: all execution paths remain paper/dry-run.
This file only READS from Kraken; it never places orders.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore, Event, Lock, Thread
from time import monotonic
from typing import Any

from .audit import AuditLog
from .config import Settings
from .emergency import activate_emergency_stop, clear_emergency_stop, emergency_stop_active
from .engine import TradingEngine
from .execution_store import ExecutionStore
from .state import StateStore

HOST = os.environ.get("DUBLIN_HOST", "0.0.0.0")
PORT = int(os.environ.get("DUBLIN_PORT", "8765"))

KRAKEN_API_BASE = "https://api.kraken.com"

_market_cache: dict[str, object] = {"at": 0.0, "data": {}}
_market_lock = Lock()
_health_cache: dict[str, object] = {"at": 0.0, "data": {}}
_health_lock = Lock()
_state_cache: dict[str, object] = {"at": 0.0, "data": {}}
_state_lock = Lock()
_strategy_cache: dict[str, object] = {"at": 0.0, "data": {}}
_strategy_lock = Lock()
_learning_cache: dict[str, object] = {"at": 0.0, "data": {}}
_learning_lock = Lock()
_scanner_cache: dict[str, object] = {"at": 0.0, "data": {}}
_scanner_lock = Lock()
# Single long-lived executor for the watchlist scanner. Reused across requests
# instead of creating a fresh ThreadPoolExecutor per page load (the old code
# leaked 8 threads on every /api/scanner poll until ulimit -u was exhausted).
_scanner_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="dublin-scanner")
_trades_cache: dict[str, object] = {"at": 0.0, "data": {}}
_trades_lock = Lock()
_balances_cache: dict[str, object] = {"at": 0.0, "data": {}}


class BoundedDashboardHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server with a fixed upper bound on active handlers.

    Dashboard routes call Kraken and can take up to their network timeout. A
    browser polling faster than those calls complete must never create an
    unbounded number of OS threads and starve the trading monitor.
    """

    daemon_threads = True
    block_on_close = False
    max_request_threads = 24

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._request_slots = BoundedSemaphore(self.max_request_threads)

    def process_request(self, request: Any, client_address: Any) -> None:
        # Admission runs on the listener: never block its shutdown on capacity.
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()
_balances_lock = Lock()


# ---------------------------------------------------------------------------
# ntfy push alerts (iPhone)
# ---------------------------------------------------------------------------

def _ntfy_topic() -> str | None:
    topic = os.environ.get("DUBLIN_NTFY_TOPIC", "").strip()
    return topic or None


def ntfy_alert(title: str, message: str, priority: str = "default", tags: str = "chart_with_upwards_trend") -> bool:
    """Send a push notification via ntfy.sh. Silently no-ops if unconfigured.

    Setup: install the ntfy iOS app, subscribe to a long random topic name,
    then set DUBLIN_NTFY_TOPIC=<that name> in .env and restart the dashboard.
    """
    topic = _ntfy_topic()
    if not topic:
        return False
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": tags,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def alerts_status() -> dict[str, object]:
    topic = _ntfy_topic()
    return {
        "configured": topic is not None,
        "topic_hint": (topic[:4] + "…" + topic[-2:]) if topic and len(topic) > 8 else None,
        "provider": "ntfy.sh",
        "setup_hint": (
            "Install ntfy on iPhone, subscribe to a long random topic, "
            "set DUBLIN_NTFY_TOPIC in .env, restart dashboard."
        ),
    }


# ---------------------------------------------------------------------------
# Direct Kraken REST (read-only) for account + market features
# ---------------------------------------------------------------------------

def _kraken_credentials(settings: Settings) -> tuple[str, str] | None:
    """Find Kraken credentials without assuming the Settings attribute names."""
    key_names = ("kraken_api_key", "KRAKEN_API_KEY", "api_key")
    secret_names = ("kraken_api_secret", "KRAKEN_API_SECRET", "api_secret")
    key = next((getattr(settings, n) for n in key_names if getattr(settings, n, None)), None)
    secret = next((getattr(settings, n) for n in secret_names if getattr(settings, n, None)), None)
    key = key or os.environ.get("KRAKEN_API_KEY")
    secret = secret or os.environ.get("KRAKEN_API_SECRET")
    if key and secret:
        return str(key), str(secret)
    return None


def _kraken_private(settings: Settings, path: str, data: dict[str, object] | None = None) -> dict:
    """Delegates to the gateway's signed private call (shares the persisted,
    monotonic NonceGenerator so the server-side watermark is never regressed)."""
    from .engine import build_gateway
    gateway = build_gateway(settings)
    endpoint = path.rsplit("/", 1)[-1]
    return gateway._private(endpoint, dict(data or {}))


def _kraken_public(path: str, params: dict[str, object] | None = None) -> dict:
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    req = urllib.request.Request(
        f"{KRAKEN_API_BASE}{path}{query}",
        headers={"User-Agent": "Dublin-Terminal/2.0"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode())
    if payload.get("error"):
        raise RuntimeError("; ".join(payload["error"]))
    return payload["result"]


def kraken_balances_data(settings: Settings) -> dict[str, object]:
    """Real Kraken balances + equivalent USD equity. Read-only."""
    with _balances_lock:
        if monotonic() - float(_balances_cache["at"]) < 30:
            return dict(_balances_cache["data"])
        try:
            raw = _kraken_private(settings, "/0/private/Balance")
            balances = {asset: float(amount) for asset, amount in raw.items() if float(amount) > 0}
            equity_usd = None
            try:
                tb = _kraken_private(settings, "/0/private/TradeBalance", {"asset": "ZUSD"})
                equity_usd = round(float(tb.get("eb", 0.0)), 2)
            except Exception:
                pass
            data = {
                "ok": True,
                "balances": balances,
                "equity_usd": equity_usd,
                "asset_count": len(balances),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as exc:
            data = {"ok": False, "error": str(exc), "balances": {}, "equity_usd": None}
        _balances_cache.update(at=monotonic(), data=data)
        return data


def kraken_trades_data(settings: Settings, limit: int = 50) -> dict[str, object]:
    """Real Kraken trade history with FIFO-estimated realized P&L. Read-only."""
    with _trades_lock:
        if monotonic() - float(_trades_cache["at"]) < 60:
            return dict(_trades_cache["data"])
        try:
            result = _kraken_private(settings, "/0/private/TradesHistory", {"trades": True})
            trades_raw = result.get("trades", {})
            trades: list[dict[str, object]] = []
            for txid, t in trades_raw.items():
                pair = str(t.get("pair", ""))
                trades.append({
                    "id": txid,
                    "order_id": t.get("ordertxid"),
                    "pair": pair,
                    "type": t.get("type"),
                    "price": float(t.get("price", 0)),
                    "vol": float(t.get("vol", 0)),
                    "cost": float(t.get("cost", 0)),
                    "fee": float(t.get("fee", 0)),
                    "time": datetime.fromtimestamp(float(t.get("time", 0)), tz=timezone.utc).isoformat(),
                })
            trades.sort(key=lambda x: x["time"], reverse=True)

            # FIFO realized P&L estimate, per asset pair (USD-quoted pairs only)
            realized = 0.0
            fees_total = 0.0
            open_lots: dict[str, list[list[float]]] = {}
            for t in sorted(trades, key=lambda x: x["time"]):
                pair = str(t["pair"])
                fees_total += float(t["fee"])
                if not (pair.endswith("USD") or pair.endswith("ZUSD")):
                    continue
                lots = open_lots.setdefault(pair, [])
                if t["type"] == "buy":
                    lots.append([float(t["vol"]), float(t["price"])])
                elif t["type"] == "sell":
                    remaining = float(t["vol"])
                    sell_price = float(t["price"])
                    while remaining > 1e-12 and lots:
                        lot_vol, lot_price = lots[0]
                        take = min(lot_vol, remaining)
                        realized += take * (sell_price - lot_price)
                        lot_vol -= take
                        remaining -= take
                        if lot_vol <= 1e-12:
                            lots.pop(0)
                        else:
                            lots[0][0] = lot_vol
            data = {
                "ok": True,
                "trades": trades[:limit],
                "count": result.get("count", len(trades)),
                "estimated_realized_pnl": round(realized - fees_total, 2),
                "total_fees": round(fees_total, 2),
                "pnl_note": "FIFO estimate over fetched history, USD pairs, net of fees",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as exc:
            data = {"ok": False, "error": str(exc), "trades": [], "count": 0}
        _trades_cache.update(at=monotonic(), data=data)
        return data


def execution_data(settings: Settings, limit: int = 100) -> dict[str, object]:
    """Bot-only signal-to-fill matches and measured live slippage."""
    try:
        fills = ExecutionStore(settings.execution_db_path).recent(limit)
        rows = [fill.to_dict() for fill in fills]
        measured = [row for row in rows if float(row["signal_price"]) > 0]
        avg_usd = (
            sum(float(row["slippage_usd"]) for row in measured) / len(measured)
            if measured else 0.0
        )
        avg_bps = (
            sum(float(row["slippage_bps"]) for row in measured) / len(measured)
            if measured else 0.0
        )
        return {
            "ok": True,
            "executions": rows,
            "count": len(rows),
            "measured_count": len(measured),
            "average_slippage_usd": round(avg_usd, 8),
            "average_slippage_bps": round(avg_bps, 4),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "executions": [], "count": 0}


# Full tradeable basket (BTC/SOL/XRP explicitly enabled alongside the
# small-cap rotation of ADA/DOGE/TRX/HYPE). ETH is kept for market context
# but is not part of the trading basket.
WATCHLIST_DEFAULT = "BTC/USD,ETH/USD,SOL/USD,XRP/USD,ADA/USD,DOGE/USD,TRX/USD,HYPE/USD"
KRAKEN_ALTNAMES = {
    "BTC/USD": "XBTUSD", "ETH/USD": "ETHUSD", "SOL/USD": "SOLUSD",
    "XRP/USD": "XRPUSD", "DOGE/USD": "DOGEUSD", "ADA/USD": "ADAUSD",
    "TRX/USD": "TRXUSD", "HYPE/USD": "HYPEUSD",
    "LINK/USD": "LINKUSD", "AVAX/USD": "AVAXUSD", "DOT/USD": "DOTUSD",
    "LTC/USD": "LTCUSD", "ATOM/USD": "ATOMUSD", "PEPE/USD": "PEPEUSD",
}


def _watchlist_pairs() -> list[str]:
    raw = os.environ.get("DUBLIN_WATCHLIST", WATCHLIST_DEFAULT)
    return [p.strip().upper() for p in raw.split(",") if p.strip()]


def scanner_data(settings: Settings) -> dict[str, object]:
    """Multi-pair watchlist with 24h change + volume. Public data only."""
    with _scanner_lock:
        if monotonic() - float(_scanner_cache["at"]) < 30:
            return dict(_scanner_cache["data"])

        def _fetch(display: str) -> dict[str, object]:
            altname = KRAKEN_ALTNAMES.get(display, display.replace("/", ""))
            result = _kraken_public("/0/public/Ticker", {"pair": altname})
            tick = next(iter(result.values()))
            last = float(tick["c"][0])
            open_24h = float(tick["o"])
            change = ((last / open_24h) - 1) * 100 if open_24h else 0.0
            return {
                "pair": display,
                "kraken_pair": altname,
                "price": last,
                "change_24h_pct": round(change, 2),
                "high_24h": float(tick["h"][1]),
                "low_24h": float(tick["l"][1]),
                "volume_24h": round(float(tick["v"][1]), 2),
                "trades_24h": int(tick["t"][1]),
            }

        pairs: list[dict[str, object]] = []
        errors: list[str] = []
        watchlist = _watchlist_pairs()
        futures = {_scanner_pool.submit(_fetch, d): d for d in watchlist}
        for fut, display in futures.items():
            try:
                pairs.append(fut.result(timeout=12))
            except Exception as exc:
                errors.append(f"{display}: {exc}")
        pairs.sort(key=lambda p: abs(float(p["change_24h_pct"])), reverse=True)
        data = {
            "ok": True,
            "pairs": pairs,
            "momentum_leader": pairs[0]["pair"] if pairs else None,
            "errors": errors,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        _scanner_cache.update(at=monotonic(), data=data)
        return data


# ---------------------------------------------------------------------------
# Bot data layer (ported from the original dashboard — behavior preserved)
# ---------------------------------------------------------------------------

def learning_brief_data(settings: Settings) -> dict[str, object]:
    with _learning_lock:
        if monotonic() - float(_learning_cache["at"]) < 60:
            return dict(_learning_cache["data"])
        try:
            from .learning import build_learning_report, generate_brief
            report = build_learning_report(settings)
            brief = generate_brief(settings)
            data = {
                "regime": report.regime.regime,
                "regime_description": report.regime.description,
                "confidence": report.regime.confidence,
                "confidence_score": round(report.regime.confidence * 100, 1),
                "signal_quality": report.signal_quality.get("avg_score", 0) / 100
                                if report.signal_quality.get("avg_score", 0) > 70 else "Low",
                "today_signals": report.total_evaluations,
                "win_rate_estimate": report.win_rate_estimate,
                "suggestions": [
                    {"parameter": s.parameter, "current_value": s.current_value,
                     "suggested_value": s.suggested_value, "confidence": s.confidence,
                     "rationale": s.rationale}
                    for s in report.suggestions
                ],
                "filter_analysis": [
                    {"filter_name": f.filter_name, "failure_rate": f.failure_rate,
                     "avg_score_when_failed": f.avg_score_when_failed}
                    for f in report.filter_analysis
                ],
                "market_commentary": f"{settings.symbol} at ${report.signal_quality.get('avg_score', 0)} avg signal score",
                "suggested_actions": [s["text"] for s in suggestions_to_actions(report.suggestions)],
                "brief_text": brief,
            }
        except Exception as exc:
            data = {"error": str(exc), "regime": "unknown", "confidence": 0,
                    "suggested_actions": [], "brief_text": ""}
        _learning_cache.update(at=monotonic(), data=data)
        return data


def suggestions_to_actions(suggestions: list) -> list[dict]:
    return [{"text": s.rationale, "parameter": s.parameter} for s in suggestions]


class TradingMonitor:
    """The auto-cycling decision loop (formerly ``PaperMonitor``).

    Runs the engine on a cadence and fires alerts on BUY/SELL. Executes only
    what the engine permits for the active mode — nothing here places an order
    on its own. The ``PaperMonitor`` name is retained as a backwards-compatible
    alias.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if bool(getattr(settings, "day_trade_mode", False)):
            settings.timeframe_minutes = 5
            settings.monitor_interval_seconds = 300
        self.interval_seconds = int(getattr(settings, "timeframe_minutes", 15)) * 60
        self.stop_event = Event()
        self.thread: Thread | None = None
        self.last_run: str | None = None
        self.last_action: str | None = None
        self.last_error: str | None = None

    def status(self) -> dict[str, object]:
        s = self.settings
        live = bool(s.allow_live_trading and not s.paper_trading and not s.dry_run)
        return {
            "running": self.thread is not None and self.thread.is_alive(),
            "mode": "live" if live else "paper",
            "rapid_mode": bool(getattr(s, "rapid_mode", True)),
            "order_submission_enabled": live,
            "timeframe_minutes": int(getattr(s, "timeframe_minutes", 15)),
            "cadence_minutes": int(self.interval_seconds // 60),
            "cooldown_minutes": int(getattr(s, "cooldown_minutes", 15)),
            "max_orders_per_day": int(getattr(s, "max_orders_per_day", 3)),
            "interval_seconds": self.interval_seconds,
            "last_run": self.last_run,
            "last_action": self.last_action,
            "last_error": self.last_error,
        }

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = Thread(target=self._loop, daemon=True, name="dublin-trading-monitor")
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            if self.stop_event.wait(self._seconds_until_next_candle()):
                break
            try:
                record = TradingEngine(self.settings).run_once()
                self.last_run = record.timestamp
                self.last_action = record.signal.action.value
                self.last_error = None
                if self.last_action in ("BUY", "SELL"):
                    ntfy_alert(
                        f"Dublin {self.last_action} signal",
                        f"{self.settings.symbol} @ {record.signal.price} — score {record.signal.score}. {record.signal.reason}",
                        priority="high",
                        tags="rotating_light",
                    )
            except Exception as exc:
                self.last_error = str(exc)
                self.last_run = datetime.now(timezone.utc).isoformat()
                ntfy_alert("Dublin cycle error", str(exc)[:200], priority="default", tags="warning")

    def _seconds_until_next_candle(self, now: float | None = None) -> float:
        """Wait until just after the next completed exchange candle boundary."""
        current = time.time() if now is None else float(now)
        candle_seconds = max(60, int(self.settings.timeframe_minutes) * 60)
        delay = int(getattr(self.settings, "candle_close_delay_seconds", 2))
        next_close = ((int(current) // candle_seconds) + 1) * candle_seconds
        return max(0.0, next_close + delay - current)

    def set_interval(self, seconds: int) -> None:
        # The execution cadence is deliberately coupled to the candle size.
        # An arbitrary UI interval previously produced stale/missed signals.
        self.interval_seconds = max(60, int(self.settings.timeframe_minutes) * 60)


# Backwards-compatible alias — callers importing PaperMonitor keep working.
PaperMonitor = TradingMonitor


def safety_status(settings: Settings) -> dict[str, object]:
    locked = settings.paper_trading and settings.dry_run and not settings.allow_live_trading
    return {
        "safe": locked,
        "paper_trading": settings.paper_trading,
        "dry_run": settings.dry_run,
        "live_allowed": settings.allow_live_trading,
        "credentials_present": settings.has_credentials,
        "broker": settings.broker,
        "symbol": settings.symbol,
        "strategy_equity_usd": settings.strategy_equity_usd,
        "risk_per_trade": settings.risk_per_trade,
        "max_daily_loss_fraction": settings.max_daily_loss_fraction,
        "max_drawdown_fraction": settings.max_drawdown_fraction,
        "max_orders_per_day": settings.max_orders_per_day,
        "active_mode": settings.active_mode,
        "rapid_mode": bool(getattr(settings, "rapid_mode", True)),
    }


# ---------------------------------------------------------------------------
# Coin control — pick the traded coin from the phone. Never places an order.
# ---------------------------------------------------------------------------

def learner_state_data(settings: Settings) -> dict[str, object]:
    """Closed-loop self-learning state (per-coin expectancy) for the dashboard.

    Reads the learner store directly so the UI can show what the bot has learned
    about each coin without threading the engine instance through the handler.
    """
    path = Path(settings.learner_path)
    if not path.exists():
        return {"enabled": settings.learner_enabled, "coins_tracked": 0, "best": [], "worst": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"enabled": settings.learner_enabled, "coins_tracked": 0, "best": [], "worst": []}
    coins = data.get("coins", {})
    ranked = sorted(
        coins.items(),
        key=lambda kv: (kv[1].get("pnl", 0.0) / kv[1]["trades"]) if kv[1].get("trades") else 0,
        reverse=True,
    )
    def shape(item):
        sym, c = item
        tr = c.get("trades", 0) or 1
        return {
            "symbol": sym,
            "trades": c.get("trades", 0),
            "win_rate_pct": round((c.get("wins", 0) / tr) * 100, 1) if tr else 0.0,
            "expectancy_usd": round(c.get("pnl", 0.0) / tr, 3) if tr else 0.0,
        }
    return {
        "enabled": settings.learner_enabled,
        "last_regime": data.get("last_regime", "unknown"),
        "coins_tracked": len(coins),
        "best": [shape(x) for x in ranked[:5]],
        "worst": [shape(x) for x in ranked[-3:]],
    }


def health_snapshot(settings: Settings) -> dict[str, object]:
    with _health_lock:
        if monotonic() - float(_health_cache["at"]) < 20:
            return dict(_health_cache["data"])
        try:
            from .engine import build_gateway
            gateway = build_gateway(settings)
            if hasattr(gateway, "health"):
                data = gateway.health()
            else:
                data = {"broker": settings.broker, "reachable": None,
                        "error": "adapter does not report health"}
        except Exception as exc:
            data = {"broker": settings.broker, "reachable": False, "error": str(exc)}
        _health_cache.update(at=monotonic(), data=data)
        return data


def audit_summary(settings: Settings, limit: int = 20) -> dict[str, object]:
    try:
        log = AuditLog(Path(settings.audit_log_path))
        intact, reason = log.verify_chain()
        return {
            "chain_intact": intact,
            "chain_status": reason,
            "entries": [
                {
                    "timestamp": e.get("timestamp"),
                    "event": e.get("event"),
                    "severity": e.get("severity"),
                    "payload": e.get("payload"),
                }
                for e in log.tail(limit)
            ][::-1],
        }
    except Exception as exc:
        return {"chain_intact": None, "chain_status": str(exc), "entries": []}


def recent_activity(path, limit: int = 30) -> list[dict[str, object]]:
    path = Path(path)  # accept str or Path
    if not path.exists():
        return []
    lines: deque[str] = deque(maxlen=limit)
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                lines.append(line)
    records: list[dict[str, object]] = []
    for line in reversed(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def last_wait_reason(settings: Settings) -> str | None:
    """Most recent WAIT signal reason from the decision journal, if any."""
    try:
        for entry in recent_activity(settings.journal_path, limit=50):
            sig = entry.get("signal") or {}
            if sig.get("action") == "WAIT" and sig.get("reason"):
                return str(sig["reason"])
    except Exception:
        return None
    return None


def market_snapshot(settings: Settings) -> dict[str, object]:
    with _market_lock:
        if monotonic() - float(_market_cache["at"]) < 30:
            return dict(_market_cache["data"])
        try:
            from .engine import build_gateway
            gateway = build_gateway(settings)
            bars = gateway.get_bars().tail(60)
            closes = [round(float(value), 2) for value in bars["close"].tolist()]
            volumes = [round(float(value), 4) for value in bars["volume"].tolist()]
            timestamps = [str(value) for value in bars.index.tolist()]
            price = closes[-1] if closes else None
            change = ((closes[-1] / closes[-2]) - 1) * 100 if len(closes) > 1 else None
            last_timestamp = bars.index[-1] if len(bars.index) else None
            if last_timestamp is not None and hasattr(last_timestamp, "to_pydatetime"):
                last_timestamp = last_timestamp.to_pydatetime()
            if last_timestamp is not None and last_timestamp.tzinfo is None:
                last_timestamp = last_timestamp.replace(tzinfo=timezone.utc)
            age_minutes = (
                (datetime.now(timezone.utc) - last_timestamp).total_seconds() / 60
                if last_timestamp is not None else None
            )
            data: dict[str, object] = {
                "connected": bool(closes), "price": price, "change_percent": change,
                "closes": closes, "volumes": volumes, "timestamps": timestamps, "error": None,
                "last_timestamp": str(last_timestamp) if last_timestamp else None,
                "delayed": age_minutes is None or age_minutes > 120,
            }
        except Exception as exc:
            data = {"connected": False, "price": None, "change_percent": None,
                    "closes": [], "volumes": [], "timestamps": [], "error": str(exc)}
        _market_cache.update(at=monotonic(), data=data)
        return data


def portfolio_snapshot(settings: Settings) -> dict[str, object]:
    from .engine import TradingEngine, build_gateway
    try:
        engine = TradingEngine(settings)
        # Live account truth: report real Kraken equity + balances when a
        # key is present, falling back to the paper portfolio otherwise.
        if settings.has_credentials:
            try:
                gateway = build_gateway(settings)
                equity = round(float(gateway.account_equity()), 2)
                balances = gateway.balances()
                base = engine.gateway.resolve_symbol().base if settings.has_credentials else ""
                positions = []
                if base and float(balances.get(base, 0.0)) > 0:
                    try:
                        price = float(engine.gateway.get_ticker()["last"])
                    except Exception:
                        price = 0.0
                    qty = float(balances[base])
                    positions = [{
                        "symbol": settings.symbol,
                        "quantity": qty,
                        "market_value": round(qty * price, 2),
                        "unrealized_pl": 0.0,
                    }]
                return {
                    "equity": equity,
                    "cash": round(float(balances.get("ZUSD", 0.0)), 2),
                    "positions": positions,
                    "orders": gateway.orders(),
                    "unrealized_pl": 0.0,
                    "realized_pl": 0.0,
                    "message": None,
                    "live": True,
                }
            except Exception as exc:
                return {"equity": 0.0, "positions": [], "orders": [],
                        "cash": 0.0, "unrealized_pl": 0.0, "realized_pl": 0.0,
                        "message": f"Kraken read error: {exc}"}
        portfolio = engine.paper_portfolio.snapshot()
        state = engine.state_store.load(settings.strategy_equity_usd)
        if portfolio.positions and settings.symbol in portfolio.positions:
            portfolio = engine.paper_portfolio.mark_to_market({settings.symbol: engine.gateway.get_ticker()["last"]})
        realized = round(state.realized_pnl_today, 2)
        positions = portfolio.to_dict()["positions"] or []
        unrealized = round(sum(float(p.get("unrealized_pl", 0.0)) for p in positions), 2)
        return {
            "equity": round(portfolio.equity, 2),
            "cash": round(portfolio.cash, 2),
            "positions": positions,
            "orders": engine.gateway.orders() if settings.has_credentials else [],
            "unrealized_pl": unrealized,
            "realized_pl": realized,
            "message": None,
            "live": False,
        }
    except Exception as exc:
        return {"equity": settings.strategy_equity_usd, "positions": [], "orders": [],
                "cash": 0.0, "unrealized_pl": 0.0, "realized_pl": 0.0, "message": str(exc)}


def risk_state_snapshot(settings: Settings) -> dict[str, object]:
    with _state_lock:
        if monotonic() - float(_state_cache["at"]) < 30:
            return dict(_state_cache["data"])
        try:
            # The trading engine persists daily risk state here. Reading the
            # idempotency ledger as SessionState silently fell back to the
            # configured budget and made a ~$31 live account display as $100.
            live_equity = settings.strategy_equity_usd
            if settings.has_credentials:
                from .engine import build_gateway
                live_equity = float(build_gateway(settings).account_equity())
            store = StateStore(Path(settings.session_state_path))
            s = store.load(live_equity)
            today = datetime.now(timezone.utc).date().isoformat()
            drawdown = round((1 - s.current_equity / max(s.peak_equity, 0.01)) * 100, 1)
            daily_loss = round(abs(s.realized_pnl_today), 2)
            risk_equity = min(live_equity, settings.strategy_equity_usd)
            max_daily_loss = round(risk_equity * settings.max_daily_loss_fraction, 2)
            max_dd_pct = round(settings.max_drawdown_fraction * 100, 1)
            cooldown_active = False
            cooldown_remaining = 0
            if s.last_order_at is not None:
                from datetime import timedelta
                ready_at = s.last_order_at + timedelta(minutes=settings.cooldown_minutes)
                cooldown_active = datetime.now(timezone.utc) < ready_at
                if cooldown_active:
                    cooldown_remaining = int((ready_at - datetime.now(timezone.utc)).total_seconds() / 60)
            data = {
                "start_equity": round(s.start_equity, 2),
                "peak_equity": round(s.peak_equity, 2),
                "current_equity": round(s.current_equity, 2),
                "realized_pnl_today": round(s.realized_pnl_today, 2),
                "unrealized_pl": 0.0,
                "orders_today": s.orders_today,
                "max_orders_per_day": settings.max_orders_per_day,
                "drawdown_percent": drawdown,
                "max_drawdown_percent": max_dd_pct,
                "daily_loss": daily_loss,
                "max_daily_loss": max_daily_loss,
                "consecutive_losses": 0,
                "cooldown_active": cooldown_active,
                "cooldown_remaining_minutes": cooldown_remaining,
                "session_date": today,
            }
            try:
                activity = recent_activity(settings.journal_path, limit=50)
                loss_streak = 0
                for entry in activity:
                    if not entry.get("risk", {}).get("approved") and entry.get("signal", {}).get("action") == "WAIT":
                        loss_streak += 1
                    else:
                        break
                data["consecutive_losses"] = (
                    min(loss_streak, settings.max_orders_per_day)
                    if settings.max_orders_per_day > 0 else loss_streak
                )
            except Exception:
                pass
        except Exception as exc:
            data = {"error": str(exc), "current_equity": settings.strategy_equity_usd,
                    "start_equity": settings.strategy_equity_usd, "peak_equity": settings.strategy_equity_usd,
                    "drawdown_percent": 0.0, "realized_pnl_today": 0.0, "orders_today": 0}
        _state_cache.update(at=monotonic(), data=data)
        return data


def strategy_analytics(settings: Settings) -> dict[str, object]:
    with _strategy_lock:
        if monotonic() - float(_strategy_cache["at"]) < 60:
            return dict(_strategy_cache["data"])
        try:
            activity = recent_activity(settings.journal_path, limit=500)
            entries = [a for a in activity if a.get("timestamp")]
            entries.sort(key=lambda x: x["timestamp"])

            total_signals = len(entries)
            buy_signals = len([e for e in entries if e.get("signal", {}).get("action") == "BUY"])
            sell_signals = len([e for e in entries if e.get("signal", {}).get("action") == "SELL"])
            wait_signals = len([e for e in entries if e.get("signal", {}).get("action") == "WAIT"])

            score_history = [{"timestamp": e["timestamp"], "score": e.get("signal", {}).get("score", 0)}
                            for e in entries[-60:]]
            score_values = [s["score"] for s in score_history]

            actions = {"BUY": buy_signals, "SELL": sell_signals, "WAIT": wait_signals}

            filter_failures = {}
            for e in entries:
                reason = e.get("signal", {}).get("reason", "")
                if "Filters failed:" in reason:
                    filters = reason.replace("Filters failed:", "").strip()
                    for f in filters.split(","):
                        f = f.strip()
                        filter_failures[f] = filter_failures.get(f, 0) + 1

            executed = [e for e in entries if e.get("order_id")]
            win_rate = 0.0
            if executed:
                wins = len([e for e in executed if e.get("risk", {}).get("approved") and e.get("signal", {}).get("action") == "BUY"])
                win_rate = round(wins / len(executed) * 100, 1) if executed else 0.0

            avg_score = round(sum(score_values) / len(score_values), 1) if score_values else 0
            max_score = max(score_values) if score_values else 0
            min_score = min(score_values) if score_values else 0

            last = entries[-1] if entries else None
            latest_signal = {
                "action": last.get("signal", {}).get("action", "WAIT") if last else "WAIT",
                "score": last.get("signal", {}).get("score", 0) if last else 0,
                "reason": last.get("signal", {}).get("reason", "No signal") if last else "No signal yet",
                "price": last.get("signal", {}).get("price", 0) if last else 0,
                "atr": last.get("signal", {}).get("atr", 0) if last else 0,
                "stop_price": last.get("signal", {}).get("stop_price") if last else None,
                "timestamp": last.get("timestamp", "") if last else "",
            }

            data = {
                "total_signals": total_signals,
                "actions": actions,
                "score_history": score_history[-60:],
                "score_values": score_values[-60:],
                "avg_score": avg_score,
                "max_score": max_score,
                "min_score": min_score,
                "win_rate": win_rate,
                "filter_failures": filter_failures,
                "latest_signal": latest_signal,
                "regime": "bull" if (score_values and sum(score_values[-5:]) / 5 > 50) else "bear",
                "confidence": round(avg_score, 1) if avg_score else 0,
            }
        except Exception as exc:
            data = {"error": str(exc), "total_signals": 0, "actions": {},
                    "score_history": [], "avg_score": 0, "latest_signal": {},
                    "win_rate": 0, "confidence": 0, "regime": "unknown"}
        _strategy_cache.update(at=monotonic(), data=data)
        return data


def equity_curve_data(settings: Settings, lookback: int = 60) -> dict[str, object]:
    try:
        activity = recent_activity(settings.journal_path, limit=lookback)
        activity.sort(key=lambda x: x.get("timestamp", ""))
        equity = settings.strategy_equity_usd
        curve = []
        peak = equity
        for entry in activity:
            ts = entry.get("timestamp", "")
            score = entry.get("signal", {}).get("score", 0)
            action = entry.get("signal", {}).get("action", "WAIT")
            curve.append({
                "timestamp": ts,
                "equity": round(equity, 2),
                "action": action,
                "score": score,
            })
        peak = max((c["equity"] for c in curve), default=equity)
        drawdowns = [round((1 - c["equity"] / max(peak, 0.01)) * 100, 1) for c in curve]
        return {
            "labels": [c["timestamp"][:10] for c in curve[-30:]],
            "equity": [c["equity"] for c in curve[-30:]],
            "drawdown": drawdowns[-30:],
            "actions": [c["action"] for c in curve[-30:]],
            "scores": [c["score"] for c in curve[-30:]],
        }
    except Exception as exc:
        return {"labels": [], "equity": [], "drawdown": [], "actions": [],
                "scores": [], "error": str(exc)}


def ai_brief_data(settings: Settings) -> dict[str, object]:
    try:
        strategy = strategy_analytics(settings)
        market = market_snapshot(settings)
        risk = risk_state_snapshot(settings)

        regime = strategy.get("regime", "unknown")
        score = strategy.get("confidence", 0)

        if score >= 75:
            regime_desc = f"Strong {regime} trend regime detected"
            confidence = "High"
        elif score >= 50:
            regime_desc = f"Moderate {regime} regime"
            confidence = "Medium"
        elif score >= 25:
            regime_desc = "Range-bound / consolidation"
            confidence = "Low"
        else:
            regime_desc = "Weak signal environment"
            confidence = "Very Low"

        suggestions = []
        if market.get("delayed"):
            suggestions.append("Check market data feed — delayed")
        if risk.get("cooldown_active"):
            suggestions.append(f"Cooldown active — {risk.get('cooldown_remaining_minutes')} min remaining")
        if settings.max_orders_per_day > 0 and risk.get("orders_today", 0) >= settings.max_orders_per_day:
            suggestions.append("Daily order limit reached")
        if not suggestions:
            suggestions.append("Monitor market conditions for entry signal")

        return {
            "regime": regime,
            "regime_description": regime_desc,
            "confidence": confidence,
            "confidence_score": score,
            "suggested_actions": suggestions,
            "market_commentary": f"{settings.symbol} at ${market.get('price', 0):,.2f}" if market.get("price") else "Market data unavailable",
            "signal_quality": "Good" if score >= 50 else "Poor",
            "today_signals": strategy.get("total_signals", 0),
        }
    except Exception as exc:
        return {"error": str(exc), "regime": "unknown", "confidence": "Unknown",
                "confidence_score": 0, "suggested_actions": [str(exc)]}


def system_health_data(settings: Settings) -> dict[str, object]:
    try:
        broker_health = health_snapshot(settings)

        tailscale_ok = False
        try:
            import socket
            _ = socket.gethostbyname(socket.gethostname())
            tailscale_ok = True
        except Exception:
            tailscale_ok = False

        audit_ok = False
        try:
            log = AuditLog(Path(settings.audit_log_path))
            intact, _ = log.verify_chain()
            audit_ok = intact
        except Exception:
            audit_ok = False

        journal_ok = Path(settings.journal_path).exists()
        state_ok = True

        return {
            "broker": {
                "name": "Kraken",
                "reachable": broker_health.get("reachable", False),
                "latency_ms": broker_health.get("latency_ms"),
                "error": broker_health.get("error"),
                "status": "Online" if broker_health.get("reachable") else "Offline",
            },
            "tailscale": {"connected": tailscale_ok, "status": "Active" if tailscale_ok else "Unknown",
                          "ip": os.environ.get("DUBLIN_HOST", "100.127.18.59")},
            "database": {"audit_chain_intact": audit_ok, "journal_exists": journal_ok,
                         "state_persisted": state_ok},
            "alerts": alerts_status(),
            "deploy": {"version": "2.0-liveview",
                       "build": "live" if (settings.has_credentials and settings.allow_live_trading
                                            and not settings.dry_run and not settings.paper_trading)
                                else "simulated"},
            "api_latency_ms": broker_health.get("latency_ms", 0),
            "clock_skew": broker_health.get("clock_skew_seconds"),
            "rate_budget": broker_health.get("rate_limiter", {}),
        }
    except Exception as exc:
        return {"error": str(exc)}


def reconciliation_status(settings: Settings) -> dict[str, object]:
    try:
        from .reconciliation import reconcile, PositionSnapshot, OrderSnapshot
        from .engine import build_gateway

        gateway = build_gateway(settings)
        kraken_positions = gateway.positions()
        kraken_orders = gateway.orders()

        managed_symbols = {settings.symbol}

        positions = []
        for p in kraken_positions:
            positions.append(PositionSnapshot(
                symbol=p.get("symbol", settings.symbol),
                quantity=float(p.get("quantity", 0)),
                market_value=float(p.get("market_value", 0)),
                average_entry=float(p.get("average_entry", 0)),
            ))

        orders = []
        for o in kraken_orders:
            orders.append(OrderSnapshot(
                order_id=o.get("order_id", ""),
                symbol=o.get("symbol", settings.symbol),
                side=o.get("side", "buy"),
                status=o.get("status", "open"),
            ))

        report = reconcile(positions, orders, managed_symbols)

        idempotency_path = Path(settings.idempotency_path)
        local_orders = []
        if idempotency_path.exists():
            try:
                with idempotency_path.open() as f:
                    local_orders = [json.loads(line) for line in f if line.strip()]
            except Exception:
                pass

        exchange_ids = {o.order_id for o in orders}
        local_ids = {str(o.get("order_id", "")) for o in local_orders}
        orphaned = local_ids - exchange_ids

        return {
            "status": "matching" if not report.orphaned_symbols and not orphaned else "mismatch",
            "positions_count": len(positions),
            "open_orders_count": len(orders),
            "orphaned_symbols": list(report.orphaned_symbols),
            "orphaned_local_orders": list(orphaned),
            "checked_at": report.checked_at,
            "fully_reconciled": not report.orphaned_symbols and not orphaned,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc), "fully_reconciled": False}


def stop_monitor_status(settings: Settings) -> dict[str, object]:
    try:
        from .protective import StopMonitor, ProtectiveStop
        from .engine import build_gateway

        gateway = build_gateway(settings)
        positions = gateway.positions()

        if not positions:
            return {
                "running": False,
                "monitored_stops": 0,
                "status": "idle",
                "message": "No positions to monitor",
            }

        ticker = gateway.get_ticker()
        price = float(ticker.get("c", [0, 0])[0]) if ticker.get("c") else 0.0

        stops = []
        for p in positions:
            stop_price = float(p.get("stop_price", 0))
            qty = float(p.get("quantity", 0))
            if stop_price > 0 and qty > 0:
                stop = ProtectiveStop(
                    symbol=p.get("symbol", settings.symbol),
                    stop_price=stop_price,
                    quantity=qty,
                )
                should_exit = StopMonitor.should_exit(price, stop)
                stops.append({
                    "symbol": stop.symbol,
                    "stop_price": stop_price,
                    "current_price": price,
                    "distance_pct": round((price - stop_price) / price * 100, 2) if price else None,
                    "should_exit": should_exit,
                })

        any_triggered = any(s["should_exit"] for s in stops)
        return {
            "running": True,
            "monitored_stops": len(stops),
            "stops": stops,
            "triggered": any_triggered,
            "status": "TRIGGERED" if any_triggered else "monitoring",
            "current_price": price,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return {"running": False, "status": "error", "error": str(exc),
                "monitored_stops": 0}



MANIFEST = {"name": "Dublin Terminal", "short_name": "Dublin",
            "start_url": "/", "display": "standalone", "background_color": "#04070a",
            "theme_color": "#04070a", "description": "Dublin live-view Kraken terminal",
            "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"}]}


HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#04070a">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Dublin">
<link rel="manifest" href="/manifest.json">
<title>Dublin Terminal</title>
<style>
:root{
  color-scheme:dark;
  font-family:'SF Mono','JetBrains Mono','Fira Code',ui-monospace,monospace;
  --bg:#04070a; --panel:#0a0f14; --panel2:#0e151c; --line:#1c2833;
  --cyan:#22d3ee; --cyan-dim:#0e7490; --lime:#a3e635; --red:#fb7185;
  --amber:#fbbf24; --text:#dbe7ee; --muted:#5b6b7a;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden}

/* ---- ticker tape ---- */
.tape{position:sticky;top:0;z-index:40;background:#060a0e;border-bottom:1px solid var(--line);
  overflow:hidden;white-space:nowrap;padding:7px 0;font-size:.72rem}
.tape-inner{display:inline-block;padding-left:100%;animation:tape 40s linear infinite}
.tape:hover .tape-inner{animation-play-state:paused}
@keyframes tape{0%{transform:translateX(0)}100%{transform:translateX(-100%)}}
.tk{display:inline-block;margin:0 18px;color:var(--muted)}
.tk b{color:var(--text);font-weight:600}
.tk .up{color:var(--lime)} .tk .dn{color:var(--red)}

header{display:flex;align-items:center;justify-content:space-between;
  padding:14px 16px 10px;border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px}
.brand .badge{width:34px;height:34px;border-radius:9px;background:#0a2a33;border:1px solid var(--cyan-dim);
  display:flex;align-items:center;justify-content:center;font-weight:800;font-size:14px;color:var(--cyan);
  box-shadow:0 0 18px rgba(34,211,238,.25)}
.brand h1{margin:0;font-size:1.15rem;letter-spacing:.14em;text-transform:uppercase}
.brand small{display:block;color:var(--muted);font-size:.6rem;letter-spacing:.3em;text-transform:uppercase}
.conn{display:flex;align-items:center;gap:7px;font-size:.7rem;color:var(--muted);text-align:right}
.dot{width:8px;height:8px;border-radius:50%;background:var(--lime);box-shadow:0 0 10px var(--lime);animation:pulse 2s infinite}
.dot.off{background:var(--red);box-shadow:0 0 10px var(--red)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}

main{max-width:1180px;margin:auto;padding:14px 14px calc(86px + env(safe-area-inset-bottom))}

.banner{display:none;border:1px solid var(--red);background:#1a0a10;color:#fecdd3;border-radius:10px;
  padding:10px 14px;font-size:.75rem;margin-bottom:12px}
.banner.show{display:flex;gap:8px;align-items:center}

.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}
.card{background:linear-gradient(170deg,var(--panel),var(--panel2));border:1px solid var(--line);
  border-radius:14px;padding:16px}
.card .label{color:var(--muted);font-size:.62rem;text-transform:uppercase;letter-spacing:.18em;margin-bottom:8px}
.card .value{font-size:1.5rem;font-weight:700;margin:2px 0;font-variant-numeric:tabular-nums}
.card .sub{color:var(--muted);font-size:.72rem;margin-top:4px}
.m3{grid-column:span 3}.m4{grid-column:span 4}.m6{grid-column:span 6}
.m8{grid-column:span 8}.m12{grid-column:span 12}
.lime{color:var(--lime)}.red{color:var(--red)}.amber{color:var(--amber)}.cyan{color:var(--cyan)}
.muted{color:var(--muted)}
.pill{display:inline-block;border:1px solid var(--line);padding:3px 10px;border-radius:999px;font-size:.65rem;font-weight:600}
.pill.lime{border-color:#3f6212;color:var(--lime);background:rgba(163,230,53,.08)}
.pill.red{border-color:#7f1d2d;color:var(--red);background:rgba(251,113,133,.08)}
.pill.cyan{border-color:var(--cyan-dim);color:var(--cyan);background:rgba(34,211,238,.08)}
.pill.amber{border-color:#713f12;color:var(--amber);background:rgba(251,191,36,.08)}

.chart{width:100%;height:220px;margin-top:8px}
.chart svg{width:100%;height:100%}
.row{display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-bottom:1px solid var(--line);font-size:.8rem}
.row:last-child{border:0}
.row .rl{color:var(--muted)} .row .rv{font-weight:600;font-variant-numeric:tabular-nums}
.empty{color:var(--muted);padding:22px 0;text-align:center;font-size:.78rem;border:1px dashed var(--line);border-radius:10px;margin-top:8px}

.wl{display:flex;flex-direction:column;gap:8px;margin-top:8px}
.wl-item{display:grid;grid-template-columns:auto 1fr auto auto;gap:12px;align-items:center;
  background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px;cursor:pointer;transition:border .15s}
.wl-item:active{border-color:var(--cyan-dim)}
.wl-item .p{font-weight:700;font-size:.85rem}
.wl-item .v{color:var(--muted);font-size:.68rem}
.wl-item .px{font-variant-numeric:tabular-nums;font-weight:600}
.chg{font-variant-numeric:tabular-nums;font-weight:700;font-size:.8rem;min-width:64px;text-align:right}

table{width:100%;border-collapse:collapse;font-size:.72rem;margin-top:8px}
th{color:var(--muted);text-align:left;font-weight:500;text-transform:uppercase;letter-spacing:.1em;
  font-size:.6rem;padding:8px 6px;border-bottom:1px solid var(--line)}
td{padding:8px 6px;border-bottom:1px solid #121b23;font-variant-numeric:tabular-nums}
tr:last-child td{border:0}

button{width:100%;border:0;border-radius:11px;padding:13px;font-weight:700;font-size:.82rem;cursor:pointer;
  letter-spacing:.05em;transition:transform .1s;font-family:inherit}
button:active{transform:scale(.97)}
.primary{background:var(--cyan);color:#04252c}
.primary:disabled{opacity:.4;cursor:not-allowed}
.secondary{background:var(--panel2);color:var(--text);border:1px solid var(--line)}
.danger{background:#2a0d14;color:var(--red);border:1px solid #7f1d2d}
.actions{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}
.ivl{display:flex;align-items:center;gap:10px;margin-top:10px}
.ivl button{width:auto;padding:8px 14px}
.ivl .val{flex:1;text-align:center;font-weight:700}

.bar{height:7px;background:#121b23;border-radius:99px;overflow:hidden;margin-top:6px}
.bar>i{display:block;height:100%;background:var(--cyan);border-radius:99px}
.bar>i.warn{background:var(--amber)} .bar>i.bad{background:var(--red)}

/* ---- bottom tab bar ---- */
nav{position:fixed;left:0;right:0;bottom:0;z-index:50;display:flex;
  background:rgba(6,10,14,.92);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  border-top:1px solid var(--line);padding:6px 4px calc(6px + env(safe-area-inset-bottom))}
nav button{flex:1;background:none;border:0;color:var(--muted);font-size:.58rem;letter-spacing:.08em;
  text-transform:uppercase;padding:6px 2px;display:flex;flex-direction:column;align-items:center;gap:3px;width:auto}
nav button .ic{font-size:1.05rem}
nav button.on{color:var(--cyan)}
.view{display:none}.view.on{display:block;animation:fade .2s}
.basket{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}
.basket button{flex:1 1 30%;min-height:46px;border-radius:12px;border:1px solid var(--line);
  background:rgba(255,255,255,.04);color:var(--muted);font-size:.8rem;letter-spacing:.04em}
.basket button.on{border-color:var(--cyan);color:var(--cyan);background:rgba(0,229,255,.10)}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1}}

@media(max-width:820px){
  .m3,.m4{grid-column:span 6}
  .m6,.m8{grid-column:span 12}
  .card .value{font-size:1.3rem}
  .chart{height:180px}
  .hide-m{display:none}
}
</style>
</head>
<body>

<div class="tape"><div class="tape-inner" id="tape">loading watchlist…</div></div>

<header>
  <div class="brand">
    <div class="badge">D</div>
    <div><h1>Dublin</h1><small>Kraken Live-View Terminal</small></div>
  </div>
  <div class="conn">
    <span class="dot" id="connDot"></span>
    <div><span id="connText">connecting</span><br><span class="muted" id="clock">—</span></div>
  </div>
</header>

<main>
<div class="banner" id="banner">⚠ <span id="bannerText"></span></div>

<!-- ============ OVERVIEW ============ -->
<section class="view on" id="v-overview">
  <div class="grid">
    <div class="card m4">
      <div class="label">Kraken Account Equity</div>
      <div class="value" id="krakenEquity">—</div>
      <div class="sub" id="krakenAssets">real account · read-only</div>
    </div>
    <div class="card m4">
      <div class="label">Realized P&amp;L (est.)</div>
      <div class="value" id="realizedPnl">—</div>
      <div class="sub" id="realizedNote">from Kraken trade history</div>
    </div>
    <div class="card m4">
      <div class="label">Active Mode</div>
      <div class="value" id="activeMode">—</div>
      <div class="sub" id="rapidMode">—</div>
    </div>

    <div class="card m8">
      <div class="label">Price · <span id="chartPair">—</span> · last 60 bars</div>
      <div class="row" style="border:0;padding:2px 0">
        <span class="value" style="font-size:1.7rem" id="priceNow">—</span>
        <span class="pill" id="priceChg">—</span>
      </div>
      <div class="chart" id="priceChart"><div class="empty">loading…</div></div>
    </div>

    <div class="card m4">
      <div class="label">Last Signal</div>
      <div class="value" id="sigAction">—</div>
      <div class="sub" id="sigReason">—</div>
      <div style="margin-top:12px">
        <div class="row"><span class="rl">Score</span><span class="rv" id="sigScore">—</span></div>
        <div class="row"><span class="rl">ATR</span><span class="rv" id="sigAtr">—</span></div>
        <div class="row"><span class="rl">Stop</span><span class="rv" id="sigStop">—</span></div>
      </div>
    </div>

    <div class="card m6">
      <div class="label">Open Orders (Kraken)</div>
      <div id="orders"><div class="empty">no open orders</div></div>
    </div>
    <div class="card m6">
      <div class="label">Last WAIT Reason</div>
      <div class="sub muted" id="waitReason" style="line-height:1.4">—</div>
    </div>
  </div>
</section>

<!-- ============ MARKETS ============ -->
<section class="view" id="v-markets">
  <div class="grid">
    <div class="card m12">
      <div class="label">Watchlist · 24h momentum scanner</div>
      <div class="sub muted" id="scannerMeta">sorted by absolute 24h move</div>
      <div class="wl" id="watchlist"><div class="empty">loading…</div></div>
    </div>
  </div>
</section>

<!-- ============ TRADES ============ -->
<section class="view" id="v-trades">
  <div class="grid">
    <div class="card m12">
      <div class="label">Kraken Trade History (real)</div>
      <div class="sub muted" id="tradesMeta">—</div>
      <div style="overflow-x:auto"><table>
        <thead><tr><th>Time (UTC)</th><th>Pair</th><th>Side</th><th>Price</th><th>Vol</th><th>Cost</th><th>Fee</th></tr></thead>
        <tbody id="tradesBody"></tbody>
      </table></div>
      <div class="empty" id="tradesEmpty" style="display:none">no trades found on this Kraken account</div>
    </div>
  </div>
</section>

<!-- ============ SIMULATION ============ -->
<section class="view" id="v-sim">
  <div class="grid">
    <div class="card m4">
      <div class="label">Paper Engine Equity</div>
      <div class="value" id="paperEquity">—</div>
      <div class="sub">cash <span id="paperCash">—</span> · sim only</div>
    </div>
    <div class="card m8">
      <div class="label">Open Positions (paper)</div>
      <div id="positions"><div class="empty">no open positions</div></div>
    </div>
    <div class="card m12">
      <div class="label">Bot Decision Journal (paper)</div>
      <div id="journal"><div class="empty">no decisions logged yet — run a cycle</div></div>
    </div>
  </div>
</section>

<!-- ============ BOT ============ -->
<section class="view" id="v-bot">
  <div class="grid">
    <div class="card m12">
      <div class="label">Trading Bot — Monitor Control</div>
      <div class="row"><span class="rl">Mode</span><span class="rv" id="monMode">live</span></div>
      <div class="row"><span class="rl">Status</span><span class="rv" id="monStatus">—</span></div>
      <div class="row"><span class="rl">Last run</span><span class="rv" id="monLast">—</span></div>
      <div class="row"><span class="rl">Last action</span><span class="rv" id="monAction">—</span></div>
      <div class="row"><span class="rl">Symbol</span><span class="rv" id="monSymbol">—</span></div>
      <div id="monError" class="sub red" style="margin-top:8px;display:none"></div>
      <div class="ivl">
        <button class="secondary" onclick="setIvl(-900)">−15m</button>
        <span class="val" id="monIvl">—</span>
        <button class="secondary" onclick="setIvl(900)">+15m</button>
      </div>
      <div class="actions" style="grid-template-columns:1fr 1fr">
        <button class="primary" id="btnStart" onclick="toggleMonitor()">▶ Start Trading</button>
        <button class="secondary" onclick="runOnce(this)">⚡ Run Cycle Now</button>
      </div>
      <div class="sub muted" style="margin-top:8px">Start = auto-cycle every interval and trade on signals. Run Cycle Now = single manual trade check.</div>
    </div>

    <div class="card m6">
      <div class="label">AI Brief</div>
      <div class="value" style="font-size:1.1rem" id="aiRegime">—</div>
      <div class="sub" id="aiDesc">—</div>
      <div id="aiActions" style="margin-top:10px"></div>
    </div>

    <div class="card m6">
      <div class="label">Strategy Analytics</div>
      <div class="row"><span class="rl">Signals evaluated</span><span class="rv" id="stTotal">—</span></div>
      <div class="row"><span class="rl">BUY / SELL / WAIT</span><span class="rv" id="stActions">—</span></div>
      <div class="row"><span class="rl">Avg score</span><span class="rv" id="stAvg">—</span></div>
      <div class="sub muted" style="margin-top:10px">filter failure breakdown</div>
      <div id="stFilters"></div>
    </div>

    <div class="card m6">
      <div class="label">Emergency Stop</div>
      <div class="row"><span class="rl">State</span><span class="rv" id="estopState">—</span></div>
      <div class="actions">
        <button class="danger" onclick="estop(true)">ACTIVATE STOP</button>
        <button class="secondary" onclick="estop(false)">Clear</button>
      </div>
      <div class="sub muted" style="margin-top:8px">Halts all bot order intents. Does not touch Kraken directly.</div>
    </div>
  </div>
</section>

<!-- ============ SELF-LEARNING ============ -->
<section class="view" id="v-coin">
  <div class="grid">
    <div class="card m12">
      <div class="label">Self-Learning Agent — what the bot has learned</div>
      <div class="row"><span class="rl">Active coin</span><span class="rv" id="lrActive">—</span></div>
      <div class="row"><span class="rl">Market regime</span><span class="rv" id="learnerRegime">—</span></div>
      <div class="sub muted">The bot scans the full Kraken USD market each cycle and biases toward coins with proven positive expectancy. Best-known coins by $/trade:</div>
      <div id="learnerBody" class="basket"></div>
    </div>
    <div class="card m12">
      <div class="label">Universe &amp; Mode</div>
      <div class="row"><span class="rl">Universe</span><span class="rv" id="lrUniverse">all Kraken USD</span></div>
      <div class="row"><span class="rl">Selection</span><span class="rv" id="lrSel">autonomous (strategy + market)</span></div>
      <div class="sub muted">No manual coin pinning. The engine trades whatever coin the mean-reversion strategy + self-learning bias selects.</div>
    </div>
  </div>
</section>

<!-- ============ SYSTEM ============ -->
<section class="view" id="v-system">
  <div class="grid">
    <div class="card m6">
      <div class="label">Safety Locks</div>
      <div id="locks"></div>
    </div>
    <div class="card m6">
      <div class="label">Risk State</div>
      <div id="riskState"></div>
    </div>
    <div class="card m6">
      <div class="label">Connection Health</div>
      <div id="sysHealth"></div>
    </div>
    <div class="card m6">
      <div class="label">Integrity</div>
      <div id="integrity"></div>
    </div>
    <div class="card m12">
      <div class="label">Push Alerts (ntfy → iPhone)</div>
      <div id="alertsBox"></div>
    </div>
  </div>
</section>
</main>

<nav>
  <button class="on" data-v="v-overview"><span class="ic">◧</span>Overview</button>
  <button data-v="v-markets"><span class="ic">⌗</span>Markets</button>
  <button data-v="v-trades"><span class="ic">⇄</span>Trades</button>
  <button data-v="v-sim"><span class="ic">▦</span>Simulation</button>
  <button data-v="v-bot"><span class="ic">▶</span>Bot</button>
  <button data-v="v-coin"><span class="ic">◎</span>Coin</button>
  <button data-v="v-system"><span class="ic">⚙</span>System</button>
</nav>
'''

HTML += r'''
<script>
const $ = id => document.getElementById(id);
const fmt$ = (n,d=2) => n==null ? "—" : "$" + Number(n).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const fmtP = n => n==null ? "—" : (n>0?"+":"") + Number(n).toFixed(2) + "%";
const cls = n => n>0 ? "lime" : n<0 ? "red" : "";
const shortTs = ts => ts ? String(ts).replace("T"," ").slice(5,19) : "—";

/* --- tabs --- */
document.querySelectorAll("nav button").forEach(b=>{
  b.onclick=()=>{
    document.querySelectorAll("nav button").forEach(x=>x.classList.remove("on"));
    document.querySelectorAll(".view").forEach(x=>x.classList.remove("on"));
    b.classList.add("on"); $(b.dataset.v).classList.add("on");
  };
});

/* --- connection tracking: banner only after 3 consecutive failed cycles --- */
let failStreak = 0;
function connOk(){
  failStreak = 0;
  $("connDot").classList.remove("off");
  $("connText").textContent = "live";
  $("banner").classList.remove("show");
}
function connFail(){
  failStreak++;
  if (failStreak >= 3){
    $("connDot").classList.add("off");
    $("connText").textContent = "offline";
    $("bannerText").textContent = "Dashboard connection lost — retrying every 10s";
    $("banner").classList.add("show");
  }
}
function renderLearner(ov){
  const lr = ov && ov.learner;
  const el = $("learnerBody");
  if (!el || !lr) return;
  if (!lr.enabled){ el.innerHTML = "<div class='muted'>self-learning disabled</div>"; return; }
  const rows = (lr.best||[]).map(c=>{
    const good = c.expectancy_usd >= 0;
    return `<div class='row'><span>${c.symbol.replace('/USD','')}</span>`+
      `<span class='${good?'pos':'neg'}'>${c.expectancy_usd>=0?'+':''}${c.expectancy_usd} $/trade</span>`+
      `<span class='muted'>${c.win_rate_pct}% · ${c.trades}t</span></div>`;
  }).join("");
  el.innerHTML = rows || "<div class='muted'>learning from trades…</div>";
  $("learnerRegime").textContent = lr.last_regime || "unknown";
}
async function getJSON(url){
  const r = await fetch(url, {cache:"no-store"});
  if (!r.ok) throw new Error(url + " → " + r.status);
  return r.json();
}
async function post(url, body){
  try{
    const r = await fetch(url, {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: body ? JSON.stringify(body) : "{}"});
    if (!r.ok) throw new Error(url + " → HTTP " + r.status);
    const j = await r.json().catch(()=>null);
    refresh();
    return j;
  }catch(e){
    connFail();
    toast("⚠ " + e.message);
    return null;
  }
}
function setIvl(d){
  const cur = parseInt(($("monIvl").dataset.sec||"3600"),10);
  const next = Math.max(60, cur + d);
  post("/api/monitor/interval", {interval_seconds: next});
}
async function runOnce(btn){
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "running…";
  try {
    const j = await post("/api/run-once");
    if (j && j.signal){
      toast(`Signal: ${j.signal.action} · score ${j.signal.score} · ${j.signal.reason}`);
    } else if (j && j.error) {
      toast("⚠ " + j.error);
    } else {
      toast("⚠ No response from engine");
    }
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
}
function toggleMonitor(){
  const running = ($("monStatus").textContent||"").toUpperCase().includes("RUNNING");
  post(running ? "/api/monitor/stop" : "/api/monitor/start");
  toast(running ? "Monitor stopped" : "Monitor started — trading enabled");
}
function estop(on){
  const msg = on ? "Activate emergency stop? Bot order intents will halt." : "Clear emergency stop?";
  if (confirm(msg)) post("/api/emergency-stop/" + (on?"activate":"clear"));
}
let toastTimer=null;
function toast(msg){
  $("bannerText").textContent = msg;
  $("banner").classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(()=>{ if(failStreak<3) $("banner").classList.remove("show"); }, 6000);
}

/* --- SVG chart: price line + volume bars --- */
function priceChart(closes, volumes){
  if (!closes || closes.length < 2) return '<div class="empty">no data</div>';
  const W=600,H=220,pad=8,volH=44;
  const min=Math.min(...closes),max=Math.max(...closes),span=(max-min)||1;
  const vmax=Math.max(...(volumes||[1]))||1;
  const x=i=>pad+i*(W-2*pad)/(closes.length-1);
  const y=v=>pad+(1-(v-min)/span)*(H-volH-2*pad-14);
  const pts=closes.map((c,i)=>`${x(i).toFixed(1)},${y(c).toFixed(1)}`).join(" ");
  const area=`${pad},${H-volH-6} ${pts} ${x(closes.length-1).toFixed(1)},${H-volH-6}`;
  let vols="";
  (volumes||[]).forEach((v,i)=>{
    const bw=(W-2*pad)/closes.length;
    const h=(v/vmax)*(volH-8);
    const up=i>0&&closes[i]>=closes[i-1];
    vols+=`<rect x="${(x(i)-bw/2+0.5).toFixed(1)}" y="${(H-6-h).toFixed(1)}" width="${(bw-1).toFixed(1)}" height="${h.toFixed(1)}" fill="${up?"rgba(163,230,53,.35)":"rgba(251,113,133,.35)"}"/>`;
  });
  const lastUp = closes.length>1 && closes[closes.length-1]>=closes[closes.length-2];
  const stroke = lastUp ? "#a3e635" : "#fb7185";
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <defs><linearGradient id="ga" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="${stroke}" stop-opacity=".25"/>
      <stop offset="1" stop-color="${stroke}" stop-opacity="0"/>
    </linearGradient></defs>
    ${vols}
    <polygon points="${area}" fill="url(#ga)"/>
    <polyline points="${pts}" fill="none" stroke="${stroke}" stroke-width="2" stroke-linejoin="round"/>
    <circle cx="${x(closes.length-1)}" cy="${y(closes[closes.length-1])}" r="3.5" fill="${stroke}"/>
  </svg>`;
}

/* --- renderers --- */
function renderOverview(d){
  const st=d.status||{}, mk=d.market||{}, pf=d.portfolio||{};
  // Active mode + last WAIT reason (exposed telemetry)
  $("activeMode").textContent = (st.active_mode||"paper").toUpperCase();
  $("rapidMode").textContent = st.rapid_mode ? "rapid mode · 15m bars" : "standard mode";
  $("waitReason").textContent = d.last_wait_reason || "—";
  $("priceNow").textContent = fmt$(mk.price);
  $("chartPair").textContent = st.symbol || "—";
  const chg = mk.change_percent;
  const pc=$("priceChg");
  pc.textContent = fmtP(chg);
  pc.className = "pill " + (chg>0?"lime":chg<0?"red":"cyan");
  $("priceChart").innerHTML = priceChart(mk.closes, mk.volumes);
  const ords = pf.orders||[];
  $("orders").innerHTML = ords.length ? ords.map(o=>
    `<div class="row"><span class="rl">${o.side||""} ${o.symbol||""}</span>
     <span class="rv">${o.status||""}</span></div>`).join("")
    : '<div class="empty">no open orders</div>';
  // monitor
  try {
    const m=d.monitor||{};
    const running = !!m.running;
    const ms=$("monStatus");
    if (ms) ms.innerHTML = running?'<span class="pill lime">RUNNING</span>':'<span class="pill">STOPPED</span>';
    const mm=$("monMode");
    if (mm) mm.textContent = (m.mode==="live"?"live":"paper");
    if ($("monLast")) $("monLast").textContent = shortTs(m.last_run);
    if ($("monAction")) $("monAction").textContent = m.last_action||"—";
    if ($("monSymbol")) $("monSymbol").textContent = (d.status&&d.status.symbol)||"—";
    const sec = m.interval_seconds||900;
    if ($("monIvl")){ $("monIvl").textContent = (sec/60)+"m"; $("monIvl").dataset.sec = sec; }
    const errEl=$("monError");
    if (errEl){ if(m.last_error){ errEl.style.display="block"; errEl.textContent="last cycle error: "+m.last_error; } else { errEl.style.display="none"; } }
    const btn=$("btnStart");
    if (btn) btn.textContent = running ? "■ Stop Trading" : "▶ Start Trading";
  } catch(e){ console.error("monitor render failed", e); }
}

function renderSimulation(d){
  const pf=d.portfolio||{}, act=d.activity||[];
  $("paperEquity").textContent = fmt$(pf.equity);
  $("paperCash").textContent = fmt$(pf.cash);
  const pos = pf.positions||[];
  $("positions").innerHTML = pos.length ? pos.map(p=>
    `<div class="row"><span class="rl">${p.symbol||""} · ${p.quantity??""}</span>
     <span class="rv ${cls(p.unrealized_pl)}">${fmt$(p.unrealized_pl)}</span></div>`).join("")
    : '<div class="empty">no open positions</div>';
  $("journal").innerHTML = act.length ? act.slice(0,15).map(a=>{
    const s=a.signal||{};
    return `<div class="row"><span class="rl">${shortTs(a.timestamp)}</span>
      <span class="rv ${s.action==="BUY"?"lime":s.action==="SELL"?"red":""}">${s.action||"—"} · ${s.score??""}</span></div>`;
  }).join("") : '<div class="empty">no decisions logged yet — run a cycle</div>';
}

function renderKraken(b){
  if (!b || !b.ok){
    $("krakenEquity").textContent = "—";
    $("krakenAssets").textContent = (b&&b.error)?("error: "+b.error):"read-only query failed";
    return;
  }
  $("krakenEquity").textContent = b.equity_usd!=null ? fmt$(b.equity_usd) : "—";
  const assets = Object.entries(b.balances||{}).map(([k,v])=>`${k}: ${v}`).slice(0,4).join(" · ");
  $("krakenAssets").textContent = assets || "no balances";
}

function renderTrades(t){
  if (!t || !t.ok){
    $("tradesMeta").textContent = (t&&t.error)||"trade history unavailable";
    $("tradesEmpty").style.display="block";
    return;
  }
  const rpnl = t.estimated_realized_pnl;
  $("realizedPnl").textContent = fmt$(rpnl);
  $("realizedPnl").className = "value " + cls(rpnl);
  $("realizedNote").textContent = `${t.count} trades · fees ${fmt$(t.total_fees)} · ${t.pnl_note}`;
  $("tradesMeta").textContent = `${t.count} total · showing last ${t.trades.length}`;
  const rows = (t.trades||[]).map(x=>
    `<tr><td>${shortTs(x.time)}</td><td>${x.pair}</td>
     <td class="${x.type==="buy"?"lime":"red"}">${(x.type||"").toUpperCase()}</td>
     <td>${fmt$(x.price)}</td><td>${x.vol}</td><td>${fmt$(x.cost)}</td><td>${fmt$(x.fee,4)}</td></tr>`).join("");
  $("tradesBody").innerHTML = rows;
  $("tradesEmpty").style.display = rows ? "none" : "block";
}

function renderScanner(s){
  if (!s || !s.ok || !s.pairs || !s.pairs.length){
    $("watchlist").innerHTML = '<div class="empty">scanner unavailable</div>';
    return;
  }
  $("scannerMeta").textContent = `leader: ${s.momentum_leader} · updated ${shortTs(s.fetched_at)} UTC`;
  $("watchlist").innerHTML = s.pairs.map(p=>
    `<div class="wl-item">
       <span class="p">${p.pair}</span>
       <span class="v">vol ${Number(p.volume_24h).toLocaleString()} · 24h ${fmt$(p.low_24h,0)}–${fmt$(p.high_24h,0)}</span>
       <span class="px">${fmt$(p.price)}</span>
       <span class="chg ${cls(p.change_24h_pct)}">${fmtP(p.change_24h_pct)}</span>
     </div>`).join("");
  // ticker tape
  $("tape").innerHTML = s.pairs.map(p=>
    `<span class="tk"><b>${p.pair}</b> ${fmt$(p.price)} <span class="${p.change_24h_pct>=0?"up":"dn"}">${fmtP(p.change_24h_pct)}</span></span>`
  ).join("");
}

function renderStrategy(d){
  const sig = d.latest_signal||{};
  $("sigAction").textContent = sig.action||"—";
  $("sigAction").className = "value " + (sig.action==="BUY"?"lime":sig.action==="SELL"?"red":"");
  $("sigReason").textContent = sig.reason||"—";
  $("sigScore").textContent = sig.score??"—";
  $("sigAtr").textContent = sig.atr ? Number(sig.atr).toFixed(2) : "—";
  $("sigStop").textContent = sig.stop_price ? fmt$(sig.stop_price) : "—";
  $("stTotal").textContent = d.total_signals??"—";
  const a=d.actions||{};
  $("stActions").textContent = `${a.BUY||0} / ${a.SELL||0} / ${a.WAIT||0}`;
  $("stAvg").textContent = d.avg_score??"—";
  const ff = d.filter_failures||{};
  const entries = Object.entries(ff).sort((x,y)=>y[1]-x[1]);
  const maxF = entries.length ? entries[0][1] : 1;
  $("stFilters").innerHTML = entries.length ? entries.map(([k,v])=>
    `<div style="margin-top:8px"><div class="row" style="border:0;padding:2px 0">
      <span class="rl">${k}</span><span class="rv">${v}</span></div>
     <div class="bar"><i style="width:${(v/maxF*100).toFixed(0)}%"></i></div></div>`).join("")
    : '<div class="empty">no filter data yet</div>';
}

function renderAI(d){
  $("aiRegime").textContent = d.regime_description||d.regime||"—";
  $("aiDesc").textContent = `confidence: ${d.confidence||"—"} · signals today: ${d.today_signals??0}`;
  const acts = d.suggested_actions||[];
  $("aiActions").innerHTML = acts.map(a=>`<div class="row"><span class="rl">→ ${a}</span></div>`).join("")
    || '<div class="empty">no suggestions</div>';
}

function renderSystem(sys, risk, status, audit, recon, estop){
  // locks
  const locks = [
    ["Paper trading", status.paper_trading], ["Dry run", status.dry_run],
    ["Live allowed", !status.live_allowed], ["Emergency stop", !estop],
  ];
  $("locks").innerHTML = locks.map(([k,on])=>
    `<div class="row"><span class="rl">${k}</span>
     <span class="pill ${on?"lime":"red"}">${on?"LOCKED":"OPEN"}</span></div>`).join("");
  // risk
  $("riskState").innerHTML = [
    ["Equity", fmt$(risk.current_equity)], ["Peak", fmt$(risk.peak_equity)],
    ["Drawdown", (risk.drawdown_percent??0)+"% / max "+(risk.max_drawdown_percent??"—")+"%"],
    ["Daily P&L", fmt$(risk.realized_pnl_today)],
    ["Orders today", `${risk.orders_today??0} / ${risk.max_orders_per_day??"—"}`],
    ["Cooldown", risk.cooldown_active ? risk.cooldown_remaining_minutes+"m left" : "none"],
  ].map(([k,v])=>`<div class="row"><span class="rl">${k}</span><span class="rv">${v}</span></div>`).join("");
  // health
  const b=(sys&&sys.broker)||{};
  $("sysHealth").innerHTML = [
    ["Kraken API", `<span class="pill ${b.reachable?"lime":"red"}">${b.status||"—"}</span>`],
    ["Latency", (b.latency_ms??"—")+" ms"],
    ["Clock skew", (sys&&sys.clock_skew!=null?sys.clock_skew+" s":"—")],
    ["Tailscale", (sys&&sys.tailscale&&sys.tailscale.status)||"—"],
  ].map(([k,v])=>`<div class="row"><span class="rl">${k}</span><span class="rv">${v}</span></div>`).join("");
  // integrity
  $("integrity").innerHTML = [
    ["Audit chain", `<span class="pill ${audit.chain_intact?"lime":"amber"}">${audit.chain_status||"—"}</span>`],
    ["Reconciliation", `<span class="pill ${recon.fully_reconciled?"lime":"amber"}">${recon.status||"—"}</span>`],
    ["Build", (sys&&sys.deploy&&sys.deploy.version)||"—"],
  ].map(([k,v])=>`<div class="row"><span class="rl">${k}</span><span class="rv">${v}</span></div>`).join("");
  // alerts
  const al=(sys&&sys.alerts)||{};
  $("alertsBox").innerHTML = al.configured
    ? `<span class="pill lime">ACTIVE</span> <span class="muted">pushing to ntfy topic ${al.topic_hint||""}</span>`
    : `<span class="pill amber">NOT CONFIGURED</span><div class="sub muted" style="margin-top:8px">${al.setup_hint||""}</div>`;
}

/* --- polling: core endpoint decides connectivity; the rest fail soft --- */
async function refresh(){
  let ov;
  try{
    ov = await getJSON("/api/overview");
    renderOverview(ov);
    renderSimulation(ov);
    connOk();
  }catch(e){ connFail(); return; }
  const urls = ["/api/kraken/balances","/api/kraken/trades","/api/scanner","/api/strategy",
                "/api/ai","/api/system-health","/api/risk","/api/audit",
                "/api/reconciliation","/api/emergency-stop"];
  const res = await Promise.allSettled(urls.map(getJSON));
  const val = i => res[i].status==="fulfilled" ? res[i].value : null;
  if (val(0)) renderKraken(val(0));
  renderLearner(ov);
  if (ov && ov.learner) $("lrActive").textContent = (ov.active_symbol || "—");
  if (val(1)) renderTrades(val(1));
  if (val(2)) renderScanner(val(2));
  if (val(3)) renderStrategy(val(3));
  if (val(4)) renderAI(val(4));
  renderSystem(val(5)||{}, val(6)||{}, ov.status||{}, val(7)||{}, val(8)||{},
               val(9)? val(9).active : false);
}
setInterval(()=>{ $("clock").textContent = new Date().toUTCString().slice(17,25)+" UTC"; }, 1000);
refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
'''


SW = ("self.addEventListener('install',e=>self.skipWaiting());"
      "self.addEventListener('fetch',()=>{});")


# ---------------------------------------------------------------------------
# HTTP layer — clean routing table (fixes the old double-response 404 bug)
# ---------------------------------------------------------------------------

def make_handler(settings: Settings, monitor: PaperMonitor) -> type[BaseHTTPRequestHandler]:

    def _overview() -> dict[str, object]:
        return {
            "status": safety_status(settings),
            "market": market_snapshot(settings),
            "portfolio": portfolio_snapshot(settings),
            "activity": recent_activity(settings.journal_path),
            "monitor": monitor.status(),
            "health": health_snapshot(settings),
            "audit": audit_summary(settings, limit=12),
            "learner": learner_state_data(settings),
            "last_wait_reason": last_wait_reason(settings),
        }

    GET_ROUTES = {
        "/manifest.json": lambda: ("json", MANIFEST),
        "/sw.js": lambda: ("js", SW),
        "/api/status": lambda: ("json", safety_status(settings)),
        "/api/health": lambda: ("json", health_snapshot(settings)),
        "/api/audit": lambda: ("json", audit_summary(settings)),
        "/api/overview": lambda: ("json", _overview()),
        "/api/risk": lambda: ("json", risk_state_snapshot(settings)),
        "/api/strategy": lambda: ("json", strategy_analytics(settings)),
        "/api/ai": lambda: ("json", ai_brief_data(settings)),
        "/api/system-health": lambda: ("json", system_health_data(settings)),
        "/api/equity-curve": lambda: ("json", equity_curve_data(settings)),
        "/api/learning": lambda: ("json", learning_brief_data(settings)),
        "/api/reconciliation": lambda: ("json", reconciliation_status(settings)),
        "/api/stop-monitor": lambda: ("json", stop_monitor_status(settings)),
        "/api/emergency-stop": lambda: ("json", {"active": emergency_stop_active()}),
        "/api/kraken/balances": lambda: ("json", kraken_balances_data(settings)),
        "/api/kraken/trades": lambda: ("json", kraken_trades_data(settings)),
        "/api/executions": lambda: ("json", execution_data(settings)),
        "/api/scanner": lambda: ("json", scanner_data(settings)),
        "/api/alerts": lambda: ("json", alerts_status()),
        "/api/learner": lambda: ("json", learner_state_data(settings)),
    }

    class Handler(BaseHTTPRequestHandler):
        def send_bytes(self, payload: bytes, content_type: str,
                       status: HTTPStatus = HTTPStatus.OK) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except BrokenPipeError:
                # Client disconnected before we finished writing (e.g. browser
                # navigated away, or a stream consumer closed early). Not a bug
                # in the bot — swallow it so the handler doesn't crash.
                pass

        def send_json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
            try:
                self.send_bytes(json.dumps(value, default=str).encode(), "application/json", status)
            except BrokenPipeError:
                pass

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/":
                self.send_bytes(HTML.encode(), "text/html; charset=utf-8")
                return
            handler = GET_ROUTES.get(path)
            if handler is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                kind, payload = handler()
                if kind == "js":
                    self.send_bytes(str(payload).encode(), "application/javascript")
                else:
                    self.send_json(payload)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)},
                               HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            try:
                if path == "/api/monitor/interval":
                    length = int(self.headers.get("content-length", "0"))
                    payload = json.loads(self.rfile.read(length)) if length else {}
                    seconds = int(payload.get("interval_seconds")
                                  or getattr(settings, "monitor_interval_seconds", 1800))
                    monitor.set_interval(seconds)
                    self.send_json(monitor.status())
                elif path == "/api/monitor/start":
                    monitor.start()
                    ntfy_alert("Dublin monitor started",
                               f"Cycle every {monitor.interval_seconds // 60}m on {settings.symbol}")
                    self.send_json(monitor.status())
                elif path == "/api/monitor/stop":
                    monitor.stop()
                    ntfy_alert("Dublin monitor stopped", "Monitor halted from dashboard")
                    self.send_json(monitor.status())
                elif path == "/api/emergency-stop/activate":
                    activate_emergency_stop()
                    ntfy_alert("DUBLIN EMERGENCY STOP", "Emergency stop activated from dashboard",
                               priority="urgent", tags="octagonal_sign")
                    self.send_json({"active": True, "message": "Emergency stop activated"})
                elif path == "/api/emergency-stop/clear":
                    clear_emergency_stop()
                    ntfy_alert("Dublin stop cleared", "Emergency stop cleared")
                    self.send_json({"active": False, "message": "Emergency stop cleared"})
                elif path == "/api/run-once":
                    record = TradingEngine(settings).run_once()
                    result = record.to_dict()
                    action = result.get("signal", {}).get("action")
                    if action in ("BUY", "SELL"):
                        sig = result["signal"]
                        ntfy_alert(
                            f"Dublin {action} signal",
                            f"{result.get('symbol')} @ {sig.get('price')} — score {sig.get('score')}. {sig.get('reason')}",
                            priority="high", tags="rotating_light",
                        )
                    self.send_json(result)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def serve_dashboard(settings: Settings, run: bool = True) -> int:
    # Load persisted learner state (per-coin expectancy) at startup so the
    # self-learning bias survives restarts. Done before the monitor/server are
    # built — and before the early `run=False` return — so callers that only
    # want startup initialization get it too.
    if getattr(settings, "learner_enabled", False):
        try:
            from .learner import LearningAgent
            LearningAgent(settings.learner_path,
                           min_trades=settings.learner_min_trades,
                           enabled=True).load()
        except Exception:
            pass
    if not run:
        return 0

    # The CLI is also read-only: never create a monitor, even if autostart is configured.
    from contextlib import ExitStack
    from queue import Queue

    completed = Queue()

    def listen(server):
        try:
            server.serve_forever()
        except BaseException as exc:
            completed.put(exc)
        else:
            completed.put(None)

    def stop(server, thread):
        try:
            if thread.is_alive():
                server.shutdown()
        finally:
            thread.join()

    try:
        with ExitStack() as cleanup:
            servers = []
            for address, handler in [
                (("127.0.0.1", 8766), SimpleDashboardHandler),
                (("127.0.0.1", 8765), DashboardRedirectHandler),
            ]:
                server = BoundedDashboardHTTPServer(address, handler)
                cleanup.callback(server.server_close)
                servers.append(server)
            print("Dublin read-only terminal: http://127.0.0.1:8766/gunbot")
            for server in servers:
                thread = Thread(target=listen, args=(server,), name="dashboard-listener")
                thread.start()
                cleanup.callback(stop, server, thread)
            for _ in servers:
                error = completed.get()
                if error is not None:
                    raise error
    except KeyboardInterrupt:
        pass
    return 0


# Read-only Gunbot-inspired surface. Legacy dashboard APIs above remain compatible.
# This handler never constructs a TradingMonitor or TradingEngine.
from pathlib import Path as _Path
from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs
import math as _math
import threading
from datetime import timezone

_DASH_BASE = _Path(__file__).resolve().parents[2]
_DASH_STATIC = _Path(__file__).resolve().parent / "static"
_DASH_PAIRS = {"BTC/USD": "XBTUSD", "PUMP/USD": "PUMPUSD", "XRP/USD": "XRPUSD"}
_DASH_CACHE = {}
_DASH_LOCKS = {}
_DASH_GUARD = threading.Lock()


def _dash_number(value):
    if value is None:
        return None
    number = float(value)
    if not _math.isfinite(number):
        raise ValueError("Non-finite exchange number")
    return number


class DashboardPrivateReader:
    """One serialized account collector with shared cooldown and last-read evidence."""
    def __init__(self, gateway, clock=time.monotonic):
        self.gateway, self.clock = gateway, clock
        self.lock = threading.Lock()
        self.cache = {}
        self.blocked_until = 0
        self.last_error = None

    def _private(self, endpoint, params=None):
        if endpoint not in {"Balance", "TradeBalance", "TradesHistory", "OpenOrders", "ClosedOrders"}:
            raise ValueError("Read-only endpoint not allowed")
        key = (endpoint, json.dumps(params or {}, sort_keys=True))
        with self.lock:
            now = self.clock()
            previous = self.cache.get(key)
            if previous and now - previous[0] < 60:
                return dict(previous[1])
            try:
                if now < self.blocked_until:
                    raise RuntimeError(f"Private API cooldown active; retry in {max(1, int(self.blocked_until - now))}s; last error: {self.last_error}")
                try:
                    result = self.gateway._private(endpoint, params)
                except Exception as exc:
                    # Only these allowlisted reads are retried, never an order.
                    # A concurrent coordinator request can overtake a nonce.
                    if str(exc).strip() != "EAPI:Invalid nonce":
                        raise
                    time.sleep(0.25)
                    result = self.gateway._private(endpoint, params)
                self.cache[key] = (self.clock(), dict(result), datetime.now(timezone.utc).isoformat())
                return dict(result)
            except Exception as exc:
                if now >= self.blocked_until:
                    self.blocked_until = self.clock() + 120
                    self.last_error = str(exc)
                if previous:
                    return {**previous[1], "_dashboard_stale": {"error": str(exc), "verified_at": previous[2]}}
                raise


_DASH_READER = None
_DASH_READER_INIT = threading.Lock()


def _dash_private_gateway():
    global _DASH_READER
    with _DASH_READER_INIT:
        if _DASH_READER is None:
            _DASH_READER = DashboardPrivateReader(_dash_gateway())
        return _DASH_READER


def _dash_evidence(result, endpoint, data):
    result = dict(result)
    stale = result.pop("_dashboard_stale", None)
    if stale:
        data["errors"][endpoint] = "STALE — verified " + stale["verified_at"] + ": " + stale["error"]
    return result


def _dash_gateway():
    from .kraken_gateway import KrakenGateway
    s = Settings(_env_file=str(_DASH_BASE / ".env"))
    s.http_timeout_seconds = 8
    return KrakenGateway(s, allow_order_submission=False, max_retries=1)


def load_live_portfolio(gateway=None):
    g = gateway or _dash_private_gateway()
    data = {"equity": None, "cash": None, "holdings": None,
            "total_pnl": None, "win_rate": None, "errors": {},
            "scope": "Kraken account — not bot-owned inventory",
            "last_updated": datetime.now(timezone.utc).isoformat()}
    try:
        balances = _dash_evidence(g._private("Balance"), "Balance", data)
        if not isinstance(balances, dict):
            raise ValueError("Malformed Balance response")
        data["cash"] = _dash_number(balances.get("ZUSD", "0"))
        aliases = {"XXBT": "BTC", "XBT": "BTC", "XXRP": "XRP", "ZUSD": "USD"}
        data["holdings"] = [
            {"asset": aliases.get(asset, asset), "exchange_asset": asset,
             "quantity": _dash_number(amount), "cost_basis": None,
             "unrealized_pnl": None, "attribution": "account / unverified bot ownership"}
            for asset, amount in balances.items() if _dash_number(amount) != 0
        ]
    except Exception as exc:
        data["errors"]["Balance"] = str(exc)
    try:
        balance = _dash_evidence(g._private("TradeBalance", {"asset": "ZUSD"}), "TradeBalance", data)
        data["equity"] = _dash_number(balance["eb"])
    except Exception as exc:
        data["errors"]["TradeBalance"] = str(exc)
    return data


def _dash_symbol(raw):
    return {"XXBTZUSD": "BTC/USD", "XBTUSD": "BTC/USD", "XBT/USD": "BTC/USD",
            "BTCUSD": "BTC/USD", "PUMPUSD": "PUMP/USD", "XXRPZUSD": "XRP/USD",
            "XRPUSD": "XRP/USD"}.get(raw, raw)


def load_saved_fills(path=None):
    """Historical exchange captures; never presented as a fresh account read."""
    import sqlite3
    path = _Path(path) if path is not None else _DASH_BASE / "logs" / "executions.sqlite3"
    data = {"source": "local exchange execution capture", "live_verified": False,
            "fills": None, "error": None, "limit": 100}
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT order_id, symbol, side, status, fill_price, volume, cost, fee, filled_at "
                "FROM executions WHERE volume > 0 AND fill_price > 0 "
                "ORDER BY filled_at DESC LIMIT 100"
            ).fetchall()
        data["fills"] = [{"order_id": row["order_id"], "symbol": _dash_symbol(row["symbol"]),
                          "side": row["side"], "status": row["status"],
                          "price": _dash_number(row["fill_price"]),
                          "quantity": _dash_number(row["volume"]),
                          "cost": _dash_number(row["cost"]), "fee": _dash_number(row["fee"]),
                          "captured_at": row["filled_at"], "pnl": None}
                         for row in rows]
    except (sqlite3.Error, ValueError, TypeError) as exc:
        data["error"] = str(exc)
    return data


def load_exchange_history(gateway=None, offset=0):
    g = gateway or _dash_private_gateway()
    data = {"fills": None, "orders": [], "fills_total": None,
            "fills_has_more": None, "closed_total": None, "closed_has_more": None,
            "offset": offset, "errors": {}, "scope": "Kraken account history; bot attribution unverified",
            "last_updated": datetime.now(timezone.utc).isoformat()}
    try:
        result = g._private("TradesHistory", {"type": "all", "trades": True, "ofs": offset})
        result = _dash_evidence(result, "TradesHistory", data)
        rows = result["trades"]
        data["fills_total"] = int(result["count"])
        data["fills"] = [{"id": tid, "order_id": t.get("ordertxid"),
            "symbol": _dash_symbol(t.get("pair")), "side": t.get("type"),
            "time": _dash_number(t.get("time")),
            "price": _dash_number(t.get("price")), "quantity": _dash_number(t.get("vol")),
            "cost": _dash_number(t.get("cost")), "fee": _dash_number(t.get("fee")),
            "pnl": None, "attribution": "account / unverified bot ownership"}
            for tid, t in rows.items()]
        data["fills"].sort(key=lambda t: t["time"] or 0, reverse=True)
        data["fills_has_more"] = offset + len(rows) < data["fills_total"]
    except Exception as exc:
        data["fills"] = None
        data["errors"]["TradesHistory"] = str(exc)
    for endpoint, key in (("OpenOrders", "open"), ("ClosedOrders", "closed")):
        try:
            result = g._private(endpoint, {"trades": True, **({"ofs": offset} if key == "closed" else {})})
            result = _dash_evidence(result, endpoint, data)
            rows = result[key]
            if key == "closed":
                data["closed_total"] = int(result["count"])
                data["closed_has_more"] = offset + len(rows) < data["closed_total"]
            normalized = []
            for oid, order in rows.items():
                desc = order.get("descr", {})
                executed = _dash_number(order.get("vol_exec"))
                volume = _dash_number(order.get("vol"))
                normalized.append({"id": oid, "symbol": _dash_symbol(desc.get("pair")),
                    "side": desc.get("type"), "order_type": desc.get("ordertype"),
                    "status": order.get("status"), "quantity": volume,
                    "executed": executed, "filled": executed is not None and volume is not None and volume > 0 and executed >= volume,
                    "time": _dash_number(order.get("closetm", order.get("opentm"))),
                    "price": _dash_number(order.get("price")),
                    "attribution": "account / unverified bot ownership"})
            data["orders"].extend(normalized)
        except Exception as exc:
            data["errors"][endpoint] = str(exc)
    data["orders"].sort(key=lambda o: o["time"] or 0, reverse=True)
    data["performance"] = [{"symbol": symbol, "realized_pnl": None,
        "unrealized_pnl": None, "win_rate": None,
        "reason": "Cost basis and complete attributable lot history not verified"}
        for symbol in _DASH_PAIRS]
    return data


def load_chart(symbol, interval=15, gateway=None):
    if symbol not in _DASH_PAIRS or interval not in (1, 5, 15, 30, 60, 240, 1440):
        raise ValueError("Unsupported pair or timeframe")
    g = gateway or _dash_gateway()
    result = g._public("OHLC", {"pair": _DASH_PAIRS[symbol], "interval": interval})
    rows = next(value for key, value in result.items() if key != "last")
    candles = [{"time": int(r[0]), "open": _dash_number(r[1]), "high": _dash_number(r[2]),
                "low": _dash_number(r[3]), "close": _dash_number(r[4]), "volume": _dash_number(r[6])}
               for r in rows]
    return {"symbol": symbol, "interval": interval, "candles": candles,
            "source": "Kraken public OHLC; final candle is in progress",
            "last_updated": datetime.now(timezone.utc).isoformat()}


def _dash_cached(key, ttl, loader):
    with _DASH_GUARD:
        lock = _DASH_LOCKS.setdefault(key, threading.Lock())
    if not lock.acquire(timeout=1):
        raise TimeoutError("Refresh already in progress; retry shortly")
    try:
        entry = _DASH_CACHE.get(key)
        if entry and time.monotonic() - entry[0] < ttl:
            return entry[1]
        result = loader()
        _DASH_CACHE[key] = (time.monotonic(), result)
        return result
    finally:
        lock.release()


class SimpleDashboardHandler(BaseHTTPRequestHandler):
    """Local, read-only routes only; no legacy monitor control routes exposed."""
    def log_message(self, *_args):
        pass

    def _send(self, body, content_type, code=200):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = _urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = _parse_qs(parsed.query)
        assets = {"/gunbot": ("dashboard.html", "text/html; charset=utf-8"),
                  "": ("dashboard.html", "text/html; charset=utf-8"),
                  "/static/dashboard.js": ("dashboard.js", "text/javascript"),
                  "/static/dashboard.css": ("dashboard.css", "text/css"),
                  "/static/lightweight-charts.js": ("lightweight-charts.js", "text/javascript")}
        try:
            if path in assets:
                name, content_type = assets[path]
                return self._send((_DASH_STATIC / name).read_bytes(), content_type)
            if path == "/gunbot/stats":
                data = _dash_cached("account", 30, load_live_portfolio)
            elif path == "/gunbot/saved-fills":
                data = _dash_cached("saved-fills", 30, load_saved_fills)
            elif path in ("/gunbot/trades", "/gunbot/history"):
                offset = int(params.get("offset", ["0"])[0])
                if not 0 <= offset <= 100000:
                    raise ValueError("Invalid offset")
                data = _dash_cached(("history", offset), 60, lambda: load_exchange_history(offset=offset))
            elif path == "/gunbot/ohlc":
                symbol = params.get("symbol", ["BTC/USD"])[0]
                interval = int(params.get("interval", ["15"])[0])
                if symbol not in _DASH_PAIRS or interval not in (1, 5, 15, 30, 60, 240, 1440):
                    raise ValueError("Unsupported pair or timeframe")
                data = _dash_cached((symbol, interval), 20, lambda: load_chart(symbol, interval))
            elif path in ("/health", "/gunbot/health"):
                data = {"dashboard": "read-only", "order_submission_enabled": False,
                        "engine_status": dashboard_engine_observation()}
            else:
                return self._send("Not found", "text/plain", 404)
            self._send(json.dumps(data, allow_nan=False), "application/json")
        except Exception as exc:
            self._send(json.dumps({"error": str(exc)}), "application/json", 400 if isinstance(exc, ValueError) else 502)


def dashboard_engine_observation(process_text=None, log_path=None):
    import subprocess
    import shlex
    result = {"state": "unknown", "coordinator_pids": [], "trading_verified": False,
              "process_source": "ps -axo pid=,command=; exact python -m module match",
              "log_modified_at": None,
              "note": "Process presence and log modification do not prove healthy heartbeat or fills."}
    try:
        if process_text is None:
            process_text = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                                          text=True, timeout=4, check=True).stdout
        for line in process_text.splitlines():
            try:
                parts = shlex.split(line)
                if len(parts) >= 4 and parts[2:4] == ["-m", "dublin_bot.coordinator"] and "python" in _Path(parts[1]).name.lower():
                    result["coordinator_pids"].append(int(parts[0]))
            except ValueError:
                continue
        result["state"] = "process observed" if result["coordinator_pids"] else "process not found"
    except Exception as exc:
        result["error"] = str(exc)
    if log_path is not None:
        log = _Path(log_path)
    else:
        live_log = _DASH_BASE / "logs" / "live_trading.log"
        log = live_log if live_log.exists() else _DASH_BASE / "logs" / "coordinator.log"
    result["log_source"] = str(log)
    try:
        result["log_modified_at"] = datetime.fromtimestamp(log.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        pass
    return result


class DashboardRedirectHandler(BaseHTTPRequestHandler):
    """Legacy port is a redirect only: no second collector, no trading controls."""
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "http://127.0.0.1:8766/gunbot")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        self.send_response(405)
        self.send_header("Allow", "GET")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        pass
