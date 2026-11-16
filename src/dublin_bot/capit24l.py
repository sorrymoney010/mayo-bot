"""CAPIT-24L — the MAIN PLAN strategy (approved 2026-11-16): CAPIT-24 signal, post-only LIMIT entry,
Binance.US fees. PAPER ONLY.

This module never places orders, never reads keys or .env, never calls a signed/private endpoint,
and never touches the PAPER_TRADING / DRY_RUN / ALLOW_LIVE_TRADING locks. It reads public
Binance.US klines (1h for the signal, 1m for the limit-fill window) and keeps a paper ledger.

Rules (docs/MAIN_PLAN_CAPIT24L_BINANCEUS.md):
  Signal   on each CLOSED 1h bar t (open time t, closes t+1h), per coin, unchanged CAPIT-24:
           z24 <= -2.5 AND volr >= 2.0   (same formulas as capit24.features)
  Entry    at t+1h place a post-only limit buy at L = 0.995 * close(t), valid 60 minutes
           (window [t+1h, t+2h)). Placement price P = open of the first 1m bar of the window.
           If P <= L a post-only order at L would cross and be rejected, so the paper rule rests
           the bid at P instead (L_eff = min(L, P)).
           STRICT fill: filled only if some 1m low in the window is STRICTLY below L_eff; the fill
           time is that minute's close, fill price = L_eff. A window whose lows reach L_eff but never
           trade through it is a TOUCH (logged, counted as NOT filled). Unfilled -> skip the signal.
  Exit     market sell 48h after the fill: price = open of the 1m bar starting at fill time + 48h.
  Book     VET, HBAR, ALGO, XLM. 2 slots x $50 on $100, 1 per coin, spot long only, ties -> most
           negative z. A slot is free for an order placed at time T if its exit time <= T.
  Floor    if book equity (seed + realized + unrealized at the primary fee) <= $70: no new entries
           (latched; needs the owner's review to clear).
  Kills    (primary book; latched; stop new entries)
           K1: >= 30 closed trades and PF < 1.10
           K2: the last two completed calendar quarters both net negative AND equity < seed
           K4: once >= 15 trades have closed, the FIRST 15 average below -100 bps net
Fee books (same signals and fills; only the cost differs, round trip):
  binanceus_l (PRIMARY): 0% maker entry + 0.02% taker exit + 3 bps exit slip = 5 bps
  binanceus10 (conservative): 10 bps (the old CAPIT-24 primary model)
  kraken125 (shadow): Kraken tier-1 0.40% maker entry + 0.80% taker exit + 5 bps slip = 125 bps
"""
from __future__ import annotations

import fcntl
import math
from pathlib import Path
from typing import Callable

import pandas as pd

from .capit24 import (BUS, COINS, START_EQUITY, _append, _http_json, _iso, _jsonl, _read_json, _ts,
                      _write_json, features, is_signal, profit_factor, quarter_of)

STRATEGY = "CAPIT-24L"
LIMIT_OFFSET = 0.005          # limit = 0.995 x signal-bar close
WINDOW_MIN = 60               # order lives 60 minutes
HOLD_H = 48
MAX_POS = 2
SLOT_USD = 50.0
FLOOR_USD = 70.0
H = pd.Timedelta("1h")
M = pd.Timedelta("1min")
COSTS = {"binanceus_l": 0.0005, "binanceus10": 0.0010, "kraken125": 0.0125}
PRIMARY = "binanceus_l"
SLEEVES = {"binanceus_l": "capit24l_binanceus", "binanceus10": "capit24l_binanceus10_shadow",
           "kraken125": "capit24l_kraken125_shadow"}
K1_MIN_TRADES, K1_MIN_PF = 30, 1.10
K4_N, K4_MIN_MEAN_BPS = 15, -100.0
FETCH_HOURS = 1500


# ───────────────────────── pure rule pieces (unit-tested) ─────────────────────────

def limit_price(signal_close: float) -> float:
    return float(signal_close) * (1.0 - LIMIT_OFFSET)


