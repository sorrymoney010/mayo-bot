"""TREND-6 — daily trend ensemble on BTC + ETH, Binance.US. PAPER ONLY (approved 2026-11-16).

This module never places orders, never reads keys or .env, never calls a signed/private endpoint,
and never touches the PAPER_TRADING / DRY_RUN / ALLOW_LIVE_TRADING locks. It reads public daily
candles and keeps a paper ledger. VENUE RULE: Binance.US only. Live daily bars and every paper fill
come from Binance.US public klines. Binance.US's own history has a hole (USD pairs suspended
2023-07 .. 2025-02); when the contiguous Binance.US tail is shorter than the 200-day SMA needs, the
missing EARLIER closes are warmed up from Binance global public archives (data.binance.vision
BTCUSDT/ETHUSDT 1d - the same data the backtest used, with Binance.US fees). Each decision records
which source fed it. No Coinbase, no Kraken data or execution.

Rule (verified in reports/trend6_verification_2026-11-16.md, commit ff9bbc9):
  Decide   after each CLOSED 00:00 UTC daily bar D, per coin, six binary signals on closes up to and
           including D: close > SMA50 / SMA100 / SMA200, and close_D / close_{D-k} - 1 > 0 for
           k = 7, 14, 28. Target weight = 0.5 x (yes / 6)  (0, 1/12, ..., 1/2 of book equity).
  Execute  at the OPEN of day D+1 (daily kline open; fallback first 1h bar open). No look-ahead:
           nothing from D+1 except its open price is used.
  Band     a coin is traded only when its target changes (signal count changed); drift alone is
           never traded (verified equivalent within 0.5 pt). A change whose notional is below the
           Binance.US $1 minimum is skipped and logged (skipped.jsonl).
  Books    same decisions, separate ledgers per cost model (cost per side on traded notional):
           binanceus    PRIMARY: 0.02% taker + 2 bps slip = 4 bps/side (8 bps RT). The next-open
                        fill is modelled as a taker order; 0% maker would be the best case, not assumed.
           binanceus10  conservative: 5 bps/side (10 bps RT, the verified backtest model)
           ref_kraken80 / ref_kraken160  REFERENCE COLUMNS ONLY (Kraken maker/taker cost); never an
                        execution venue - they show what the same decisions would cost there.
  Kills    on the PRIMARY book, latched. When killed: no new buys (sells/exits still run).
           K-DD  (any time) max drawdown at the daily opens > 45%.
           K-REL (only once >= 30 closed round trips exist) ALL of:
                 (a) strategy return since paper start < 50/50 buy-and-hold return, same period;
                 (b) equity more than 10% below its running peak;
                 (c) equity lower than 30 days earlier (still drawing down).
           (Owner-approved 2026-11-16; replaces the CAPIT-style PF < 1.10 after 30 rule, which
           would have falsely killed TREND-6 from 7 of 26 historical start dates.)
  Trades   a round trip = units go from 0 to > 0 and back to 0 in one coin (one book).
  Benchmark 50/50 BTC/ETH bought at the first execution open, held, no cost.
"""
from __future__ import annotations

import fcntl
import io
import json
import math
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

import pandas as pd

STRATEGY = "TREND-6"
COINS = ("BTC", "ETH")
BUS_SYMBOL = {"BTC": "BTCUSD", "ETH": "ETHUSD"}
VISION_SYMBOL = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
BUS = "https://api.binance.us/api/v3/klines"
VISION = "https://data.binance.vision/data/spot/{kind}/klines/{sym}/1d/{sym}-1d-{period}.zip"
UA = {"User-Agent": "mayo-trend6-paper/1.0 (public data, paper only)"}
START_EQUITY = 100.0
PER_COIN = 0.5
SMAS = (50, 100, 200)
MOMS = (7, 14, 28)
NEED = 201                     # closes needed for SMA200 (200) + safety
MIN_NOTIONAL = 1.0             # Binance.US spot minimum order value (USD)
SIDE_COST = {"binanceus": 0.0004, "binanceus10": 0.0005, "ref_kraken80": 0.0040, "ref_kraken160": 0.0080}
PRIMARY = "binanceus"
REFERENCE_ONLY = ("ref_kraken80", "ref_kraken160")
KILL_DD = 0.45
KILL_MIN_TRADES = 30
KILL_REL_DD = 0.10
KILL_LOOKBACK_DAYS = 30
D1 = pd.Timedelta("1D")


