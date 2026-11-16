"""CAPIT-24 crash-buy — the ONE active paper strategy (frozen 2026-11-15). PAPER ONLY.

This module never places orders, never reads keys or .env, and never touches the
PAPER_TRADING / DRY_RUN / ALLOW_LIVE_TRADING locks. It reads public Binance.US
klines and keeps a paper ledger on disk.

Frozen rule (research/edge2/candidate.py, reports/low_price_coins_study_2026-11-15.md):
  on each CLOSED 1h bar t, per coin:
    z24  = 24h log return / (trailing-720h stdev of 1h log returns, excluding bar t, * sqrt(24))
    volr = 24h quote volume / (trailing-720h mean hourly quote volume, ending 24h earlier, * 24)
    signal if z24 <= -2.5 AND volr >= 2.0
  -> paper buy at the OPEN of bar t+1h, paper sell at the OPEN of bar t+1h+48h.
  Book: VET, HBAR, ALGO, XLM. Max 2 concurrent positions, 1 per coin, ties -> most negative z.
  Each slot = 50% of the starting paper equity ($100 -> $50 notional, non-compounded,
  same convention as the backtest). A slot frees for signals on bars t >= its exit time.

No z / volume / hold retuning. Do not add coins mid-test.

Fee books (same signals, same fills; only the cost differs):
  binanceus (PRIMARY): 0.02% taker fee each side + 3 bps slip each side = 10 bps round trip.
                        (Binance.US is 0% maker / 0.02% taker; next-open fills are taker.
                        Maker-only 0 bps and taker-no-slip 4 bps are logged per trade too.)
  kraken80 (SHADOW):   Kraken tier-1 maker 0.40% each side = 80 bps round trip.
                        (Kraken taker 160 bps is logged per trade; the backtest already
                        FAILS fold majority at 160 bps, so Kraken market orders are a kill.)

Kill rules (evaluated every cycle for each fee book; primary kill stops NEW entries):
  K1: >= 30 closed trades and profit factor < 1.10 at that book's fee -> KILL.
  K2: the last two COMPLETED calendar quarters (UTC, by exit time) are both net negative
      AND the cumulative realized book is below cash (equity < seed) -> KILL.
"""
from __future__ import annotations

import fcntl
import json
import math
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

COINS = ("VET", "HBAR", "ALGO", "XLM")
N, Z, VM, HOLD_H, MAX_POS = 24, 2.5, 2.0, 48, 2
WIN, MINP = 720, 360
START_EQUITY = 100.0
SLOT_FRAC = 0.5
H = pd.Timedelta("1h")

# round-trip costs as a fraction of notional
COSTS = {
    "bus_maker": 0.0000,   # Binance.US 0% maker both sides (best case, not assumed)
    "bus_taker": 0.0004,   # Binance.US 0.02% taker both sides, no slip
    "bus_base": 0.0010,    # 0.02% taker + 3 bps slip each side  <- PRIMARY book
    "krk_maker": 0.0080,   # Kraken tier-1 0.40% maker both sides <- SHADOW book
    "krk_taker": 0.0160,   # Kraken tier-1 0.80% taker both sides (logged only)
}
BOOKS = {"binanceus": "bus_base", "kraken80": "krk_maker"}
SLEEVES = {"binanceus": "capit24_binanceus", "kraken80": "capit24_kraken80_shadow"}
PRIMARY = "binanceus"
KILL_MIN_TRADES, KILL_MIN_PF = 30, 1.10


def features(k: pd.DataFrame) -> pd.DataFrame:
    """k: CLOSED 1h bars indexed by open time with columns c (close) and qv (quote volume).
    Returns z and volr exactly as research/edge2/candidate.py computes them."""
    lv = np.log(k["c"].astype(float))
    sd = lv.diff().rolling(WIN, min_periods=MINP).std().shift(1)
    z = lv.diff(N) / (sd * math.sqrt(N))
    qv = k["qv"].astype(float)
    volr = qv.rolling(N).sum() / (qv.rolling(WIN, min_periods=MINP).mean().shift(N) * N)
    return pd.DataFrame({"z": z, "volr": volr})


def is_signal(z: float, volr: float) -> bool:
    return bool(np.isfinite(z) and np.isfinite(volr) and z <= -Z and volr >= VM)


