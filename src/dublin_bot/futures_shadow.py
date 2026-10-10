"""SHADOW Kraken perpetual sleeves: signals + a virtual P&L ledger only.

Two rules from reports/perp_study_2026-10-10.md:

* ``d1flip``      daily-filter long/short on the perp's own closed UTC daily bars:
                  long while risk-on (close > SMA50 and SMA50 > SMA50 five days ago),
                  short while strictly bearish (close < SMA50 and SMA50 falling), flat
                  otherwise (``bear="repo"``: short whenever known and not risk-on).
                  Checked once per new 1h bar.
* ``donchian4h``  turtle channels on closed 4h bars, both directions: enter long on a
                  close above the prior N-day high, short below the prior N-day low; exit
                  a long on a close below the prior M-day low (short: above the M-day
                  high); an opposite entry flips. Default N=55, M=20, no vol filter.
                  Checked once per new 4h bar.

What a shadow does NOT do: it books nothing into the paper portfolio or the learner,
places no order and imports no order route, and never reads or writes a safety lock
or a key. It only writes its own files under ``logs_dir``:
``futures_shadow_<name>.jsonl`` (status/events), ``futures_shadow_<name>_trades.jsonl``
(closed virtual trades) and ``futures_shadow_<name>_state.json`` (open virtual positions).

Data: public Kraken Futures endpoints only (charts, single-symbol ticker, historical
funding). Every HTTP call has a short timeout and goes through one process-wide rate
limiter; the funding history is cached and fetched only while a position is open.

Costs: 5 bps taker + 2 bps slippage per side at the mark. Funding: Kraken's published
hourly relative rates for every hour held (prorated for partial hours; positive rate =
longs pay), plus the live ticker rate for the not-yet-published tail at close.
Liquidation: isolated margin on the mark (checked once per bar), loss capped at margin.
"""
from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .daily_filter import riskon_table
from .futures_costs import SPOT_TO_PERP, clamp_leverage, liquidation_price, relative_funding_rate

FEE_BPS = 5.0
SLIP_BPS = 2.0
DAY = 86400
BAR_SETTLE_SECONDS = 60          # wait this long after a bar boundary before reading candles
HTTP_TIMEOUT = 8.0
MIN_CALL_INTERVAL = 0.25         # process-wide rate limit between public HTTP calls
FUNDING_TTL = 50 * 60
RETRY_AFTER_ERROR = 240          # do not retry a failed bar more often than this

CHARTS = "https://futures.kraken.com/api/charts/v1/trade/{perp}/{res}?from={a}&to={b}"
TICKER = "https://futures.kraken.com/derivatives/api/v3/tickers/{perp}"
FUNDING = "https://futures.kraken.com/derivatives/api/v3/historical-funding-rates?symbol={perp}"


# ───────────────────────── public data (rate-limited) ─────────────────────────

