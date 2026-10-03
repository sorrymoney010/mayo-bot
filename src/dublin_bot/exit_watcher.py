"""Event-driven paper exit watcher.

The paper loop wakes every few minutes. This watcher consumes live prices
(websocket ticker, the tick-collector file, or a public REST poll) and builds
1-minute candles in memory so a stop, trailing stop or take-profit books
within a couple of seconds of the cross. Entries are untouched.

It runs inside the paper-loop process, which already holds the single-instance
lock and has passed the ledger-owner check. A standalone process must take
that same lock and refuses when it cannot. The sell goes through
``PaperPortfolio.try_record_sell``, which reloads the book under a file lock,
so the slow cycle cannot double-close the same lot.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .instance import ledger_owner_ok
from .paper import PaperPortfolio, paper_book_lock
from .trailing import TrailBook, fresh_state, on_price, sleeve_trail_enabled

PAPER_BOOK = Path("logs/paper_portfolio.json")
PAPER_LOTS = Path("logs/paper_bot_positions.json")


@dataclass
class MinuteCandle:
    symbol: str
    minute: int
    open: float
    high: float
    low: float
    close: float
    trades: int = 1


@dataclass
class CandleBook:
    """In-memory 1-minute candles. ``history`` keeps closed minutes per symbol."""

    history: dict[str, list[MinuteCandle]] = field(default_factory=dict)
    current: dict[str, MinuteCandle] = field(default_factory=dict)
    keep: int = 240

    def add(self, symbol: str, ts: float, price: float) -> MinuteCandle:
        minute = int(ts) // 60 * 60
        cur = self.current.get(symbol)
        if cur is None or cur.minute != minute:
            if cur is not None:
                self.history.setdefault(symbol, []).append(cur)
                self.history[symbol] = self.history[symbol][-self.keep:]
            cur = MinuteCandle(symbol=symbol, minute=minute, open=price, high=price,
                               low=price, close=price)
            self.current[symbol] = cur
        else:
            cur.high = max(cur.high, price)
            cur.low = min(cur.low, price)
            cur.close = price
            cur.trades += 1
        return cur


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


# A websocket print older than this is ignored. The tick-file tail may be a
# few seconds behind the collector and is still a live price inside this window.
WS_FRESH_SECONDS = 5.0
TICK_FRESH_SECONDS = 15.0
_TAIL_BYTES = 4096
_LOG_INTERVAL = 60.0
_LOG_AT: dict[str, float] = {}


def log_limited(path: Path, key: str, message: str, *, interval: float = _LOG_INTERVAL) -> None:
    """Append one line, at most once per ``interval`` for each ``key``."""
    now = time.time()
    if now - _LOG_AT.get(key, 0.0) < interval:
        return
    _LOG_AT[key] = now
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now(timezone.utc).isoformat()}] {message}\n")
    except OSError:
        return


def claim_opened(sleeve: str, opened: str, pos) -> str:
    """Stable non-empty stamp so two lots on one coin do not share a claim key."""
    stamp = str(opened or "").strip()
    if sleeve == "primary":
        stamp = str(getattr(pos, "opened_at", "") or "").strip() or stamp
    if stamp:
        return stamp
    entry = float(getattr(pos, "entry_price", 0.0) or 0.0)
    trades = int(getattr(pos, "trades", 0) or 0)
    return f"legacy:{entry:.8f}:{trades}"


def claim_key(sleeve: str, symbol: str, opened: str) -> str:
    return f"{sleeve}|{symbol}|{opened}"


def spot_exit_levels(settings, symbol: str, entry: float) -> tuple[str, float | None, float | None, str]:
    """(sleeve, stop_price, tp_price, opened_at) for one spot lot.

    Strategy-signal exits (chandelier, RSI, EMA) stay on the slow cycle.
    Only hard stop, hard take-profit and the trailing stop are fast.
    """
    meanrev = _read_json(Path(getattr(settings, "meanrev_state_path", "logs/meanrev_sleeve.json")), {})
    mpos = (meanrev.get("positions") or {}).get(symbol)
    if isinstance(mpos, dict) and float(mpos.get("qty") or 0) > 0:
        return "meanrev_4h", float(mpos["stop"]), float(mpos["tp"]), str(mpos.get("filled_at") or mpos.get("opened_at") or "")
    trend = _read_json(Path(getattr(settings, "trendhold_state_path", "logs/trendhold_sleeve.json")), {})
    tpos = (trend.get("positions") or {}).get(symbol)
    if isinstance(tpos, dict) and float(tpos.get("qty") or 0) > 0:
        return "trendhold_4h", None, None, str(tpos.get("filled_at") or "")
    stop = entry * (1.0 - float(settings.stop_loss_pct))
    tp = entry * (1.0 + float(settings.take_profit_pct))
    return "primary", stop, tp, ""


def _drop_sleeve_position(settings, symbol: str, sleeve: str, *, lots_path: Path = PAPER_LOTS) -> None:
    if sleeve == "meanrev_4h":
        path = Path(getattr(settings, "meanrev_state_path", "logs/meanrev_sleeve.json"))
        data = _read_json(path, {})
        if isinstance(data, dict):
            (data.get("positions") or {}).pop(symbol, None)
            _write_json(path, data)
    elif sleeve == "trendhold_4h":
        path = Path(getattr(settings, "trendhold_state_path", "logs/trendhold_sleeve.json"))
        data = _read_json(path, {})
        if isinstance(data, dict):
            (data.get("positions") or {}).pop(symbol, None)
            _write_json(path, data)
    lots = _read_json(lots_path, {})
    if isinstance(lots, dict) and symbol in lots:
        lots.pop(symbol, None)
        _write_json(lots_path, lots)


class ExitWatcher:
    def __init__(self, settings, *, portfolio: PaperPortfolio | None = None,
                 held_lock=None, require_owner: bool = True) -> None:
        self.settings = settings
        self.portfolio = portfolio or PaperPortfolio(PAPER_BOOK)
        base = self.portfolio.path.parent
        self.claims_path = base / "exit_claims.json"
        self.fast_exits_path = base / "fast_exits.jsonl"
        self.lots_path = base / "paper_bot_positions.json"
        self.held_lock = held_lock
        self.require_owner = require_owner
        self.candles = CandleBook()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.closes: list[dict] = []
        self.error_log = base / "exit_watcher.log"
        self._err_at: dict[str, float] = {}
        self.trail = TrailBook(getattr(settings, "trailing_state_path", "logs/trailing_state.json"))

    def allowed(self) -> tuple[bool, str]:
        if self.require_owner and not ledger_owner_ok():
            return False, "not the ledger owner"
        if not (self.settings.paper_trading and self.settings.dry_run
                and not self.settings.allow_live_trading):
            return False, "safety locks are not the paper posture"
        return True, "ok"

    def start(self) -> bool:
        ok, _why = self.allowed()
        if not ok:
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="paper-exit-watcher", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def run_once(self, ticks: list[tuple[str, float, float]]) -> list[dict]:
        """Apply ``(symbol, ts, price)`` ticks. Returns the closes booked."""
        ok, why = self.allowed()
        if not ok:
            return [{"refused": why}]
        booked = []
        for symbol, ts, price in ticks:
            self.candles.add(symbol, float(ts), float(price))
            hit = self.evaluate(symbol, float(price), float(ts))
            if hit:
                booked.append(hit)
        return booked

    def evaluate(self, symbol: str, price: float, ts: float) -> dict | None:
        if symbol.startswith("PF:"):
            return self._futures(symbol[3:], price, ts)
        return self._spot(symbol, price, ts)

    def _spot(self, symbol: str, price: float, ts: float) -> dict | None:
        snap = self.portfolio.load(
            equity=float(self.settings.strategy_equity_usd),
            cash=float(self.settings.strategy_equity_usd),
        )
        pos = snap.positions.get(symbol)
        if pos is None or pos.quantity <= 1e-12 or pos.entry_price <= 0:
            return None
        entry = float(pos.entry_price)
        sleeve, stop, tp, opened = spot_exit_levels(self.settings, symbol, entry)
        opened = claim_opened(sleeve, opened, pos)
        reason = None
        fill_px = price
        maker = False
        if stop is not None and price <= stop:
            reason = "stop"
        elif tp is not None and price >= tp:
            reason, fill_px, maker = "tp", tp, True
        else:
            trail_reason = self._trail(sleeve, symbol, "long", entry, price, opened)
            if trail_reason:
                reason = trail_reason
        if reason is None:
            return None
        return self._book_spot(symbol, sleeve, pos.quantity, fill_px, reason, maker, opened, ts)

    def _trail(self, sleeve: str, symbol: str, side: str, entry: float, price: float,
               opened: str) -> str | None:
        if not sleeve_trail_enabled(self.settings, sleeve):
            return None
        # ATR is unknown on a bare tick. The persisted state carries the ATR
        # captured when the sleeve armed the trail; without that, do nothing.
        self.trail.load()
        seed = fresh_state(
            sleeve=sleeve, symbol=symbol, side=side, entry=entry, atr=0.0,
            activate_atr=float(self.settings.trailing_activate_atr),
            trail_atr=float(self.settings.trailing_atr_mult), opened_at=opened,
        )
        st = self.trail.states.get(seed.key())
        if st is None:
            return None
        before = (st.armed, st.peak, st.stop, st.atr)
        st, reason = on_price(st, price)
        after = (st.armed, st.peak, st.stop, st.atr)
        if reason:
            self.trail.drop(st)
            self.trail.save()
            return reason
        if after != before:
            self.trail.put(st)
            self.trail.save()
        return None

    def _book_spot(self, symbol, sleeve, qty, fill_px, reason, maker, opened, ts) -> dict | None:
        from .fills import FillModel
        key = claim_key(sleeve, symbol, opened)
        with paper_book_lock(self.portfolio.path):
            claims = self._load_claims()
            claims = self._prune_claims(claims)
            if key in claims:
                return None
            model = FillModel(self.settings)
            if maker:
                fee = float(qty) * float(fill_px) * model.maker_fee_bps / 1e4
                price = float(fill_px)
            else:
                fill = model.sell(price=float(fill_px), volume=float(qty))
                fee, price = fill.fee, fill.price
            when = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            realized = self.portfolio.try_record_sell(symbol, float(qty), price, fee, when)
            if realized is None:
                return None
            claims[key] = ts
            _write_json(self.claims_path, claims)
            _drop_sleeve_position(self.settings, symbol, sleeve, lots_path=self.lots_path)
            # The lot is flat now. Drop its claim so the next open on this coin
            # is not blocked, and drop any older keys for positions already gone.
            self._prune_claims(claims)
        row = {"ts": when, "symbol": symbol, "sleeve": sleeve, "reason": reason,
               "price": price, "qty": qty, "realized": realized, "venue": "spot"}
        self._log(row)
        self._learn(symbol, sleeve, realized, qty, price)
        return row

    def _futures(self, symbol: str, mark: float, ts: float) -> dict | None:
        from .futures_sleeve import load_ledger, save_ledger
        from .futures_costs import fee_usd, short_pnl
        path = Path(getattr(self.settings, "futures_ledger_path", "logs/paper_futures.json"))
        with paper_book_lock(self.portfolio.path):
            book = load_ledger(path)
            pos = book.get("positions", {}).get(symbol)
            if not isinstance(pos, dict):
                return None
            opened = str(pos.get("opened_at") or "")
            key = claim_key("futures_short", symbol, opened or f"legacy:{float(pos.get('entry') or 0):.8f}")
            claims = self._prune_claims(self._load_claims())
            if key in claims:
                return None
            reason = None
            exit_px = mark
            if mark >= float(pos["liq"]):
                reason, exit_px = "liquidation", float(pos["liq"])
            else:
                tr = self._trail("futures_short", symbol, "short", float(pos["entry"]), mark, opened)
                if tr:
                    reason = tr
            if reason is None:
                return None
            notional = abs(float(pos["notional"]))
            pnl = short_pnl(float(pos["entry"]), exit_px, notional)
            exit_fee = fee_usd(notional)
            self.portfolio.adjust_cash(float(pos["margin"]) + pnl - exit_fee)
            book["positions"].pop(symbol, None)
            save_ledger(book, path)
            claims[key] = ts
            _write_json(self.claims_path, claims)
            self._prune_claims(claims)
        row = {"ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
               "symbol": symbol, "sleeve": "futures_short", "reason": reason,
               "price": exit_px, "realized": pnl - exit_fee, "venue": "futures"}
        self._log(row)
        return row

    def _log(self, row: dict) -> None:
        self.closes.append(row)
        self.fast_exits_path.parent.mkdir(parents=True, exist_ok=True)
        with self.fast_exits_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

    def _load_claims(self) -> dict:
        claims = _read_json(self.claims_path, {})
        return claims if isinstance(claims, dict) else {}

    def _open_claim_keys(self) -> set[str]:
        """Claim keys for lots the book still holds. Caller holds the book lock."""
        equity = float(self.settings.strategy_equity_usd)
        snap = self.portfolio.load(equity=equity, cash=equity)
        keys: set[str] = set()
        for sym, pos in snap.positions.items():
            sleeve, _stop, _tp, opened = spot_exit_levels(self.settings, sym, float(pos.entry_price))
            keys.add(claim_key(sleeve, sym, claim_opened(sleeve, opened, pos)))
        try:
            from .futures_sleeve import load_ledger
            path = Path(getattr(self.settings, "futures_ledger_path", "logs/paper_futures.json"))
            book = load_ledger(path)
        except Exception:
            book = {"positions": {}}
        for sym, pos in (book.get("positions") or {}).items():
            if not isinstance(pos, dict):
                continue
            opened = str(pos.get("opened_at") or "") or f"legacy:{float(pos.get('entry') or 0):.8f}"
            keys.add(claim_key("futures_short", sym, opened))
        return keys

    def _prune_claims(self, claims: dict) -> dict:
        keep = self._open_claim_keys()
        pruned = {k: v for k, v in claims.items() if k in keep}
        if pruned != claims:
            _write_json(self.claims_path, pruned)
        return pruned

    def _note_error(self, exc: BaseException) -> None:
        log_limited(self.error_log, type(exc).__name__, f"{type(exc).__name__}: {exc}")

    def _learn(self, symbol: str, sleeve: str, realized: float, qty: float, price: float) -> None:
        try:
            from .learner import enqueue_learner_exit
            key = {"meanrev_4h": "meanrev_mk", "trendhold_4h": "trendhold",
                   "primary": "regime"}.get(sleeve, sleeve)
            enqueue_learner_exit(
                self.settings.learner_path, symbol=symbol, pnl=float(realized), strategy=key,
                notional=abs(float(qty) * float(price)), ts=time.time(),
                detail={"exit_reason": "fast_exit", "sleeve": sleeve},
            )
        except Exception as exc:
            self._note_error(exc)

    def _loop(self) -> None:
        feed = None
        symbols = ("BTC/USD", "ETH/USD", "SOL/USD")
        while not self._stop.is_set():
            now = time.time()
            try:
                fresh = _fresh_ticks(getattr(self.settings, "pipeline_data_dir", "data"), symbols, now)
                if len(fresh) == len(symbols):
                    if feed is not None:
                        try:
                            feed.stop()
                        except Exception as exc:
                            self._note_error(exc)
                        feed = None
                elif feed is None:
                    feed = _try_feed()
                for symbol, price in _collect_prices(self.settings, feed, now).items():
                    self.candles.add(symbol, now, price)
                    try:
                        self.evaluate(symbol, price, now)
                    except Exception as exc:
                        self._note_error(exc)
            except Exception as exc:
                self._note_error(exc)
            self._stop.wait(2.0)
        if feed is not None:
            try:
                feed.stop()
            except Exception as exc:
                self._note_error(exc)


def _try_feed():
    try:
        from .realtime import KrakenRealtimeFeed
        pairs = {"BTC/USD": "XBT/USD", "ETH/USD": "ETH/USD", "SOL/USD": "SOL/USD"}
        feed = KrakenRealtimeFeed(pairs)
        feed.start()
        return feed
    except Exception as exc:
        log_limited(Path("logs/exit_watcher.log"), "ws_feed", f"{type(exc).__name__}: {exc}")
        return None


def _collect_prices(settings, feed, now: float) -> dict[str, float]:
    """Fresh websocket price wins. A stale or partial tick-file line does not.

    Futures marks are one public request, and only when that sleeve is enabled.
    """
    out: dict[str, float] = {}
    symbols = ("BTC/USD", "ETH/USD", "SOL/USD")
    if feed is not None:
        for sym in symbols:
            snap = feed.latest(sym)
            if snap is not None and snap.last > 0 and now - float(snap.updated_at) < WS_FRESH_SECONDS:
                out[sym] = float(snap.last)
    ticks = _fresh_ticks(getattr(settings, "pipeline_data_dir", "data"), symbols, now)
    for sym, (px, _ts) in ticks.items():
        if sym not in out:
            out[sym] = px
    for sym in symbols:
        if sym not in out:
            px = _rest_last(sym)
            if px:
                out[sym] = px
    if getattr(settings, "futures_sleeve_enabled", False):
        out.update(_futures_marks())
    return out


def _rest_last(symbol: str) -> float | None:
    """Public spot ticker. None on any failure (watcher just waits)."""
    pair = {"BTC/USD": "XBTUSD", "ETH/USD": "ETHUSD", "SOL/USD": "SOLUSD"}.get(symbol)
    if not pair:
        return None
    try:
        from urllib.request import urlopen
        with urlopen(f"https://api.kraken.com/0/public/Ticker?pair={pair}", timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        result = payload.get("result") or {}
        row = next(iter(result.values()))
        return float(row["c"][0])
    except Exception:
        return None


def _last_complete_line(path: Path) -> str | None:
    """Last newline-terminated CSV row, reading only the tail of the file.

    A row without a trailing newline is still being written and is ignored.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size <= 0:
        return None
    try:
        with path.open("rb") as fh:
            fh.seek(max(0, size - _TAIL_BYTES))
            blob = fh.read()
    except OSError:
        return None
    if size > _TAIL_BYTES:
        cut = blob.find(b"\n")
        if cut < 0:
            return None
        blob = blob[cut + 1:]
    if not blob.endswith(b"\n"):
        cut = blob.rfind(b"\n")
        if cut < 0:
            return None
        blob = blob[:cut + 1]
    lines = []
    for raw in blob.decode("utf-8", "replace").splitlines():
        if raw.strip() and not raw.startswith("trade_id"):
            lines.append(raw)
    return lines[-1] if lines else None