@dataclass
class Fill:
    coin: str
    signal_t: pd.Timestamp
    entry_t: pd.Timestamp
    exit_t: pd.Timestamp
    z: float
    volr: float
    entry_px: float


def slot_usd(start_equity: float = START_EQUITY) -> float:
    return SLOT_FRAC * start_equity


def trade_costs(gross: float) -> dict[str, float]:
    return {name: gross - c for name, c in COSTS.items()}


def profit_factor(vals: list[float]) -> float | None:
    if not vals:
        return None
    w = sum(v for v in vals if v > 0)
    lo = -sum(v for v in vals if v < 0)
    return math.inf if lo == 0 else w / lo


def quarter_of(ts: float) -> str:
    t = pd.Timestamp(ts, unit="s", tz="UTC")
    return f"{t.year}Q{(t.month - 1) // 3 + 1}"


def kill_check(rows: list[dict], *, now_ts: float, seed: float = START_EQUITY) -> dict:
    """rows: closed trades of ONE fee book (fields net_bps, pnl, ts=exit epoch)."""
    n = len(rows)
    pf = profit_factor([float(r["net_bps"]) for r in rows])
    pnl = sum(float(r["pnl"]) for r in rows)
    equity = seed + pnl
    k1 = n >= KILL_MIN_TRADES and pf is not None and pf < KILL_MIN_PF
    by_q: dict[str, float] = {}
    for r in rows:
        q = quarter_of(float(r["ts"]))
        by_q[q] = by_q.get(q, 0.0) + float(r["pnl"])
    cur_q = quarter_of(now_ts)
    t = pd.Timestamp(now_ts, unit="s", tz="UTC").tz_localize(None).to_period("Q")
    last_two = [str(t - 2), str(t - 1)]   # the two most recent COMPLETED quarters
    k2 = all(q in by_q and by_q[q] < 0 for q in last_two) and equity < seed
    return {
        "trades": n, "pf": (None if pf is None else ("inf" if pf == math.inf else round(pf, 3))),
        "pnl_usd": round(pnl, 4), "equity": round(equity, 4), "seed": seed,
        "quarters": {q: round(v, 4) for q, v in sorted(by_q.items())}, "current_quarter": cur_q,
        "k1_pf_below_1_10_after_30": k1, "k2_two_neg_quarters_and_below_cash": k2,
        "killed": bool(k1 or k2),
    }


# ───────────────────────── paper runner (one cycle) ─────────────────────────

BUS = "https://api.binance.us/api/v3/klines"
UA = {"User-Agent": "mayo-capit24-paper/1.0 (public data, paper only)"}
FETCH_HOURS = 1100


def _http_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_klines(symbol: str, *, hours: int = FETCH_HOURS, interval: str = "1h",
                 now: pd.Timestamp | None = None, http=_http_json) -> pd.DataFrame:
    """Public Binance.US klines -> DataFrame indexed by open time (UTC): o h l c qv ct.
    Includes the still-open bar; callers split closed vs open with ``ct``."""
    now = now or pd.Timestamp.now(tz="UTC")
    step = 3_600_000 if interval == "1h" else 86_400_000
    start = int((now - pd.Timedelta(hours=hours)).timestamp() * 1000)
    rows: list = []
    while True:
        d = http(f"{BUS}?symbol={symbol}&interval={interval}&startTime={start}&limit=1000")
        if isinstance(d, dict):
            raise RuntimeError(f"{symbol}: {d}")
        rows += d
        if len(d) < 1000:
            break
        start = d[-1][0] + step
    if not rows:
        raise RuntimeError(f"{symbol}: no klines")
    df = pd.DataFrame(rows).iloc[:, :8]
    df.columns = ["t", "o", "h", "l", "c", "v", "ct", "qv"]
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.set_index("t")[["o", "h", "l", "c", "qv", "ct"]].astype(float)
    return df[~df.index.duplicated()].sort_index()


def fetch_coin(coin: str, *, now: pd.Timestamp | None = None, http=_http_json) -> pd.DataFrame:
    """Price = <COIN>USDT; quote volume = USDT + USD pair volume (Binance.US is thin, so both
    pairs are summed - same convention as shadow/capit24)."""
    px = fetch_klines(f"{coin}USDT", now=now, http=http)
    try:
        qv2 = fetch_klines(f"{coin}USD", now=now, http=http)["qv"]
        px["qv"] = px["qv"] + qv2.reindex(px.index).fillna(0.0)
    except Exception:  # USD pair missing/broken -> USDT volume only
        pass
    return px