class PublicFeed:
    """Read-only Kraken Futures public data. ``opener(url, timeout) -> bytes`` is injectable."""

    _rl_lock = threading.Lock()
    _last_call = 0.0
    _funding_cache: dict[str, tuple[float, list[tuple[int, float]]]] = {}
    _funding_lock = threading.Lock()

    def __init__(self, opener: Callable[[str, float], bytes] | None = None,
                 min_interval: float = MIN_CALL_INTERVAL) -> None:
        self._opener = opener or self._urlopen
        self.min_interval = min_interval
        self.calls = 0

    @staticmethod
    def _urlopen(url: str, timeout: float) -> bytes:
        from urllib.request import Request, urlopen
        req = Request(url, headers={"User-Agent": "mayo-bot-paper-shadow/1.0"})
        with urlopen(req, timeout=timeout) as r:  # noqa: S310 — fixed public GET
            return r.read()

    def _get(self, url: str) -> Any:
        with PublicFeed._rl_lock:
            wait = PublicFeed._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            PublicFeed._last_call = time.monotonic()
        self.calls += 1
        raw = self._opener(url, HTTP_TIMEOUT)
        return json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)

    def candles(self, perp: str, res: str, start: int, end: int) -> pd.DataFrame:
        rows = self._get(CHARTS.format(perp=perp, res=res, a=int(start), b=int(end))).get("candles") or []
        return pd.DataFrame({
            "time": [int(r["time"]) // 1000 for r in rows],
            "open": [float(r["open"]) for r in rows], "high": [float(r["high"]) for r in rows],
            "low": [float(r["low"]) for r in rows], "close": [float(r["close"]) for r in rows]})

    def ticker(self, perp: str) -> dict:
        t = self._get(TICKER.format(perp=perp)).get("ticker")
        if not isinstance(t, dict):
            raise ValueError(f"no public ticker for {perp}")
        mark = _f(t, "markPrice") or _f(t, "last")
        if not mark:
            raise ValueError(f"no mark for {perp}")
        return {"mark": mark, "rate_rel": relative_funding_rate(_f(t, "fundingRate"), mark)}

    def funding(self, perp: str, *, now: float | None = None) -> list[tuple[int, float]]:
        """[(timestamp, relative hourly rate)] — cached for FUNDING_TTL per symbol."""
        now = time.time() if now is None else now
        with PublicFeed._funding_lock:
            hit = PublicFeed._funding_cache.get(perp)
            if hit and now - hit[0] < FUNDING_TTL:
                return hit[1]
        rows = self._get(FUNDING.format(perp=perp)).get("rates") or []
        out = []
        for r in rows:
            try:
                ts = int(datetime.fromisoformat(str(r["timestamp"]).replace("Z", "+00:00")).timestamp())
                out.append((ts, float(r["relativeFundingRate"])))
            except (KeyError, TypeError, ValueError):
                continue
        with PublicFeed._funding_lock:
            PublicFeed._funding_cache[perp] = (now, out)
        return out


def _f(row, key):
    try:
        v = row.get(key) if row else None
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


# ───────────────────────── rules (pure, closed bars only) ─────────────────────────

def d1flip_target(daily: pd.DataFrame, *, now: float, bear: str = "strict") -> tuple[int, dict]:
    tab = riskon_table(daily, now=now)  # closed days only
    if tab is None or not len(tab):
        return 0, {"reason": "no daily data"}
    last = tab.iloc[-1]
    on = last["riskon"]
    if not (isinstance(on, float) and math.isfinite(on)):
        return 0, {"reason": "SMA50 warming up"}
    info = {"bar": time.strftime("%Y-%m-%d", time.gmtime(int(last["close_time"]) - DAY)),
            "close": round(float(last["close"]), 6), "sma50": round(float(last["sma50"]), 6),
            "sma50_prev5": round(float(last["sma50_prev"]), 6)}
    if on > 0.5:
        return 1, {**info, "reason": "risk-on"}
    strict = bool(last["close"] < last["sma50"] and last["sma50"] < last["sma50_prev"])
    if strict or bear == "repo":
        return -1, {**info, "reason": "bear" if strict else "not risk-on (repo bear)"}
    return 0, {**info, "reason": "neutral"}


def donchian_decide(bars: pd.DataFrame, current: int, *, now: float, entry_days: int = 55,
                    exit_days: int = 20, tf_minutes: int = 240) -> tuple[int, dict]:
    """New side from the last CLOSED bar. Channels exclude the signal bar itself."""
    tf = tf_minutes * 60
    b = bars[bars["time"] + tf <= now].sort_values("time").reset_index(drop=True)
    bpd = DAY // tf
    n_in, n_out = entry_days * bpd, exit_days * bpd
    if len(b) < n_in + 1:
        return current, {"reason": f"warming up ({len(b)}/{n_in + 1} closed bars)"}
    last = b.iloc[-1]
    prior = b.iloc[:-1]
    hi_n, lo_n = float(prior["high"].iloc[-n_in:].max()), float(prior["low"].iloc[-n_in:].min())
    hi_m, lo_m = float(prior["high"].iloc[-n_out:].max()), float(prior["low"].iloc[-n_out:].min())
    c = float(last["close"])
    le, se = c > hi_n, c < lo_n
    info = {"bar": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(last["time"]))), "close": c,
            "hi_entry": hi_n, "lo_entry": lo_n, "hi_exit": hi_m, "lo_exit": lo_m}
    if current > 0:
        if se:
            return -1, {**info, "reason": "flip: close below entry low"}
        if c < lo_m:
            return 0, {**info, "reason": "exit: close below exit low"}
        return 1, {**info, "reason": "hold long"}
    if current < 0:
        if le:
            return 1, {**info, "reason": "flip: close above entry high"}
        if c > hi_m:
            return 0, {**info, "reason": "exit: close above exit high"}
        return -1, {**info, "reason": "hold short"}
    if le:
        return 1, {**info, "reason": "breakout long"}
    if se:
        return -1, {**info, "reason": "breakout short"}
    return 0, {**info, "reason": "no breakout"}


# ───────────────────────── ledger / sleeve ─────────────────────────

