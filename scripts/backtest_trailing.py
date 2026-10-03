#!/usr/bin/env python3
"""Walk-forward: current exits vs an ATR trailing take-profit overlay.

The overlay is promoted for a sleeve only when ``research.improves`` is true
on pooled out-of-sample trades (mean bps up, return up, drawdown not worse by
more than 1 point, at least 8 OOS trades). Otherwise the sleeve stays off.

Fees are the spot model: 40 bps taker / 25 bps maker / 10 bps slippage.

    python scripts/fetch_kraken_history.py ohlc --tfs 60 240 1440
    python scripts/backtest_trailing.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dublin_bot.backtest_core import Costs, Spec, add_indicators, metrics, walk_forward  # noqa: E402
from dublin_bot.research import buy_and_hold, improves  # noqa: E402

# Paper defaults. The first (and only) spec is what the sleeve runs; the
# overlay does not re-pick entry parameters.
SLEEVES = {
    "regime": Spec("regime", {"lookback": 20, "atr_mult": 3.0, "min_atr_rank": 0.0,
                              "stop": 0.03, "tp": 0.25, "adx_enter": 25.0, "adx_exit": 20.0,
                              "d1": True}),
    "meanrev": Spec("meanrev", {"rsi_os": 38.0, "rsi_exit": 55.0, "stop": 0.03, "tp": 0.25,
                                "entry": "limit", "limit_offset": 0.001, "d1": True}),
    "trendhold": Spec("trendhold", {"ema_fast": 20, "ema_slow": 100, "d1": True}),
}
TFS = {"regime": 60, "meanrev": 240, "trendhold": 240}
GRID = ((1.0, 1.0), (1.5, 1.0), (2.0, 1.5))
SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"]


def load(symbol: str, tf: int, data: Path):
    import pandas as pd
    from dublin_bot.daily_filter import attach_d1, riskon_table
    base = symbol.replace("/", "")
    parts = []
    for suffix in ("_trades", ""):
        p = data / f"kraken_{base}_{tf}m{suffix}.csv"
        if p.exists():
            df = pd.read_csv(p)
            if len(df):
                parts.append(df)
    if not parts:
        return None
    df = pd.concat(parts).drop_duplicates("time", keep="last").sort_values("time").reset_index(drop=True)
    daily = None
    for suffix in ("_trades", ""):
        p = data / f"kraken_{base}_1440m{suffix}.csv"
        if p.exists():
            daily = pd.read_csv(p)
            break
    df = add_indicators(df)
    if daily is not None and len(daily):
        df = attach_d1(df, riskon_table(daily), tf)
    return df


def _pool(frames, spec, costs):
    from dublin_bot.backtest_core import Trade
    oos: list[Trade] = []
    holds = []
    for df in frames:
        if df is None or len(df) < 400:
            continue
        wf = walk_forward(df, [spec], costs)
        oos.extend(wf.oos_trades)
        bh = buy_and_hold(df, costs)
        if bh:
            holds.append(bh)
    return metrics(oos), holds


def main() -> int:
    data = ROOT / "data"
    costs = Costs(fee_bps=40.0, slippage_bps=10.0, maker_bps=25.0)
    day = time.strftime("%Y-%m-%d")
    report = {"generated_at": day, "rule": "improves() in dublin_bot.research",
              "costs": {"taker_bps": 40, "maker_bps": 25, "slip_bps": 10},
              "grid": [{"activate_atr": a, "trail_atr": t} for a, t in GRID],
              "sleeves": {}}
    any_bars = False
    for name, spec in SLEEVES.items():
        tf = TFS[name]
        frames = [load(sym, tf, data) for sym in SYMBOLS]
        if not any(f is not None and len(f) >= 400 for f in frames):
            report["sleeves"][name] = {"history": "missing", "promote": False, "tf": tf}
            continue
        any_bars = True
        base, holds = _pool(frames, spec, costs)
        best = None
        trials = []
        for act, mult in GRID:
            alt_spec = Spec(spec.name, {**spec.params, "trail_activate_atr": act, "trail_atr": mult})
            alt, _ = _pool(frames, alt_spec, costs)
            ok = improves(base.avg_net_bps, alt.avg_net_bps, base.total_return_pct, alt.total_return_pct,
                          base.max_dd_pct, alt.max_dd_pct, alt.trades)
            row = {"activate_atr": act, "trail_atr": mult, "promote": ok, **alt.to_dict()}
            trials.append(row)
            if ok and (best is None or row["avg_net_bps"] > best["avg_net_bps"]):
                best = row
        bh_bps = sum(h["net_bps"] for h in holds) / len(holds) if holds else None
        report["sleeves"][name] = {
            "tf": tf, "history": "cache", "promote": best is not None,
            "baseline": base.to_dict(), "trials": trials, "chosen": best,
            "buy_and_hold_mean_net_bps": bh_bps,
        }
    if not any_bars:
        report["history"] = "missing"
        report["command"] = "python scripts/fetch_kraken_history.py ohlc --tfs 60 240 1440 && python scripts/backtest_trailing.py"
    else:
        report["history"] = "cache"
        report["sample"] = (
            "Kraken public OHLC is capped near 720 bars "
            "(about 30 days at 1h, 120 days at 4h). This is not a multi-year sample."
        )
    out = ROOT / "reports" / f"trailing_tp_{day}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