def btc_r1_series(*, now: pd.Timestamp | None = None, http=_http_json) -> pd.Series:
    """Pre-specified R1 variant flag (LOGGED ONLY, never a switch): BTC daily close > SMA200.
    Indexed by the time the daily bar CLOSED (open + 1 day)."""
    d = fetch_klines("BTCUSDT", hours=420 * 24, interval="1d", now=now, http=http)
    now = now or pd.Timestamp.now(tz="UTC")
    d = d[d["ct"] / 1000 < now.timestamp()]
    sma = d["c"].rolling(200).mean()
    flag = (d["c"] > sma).astype(float).where(sma.notna())
    flag.index = flag.index + pd.Timedelta("1D")
    return flag


def r1_at(series: pd.Series | None, ts: pd.Timestamp):
    if series is None or not len(series):
        return None
    s = series[series.index <= ts]
    if not len(s) or pd.isna(s.iloc[-1]):
        return None
    return bool(s.iloc[-1])


def _iso(t: pd.Timestamp) -> str:
    return pd.Timestamp(t).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s).tz_convert("UTC") if pd.Timestamp(s).tzinfo else pd.Timestamp(s, tz="UTC")


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(p: Path, obj) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


def _append(p: Path, rec: dict) -> None:
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def _jsonl(p: Path) -> list[dict]:
    out: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