@dataclass
class ShadowResult:
    sleeve: str
    ran: bool = False
    reason: str = ""
    actions: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    positions: dict = field(default_factory=dict)
    equity: float = 0.0
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    def line(self) -> str:
        pos = ",".join(f"{k}:{'L' if v['side'] > 0 else 'S'}@{v['entry']:.6g}({v['upnl_bps']:+.0f}bps)"
                       for k, v in self.positions.items()) or "flat"
        acts = ",".join(f"{a['event']}:{a['symbol']}" for a in self.actions) or "none"
        return (f"SHADOW_FUTURES sleeve={self.sleeve} ran={self.ran} actions={acts} positions={pos} "
                f"equity={self.equity:.2f} errors={len(self.errors)} {self.seconds:.1f}s {self.reason}")


class FuturesShadow:
    name = "base"
    tf_minutes = 60

    def __init__(self, *, logs_dir: Path | str = Path("logs"), feed: PublicFeed | None = None,
                 book_usd: float = 500.0, leverage: float = 1.0, symbols: list[str] | None = None,
                 maintenance: float = 0.01) -> None:
        self.logs_dir = Path(logs_dir)
        self.feed = feed or PublicFeed()
        self.book_usd = float(book_usd)
        self.leverage = clamp_leverage(leverage)
        self.symbols = list(symbols or ["BTC/USD", "ETH/USD", "SOL/USD"])
        self.maintenance = maintenance
        self._run_lock = threading.Lock()

    # files
    @property
    def events_path(self) -> Path:
        return self.logs_dir / f"futures_shadow_{self.name}.jsonl"

    @property
    def trades_path(self) -> Path:
        return self.logs_dir / f"futures_shadow_{self.name}_trades.jsonl"

    @property
    def state_path(self) -> Path:
        return self.logs_dir / f"futures_shadow_{self.name}_state.json"

    def load(self) -> dict:
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d.setdefault("positions", {})
                d.setdefault("equity", self.book_usd)
                return d
        except (OSError, ValueError):
            pass
        return {"positions": {}, "equity": self.book_usd, "last_bar": None, "last_attempt": 0.0}

    def save(self, d: dict) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1, default=str), encoding="utf-8")
        tmp.replace(self.state_path)

    def _append(self, path: Path, row: dict) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

    # scheduling
    def bar_id(self, now: float) -> int:
        return int((now - BAR_SETTLE_SECONDS) // (self.tf_minutes * 60))

    def due(self, now: float | None = None, state: dict | None = None) -> bool:
        now = time.time() if now is None else now
        st = state or self.load()
        if st.get("last_bar") == self.bar_id(now):
            return False
        if st.get("last_error") and now - float(st.get("last_attempt") or 0.0) < RETRY_AFTER_ERROR:
            return False  # failed recently on this bar: back off, retry later
        return True

    # rule hook
    def decide(self, perp: str, current: int, now: float) -> tuple[int, dict]:
        raise NotImplementedError

    # funding
    def _accrue(self, pos: dict, perp: str, now: float) -> None:
        a = float(pos["funding_ts"])
        rates = self.feed.funding(perp, now=now)
        add, last_t = 0.0, a
        for ts, r in rates:
            if ts <= a or ts > now:
                continue
            lo = max(ts - 3600, a, float(pos["entry_ts"]))
            frac = max(min((ts - lo) / 3600.0, 1.0), 0.0)
            add += -pos["side"] * r * frac
            last_t = max(last_t, ts)
        pos["funding"] = float(pos["funding"]) + add
        pos["funding_ts"] = last_t

    def _tail_funding(self, pos: dict, rate_rel: float | None, now: float) -> float:
        hrs = max(now - float(pos["funding_ts"]), 0.0) / 3600.0
        return -pos["side"] * (rate_rel or 0.0) * min(hrs, 2.0)

    # cycle
    def run_cycle(self, *, now: float | None = None, force: bool = False) -> ShadowResult:
        res = ShadowResult(self.name)
        t0 = time.monotonic()
        if not self._run_lock.acquire(blocking=False):
            res.reason = "previous cycle still running"
            return res
        try:
            now = time.time() if now is None else float(now)
            st = self.load()
            if not force and not self.due(now, st):
                res.reason = "not due (once per new bar)"
                self._fill_status(res, st, marks={})
                return res
            res.ran = True
            marks: dict[str, float] = {}
            per_sleeve = float(st["equity"]) / max(len(self.symbols), 1)
            for sym in self.symbols:
                perp = SPOT_TO_PERP.get(sym, sym)
                try:
                    self._one(st, perp, per_sleeve, now, res, marks)
                except Exception as exc:  # noqa: BLE001 — one symbol never stops the rest
                    res.errors.append(f"{perp}: {type(exc).__name__}: {exc}"[:200])
            st["last_attempt"] = now
            if res.errors:
                st["last_error"] = res.errors[-1]
            else:
                st["last_bar"] = self.bar_id(now)
                st["last_error"] = None
            st["updated"] = now
            self.save(st)
            self._fill_status(res, st, marks)
            res.seconds = time.monotonic() - t0
            self._append(self.events_path, {"ts": now, "event": "status", **res.to_dict()})
            return res
        finally:
            res.seconds = time.monotonic() - t0
            self._run_lock.release()

    def _one(self, st: dict, perp: str, per_sleeve: float, now: float, res: ShadowResult,
             marks: dict) -> None:
        q = self.feed.ticker(perp)
        mark = q["mark"]
        marks[perp] = mark
        pos = st["positions"].get(perp)
        if pos:
            self._accrue(pos, perp, now)
            liq = float(pos.get("liq") or 0.0)
            if (pos["side"] > 0 and liq > 0 and mark <= liq) or (pos["side"] < 0 and liq > 0 and mark >= liq):
                self._close(st, perp, pos, liq, "liquidation", now, res, q)
                pos = None
        cur = int(pos["side"]) if pos else 0
        new, info = self.decide(perp, cur, now)
        if new != cur:
            res.actions.append({"event": "decide", "symbol": perp, "from": cur, "to": new})
        if pos and new != cur:
            self._close(st, perp, pos, mark, info.get("reason", "signal"), now, res, q)
            pos = None
        if not pos and new != 0:
            side = "long" if new > 0 else "short"
            liq = 0.0 if (new > 0 and self.leverage <= 1.0) else liquidation_price(
                mark, side=side, leverage=self.leverage, maintenance=self.maintenance)
            st["positions"][perp] = {"side": new, "entry": mark, "entry_ts": now,
                                     "notional": per_sleeve * self.leverage, "leverage": self.leverage,
                                     "liq": liq, "funding": 0.0, "funding_ts": now,
                                     "reason": info.get("reason")}
            row = {"ts": now, "event": "open", "symbol": perp, "side": side, "mark": mark,
                   "notional": round(per_sleeve * self.leverage, 4), "info": info}
            self._append(self.events_path, row)
            res.actions.append({"event": "open", "symbol": perp, "side": side})

    def _close(self, st, perp, pos, px, reason, now, res, q) -> None:
        side = int(pos["side"])
        funding = float(pos["funding"]) + self._tail_funding(pos, q.get("rate_rel"), now)
        gross = side * (px / float(pos["entry"]) - 1.0)
        fees = 2 * (FEE_BPS + SLIP_BPS) / 1e4
        net = gross - fees + funding
        if reason == "liquidation":
            net = -1.0 / float(pos["leverage"])
        pnl = float(pos["notional"]) * net
        st["equity"] = float(st["equity"]) + pnl
        row = {"ts": now, "sleeve": f"futures_shadow_{self.name}", "symbol": perp,
               "side": "long" if side > 0 else "short", "entry": pos["entry"], "exit": px,
               "entry_ts": pos["entry_ts"], "notional": pos["notional"], "leverage": pos["leverage"],
               "gross_bps": round(gross * 1e4, 2), "funding_bps": round(funding * 1e4, 2),
               "fees_bps": round(fees * 1e4, 2), "net_bps": round(net * 1e4, 2), "pnl": round(pnl, 6),
               "reason": reason, "shadow": True}
        self._append(self.trades_path, row)
        self._append(self.events_path, {"event": "close", **row})
        st["positions"].pop(perp, None)
        res.actions.append({"event": "close", "symbol": perp, "net_bps": row["net_bps"]})

    def _fill_status(self, res: ShadowResult, st: dict, marks: dict) -> None:
        res.equity = float(st["equity"])
        res.positions = {}
        for perp, p in st["positions"].items():
            m = marks.get(perp)
            up = p["side"] * (m / p["entry"] - 1.0) + float(p["funding"]) if m else float("nan")
            res.positions[perp] = {"side": p["side"], "entry": p["entry"],
                                   "since": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(p["entry_ts"])),
                                   "funding_bps": round(float(p["funding"]) * 1e4, 2),
                                   "upnl_bps": round(up * 1e4, 1) if m else float("nan")}

    # promotion
    def promotion(self) -> dict:
        from .promotion import check_strategy, load_jsonl
        return check_strategy(load_jsonl(self.trades_path))