def evaluate_limit(m1: pd.DataFrame, place_t: pd.Timestamp, limit: float) -> dict:
    """m1: 1m bars (index = open time UTC; columns o h l c). Evaluates the 60-minute window
    [place_t, place_t + 60m). Returns status filled / touch / unfilled / no_data."""
    w = m1[(m1.index >= place_t) & (m1.index < place_t + pd.Timedelta(minutes=WINDOW_MIN))]
    if not len(w):
        return {"status": "no_data", "limit": limit, "limit_eff": None, "bars": 0}
    p = float(w["o"].iloc[0])
    eff = min(float(limit), p)
    lows = w["l"].astype(float)
    below = lows[lows < eff]
    touched = bool((lows <= eff).any())
    out = {"limit": float(limit), "limit_eff": eff, "placement_px": p, "repriced": p <= limit,
           "bars": int(len(w)), "window_low": float(lows.min()), "touched": touched}
    if len(below):
        out.update(status="filled", fill_px=eff, fill_t=below.index[0] + M)
    elif touched:
        out.update(status="touch")
    else:
        out.update(status="unfilled")
    return out


def exit_time(fill_t: pd.Timestamp) -> pd.Timestamp:
    return pd.Timestamp(fill_t) + pd.Timedelta(hours=HOLD_H)


def net_of(gross: float, book: str) -> float:
    return gross - COSTS[book]


def kill_check(rows: list[dict], *, now_ts: float, seed: float = START_EQUITY) -> dict:
    """rows: closed trades of ONE fee book in exit order (fields net_bps, pnl, ts=exit epoch)."""
    rows = sorted(rows, key=lambda r: float(r["ts"]))
    n = len(rows)
    nets = [float(r["net_bps"]) for r in rows]
    pf = profit_factor(nets)
    pnl = sum(float(r["pnl"]) for r in rows)
    equity = seed + pnl
    k1 = n >= K1_MIN_TRADES and pf is not None and pf < K1_MIN_PF
    by_q: dict[str, float] = {}
    for r in rows:
        q = quarter_of(float(r["ts"]))
        by_q[q] = by_q.get(q, 0.0) + float(r["pnl"])
    t = pd.Timestamp(now_ts, unit="s", tz="UTC").tz_localize(None).to_period("Q")
    last_two = [str(t - 2), str(t - 1)]
    k2 = all(q in by_q and by_q[q] < 0 for q in last_two) and equity < seed
    first = nets[:K4_N]
    k4 = n >= K4_N and (sum(first) / K4_N) < K4_MIN_MEAN_BPS
    return {"trades": n, "pf": None if pf is None else ("inf" if pf == math.inf else round(pf, 3)),
            "mean_net_bps": round(sum(nets) / n, 2) if n else None,
            "first15_mean_bps": round(sum(first) / K4_N, 2) if n >= K4_N else None,
            "pnl_usd": round(pnl, 4), "equity_realized": round(equity, 4), "seed": seed,
            "quarters": {q: round(v, 4) for q, v in sorted(by_q.items())},
            "k1_pf_below_1_10_after_30": k1, "k2_two_neg_quarters_and_below_cash": k2,
            "k4_first15_mean_below_minus_100bps": k4, "killed": bool(k1 or k2 or k4)}


def floor_hit(equity: float) -> bool:
    return equity <= FLOOR_USD


# ───────────────────────── public data ─────────────────────────

def fetch_1m(symbol: str, start: pd.Timestamp, minutes: int, *, http=_http_json) -> pd.DataFrame:
    """Public Binance.US 1m klines from ``start`` (UTC) for ``minutes`` bars (<= 1000)."""
    s = int(pd.Timestamp(start).timestamp() * 1000)
    d = http(f"{BUS}?symbol={symbol}&interval=1m&startTime={s}&limit={int(minutes)}")
    if isinstance(d, dict):
        raise RuntimeError(f"{symbol}: {d}")
    if not d:
        return pd.DataFrame(columns=["o", "h", "l", "c", "ct"])
    df = pd.DataFrame(d).iloc[:, :7]
    df.columns = ["t", "o", "h", "l", "c", "v", "ct"]
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    return df.set_index("t")[["o", "h", "l", "c", "ct"]].astype(float)


# ───────────────────────── paper runner ─────────────────────────

