#!/usr/bin/env python3
"""Walk-forward the paper perpetual short (bearish mirror of trend-hold).

Costs are Kraken Futures' published base tier: 5 bps taker each side, plus
hourly funding when a cached funding series is present
(data/futures_funding_<PERP>.csv with columns time,rate). Without that file,
funding is zero and the report says so — it does not invent a rate.

The sleeve stays OFF unless out-of-sample mean net bps > 0, total return > 0,
and the promotion bar in dublin_bot.promotion passes (30 trades, lower bound,
ex-best-2). A smaller positive sample is reported and left off.

    python scripts/fetch_kraken_history.py ohlc --tfs 60 240 1440
    python scripts/backtest_futures_short.py --fetch-funding

History is the same spot cache the other studies use (perps track spot closely
enough for this research pass). A public futures candle cache, when present
as data/futures_<PERP>_<tf>.csv, is preferred.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dublin_bot.backtest_core import add_indicators  # noqa: E402
from dublin_bot.futures_backtest import (  # noqa: E402
    ShortSimParams,
    short_metrics,
    simulate_perp_short,
)
from dublin_bot.futures_costs import SPOT_TO_PERP  # noqa: E402
from dublin_bot.promotion import check_strategy  # noqa: E402
from dublin_bot.research import buy_and_hold  # noqa: E402
from dublin_bot.backtest_core import Costs  # noqa: E402

SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"]


def _read(path: Path):
    import pandas as pd
    if not path.exists():
        return None
    df = pd.read_csv(path)
    return df if len(df) else None


def load_price(symbol: str, tf: int, data: Path):
    import pandas as pd
    from dublin_bot.daily_filter import attach_d1, riskon_table
    perp = SPOT_TO_PERP[symbol]
    base = symbol.replace("/", "")
    df = _read(data / f"futures_{perp}_{tf}.csv")
    src = "futures"
    if df is None:
        parts = []
        for suffix in ("_trades", ""):
            got = _read(data / f"kraken_{base}_{tf}m{suffix}.csv")
            if got is not None:
                parts.append(got)
        if not parts:
            return None, "missing"
        df = pd.concat(parts).drop_duplicates("time", keep="last").sort_values("time").reset_index(drop=True)
        src = "spot_cache"
    daily = None
    for suffix in ("_trades", ""):
        daily = _read(data / f"kraken_{base}_1440m{suffix}.csv")
        if daily is not None:
            break
    df = add_indicators(df)
    if daily is not None:
        df = attach_d1(df, riskon_table(daily), tf)
    return df, src


def load_funding(symbol: str, data: Path, index_times: np.ndarray, bar_hours: float) -> tuple[np.ndarray | None, str]:
    """Hourly funding rate aligned to bars. None if no cache (caller uses zero)."""
    perp = SPOT_TO_PERP[symbol]
    raw = _read(data / f"futures_funding_{perp}.csv")
    if raw is None or "time" not in raw or "rate" not in raw:
        return None, "missing"
    raw = raw.sort_values("time")
    # Sum the hourly prints that fall inside each bar into an average hourly rate.
    rates = np.zeros(len(index_times))
    times = raw["time"].to_numpy(float)
    vals = raw["rate"].to_numpy(float)
    width = bar_hours * 3600.0
    for i, t0 in enumerate(index_times):
        mask = (times >= t0) & (times < t0 + width)
        if mask.any():
            rates[i] = float(np.mean(vals[mask]))
    return rates, "cache"


def _oos_split(trades, n: int, warm: int = 210, folds: int = 4):
    usable = n - warm
    if usable < (folds + 1) * 20:
        return []
    edges = [warm + (usable * k) // (folds + 1) for k in range(folds + 2)]
    oos = []
    for k in range(1, folds + 1):
        lo, hi = edges[k], edges[k + 1]
        oos.extend(t for t in trades if lo <= t.entry_i < hi)
    return oos


def fetch_funding(data: Path) -> None:
    """Cache public historical relative funding rates. No keys, no orders."""
    from dublin_bot.futures_public import FuturesPublic
    client = FuturesPublic()
    data.mkdir(parents=True, exist_ok=True)
    for perp in ("PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD"):
        rows = client.funding_history(perp)
        lines = ["time,rate"]
        for row in rows:
            raw_ts = row.get("timestamp") or row.get("time")
            rel = row.get("relativeFundingRate")
            if rel is None and row.get("fundingRate") is not None and row.get("markPrice"):
                rel = float(row["fundingRate"]) / float(row["markPrice"])
            if raw_ts is None or rel is None:
                continue
            if isinstance(raw_ts, (int, float)):
                ts = float(raw_ts)
                if ts > 1e12:
                    ts /= 1000.0
            else:
                ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00")).timestamp()
            lines.append(f"{int(ts)},{float(rel)}")
        path = data / f"futures_funding_{perp}.csv"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"funding {perp}: {len(lines) - 1} hourly rates -> {path}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch-funding", action="store_true",
                    help="download public historical relative funding rates before the study")
    args = ap.parse_args()
    data = ROOT / "data"
    if args.fetch_funding:
        fetch_funding(data)
    day = time.strftime("%Y-%m-%d")
    report = {"generated_at": day, "fees": {"taker_bps": 5.0, "maker_bps": 2.0, "source":
              "https://support.kraken.com/articles/360048917612-fee-schedule"},
              "leverage_cap": 2.0, "runs": []}
    saw = False
    for tf, hours in ((60, 1.0), (240, 4.0)):
        for lev in (1.0, 2.0):
            all_oos = []
            sources = []
            funding_note = []
            holds = []
            for sym in SYMBOLS:
                df, src = load_price(sym, tf, data)
                sources.append(src)
                if df is None or len(df) < 400:
                    continue
                saw = True
                rates, fsrc = load_funding(sym, data, df["time"].to_numpy(float), hours)
                funding_note.append(fsrc)
                params = ShortSimParams(leverage=lev, warm=210)
                trades = simulate_perp_short(df, params, funding_hourly=rates, bar_hours=hours)
                all_oos.extend(_oos_split(trades, len(df)))
                bh = buy_and_hold(df, Costs(fee_bps=40.0, slippage_bps=10.0, maker_bps=25.0))
                if bh:
                    holds.append(bh)
            m = short_metrics(all_oos, leverage=lev)
            rows = []
            for t in all_oos:
                notional = 500.0 * min(0.25 * lev, 0.75)
                rows.append({"net_bps": t.net * 1e4, "pnl": notional * t.net})
            bar = check_strategy(rows)
            beats = bool(bar["passes"])
            report["runs"].append({
                "tf": tf, "leverage": lev, "sources": sources, "funding": funding_note,
                "oos": m.to_dict(), "promotion": bar, "promote": beats,
                "buy_and_hold_mean_net_bps": (sum(h["net_bps"] for h in holds) / len(holds)) if holds else None,
                "buy_and_hold_mean_return_pct": (sum(h["return_pct"] for h in holds) / len(holds)) if holds else None,
                "buy_and_hold_mean_max_dd_pct": (sum(h["max_dd_pct"] for h in holds) / len(holds)) if holds else None,
            })
    if not saw:
        report["history"] = "missing"
        report["command"] = (
            "python scripts/fetch_kraken_history.py ohlc --tfs 60 240 1440 && "
            "python scripts/backtest_futures_short.py --fetch-funding"
        )
    else:
        report["history"] = "cache"
        report["sample"] = (
            "Kraken public OHLC is capped near 720 bars "
            "(about 30 days at 1h, 120 days at 4h). Daily bars cover the filter only."
        )
        report["default_on"] = any(r["promote"] for r in report["runs"])
    out = ROOT / "reports" / f"futures_short_{day}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