class D1FlipShadow(FuturesShadow):
    name = "d1flip"
    tf_minutes = 60

    def __init__(self, *, bear: str = "strict", **kw) -> None:
        super().__init__(**kw)
        self.bear = bear

    def decide(self, perp: str, current: int, now: float) -> tuple[int, dict]:
        daily = self.feed.candles(perp, "1d", now - 150 * DAY, now)
        return d1flip_target(daily, now=now, bear=self.bear)


class Donchian4hShadow(FuturesShadow):
    name = "donchian4h"
    tf_minutes = 240

    def __init__(self, *, entry_days: int = 55, exit_days: int = 20, **kw) -> None:
        super().__init__(**kw)
        self.entry_days, self.exit_days = int(entry_days), int(exit_days)

    def decide(self, perp: str, current: int, now: float) -> tuple[int, dict]:
        bars = self.feed.candles(perp, "4h", now - (self.entry_days + 10) * DAY, now)
        return donchian_decide(bars, current, now=now, entry_days=self.entry_days,
                               exit_days=self.exit_days, tf_minutes=self.tf_minutes)


# ───────────────────────── loop integration ─────────────────────────

def shadows_allowed(settings) -> tuple[bool, str]:
    if not (settings.paper_trading and settings.dry_run and not settings.allow_live_trading):
        return False, "requires paper + dry-run + no live"
    return True, "ok"