# ───────────────────────── pure rule pieces (unit-tested) ─────────────────────────

def signals_at(close: pd.Series, day: pd.Timestamp) -> dict | None:
    """close: daily closes indexed by bar OPEN date (UTC midnight). Uses closes <= day only.
    Returns {'signals': {name: bool}, 'yes': int, 'weight': float} or None if not enough history."""
    c = close[close.index <= day].dropna()
    if len(c) < NEED or c.index[-1] != day:
        return None
    last = float(c.iloc[-1])
    sig = {}
    for n in SMAS:
        sig[f"sma{n}"] = bool(last > float(c.iloc[-n:].mean()))
    for k in MOMS:
        prev = c.index[-1] - pd.Timedelta(days=k)
        if prev not in c.index:
            return None
        sig[f"ret{k}"] = bool(last / float(c.loc[prev]) - 1 > 0)
    yes = sum(sig.values())
    return {"signals": sig, "yes": int(yes), "weight": target_weight(yes), "close": last}


def target_weight(yes: int) -> float:
    return PER_COIN * int(yes) / 6.0


def profit_factor(vals: list[float]) -> float | None:
    if not vals:
        return None
    w = sum(v for v in vals if v > 0)
    lo = -sum(v for v in vals if v < 0)
    return math.inf if lo == 0 else w / lo


def kill_check(closed_trades: int, equity: float, peak: float, max_dd: float, bench: float | None,
               equity_30d_ago: float | None) -> dict:
    """Kill inputs + verdict for the primary book. Returns {'rule': str|None, 'inputs': {...}}."""
    strat_ret = equity / START_EQUITY - 1
    bench_ret = (bench / START_EQUITY - 1) if bench is not None else None
    dd = equity / peak - 1 if peak > 0 else 0.0
    behind = bench_ret is not None and strat_ret < bench_ret
    deep = dd < -KILL_REL_DD
    falling = equity_30d_ago is not None and equity < equity_30d_ago
    armed = closed_trades >= KILL_MIN_TRADES
    inputs = {"closed_trades": int(closed_trades), "strat_ret": round(strat_ret, 6),
              "bench_ret": None if bench_ret is None else round(bench_ret, 6),
              "dd_from_peak": round(dd, 6), "max_dd": round(max_dd, 6),
              "equity_30d_ago": None if equity_30d_ago is None else round(equity_30d_ago, 6),
              "rel_armed": armed, "behind_bench": behind, "dd_over_10pct": deep, "below_30d_ago": falling}
    reasons = []
    if max_dd < -KILL_DD:
        reasons.append("K-DD: max drawdown > 45%")
    if armed and behind and deep and falling:
        reasons.append("K-REL: >=30 trades, behind buy-and-hold, >10% below peak, below 30 days ago")
    return {"rule": "; ".join(reasons) or None, "reasons": reasons, "inputs": inputs}


def equity_at_or_before(hist: list, when: pd.Timestamp) -> float | None:
    """hist: [[iso_time, equity, bench], ...] in time order."""
    out = None
    for t, e, _b in hist:
        if pd.Timestamp(t) <= when:
            out = e
        else:
            break
    return out