class CapitPaper:
    """One paper cycle per ``run_cycle()``. Catch-up safe and idempotent:
    a cursor (last decided signal bar) lives in state.json; trade/signal ids are de-duplicated."""

    def __init__(self, out_dir: Path | str, *, coins=COINS, start_equity: float = START_EQUITY,
                 fetch: Callable[[str], pd.DataFrame] | None = None,
                 fetch_r1: Callable[[], pd.Series] | None = None,
                 now: Callable[[], pd.Timestamp] | None = None) -> None:
        self.out = Path(out_dir)
        self.coins = tuple(coins)
        self.seed = float(start_equity)
        self.now = now or (lambda: pd.Timestamp.now(tz="UTC"))
        self.fetch = fetch or (lambda c: fetch_coin(c, now=self.now()))
        self.fetch_r1 = fetch_r1 if fetch_r1 is not None else (lambda: btc_r1_series(now=self.now()))
        self.state_p = self.out / "state.json"
        self.status_p = self.out / "status.json"
        self.trades_p = self.out / "closed_trades.jsonl"
        self.signals_p = self.out / "signals.jsonl"
        self.equity_p = self.out / "equity.jsonl"
        self.events_p = self.out / "events.jsonl"

    # ── helpers ──
    def _load_state(self) -> dict:
        return _read_json(self.state_p, {})

    def _trade_rows(self, book: str, entry_px: float, exit_px: float, pos: dict, exit_t) -> dict:
        gross = exit_px / entry_px - 1.0
        cost_key = BOOKS[book]
        net = gross - COSTS[cost_key]
        notional = slot_usd(self.seed)
        tid = pos["trade_id"] + ("" if book == PRIMARY else f":{book}")
        return {
            "trade_id": tid, "sleeve": SLEEVES[book], "strategy": "CAPIT-24",
            "venue": "binanceus" if book == PRIMARY else "kraken (shadow, same signals/fills)",
            "fee_book": book, "fee_model": cost_key, "cost_rt_bps": round(1e4 * COSTS[cost_key], 2),
            "symbol": f"{pos['coin']}/USDT", "coin": pos["coin"], "side": "long",
            "signal_t": pos["signal_t"], "entry_t": pos["entry_t"], "exit_t": _iso(exit_t),
            "entry_ts": _ts(pos["entry_t"]).timestamp(), "ts": pd.Timestamp(exit_t).timestamp(),
            "entry_px": entry_px, "exit_px": exit_px, "notional_usd": notional,
            "gross_bps": round(1e4 * gross, 2), "net_bps": round(1e4 * net, 2),
            "pnl": round(notional * net, 6),
            "net_bps_all": {k: round(1e4 * v, 2) for k, v in trade_costs(gross).items()},
            "z": pos["z"], "volr": pos["volr"], "r1_btc_above_sma200": pos.get("r1"),
            "exit_reason": "time_48h", "hold_h": HOLD_H, "source": "fill", "paper": True,
        }

    # ── one cycle ──
    def run_cycle(self) -> dict:
        self.out.mkdir(parents=True, exist_ok=True)
        lock = (self.out / "cycle.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"ok": False, "error": "another cycle holds cycle.lock"}
        try:
            return self._cycle()
        except Exception as e:  # no partial decisions: state is only written at the end
            st = _read_json(self.status_p, {})
            st.update({"ok": False, "last_error": f"{type(e).__name__}: {e}",
                       "last_error_at": _iso(self.now())})
            _write_json(self.status_p, st)
            _append(self.events_p, {"ts": _iso(self.now()), "event": "cycle_error", "error": str(e)})
            raise
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    def _cycle(self) -> dict:
        now = self.now()
        data = {c: self.fetch(c) for c in self.coins}          # any failure aborts the cycle
        try:
            r1s = self.fetch_r1() if self.fetch_r1 else None
        except Exception:
            r1s = None
        closed = {c: d[d["ct"] / 1000 < now.timestamp()] for c, d in data.items()}
        feats = {c: features(closed[c]) for c in self.coins}
        last_closed = min(closed[c].index[-1] for c in self.coins)
        last_open = min(data[c].index[-1] for c in self.coins)   # newest bar whose OPEN is known
        st = self._load_state()
        if not st:
            st = {"strategy": "CAPIT-24 frozen", "started_at": _iso(now), "seed_equity": self.seed,
                  "seed_cursor": _iso(last_closed), "cursor": _iso(last_closed), "open": [],
                  "killed": {b: False for b in BOOKS}}
            _append(self.events_p, {"ts": _iso(now), "event": "start", "seed_cursor": st["cursor"],
                                    "note": "forward paper test starts after this bar; no backfill"})
        cursor = _ts(st["cursor"])
        open_pos: list[dict] = st.get("open", [])
        have_ids = {r.get("trade_id") for r in _jsonl(self.trades_p)}
        have_sig = {r.get("id") for r in _jsonl(self.signals_p)}
        new_trades: list[dict] = []
        new_signals: list[dict] = []

        def close_due(upto: pd.Timestamp) -> None:
            nonlocal open_pos
            keep = []
            for p in open_pos:
                x = _ts(p["exit_t"])
                d = data[p["coin"]]
                if x <= upto and x in d.index:
                    xp = float(d.loc[x, "o"])
                    for book in BOOKS:
                        row = self._trade_rows(book, float(p["entry_px"]), xp, p, x)
                        if row["trade_id"] not in have_ids:
                            _append(self.trades_p, row)
                            have_ids.add(row["trade_id"])
                            new_trades.append(row)
                else:
                    keep.append(p)
            open_pos = keep

        t = cursor + H
        while t + H <= last_open and t <= last_closed:
            # a slot frees for signals on bars t >= its exit time (candidate.py: keep if exit > t)
            close_due(t)
            cands = []
            for c in self.coins:
                f = feats[c]
                if t not in f.index:
                    continue
                zz, vr = float(f.at[t, "z"]), float(f.at[t, "volr"])
                if is_signal(zz, vr):
                    cands.append((zz, c, vr))
            for zz, c, vr in sorted(cands):
                e, x = t + H, t + H + pd.Timedelta(hours=HOLD_H)
                sid = f"{c}-{_iso(t)}"
                rec = {"id": sid, "coin": c, "signal_t": _iso(t), "entry_t": _iso(e), "exit_t": _iso(x),
                       "z": round(zz, 4), "volr": round(vr, 4), "r1_btc_above_sma200": r1_at(r1s, e),
                       "source": "api.binance.us klines 1h"}
                if st["killed"].get(PRIMARY):
                    rec["taken"], rec["reason"] = False, "primary book KILLED - no new entries"
                elif len(open_pos) >= MAX_POS:
                    rec["taken"], rec["reason"] = False, "max 2 positions"
                elif any(p["coin"] == c for p in open_pos):
                    rec["taken"], rec["reason"] = False, "already holding coin"
                elif e not in data[c].index:
                    rec["taken"], rec["reason"] = False, "no entry bar"
                else:
                    px = float(data[c].loc[e, "o"])
                    open_pos.append({"trade_id": f"capit24-{c}-{_iso(e)}", "coin": c, "signal_t": _iso(t),
                                     "entry_t": _iso(e), "exit_t": _iso(x), "z": round(zz, 4),
                                     "volr": round(vr, 4), "entry_px": px, "r1": rec["r1_btc_above_sma200"],
                                     "notional_usd": slot_usd(self.seed)})
                    rec["taken"], rec["entry_px"] = True, px
                if sid not in have_sig:
                    _append(self.signals_p, rec)
                    have_sig.add(sid)
                    new_signals.append(rec)
            st["cursor"] = _iso(t)
            t += H
        close_due(last_open)

        # ── books, kill rules, equity ──
        all_rows = _jsonl(self.trades_p)
        books = {}
        for book in BOOKS:
            rows = [r for r in all_rows if r.get("fee_book") == book]
            kc = kill_check(rows, now_ts=now.timestamp(), seed=self.seed)
            if kc["killed"] and not st["killed"].get(book):
                st["killed"][book] = True
                _append(self.events_p, {"ts": _iso(now), "event": "KILL", "book": book, "detail": kc})
            kc["killed_latched"] = bool(st["killed"].get(book))
            books[book] = kc
        unreal = {}
        for p in open_pos:
            last_px = float(closed[p["coin"]]["c"].iloc[-1])
            g = last_px / float(p["entry_px"]) - 1.0
            unreal[p["trade_id"]] = {"coin": p["coin"], "mark_px": last_px, "gross_bps": round(1e4 * g, 1),
                                     "unrealized_usd_binanceus": round(slot_usd(self.seed) * (g - COSTS["bus_base"]), 4)}
        u_bus = sum(v["unrealized_usd_binanceus"] for v in unreal.values())
        eq = {"ts": now.timestamp(), "iso": _iso(now), "seed": self.seed,
              "realized": books[PRIMARY]["pnl_usd"],
              "equity": round(self.seed + books[PRIMARY]["pnl_usd"] + u_bus, 4),
              "open_positions": len(open_pos), "book": PRIMARY}
        _append(self.equity_p, eq)

        near = {}
        for c in self.coins:
            f = feats[c].dropna()
            if len(f):
                near[c] = {"bar": _iso(f.index[-1]), "z": round(float(f["z"].iloc[-1]), 3),
                           "volr": round(float(f["volr"].iloc[-1]), 3),
                           "close": float(closed[c]["c"].iloc[-1]),
                           "signal": is_signal(float(f["z"].iloc[-1]), float(f["volr"].iloc[-1]))}
        st["open"] = open_pos
        st["last_cycle"] = _iso(now)
        _write_json(self.state_p, st)
        status = {
            "ok": True, "paper_only": True, "orders_placed": 0, "strategy": "CAPIT-24 (frozen)",
            "rule": {"z": -Z, "vol_mult": VM, "N_h": N, "hold_h": HOLD_H, "max_pos": MAX_POS,
                     "slot_usd": slot_usd(self.seed), "entry": "next 1h open", "exit": "open 48h later"},
            "coins": list(self.coins), "venue_data": "api.binance.us public klines (USDT price; USDT+USD volume)",
            "fee_books": {b: {"cost_key": k, "round_trip_bps": round(1e4 * COSTS[k], 1)} for b, k in BOOKS.items()},
            "last_cycle": _iso(now), "last_closed_bar": _iso(last_closed), "cursor": st["cursor"],
            "started_at": st["started_at"], "seed_cursor": st["seed_cursor"],
            "open_positions": open_pos, "unrealized": unreal, "equity_binanceus": eq["equity"],
            "new_signals_this_cycle": new_signals, "new_closed_trades_this_cycle": len(new_trades),
            "books": books, "killed": st["killed"], "latest_bar_by_coin": near,
            "last_error": None,
        }
        _write_json(self.status_p, status)
        try:  # report only; never changes a lock
            from .promotion import write_report
            write_report(self.out, now=now.timestamp())
        except Exception as e:  # pragma: no cover
            _append(self.events_p, {"ts": _iso(now), "event": "promotion_report_error", "error": str(e)})
        return status
