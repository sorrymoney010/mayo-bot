#!/usr/bin/env python3
"""Fee-aware walk-forward backtest over cached Kraken public history.

Evaluates three strategy families on BTC/ETH/SOL:
  * breakout  – N-bar high + volume > N-bar avg, fixed % stop / TP (current paper sleeve)
  * momentum  – RSI band + price > EMA200 + ADX hysteresis (25/20), %-stop/TP,
                exit on close < EMA50
  * regime    – ADX/volatility gated trend-follower: only enters when ADX says
                "trend" (and optional ATR% expansion) AND EMA20>EMA50, price >
                EMA200, N-bar high break. Chandelier ATR trailing stop; goes flat
                when ADX drops into chop. Sits out entirely in chop.

Costs default to Kraken tier-1 TAKER 0.40%/side + 5 bps slippage/side
(0.90% round trip). ``--fee-bps 25 --slip-bps 0`` models maker/limit fills.

Data: data/kraken_<SYM>_<tf>m.csv (OHLC endpoint, ~720 bars) and, when present,
data/kraken_<SYM>_<tf>m_trades.csv (trade-built deeper history). The longer
of the two is used. Run scripts/fetch_kraken_history.py first.

Outputs a table to stdout and JSON to data/walkforward_results.json
(the paper learner reads per-symbol OOS expectancy from it as a prior).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dublin_bot.backtest_core import (  # noqa: E402
    Costs, add_indicators, default_grid, metrics, simulate, walk_forward,
)

DATA = ROOT / "data"
SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "PUMP/USD"]


def load(symbol: str, tf: int) -> tuple[pd.DataFrame, str]:
    """Stitch trade-built history with the OHLC-endpoint cache (OHLC wins on overlap)."""
    base = symbol.replace("/", "")
    parts, srcs = [], []
    for suffix, src in (("_trades", "trades"), ("", "ohlc")):
        p = DATA / f"kraken_{base}_{tf}m{suffix}.csv"
        if p.exists():
            df = pd.read_csv(p)
            if len(df):
                parts.append(df)
                srcs.append(src)
    if not parts:
        return pd.DataFrame(), "none"
    df = (pd.concat(parts).drop_duplicates("time", keep="last")
          .sort_values("time").reset_index(drop=True))
    # Keep only the most recent CONTIGUOUS run (a gap > 6 bars would corrupt
    # indicators / fake a breakout), e.g. an unfinished trades backfill.
    gaps = df["time"].diff().fillna(tf * 60) > tf * 60 * 6
    if gaps.any():
        last_gap = int(gaps[gaps].index[-1])
        df = df.iloc[last_gap:].reset_index(drop=True)
    return df, "+".join(srcs)


def fmt_row(cols: list, widths: list[int]) -> str:
    return " ".join(str(c).rjust(w) if i else str(c).ljust(w) for i, (c, w) in enumerate(zip(cols, widths)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tfs", nargs="*", type=int, default=[15, 60, 240])
    ap.add_argument("--symbols", nargs="*", default=SYMBOLS)
    ap.add_argument("--fee-bps", type=float, default=40.0)
    ap.add_argument("--slip-bps", type=float, default=5.0)
    ap.add_argument("--maker-bps", type=float, default=25.0)
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--equity", type=float, default=500.0)
    ap.add_argument("--out", default=str(DATA / "walkforward_results.json"))
    a = ap.parse_args()

    costs = Costs(fee_bps=a.fee_bps, slippage_bps=a.slip_bps, maker_bps=a.maker_bps)
    grid = default_grid()
    results: list[dict] = []
    widths = [8, 5, 11, 12, 6, 6, 8, 8, 7, 7, 6, 6, 8, 8]
    header = ["symbol", "tf", "family", "src", "days", "IS_n", "IS_bps", "IS_$/t",
              "OOS_n", "OOS_win", "OOSbps", "OOS$/t", "OOS_dd%", "FULL_bps"]
    print(f"costs: fee {a.fee_bps}bps/side + slippage {a.slip_bps}bps/side "
          f"= {2 * (a.fee_bps + a.slip_bps):.0f}bps round trip taker; maker {a.maker_bps}bps/side (*_mk entries, TP exits); equity ${a.equity:.0f}, 1% risk, 40% cap")
    print(fmt_row(header, widths))
    for tf in a.tfs:
        for sym in a.symbols:
            raw, src = load(sym, tf)
            if len(raw) < 400:
                print(f"{sym} {tf}m: only {len(raw)} bars — skipped")
                continue
            d = add_indicators(raw)
            days = (raw["time"].iloc[-1] - raw["time"].iloc[0]) / 86400
            for fam, specs in grid.items():
                wf = walk_forward(d, specs, costs, folds=a.folds)
                m_is = metrics(wf.is_trades, equity=a.equity)
                m_oos = metrics(wf.oos_trades, equity=a.equity)
                # Full-period, fixed DEFAULT params (no selection) for reference.
                full = simulate(d, specs[0], costs)
                m_full = metrics(full, equity=a.equity)
                # Per-regime OOS breakdown (feeds the learner prior).
                by_regime: dict[str, dict] = {}
                for reg in sorted({t.regime for t in wf.oos_trades}):
                    by_regime[reg] = metrics([t for t in wf.oos_trades if t.regime == reg],
                                             equity=a.equity).to_dict()
                row = {
                    "symbol": sym, "tf": tf, "family": fam, "source": src,
                    "bars": len(raw), "days": round(days, 1),
                    "costs": {"fee_bps": a.fee_bps, "slippage_bps": a.slip_bps,
                              "maker_bps": a.maker_bps},
                    "is": m_is.to_dict(), "oos": m_oos.to_dict(),
                    "oos_by_regime": by_regime,
                    "full_default": m_full.to_dict(), "default_spec": specs[0].label(),
                    "chosen_per_fold": wf.chosen,
                }
                results.append(row)
                print(fmt_row([
                    sym.split("/")[0], f"{tf}m", fam, src, f"{days:.0f}",
                    m_is.trades, f"{m_is.avg_net_bps:.0f}", f"{m_is.expectancy_usd:.2f}",
                    m_oos.trades, f"{m_oos.win_rate * 100:.0f}%", f"{m_oos.avg_net_bps:.0f}",
                    f"{m_oos.expectancy_usd:.2f}", f"{m_oos.max_dd_pct:.1f}",
                    f"{m_full.avg_net_bps:.0f}({m_full.trades})",
                ], widths))
    # Aggregate per (tf,family) across symbols, OOS.
    print("\nOOS aggregate across symbols (sum of trades, trade-weighted bps):")
    agg: dict[tuple, list] = {}
    for r in results:
        agg.setdefault((r["tf"], r["family"]), []).append(r)
    summary = []
    for (tf, fam), rows in sorted(agg.items()):
        n = sum(r["oos"]["trades"] for r in rows)
        bps = sum(r["oos"]["avg_net_bps"] * r["oos"]["trades"] for r in rows) / n if n else 0.0
        usd = sum(r["oos"]["expectancy_usd"] * r["oos"]["trades"] for r in rows) / n if n else 0.0
        wins = sum(r["oos"]["wins"] for r in rows)
        summary.append({"tf": tf, "family": fam, "oos_trades": n,
                        "oos_win_rate": round(wins / n, 3) if n else 0.0,
                        "oos_avg_net_bps": round(bps, 1), "oos_expectancy_usd": round(usd, 3)})
        print(f"  {tf:>4}m {fam:<9} n={n:<4} win={wins / n * 100 if n else 0:5.1f}% "
              f"net={bps:7.1f}bps  ${usd:+.3f}/trade")
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "costs": {"fee_bps": a.fee_bps, "slippage_bps": a.slip_bps, "maker_bps": a.maker_bps},
        "results": results, "summary": summary,
    }
    Path(a.out).write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