def contiguous_tail(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Longest run of consecutive days that ends at the last index entry."""
    if not len(idx):
        return idx
    idx = idx.sort_values()
    gaps = (idx[1:] - idx[:-1]) != D1
    if not gaps.any():
        return idx
    last_gap = max(i for i, g in enumerate(gaps) if g)
    return idx[last_gap + 1:]


def build_closes(bus: pd.DataFrame, warm: pd.DataFrame | None, day: pd.Timestamp) -> tuple[pd.Series, dict]:
    """Closes up to ``day`` for one coin: the Binance.US contiguous tail, warmed up with EARLIER
    Binance global (data.binance.vision) closes when the tail is shorter than NEED.
    Returns (closes, source info)."""
    b = bus[bus.index <= day]["c"].astype(float)
    tail = contiguous_tail(b.index)
    s = b.loc[tail]
    info = {"binanceus_days": int(len(s)), "binance_global_days": 0, "source": "binanceus"}
    if len(s) < NEED:
        if warm is None or not len(warm):
            info["source"] = "binanceus_insufficient"
            return s, info
        first = s.index[0] if len(s) else day + D1
        w = warm[(warm.index < first)]["c"].astype(float)
        s = pd.concat([w, s]).sort_index()
        s = s[~s.index.duplicated(keep="last")]
        info.update(binance_global_days=int(len(w)), source="binanceus+binance_global_warmup")
    return s, info


# ───────────────────────── public data (no keys) ─────────────────────────

def _http_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_bus_daily(coin: str, http=_http_json) -> pd.DataFrame:
    d = http(f"{BUS}?symbol={BUS_SYMBOL[coin]}&interval=1d&limit=1000")
    if isinstance(d, dict) or not d:
        raise RuntimeError(f"{coin}: bad klines {str(d)[:200]}")
    df = pd.DataFrame(d).iloc[:, :7]
    df.columns = ["t", "o", "h", "l", "c", "v", "ct"]
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.set_index("t")[["o", "h", "l", "c", "ct"]].astype(float)
    return df[~df.index.duplicated()].sort_index()


def fetch_bus_first_hour_open(coin: str, day: pd.Timestamp, http=_http_json) -> float | None:
    st = int(day.timestamp() * 1000)
    d = http(f"{BUS}?symbol={BUS_SYMBOL[coin]}&interval=1h&startTime={st}&limit=1")
    if isinstance(d, list) and d and int(d[0][0]) == st:
        return float(d[0][1])
    return None


def _http_bytes(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def fetch_vision_daily(coin: str, end: pd.Timestamp, months: int = 10, get=_http_bytes) -> pd.DataFrame:
    """Binance global public 1d archives (data.binance.vision) for the ``months`` calendar months
    ending at ``end`` - warm-up closes only, never fills. Daily archives cover the current month."""
    sym = VISION_SYMBOL[coin]
    end = _day(end)
    frames = []
    per = pd.period_range(end=end.tz_localize(None).to_period("M"), periods=months, freq="M")
    for p in per:
        raw = get(VISION.format(kind="monthly", sym=sym, period=str(p)))
        if raw is None:  # month not archived yet -> daily files
            for d in pd.date_range(p.start_time, min(p.end_time, end.tz_localize(None)), freq="D"):
                r2 = get(VISION.format(kind="daily", sym=sym, period=d.strftime("%Y-%m-%d")))
                if r2 is not None:
                    frames.append(_vision_frame(r2))
            continue
        frames.append(_vision_frame(raw))
    if not frames:
        raise RuntimeError(f"binance vision {coin}: no archives")
    df = pd.concat(frames)
    return df[~df.index.duplicated()].sort_index()


def _vision_frame(raw: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        txt = z.read(z.namelist()[0]).decode()
    df = pd.read_csv(io.StringIO(txt), header=None)
    if not str(df.iloc[0, 0]).strip().isdigit():
        df = df.iloc[1:]
    t = df[0].astype("int64")
    t = t.where(t < 10**14, t // 1000)  # 2025+ archives use microseconds
    out = pd.DataFrame({"o": df[1].astype(float).values, "h": df[2].astype(float).values,
                        "l": df[3].astype(float).values, "c": df[4].astype(float).values},
                       index=pd.to_datetime(t.values, unit="ms", utc=True).normalize())
    return out


# ───────────────────────── ledger ─────────────────────────

def _new_book() -> dict:
    return {"cash": START_EQUITY, "units": {c: 0.0 for c in COINS}, "fees": 0.0, "turnover": 0.0,
            "peak": START_EQUITY, "max_dd": 0.0, "open_rt": {}, "closed": [], "equity": START_EQUITY}


def book_equity(book: dict, px: dict) -> float:
    return book["cash"] + sum(book["units"][c] * px[c] for c in COINS)


def apply_target(book: dict, coin: str, target: float, px: dict, side_cost: float,
                 allow_buy: bool = True) -> dict:
    """Trade one coin of one book to ``target`` weight (of book equity at ``px``) at ``px[coin]``.
    Buys are capped by available cash (incl. the fee). Returns a fill/skip record."""
    price = float(px[coin])
    eq = book_equity(book, px)
    cur_val = book["units"][coin] * price
    want = target * eq
    notional = want - cur_val
    if target == 0.0 and book["units"][coin] > 0:
        notional = -cur_val  # full exit
    if notional > 0:
        notional = min(notional, max(book["cash"], 0.0) / (1 + side_cost))
    rec = {"coin": coin, "target": target, "price": price, "notional": round(notional, 6),
           "equity_before": round(eq, 6)}
    if notional > 0 and not allow_buy:
        rec["status"] = "skipped_killed"
        return rec
    if abs(notional) < MIN_NOTIONAL:
        rec["status"] = "skipped_min_notional" if abs(notional) > 1e-9 else "noop"
        return rec
    fee = abs(notional) * side_cost
    was_flat = book["units"][coin] <= 1e-15
    if notional > 0:
        book["units"][coin] += notional / price
        book["cash"] -= notional + fee
        flow = -(notional + fee)
    else:
        if target == 0.0:
            book["units"][coin] = 0.0
        else:
            book["units"][coin] -= -notional / price
        book["cash"] += -notional - fee
        flow = -notional - fee
    book["fees"] += fee
    book["turnover"] += abs(notional)
    rt = book["open_rt"].get(coin)
    if was_flat and notional > 0:
        rt = {"coin": coin, "flows": 0.0, "n_fills": 0}
    if rt is not None:
        rt["flows"] += flow
        rt["n_fills"] += 1
        book["open_rt"][coin] = rt
    rec.update(status="filled", side="buy" if notional > 0 else "sell", fee=round(fee, 6))
    if book["units"][coin] <= 1e-15 and rt is not None:
        book["closed"].append(round(rt["flows"], 6))
        rec["round_trip_pnl"] = round(rt["flows"], 6)
        book["open_rt"].pop(coin, None)
    return rec


# ───────────────────────── runner ─────────────────────────

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


def _day(s) -> pd.Timestamp:
    t = pd.Timestamp(s)
    return (t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC")).normalize()


class Trend6Paper:
    def __init__(self, out: Path, *, now: Callable[[], pd.Timestamp] | None = None,
                 fetch_bus: Callable[[str], pd.DataFrame] = fetch_bus_daily,
                 fetch_warm: Callable[[str, pd.Timestamp], pd.DataFrame] = fetch_vision_daily,
                 fetch_hour_open: Callable[[str, pd.Timestamp], float | None] = fetch_bus_first_hour_open):
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.now = now or (lambda: pd.Timestamp.now(tz="UTC"))
        self.fetch_bus, self.fetch_warm, self.fetch_hour_open = fetch_bus, fetch_warm, fetch_hour_open

    def run_cycle(self) -> dict:
        lock = (self.out / "cycle.lock").open("a")
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return {"ok": False, "error": "another cycle holds cycle.lock"}
            try:
                return self._cycle()
            except Exception as e:  # network / data errors: no state change, retried next cycle
                st = {"ok": False, "error": f"{type(e).__name__}: {e}", "at": str(self.now())}
                _append(self.out / "events.jsonl", {"event": "cycle_error", **st})
                return st
        finally:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
            finally:
                lock.close()

    def _cycle(self) -> dict:
        now = self.now()
        bus = {c: self.fetch_bus(c) for c in COINS}
        closed = {c: bus[c][bus[c]["ct"] / 1000 < now.timestamp()] for c in COINS}
        last_closed = min(closed[c].index[-1] for c in COINS)
        state = _read_json(self.out / "state.json", None)
        if state is None:  # forward-only start: first decision = the latest closed bar
            state = {"strategy": STRATEGY, "created": str(now), "cursor": str(last_closed - D1),
                     "books": {b: _new_book() for b in SIDE_COST}, "last_target": {c: 0.0 for c in COINS},
                     "bench_units": None, "killed": None, "decisions": 0, "equity_hist": []}
            _append(self.out / "events.jsonl", {"event": "start", "at": str(now), "cursor": state["cursor"],
                                                "paper_only": True})
        if "equity_hist" not in state:  # state from before the K-REL rule: rebuild from the ledger
            state["equity_hist"] = [[r["at"], r[PRIMARY], r["bench_5050_hold"]]
                                    for r in _jsonl(self.out / "equity.jsonl") if PRIMARY in r]
            _append(self.out / "events.jsonl", {"event": "kill_rule_migrated", "at": str(now),
                                                "rule": "K-REL replaces PF<1.10", "hist": len(state["equity_hist"])})
        cursor = _day(state["cursor"])
        warm_cache: dict = {}
        new = []
        day = cursor + D1
        while day <= last_closed:
            exec_day = day + D1
            opens = {}
            for c in COINS:
                if exec_day in bus[c].index:
                    opens[c] = (float(bus[c].loc[exec_day, "o"]), "binanceus_1d_open")
                else:
                    ho = self.fetch_hour_open(c, exec_day)
                    if ho is not None:
                        opens[c] = (ho, "binanceus_1h_first_open")
            if len(opens) < len(COINS):
                break  # next open not available yet: retry next cycle (no state change for this day)
            dec = {"strategy": STRATEGY, "decision_day": str(day.date()), "decided_on_close_at": str(day + D1),
                   "exec_at_open": str(exec_day), "decided_at": str(now), "coins": {}}
            targets = {}
            for c in COINS:
                closes, info = build_closes(closed[c], None, day)
                if info["source"] == "binanceus_insufficient":
                    if c not in warm_cache:
                        warm_cache[c] = self.fetch_warm(c, day)
                    closes, info = build_closes(closed[c], warm_cache[c], day)
                s = signals_at(closes, day)
                if s is None:
                    raise RuntimeError(f"{c}: not enough history for {day.date()} ({info})")
                targets[c] = s["weight"]
                dec["coins"][c] = {**s, "data": info, "exec_open": opens[c][0], "exec_open_src": opens[c][1],
                                   "exec_venue": "binanceus"}
            px = {c: opens[c][0] for c in COINS}
            allow_buy = state["killed"] is None
            fills = []
            for bname, side in SIDE_COST.items():
                book = state["books"][bname]
                # sells first so buys see the freed cash
                order = sorted(COINS, key=lambda c: targets[c] - state["last_target"][c])
                for c in order:
                    if abs(targets[c] - state["last_target"][c]) < 1e-12:
                        continue  # band: drift is never traded
                    r = apply_target(book, c, targets[c], px, side, allow_buy)
                    r.update(book=bname, exec_at=str(exec_day), decision_day=str(day.date()))
                    if r["status"] == "filled":
                        _append(self.out / "fills.jsonl", r)
                        fills.append(r)
                    elif r["status"] != "noop":
                        _append(self.out / "skipped.jsonl", r)
                eq = book_equity(book, px)
                book["equity"] = eq
                book["peak"] = max(book["peak"], eq)
                book["max_dd"] = min(book["max_dd"], eq / book["peak"] - 1)
            for c in COINS:
                state["last_target"][c] = targets[c]
            if state["bench_units"] is None:
                state["bench_units"] = {c: START_EQUITY * 0.5 / px[c] for c in COINS}
            bench = sum(state["bench_units"][c] * px[c] for c in COINS)
            prim = state["books"][PRIMARY]
            hist = state["equity_hist"]
            e30 = equity_at_or_before(hist, exec_day - pd.Timedelta(days=KILL_LOOKBACK_DAYS))
            hist.append([str(exec_day), round(prim["equity"], 6), round(bench, 6)])
            kc = kill_check(len(prim["closed"]), prim["equity"], prim["peak"], prim["max_dd"], bench, e30)
            state["kill_inputs"] = {**kc["inputs"], "at": str(exec_day)}
            if state["killed"] is None and kc["rule"]:
                state["killed"] = {"rule": kc["rule"], "reasons": kc["reasons"], "at": str(exec_day),
                                   "inputs": kc["inputs"]}
                _append(self.out / "events.jsonl", {"event": "KILLED", **state["killed"]})
            dec["targets"] = targets
            dec["fills"] = len(fills)
            _append(self.out / "decisions.jsonl", dec)
            _append(self.out / "equity.jsonl", {"at": str(exec_day), "bench_5050_hold": round(bench, 4),
                                                **{b: round(state["books"][b]["equity"], 4) for b in SIDE_COST},
                                                "exposure_target": round(sum(targets.values()), 4)})
            state["cursor"] = str(day)
            state["decisions"] += 1
            new.append(dec)
            day += D1
        _write_json(self.out / "state.json", state)
        st = self.status(state, bus, now, new)
        _write_json(self.out / "status.json", st)
        return st

    def status(self, state: dict, bus: dict, now: pd.Timestamp, new: list) -> dict:
        last_px = {c: float(bus[c]["c"].iloc[-1]) for c in COINS}
        books = {}
        for b in SIDE_COST:
            bk = state["books"][b]
            eq = book_equity(bk, last_px)
            books[b] = {"equity_mark": round(eq, 4), "cash": round(bk["cash"], 4),
                        "units": bk["units"], "fees": round(bk["fees"], 4), "turnover_usd": round(bk["turnover"], 2),
                        "closed_round_trips": len(bk["closed"]), "open_round_trips": sorted(bk["open_rt"]),
                        "pf": profit_factor(bk["closed"]), "max_dd_at_opens": round(bk["max_dd"], 4),
                        "win_rate": (round(sum(v > 0 for v in bk["closed"]) / len(bk["closed"]), 3)
                                     if bk["closed"] else None)}
        prim = state["books"][PRIMARY]
        eq = book_equity(prim, last_px)
        exp = sum(prim["units"][c] * last_px[c] for c in COINS) / eq if eq > 0 else 0.0
        bench = (sum(state["bench_units"][c] * last_px[c] for c in COINS) if state["bench_units"] else None)
        last_dec = _read_last(self.out / "decisions.jsonl")
        return {"ok": True, "strategy": STRATEGY, "mode": "PAPER", "orders_placed": 0, "at": str(now),
                "cursor_decision_day": state["cursor"][:10], "decisions_total": state["decisions"],
                "new_decisions_this_cycle": [d["decision_day"] for d in new],
                "last_decision": last_dec, "targets": state["last_target"],
                "primary_book": PRIMARY, "reference_only_books": list(REFERENCE_ONLY),
                "venue": "binanceus (paper)", "equity_primary": round(eq, 4),
                "exposure_primary": round(exp, 4), "cash_pct_primary": round(1 - exp, 4),
                "bench_5050_hold": round(bench, 4) if bench is not None else None,
                "last_price": last_px, "books": books, "killed": state["killed"],
                "kill_inputs": state.get("kill_inputs"),
                "kill_rules": {"K-DD": f"max drawdown > {KILL_DD:.0%} (any time)",
                               "K-REL": (f">= {KILL_MIN_TRADES} closed round trips AND return < 50/50 buy-and-hold "
                                         f"AND > {KILL_REL_DD:.0%} below peak AND equity < {KILL_LOOKBACK_DAYS} days ago"),
                               "replaced": "PF < 1.10 after 30 (removed 2026-11-16, owner-approved)"}}


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


def _read_last(p: Path):
    try:
        lines = [x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        return json.loads(lines[-1]) if lines else None
    except (OSError, ValueError):
        return None