def build_shadows(settings, *, feed: PublicFeed | None = None) -> list[FuturesShadow]:
    """Enabled shadows for these settings (empty when off or a safety lock is not engaged)."""
    ok, _ = shadows_allowed(settings)
    if not ok:
        return []
    g = lambda k, d: getattr(settings, k, d)  # noqa: E731
    common = {"logs_dir": Path(g("futures_shadow_logs_dir", Path("logs"))), "feed": feed or PublicFeed(),
              "book_usd": float(g("futures_shadow_book_usd", 500.0)),
              "symbols": list(g("futures_shadow_symbols", ["BTC/USD", "ETH/USD", "SOL/USD"]))}
    out: list[FuturesShadow] = []
    if g("futures_shadow_d1flip_enabled", False):
        out.append(D1FlipShadow(bear=g("futures_shadow_d1flip_bear", "strict"),
                                leverage=g("futures_shadow_d1flip_leverage", 1.0), **common))
    if g("futures_shadow_donchian_enabled", False):
        out.append(Donchian4hShadow(entry_days=g("futures_shadow_donchian_entry_days", 55),
                                    exit_days=g("futures_shadow_donchian_exit_days", 20),
                                    leverage=g("futures_shadow_donchian_leverage", 1.0), **common))
    return out


class ShadowRunner:
    """Runs due shadows in daemon threads so the paper loop is never blocked for more than
    ``budget`` seconds, never crashes, and never runs two cycles of one sleeve at once."""

    def __init__(self, shadows: list[FuturesShadow], log: Callable[[str], None], *,
                 budget: float = 3.0) -> None:
        self.shadows = shadows
        self.log = log
        self.budget = float(budget)
        self._threads: dict[str, threading.Thread] = {}

    def init_status(self) -> None:
        for s in self.shadows:
            try:
                st = s.load()
                res = ShadowResult(s.name, reason="init (state loaded; first cycle checks on the next new bar)")
                s._fill_status(res, st, marks={})
                self.log(res.line())
            except Exception as exc:  # noqa: BLE001
                self.log(f"WARN SHADOW_FUTURES sleeve={s.name} init: {type(exc).__name__}: {exc}"[:400])

    def _work(self, s: FuturesShadow) -> None:
        try:
            res = s.run_cycle()
            if res.ran:
                self.log(res.line()[:900])
                for e in res.errors:
                    self.log(f"WARN SHADOW_FUTURES sleeve={s.name} {e}"[:400])
        except Exception as exc:  # noqa: BLE001 — a shadow must never take the loop down
            self.log(f"WARN SHADOW_FUTURES sleeve={s.name} {type(exc).__name__}: {exc}"[:400])

    def tick(self, now: float | None = None) -> None:
        started = []
        for s in self.shadows:
            try:
                t = self._threads.get(s.name)
                if t is not None and t.is_alive():
                    continue
                if not s.due(now):
                    continue
                t = threading.Thread(target=self._work, args=(s,), name=f"shadow-{s.name}", daemon=True)
                self._threads[s.name] = t
                t.start()
                started.append(t)
            except Exception as exc:  # noqa: BLE001
                self.log(f"WARN SHADOW_FUTURES sleeve={s.name} tick: {type(exc).__name__}: {exc}"[:400])
        deadline = time.monotonic() + self.budget
        for t in started:
            t.join(max(deadline - time.monotonic(), 0.0))