class Capit24LPaper:
    """One paper cycle per ``run_cycle()``. Catch-up safe and idempotent: a cursor (last decided
    signal bar) lives in state.json; trade and signal ids are de-duplicated. A signal bar t is only
    decided once its whole 60-minute limit window has closed (now >= t + 2h)."""

    def __init__(self, out_dir: Path | str, *, coins=COINS, start_equity: float = START_EQUITY,
                 fetch_1h: Callable[[str], pd.DataFrame] | None = None,
                 fetch_m1: Callable[[str, pd.Timestamp, int], pd.DataFrame] | None = None,
                 now: Callable[[], pd.Timestamp] | None = None,
                 seed_cursor: pd.Timestamp | None = None) -> None:
        self.out = Path(out_dir)
        self.coins = tuple(coins)
        self.seed = float(start_equity)
        self.now = now or (lambda: pd.Timestamp.now(tz="UTC"))
        self.fetch_1h = fetch_1h or (lambda c: _fetch_1h(c, self.now()))
        self.fetch_m1 = fetch_m1 or (lambda c, s, n: fetch_1m(f"{c}USDT", s, n))
        self.seed_cursor = seed_cursor
        self.state_p = self.out / "state.json"
        self.status_p = self.out / "status.json"
        self.trades_p = self.out / "closed_trades.jsonl"
        self.signals_p = self.out / "signals.jsonl"
        self.equity_p = self.out / "equity.jsonl"
        self.events_p = self.out / "events.jsonl"

    def run_cycle(self) -> dict:
        self.out.mkdir(parents=True, exist_ok=True)
        lock = (self.out / "cycle.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"ok": False, "error": "another cycle holds cycle.lock"}
        try:
            return self._cycle()
        except Exception as e:
            st = _read_json(self.status_p, {})
            st.update({"ok": False, "last_error": f"{type(e).__name__}: {e}", "last_error_at": _iso(self.now())})
            _write_json(self.status_p, st)
            _append(self.events_p, {"ts": _iso(self.now()), "event": "cycle_error", "error": str(e)})
            raise
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    def _rows(self, pos: dict, exit_px: float, exit_t: pd.Timestamp) -> list[dict]:
        gross = exit_px / float(pos["entry_px"]) - 1.0
        out = []
        for book, c in COSTS.items():
            net = gross - c
            out.append({
                "trade_id": pos["trade_id"] + ("" if book == PRIMARY else f":{book}"),
                "sleeve": SLEEVES[book], "strategy": STRATEGY, "fee_book": book,
                "cost_rt_bps": round(1e4 * c, 2), "venue": "binanceus" if book != "kraken125" else "kraken (shadow)",
                "symbol": f"{pos['coin']}/USDT", "coin": pos["coin"], "side": "long",
                "signal_t": pos["signal_t"], "place_t": pos["place_t"], "entry_t": pos["entry_t"],
                "exit_t": _iso(exit_t), "entry_ts": _ts(pos["entry_t"]).timestamp(),
                "ts": pd.Timestamp(exit_t).timestamp(), "entry_px": float(pos["entry_px"]),
                "exit_px": exit_px, "notional_usd": SLOT_USD, "gross_bps": round(1e4 * gross, 2),
                "net_bps": round(1e4 * net, 2), "pnl": round(SLOT_USD * net, 6), "z": pos["z"],
                "volr": pos["volr"], "exit_reason": "time_48h_market", "entry_type": "post_only_limit",
                "source": "fill", "paper": True})
        return out

    def _cycle(self) -> dict:
        now = self.now()
        data = {c: self.fetch_1h(c) for c in self.coins}       # any failure aborts the cycle
        closed = {c: d[d["ct"] / 1000 < now.timestamp()] for c, d in data.items()}
        feats = {c: features(closed[c]) for c in self.coins}
        last_closed = min(closed[c].index[-1] for c in self.coins)
        st = _read_json(self.state_p, {})
        if not st:
            seed = pd.Timestamp(self.seed_cursor) if self.seed_cursor is not None else last_closed
            st = {"strategy": STRATEGY, "started_at": _iso(now), "seed_equity": self.seed,
                  "seed_cursor": _iso(seed), "cursor": _iso(seed), "open": [], "killed": False,
                  "kill_detail": None, "floor_halt": False}
            _append(self.events_p, {"ts": _iso(now), "event": "start", "seed_cursor": st["cursor"],
                                    "note": "forward paper test: signal bars after the seed cursor only"})
        open_pos: list[dict] = st.get("open", [])
        have_ids = {r.get("trade_id") for r in _jsonl(self.trades_p)}
        have_sig = {r.get("id") for r in _jsonl(self.signals_p)}
        new_sig: list[dict] = []
        new_tr: list[dict] = []
        m1_cache: dict = {}

        def m1(c, start, n):
            key = (c, _iso(start), n)
            if key not in m1_cache:
                m1_cache[key] = self.fetch_m1(c, start, n)
            return m1_cache[key]

        def close_due(upto: pd.Timestamp) -> None:
            nonlocal open_pos
            keep = []
            for p in open_pos:
                x = _ts(p["exit_t"])
                if x <= upto and x + M <= now:
                    bars = m1(p["coin"], x, 5)
                    bars = bars[bars.index >= x]
                    if len(bars):
                        xp = float(bars["o"].iloc[0])
                        for row in self._rows(p, xp, bars.index[0]):
                            if row["trade_id"] not in have_ids:
                                _append(self.trades_p, row)
                                have_ids.add(row["trade_id"])
                                new_tr.append(row)
                        continue
                keep.append(p)
            open_pos = keep

        def realized(book=PRIMARY):
            return [r for r in _jsonl(self.trades_p) if r.get("fee_book") == book]

        def mark_equity(at: pd.Timestamp) -> float:
            eq = self.seed + sum(float(r["pnl"]) for r in realized())
            for p in open_pos:
                k = closed[p["coin"]]
                k = k[k.index + H <= at]
                if len(k):
                    g = float(k["c"].iloc[-1]) / float(p["entry_px"]) - 1.0
                    eq += SLOT_USD * (g - COSTS[PRIMARY])
            return eq

        t = _ts(st["cursor"]) + H
        while t <= last_closed and t + 2 * H <= now:
            place = t + H
            close_due(place)
            kc = kill_check(realized(), now_ts=place.timestamp(), seed=self.seed)
            if kc["killed"] and not st["killed"]:
                st["killed"], st["kill_detail"] = True, kc
                _append(self.events_p, {"ts": _iso(place), "event": "KILL", "detail": kc})
            eq_now = mark_equity(place)
            if floor_hit(eq_now) and not st["floor_halt"]:
                st["floor_halt"] = True
                _append(self.events_p, {"ts": _iso(place), "event": "FLOOR_HALT", "equity": round(eq_now, 4)})
            cands = []
            for c in self.coins:
                f = feats[c]
                if t in f.index and is_signal(float(f.at[t, "z"]), float(f.at[t, "volr"])):
                    cands.append((float(f.at[t, "z"]), c, float(f.at[t, "volr"])))
            for zz, c, vr in sorted(cands):
                sid = f"{c}-{_iso(t)}"
                lim = limit_price(float(closed[c].loc[t, "c"]))
                rec = {"id": sid, "coin": c, "signal_t": _iso(t), "place_t": _iso(place),
                       "expire_t": _iso(place + pd.Timedelta(minutes=WINDOW_MIN)), "z": round(zz, 4),
                       "volr": round(vr, 4), "signal_close": float(closed[c].loc[t, "c"]), "limit": lim,
                       "source": "api.binance.us klines 1h + 1m"}
                if st["killed"]:
                    rec.update(taken=False, reason="KILLED - no new entries")
                elif st["floor_halt"]:
                    rec.update(taken=False, reason=f"equity <= ${FLOOR_USD:.0f} floor - no new entries")
                elif len(open_pos) >= MAX_POS:
                    rec.update(taken=False, reason="max 2 positions")
                elif any(p["coin"] == c for p in open_pos):
                    rec.update(taken=False, reason="already holding coin")
                else:
                    ev = evaluate_limit(m1(c, place, WINDOW_MIN), place, lim)
                    rec["limit_eval"] = {k: (_iso(v) if isinstance(v, pd.Timestamp) else v) for k, v in ev.items()}
                    if ev["status"] == "filled":
                        ft = ev["fill_t"]
                        pos = {"trade_id": f"capit24l-{c}-{_iso(place)}", "coin": c, "signal_t": _iso(t),
                               "place_t": _iso(place), "entry_t": _iso(ft), "exit_t": _iso(exit_time(ft)),
                               "entry_px": ev["fill_px"], "z": round(zz, 4), "volr": round(vr, 4),
                               "notional_usd": SLOT_USD}
                        open_pos.append(pos)
                        rec.update(taken=True, fill="strict", entry_px=ev["fill_px"], entry_t=_iso(ft))
                    else:
                        rec.update(taken=False, fill=ev["status"],
                                   reason={"touch": "limit touched but never traded through - not filled",
                                           "unfilled": "limit not reached in 60 min - skipped",
                                           "no_data": "no 1m bars in window - skipped"}[ev["status"]])
                if sid not in have_sig:
                    _append(self.signals_p, rec)
                    have_sig.add(sid)
                    new_sig.append(rec)
            st["cursor"] = _iso(t)
            t += H
        close_due(now)

        books = {b: kill_check(realized(b), now_ts=now.timestamp(), seed=self.seed) for b in COSTS}
        if books[PRIMARY]["killed"] and not st["killed"]:
            st["killed"], st["kill_detail"] = True, books[PRIMARY]
            _append(self.events_p, {"ts": _iso(now), "event": "KILL", "detail": books[PRIMARY]})
        eq = mark_equity(now)
        if floor_hit(eq) and not st["floor_halt"]:
            st["floor_halt"] = True
            _append(self.events_p, {"ts": _iso(now), "event": "FLOOR_HALT", "equity": round(eq, 4)})
        _append(self.equity_p, {"ts": now.timestamp(), "iso": _iso(now), "seed": self.seed,
                                "equity": round(eq, 4), "open_positions": len(open_pos), "book": PRIMARY})
        sigs = _jsonl(self.signals_p)
        fills = {k: sum(1 for s in sigs if s.get("fill") == k) for k in ("strict", "touch", "unfilled", "no_data")}
        near = {}
        for c in self.coins:
            f = feats[c].dropna()
            if len(f):
                near[c] = {"bar": _iso(f.index[-1]), "z": round(float(f["z"].iloc[-1]), 3),
                           "volr": round(float(f["volr"].iloc[-1]), 3), "close": float(closed[c]["c"].iloc[-1])}
        st["open"] = open_pos
        st["last_cycle"] = _iso(now)
        _write_json(self.state_p, st)
        status = {"ok": True, "paper_only": True, "orders_placed": 0, "strategy": STRATEGY,
                  "rule": {"z": -2.5, "vol_mult": 2.0, "limit_offset": LIMIT_OFFSET, "window_min": WINDOW_MIN,
                           "fill": "strictly below limit (touch logged, not filled)", "hold_h": HOLD_H,
                           "exit": "market, 1m open at fill+48h", "max_pos": MAX_POS, "slot_usd": SLOT_USD,
                           "floor_usd": FLOOR_USD},
                  "coins": list(self.coins), "fee_books_rt_bps": {b: round(1e4 * c, 1) for b, c in COSTS.items()},
                  "last_cycle": _iso(now), "last_closed_bar": _iso(last_closed), "cursor": st["cursor"],
                  "started_at": st["started_at"], "seed_cursor": st["seed_cursor"], "open_positions": open_pos,
                  "equity_primary": round(eq, 4), "floor_halt": st["floor_halt"], "killed": st["killed"],
                  "fill_counts": fills, "new_signals_this_cycle": new_sig,
                  "new_closed_trades_this_cycle": len(new_tr), "books": books, "latest_bar_by_coin": near,
                  "last_error": None}
        _write_json(self.status_p, status)
        try:  # report only; never changes a lock
            from .promotion import write_report
            write_report(self.out, now=now.timestamp())
        except Exception as e:  # pragma: no cover
            _append(self.events_p, {"ts": _iso(now), "event": "promotion_report_error", "error": str(e)})
        return status


def _fetch_1h(coin: str, now: pd.Timestamp) -> pd.DataFrame:
    from .capit24 import fetch_klines
    px = fetch_klines(f"{coin}USDT", hours=FETCH_HOURS, now=now)
    try:
        qv2 = fetch_klines(f"{coin}USD", hours=FETCH_HOURS, now=now)["qv"]
        px["qv"] = px["qv"] + qv2.reindex(px.index).fillna(0.0)
    except Exception:
        pass
    return px
