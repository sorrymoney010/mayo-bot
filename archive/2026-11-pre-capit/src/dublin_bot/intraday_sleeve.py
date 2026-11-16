"""Intraday PAPER sleeve for BTC/ETH/SOL. ACTIVE strategy (v2 "fewer, longer", 2026-10-24): ``mtf_synth`` —
ONE strategy on 15m/30m/1h only (reports/intraday_fewer_longer_2026-10-24.md, research/intraday/bt3.py):
1h STRONG trend (EMA20>EMA50, close>EMA200, EMA50 rising over 12h, EMA20 rising over 6h) + 1h volatility
expansion (ATR14(1h) > its 100-bar mean) + 30m momentum (close>EMA20, RSI14 50-70) + 15m trigger (close >
prior 48-bar/12h high, volume >= 1.10x avg); post-only limit entry good 15 min; stop 2.5xATR14(1h);
first (and only) target = 3R, must be >= 2.5% away (else skip); half off there (maker), stop to entry+0.2%,
runner exits on 1h close < 1h EMA20; 24h time stop; max 2 open, max 2 entries/day (Chicago), 12h per-pair
cooldown after a losing trade. v1 (1.5xATR15m stop, 1/3@1R+2R, 12h) lost: PF 0.04 at 40/80 bps.
Legacy single-timeframe variants below are kept for research only.

PAPER ONLY: no gateway, no keys, no private API, no orders. Public Kraken OHLC + the box tick
collector (data/ticks via dublin_bot.pipeline) only. Own sub-book under logs/intraday/.

Variants (see reports/intraday_reset_2026-10-24.md for sources):
  user_breakout  - Mayo original spec (.env.example, first commit): EMA20>EMA50, close>EMA200,
                   RSI14 in [45,68], close > prior 20-bar high, volume >= 1.10x 20-bar avg.
  user_emacross  - user's 2nd bot (kraken-ema-bot): EMA20 crosses above EMA50, RSI14 in [50,70].
  pullback_trend - research variant: uptrend (EMA20>EMA50, close>EMA200) pullback that tags EMA20
                   and closes back above it, RSI14 in [40,60] (maker-friendly entry).
Common management (user's cash-out method): stop 1.5xATR14; 1/3 off at 1R, 2R, 3R; stop to
breakeven after TP1 ("close half / move SL to entry"); 12h time stop.
Fills: entry = post-only limit at signal-bar close, fills only if a later trade prints BELOW it
within one bar (else cancelled, counted as no-fill); TPs = maker; stop/time = taker + slippage.
Fees default to Kraken Tier-1 (40 maker / 80 taker bps, kraken.com/features/fee-schedule, Jul 2026).
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
PAIRS = {"BTC/USD": "XBTUSD", "ETH/USD": "ETHUSD", "SOL/USD": "SOLUSD"}
VARIANTS = ("user_breakout", "user_emacross", "pullback_trend", "mtf_synth")
# mtf_synth v2 "fewer, longer" (walk-forward pick in research/intraday/bt3.py; few params on purpose)
MTF = {"stop_atr": 2.5, "tp_r": 3.0, "part": 0.5, "min_target": 0.025, "be_bps": 20, "time_stop_h": 48.0,
       "atr_x": False,  # Binance.US re-run (bt4.py): walk-forward picked no ATR-expansion filter, 48h time stop
       "rsi": (50, 70), "brk_bars": 48, "max_open": 2, "per_day": 2, "cooldown_h": 12.0}


def _env(name, default):
    return os.environ.get(name, default)


def sleeve_active() -> tuple[bool, str]:
    if _env("INTRADAY_SLEEVE_ENABLED", "false").lower() != "true":
        return False, "INTRADAY_SLEEVE_ENABLED!=true"
    if _env("PAPER_TRADING", "true").lower() != "true" or _env("DRY_RUN", "true").lower() != "true" \
            or _env("ALLOW_LIVE_TRADING", "false").lower() != "false":
        return False, "safety locks not paper/dry/no-live"
    return True, "paper"


# ---------------- indicators / signals (shared with backtest) ----------------
def ema(x, n):
    return x.ewm(span=n, adjust=False).mean()


def rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def atr(d, n=14):
    tr = pd.concat([d.high - d.low, (d.high - d.close.shift()).abs(),
                    (d.low - d.close.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def signal_frame(d: pd.DataFrame) -> pd.DataFrame:
    c = d.close
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    r = rsi(c)
    hi20 = d.high.rolling(20).max().shift()
    vr = d.volume / d.volume.rolling(20).mean().shift()
    f = pd.DataFrame(index=d.index)
    f["close"], f["atr"], f["rsi"], f["vr"] = c, atr(d), r, vr
    f["ema20"], f["ema50"], f["ema200"], f["hi20"] = e20, e50, e200, hi20
    f["user_breakout"] = (e20 > e50) & (c > e200) & (r >= 45) & (r <= 68) & (c > hi20) & (vr >= 1.10)
    f["user_emacross"] = (e20 > e50) & (e20.shift() <= e50.shift()) & (r >= 50) & (r <= 70)
    f["pullback_trend"] = (e20 > e50) & (c > e200) & (d.low <= e20) & (c > e20) & (r >= 40) & (r <= 60)
    return f


def why_not(row) -> str:
    bits = []
    if not row.ema20 > row.ema50: bits.append("ema20<=ema50")
    if not row.close > row.ema200: bits.append("below_ema200")
    if not 45 <= row.rsi <= 68: bits.append(f"rsi={row.rsi:.0f}")
    if not row.close > row.hi20: bits.append("no_20bar_breakout")
    if not row.vr >= 1.10: bits.append(f"vol={row.vr:.2f}x")
    return ",".join(bits) or "all_pass"


# ---------------- data ----------------
def rest_ohlc(symbol: str, tf: int) -> pd.DataFrame:
    url = f"https://api.kraken.com/0/public/OHLC?pair={PAIRS[symbol]}&interval={tf}"
    with urlopen(Request(url, headers={"User-Agent": "mayo-bot-paper/intraday"}), timeout=15) as r:  # noqa: S310
        js = json.load(r)
    if js.get("error"):
        raise RuntimeError(js["error"])
    rows = next(v for k, v in js["result"].items() if k != "last")
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "vwap", "volume", "count"])
    df.index = pd.to_datetime(df.t.astype(int), unit="s", utc=True)
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    now = time.time()
    return df[df.index.map(lambda x: x.timestamp() + tf * 60 <= now)]  # closed bars only


class IntradaySleeve:
    def __init__(self, root: Path = ROOT, now_fn=time.time):
        self.root = root
        self.dir = root / "logs" / "intraday"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.trades_path = self.dir / "trades.jsonl"
        self.events_path = self.dir / "events.jsonl"
        self.now = now_fn
        self.tfs = [int(x) for x in _env("INTRADAY_TFS", "15,30,60").split(",")]
        self.variants = [v for v in _env("INTRADAY_VARIANTS", ",".join(VARIANTS)).split(",") if v in VARIANTS]
        from dublin_bot.pipeline.binanceus import venue
        self.venue = venue()
        if self.venue == "binanceus":  # Binance.US zero-fee schedule: 0 maker / 2 taker (blog.binance.us/zero-fee-trading)
            self.maker = float(_env("BINANCEUS_MAKER_FEE_BPS", "0"))
            self.taker = float(_env("BINANCEUS_TAKER_FEE_BPS", "2"))
            self.slip = float(_env("BINANCEUS_SLIPPAGE_BPS", "3"))
        else:
            self.maker = float(_env("INTRADAY_MAKER_FEE_BPS", "40"))
            self.taker = float(_env("INTRADAY_TAKER_FEE_BPS", "80"))
            self.slip = float(_env("INTRADAY_SLIPPAGE_BPS", "10"))
        self.risk = float(_env("INTRADAY_RISK_PER_TRADE", "0.01"))
        self.cap = float(_env("INTRADAY_MAX_POSITION_FRACTION", "0.10"))
        self.max_open = int(_env("INTRADAY_MAX_OPEN", "3"))
        self.daily_loss = float(_env("INTRADAY_MAX_DAILY_LOSS_FRACTION", "0.03"))
        self.time_stop_h = float(_env("INTRADAY_TIME_STOP_HOURS", "12"))
        self.state = self._load()
        self.state.setdefault("entries_day", {}); self.state.setdefault("cooldown", {})
        self._src = None

    # ---- state ----
    def _load(self) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        eq = float(_env("INTRADAY_START_EQUITY_USD", "466.81"))
        return {"cash": eq, "start_equity": eq, "positions": {}, "pending": {}, "last_bar": {},
                "stats": {"fills": 0, "nofills": 0, "closed": 0}, "day": "", "day_start_equity": eq}

    def _save(self):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, default=str))
        tmp.replace(self.state_path)

    def _event(self, path: Path, rec: dict):
        rec = {"ts": pd.Timestamp(self.now(), unit="s", tz="UTC").isoformat(), "venue": self.venue, **rec}
        with path.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    # ---- data ----
    def _source(self):
        if self._src is None:
            from dublin_bot.pipeline.source import get_source
            from dublin_bot.pipeline.binanceus import venue_data_dir
            self._src = get_source(venue_data_dir(self.root / "data", self.venue), stale_seconds=600.0)
        return self._src

    def bars(self, symbol: str, tf: int) -> pd.DataFrame:
        base = 15 if tf == 30 else tf
        src = self._source()
        if self.venue == "binanceus":
            from dublin_bot.pipeline.binanceus import BinanceUSPublic
            fetch = lambda: BinanceUSPublic(now_fn=self.now).klines(symbol, base, 720)  # noqa: E731
        else:
            fetch = lambda: rest_ohlc(symbol, base)  # noqa: E731
        b = src.get_bars(symbol, base, 600, rest_fetch=fetch)
        b = b[["open", "high", "low", "close", "volume"]].astype(float)
        if tf == 30:
            b = b.resample("30min").agg({"open": "first", "high": "max", "low": "min",
                                         "close": "last", "volume": "sum"}).dropna()
            b = b[b.index.map(lambda x: x.timestamp() + 1800 <= self.now())]
        return b

    def minute_path(self, symbol: str, since: float) -> pd.DataFrame:
        src = self._source()
        n = int(max(2, (self.now() - since) // 60 + 2))
        try:
            m = src.local_bars(symbol, 1, min(n, 1440))
        except Exception:  # noqa: BLE001
            m = pd.DataFrame()
        if len(m):
            m = m[m.index.map(lambda x: x.timestamp() >= since - 60)]
        return m

    def equity(self, marks: dict) -> float:
        eq = self.state["cash"]
        for p in self.state["positions"].values():
            eq += p["qty"] * marks.get(p["symbol"], p["entry"])
        return eq

    # ---- cycle ----
    def run_cycle(self) -> dict:
        ok, why = sleeve_active()
        out = {"active": ok, "why": why, "actions": [], "signals": {}, "errors": []}
        if not ok:
            return out
        marks = {}
        for sym in PAIRS:
            try:
                m = self.minute_path(sym, self.now() - 600)
                if len(m):
                    marks[sym] = float(m.close.iloc[-1])
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"mark {sym}: {exc}")
        self._roll_day(marks)
        self._work_pending(out)
        self._manage(out, marks)
        if self.variants == ["mtf_synth"]:
            for sym in PAIRS:
                try:
                    self._evaluate_mtf(sym, out, marks)
                except Exception as exc:  # noqa: BLE001
                    out["errors"].append(f"{sym}@mtf: {type(exc).__name__}: {exc}"[:200])
            self._runner_exit(out, marks)
        for tf in ([] if self.variants == ["mtf_synth"] else self.tfs):
            for sym in PAIRS:
                try:
                    self._evaluate(sym, tf, out, marks)
                except Exception as exc:  # noqa: BLE001
                    out["errors"].append(f"{sym}@{tf}m: {type(exc).__name__}: {exc}"[:200])
        out["equity"] = round(self.equity(marks), 2)
        out["open"] = len(self.state["positions"])
        out["pending"] = len(self.state["pending"])
        out["stats"] = self.state["stats"]
        self._save()
        return out

    def _roll_day(self, marks):
        day = time.strftime("%Y-%m-%d", time.gmtime(self.now()))
        if self.state["day"] != day:
            self.state["day"] = day
            self.state["day_start_equity"] = self.equity(marks)

    def _halted(self, marks) -> bool:
        return self.equity(marks) < self.state["day_start_equity"] * (1 - self.daily_loss)

    def _evaluate(self, sym, tf, out, marks):
        b = self.bars(sym, tf)
        if len(b) < 220:
            out["signals"][f"{sym}@{tf}m"] = f"warmup bars={len(b)}"
            return
        f = signal_frame(b)
        row = f.iloc[-1]
        bar_ts = f.index[-1].isoformat()
        fired = [v for v in self.variants if bool(row[v])]
        out["signals"][f"{sym}@{tf}m"] = (
            f"bar={bar_ts[11:16]}Z close={row.close:.2f} rsi={row.rsi:.0f} vol={row.vr:.2f}x "
            f"fire={'+'.join(fired) or 'none'} ({why_not(row)})")
        for v in self.variants:
            lane = f"{v}|{tf}|{sym}"
            if self.state["last_bar"].get(lane) == bar_ts:
                continue
            self.state["last_bar"][lane] = bar_ts
            if not bool(row[v]):
                continue
            if lane in self.state["positions"] or lane in self.state["pending"]:
                continue
            if len(self.state["positions"]) + len(self.state["pending"]) >= self.max_open:
                out["actions"].append({"event": "skip_max_open", "symbol": sym, "lane": lane})
                continue
            if self._halted(marks):
                out["actions"].append({"event": "skip_daily_loss", "symbol": sym, "lane": lane})
                continue
            a = float(row.atr)
            if not math.isfinite(a) or a <= 0:
                continue
            lim = float(row.close)
            risk_d = 1.5 * a
            eq = self.equity(marks)
            qty = min(eq * self.risk / risk_d, eq * self.cap / lim)
            if qty * lim < 5:
                continue
            self.state["pending"][lane] = {
                "lane": lane, "variant": v, "tf": tf, "symbol": sym, "limit": lim, "qty": qty,
                "risk": risk_d, "placed": self.now(), "expires": self.now() + tf * 60, "bar": bar_ts}
            out["actions"].append({"event": "post_only_limit", "symbol": sym, "lane": lane, "limit": lim})
            self._event(self.events_path, {"event": "post_only_limit", **self.state["pending"][lane]})

    # ---- ONE synthesized strategy: 1h trend + 30m momentum + 15m breakout trigger ----
    def mtf_state(self, sym):
        b15, b30, b60 = self.bars(sym, 15), self.bars(sym, 30), self.bars(sym, 60)
        if len(b15) < 40 or len(b30) < 40 or len(b60) < 210:
            return None, f"warmup 15m={len(b15)} 30m={len(b30)} 1h={len(b60)}"
        h = b60.close
        he20, he50, he200 = ema(h, 20), ema(h, 50), ema(h, 200)
        m_rsi, m_e20 = rsi(b30.close), ema(b30.close, 20)
        a = atr(b60); hi = b15.high.rolling(MTF["brk_bars"]).max().shift()
        a_x = bool(a.iloc[-1] > a.rolling(100).mean().iloc[-1]) if MTF["atr_x"] else True
        strong = bool(he50.iloc[-1] > he50.iloc[-13] and he20.iloc[-1] > he20.iloc[-7])
        vr = b15.volume / b15.volume.rolling(20).mean().shift()
        st = {"bar": b15.index[-1].isoformat(), "close": float(b15.close.iloc[-1]), "atr": float(a.iloc[-1]),
              "h_up": bool(he20.iloc[-1] > he50.iloc[-1] and h.iloc[-1] > he200.iloc[-1]),
              "h_strong": strong, "atr_x": a_x,
              "h_close": float(h.iloc[-1]), "h_e20": float(he20.iloc[-1]), "h_bar": b60.index[-1].isoformat(),
              "m_rsi": float(m_rsi.iloc[-1]), "m_ok": bool(b30.close.iloc[-1] > m_e20.iloc[-1]),
              "brk": bool(b15.close.iloc[-1] > hi.iloc[-1]), "vr": float(vr.iloc[-1])}
        st["tgt_pct"] = MTF["tp_r"] * MTF["stop_atr"] * st["atr"] / st["close"]
        lo, hi_ = MTF["rsi"]
        fails = [k for k, ok in (("1h_trend", st["h_up"]), ("1h_strong", st["h_strong"]), ("1h_atr_expansion", a_x),
                                 ("30m>ema20", st["m_ok"]), (f"30m_rsi{lo}-{hi_}", lo <= st["m_rsi"] <= hi_),
                                 ("15m_48bar_breakout", st["brk"]), ("15m_vol>=1.1x", st["vr"] >= 1.10),
                                 ("target>=2.5%", st["tgt_pct"] >= MTF["min_target"])) if not ok]
        st["fire"] = not fails
        return st, ("FIRE" if not fails else "wait: " + ",".join(fails))

    def _evaluate_mtf(self, sym, out, marks):
        st, why = self.mtf_state(sym)
        if st is None:
            out["signals"][f"{sym}@15/30/60m"] = why
            return
        out["signals"][f"{sym}@15/30/60m"] = (f"bar={st['bar'][11:16]}Z close={st['close']:.2f} 1h_up={st['h_up']} "
                                             f"strong={st['h_strong']} atr_x={st['atr_x']} 30m_rsi={st['m_rsi']:.0f} "
                                             f"vol={st['vr']:.2f}x tgt={st['tgt_pct'] * 100:.2f}% {why}")
        lane = f"mtf_synth|15|{sym}"
        if self.state["last_bar"].get(lane) == st["bar"]:
            return
        self.state["last_bar"][lane] = st["bar"]
        if not st["fire"] or lane in self.state["positions"] or lane in self.state["pending"]:
            return
        day = pd.Timestamp(self.now(), unit="s", tz="UTC").tz_convert("America/Chicago").strftime("%Y-%m-%d")
        if len(self.state["positions"]) + len(self.state["pending"]) >= min(self.max_open, MTF["max_open"]) \
                or self._halted(marks) or self.state["entries_day"].get(day, 0) >= MTF["per_day"] \
                or self.state["cooldown"].get(sym, 0) > self.now():
            out["actions"].append({"event": "skip_limits", "symbol": sym, "lane": lane})
            return
        lim, risk_d = st["close"], MTF["stop_atr"] * st["atr"]
        eq = self.equity(marks)
        qty = min(eq * self.risk / risk_d, eq * self.cap / lim)
        if qty * lim < 5 or not math.isfinite(qty):
            return
        self.state["pending"][lane] = {"lane": lane, "variant": "mtf_synth", "tf": 15, "symbol": sym, "limit": lim,
                                       "qty": qty, "risk": risk_d, "placed": self.now(), "expires": self.now() + 900,
                                       "bar": st["bar"], "ladder": [MTF["tp_r"]], "part": MTF["part"],
                                       "be_bps": MTF["be_bps"], "time_stop_h": MTF["time_stop_h"]}
        self.state["entries_day"] = {day: self.state["entries_day"].get(day, 0) + 1}
        out["actions"].append({"event": "post_only_limit", "symbol": sym, "lane": lane, "limit": lim})
        self._event(self.events_path, {"event": "post_only_limit", **self.state["pending"][lane]})

    def _runner_exit(self, out, marks):
        for lane, pos in list(self.state["positions"].items()):
            if pos.get("variant") != "mtf_synth" or pos["hit"] < 1 or pos["qty"] <= 1e-12:
                continue
            b60 = self.bars(pos["symbol"], 60)
            if len(b60) > 25 and b60.close.iloc[-1] < ema(b60.close, 20).iloc[-1] \
                    and b60.index[-1].timestamp() + 3600 > pos["opened"]:
                px = marks.get(pos["symbol"], float(b60.close.iloc[-1])) * (1 - self.slip / 1e4)
                self._sell(pos, pos["qty"], px, self.taker, "runner_1h_close<ema20", out)
                self._close_if_done(lane, pos)

    def _work_pending(self, out):
        for lane, p in list(self.state["pending"].items()):
            m = self.minute_path(p["symbol"], p["placed"])
            m = m[m.index.map(lambda x: x.timestamp() >= p["placed"])] if len(m) else m
            hit = m[m.low < p["limit"]] if len(m) else m
            if len(hit):
                cost = p["qty"] * p["limit"] * (1 + self.maker / 1e4)
                if cost > self.state["cash"]:
                    del self.state["pending"][lane]
                    continue
                self.state["cash"] -= cost
                pos = {**p, "entry": p["limit"], "stop": p["limit"] - p["risk"],
                       "tps": [p["limit"] + p["risk"] * k for k in p.get("ladder", (1, 2, 3))], "hit": 0,
                       "qty0": p["qty"], "opened": float(hit.index[0].timestamp()),
                       "checked": float(hit.index[0].timestamp()) + 60, "realized": -p["qty"] * p["limit"] * self.maker / 1e4}
                self.state["positions"][lane] = pos
                del self.state["pending"][lane]
                self.state["stats"]["fills"] += 1
                out["actions"].append({"event": "maker_fill", "symbol": p["symbol"], "lane": lane})
                self._event(self.events_path, {"event": "maker_fill", "lane": lane, "px": p["limit"]})
            elif self.now() > p["expires"]:
                del self.state["pending"][lane]
                self.state["stats"]["nofills"] += 1
                out["actions"].append({"event": "limit_expired_nofill", "symbol": p["symbol"], "lane": lane})
                self._event(self.events_path, {"event": "nofill", "lane": lane})

    def _sell(self, pos, qty, px, fee_bps, reason, out):
        proceeds = qty * px * (1 - fee_bps / 1e4)
        self.state["cash"] += proceeds
        pos["qty"] -= qty
        pos["realized"] += qty * (px - pos["entry"]) - qty * px * fee_bps / 1e4
        out["actions"].append({"event": reason, "symbol": pos["symbol"], "lane": pos["lane"], "px": px})
        self._event(self.events_path, {"event": reason, "lane": pos["lane"], "qty": qty, "px": px})

    def _manage(self, out, marks):
        for lane, pos in list(self.state["positions"].items()):
            m = self.minute_path(pos["symbol"], pos["checked"])
            if len(m):
                m = m[m.index.map(lambda x: x.timestamp() >= pos["checked"] - 60)]
            for ts, bar in m.iterrows():
                if pos["qty"] <= 1e-12:
                    break
                if bar.low <= pos["stop"]:  # conservative: stop first
                    px = min(pos["stop"], bar.open) * (1 - self.slip / 1e4)
                    self._sell(pos, pos["qty"], px, self.taker,
                               "stop_breakeven" if pos["hit"] else "stop_loss", out)
                    break
                while pos["hit"] < len(pos["tps"]) and bar.high > pos["tps"][pos["hit"]]:
                    if "part" in pos:
                        q = min(pos["qty"], pos["qty0"] * pos["part"]) if pos["hit"] < len(pos["tps"]) - 1 or pos["part"] < 1 else pos["qty"]
                    else:
                        q = pos["qty0"] / 3 if pos["hit"] < 2 else pos["qty"]
                    self._sell(pos, q, pos["tps"][pos["hit"]], self.maker, f"tp{pos['hit'] + 1}", out)
                    pos["hit"] += 1
                    if pos["hit"] == 1:
                        pos["stop"] = pos["entry"] * (1 + pos.get("be_bps", 0) / 1e4)
                pos["checked"] = ts.timestamp() + 60
            if pos["qty"] > 1e-12 and self.now() - pos["opened"] > pos.get("time_stop_h", self.time_stop_h) * 3600 and pos["symbol"] in marks:
                self._sell(pos, pos["qty"], marks[pos["symbol"]] * (1 - self.slip / 1e4), self.taker, "time_stop", out)
            self._close_if_done(lane, pos)

    def _close_if_done(self, lane, pos):
        if lane in self.state["positions"] and pos["qty"] <= 1e-12:
            notional = pos["qty0"] * pos["entry"]
            rec = {"lane": lane, "variant": pos["variant"], "tf": pos["tf"], "symbol": pos["symbol"],
                   "entry": pos["entry"], "opened": pos["opened"], "closed": self.now(),
                   "tps_hit": pos["hit"], "pnl_usd": round(pos["realized"], 4),
                   "net_bps": round(pos["realized"] / notional * 1e4, 1)}
            self._event(self.trades_path, rec)
            if pos["realized"] < 0 and pos.get("variant") == "mtf_synth":
                self.state.setdefault("cooldown", {})[pos["symbol"]] = self.now() + MTF["cooldown_h"] * 3600
            self.state["stats"]["closed"] += 1
            del self.state["positions"][lane]