def _parse_tick_line(line: str) -> tuple[float, float] | None:
    """``(price, ts)`` from ``trade_id,ts,price,...``. None if partial or junk."""
    parts = line.split(",")
    if len(parts) < 6:
        return None
    try:
        ts = float(parts[1])
        price = float(parts[2])
    except ValueError:
        return None
    if price <= 0 or ts <= 0:
        return None
    return price, ts


def _fresh_ticks(data_dir, symbols, now: float) -> dict[str, tuple[float, float]]:
    """Today's last complete tick, when its timestamp is still fresh.

    The file day comes from ``now`` (the same clock as the freshness check),
    not a second wall-clock read.
    """
    out: dict[str, tuple[float, float]] = {}
    day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
    root = Path(data_dir) / "ticks"
    key = {"BTC/USD": "BTCUSD", "ETH/USD": "ETHUSD", "SOL/USD": "SOLUSD"}
    for sym in symbols:
        line = _last_complete_line(root / key[sym] / f"{day}.csv")
        if line is None:
            continue
        parsed = _parse_tick_line(line)
        if parsed is None:
            continue
        price, ts = parsed
        if now - ts > TICK_FRESH_SECONDS:
            continue
        out[sym] = (price, ts)
    return out


def _futures_marks() -> dict[str, float]:
    """One public tickers download for the three perps. Caller checks the sleeve flag."""
    try:
        from .futures_costs import SPOT_TO_PERP
        from .futures_public import FuturesPublic
        marks = FuturesPublic(timeout=5).marks(list(SPOT_TO_PERP.values()))
    except Exception as exc:
        log_limited(Path("logs/exit_watcher.log"), "futures_marks", f"{type(exc).__name__}: {exc}")
        return {}
    out = {}
    for spot, perp in SPOT_TO_PERP.items():
        px = marks.get(perp)
        if px:
            out[f"PF:{spot}"] = float(px)
    return out
