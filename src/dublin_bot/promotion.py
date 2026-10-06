"""Live-promotion bar — a REPORT ONLY.

This module reads paper results and says whether each strategy clears the bar.
It never reads or writes settings, env files or locks, and nothing in the bot
imports it to make a trading decision. Going live stays a manual, human change
of PAPER_TRADING / DRY_RUN / ALLOW_LIVE_TRADING.

Bar, per strategy (sleeve key), on closed PAPER trades after fees:
  1. at least 30 closed trades
  2. mean net bps > 0 AND the one-sided 90% lower confidence bound > 0
  3. still positive without its best 2 trades (mean of the rest > 0)
  4. at least as good as cash: total realized P&L >= 0
  5. not a retired sleeve (15m, daily, momentum, breakout, futures, PUMP)
  6. when a buy-and-hold benchmark is supplied, strategy return must beat it
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .learner import mean_bounds

MIN_TRADES = 30
CONF = 0.90

# Audit 2026-10-04: these families lost after costs or were noise. A green
# printout on them is not a promotion, even if a short window looks positive.
RETIRED_MARKERS = (
    "momentum", "breakout", "futures", "pump", "sr_flip", "pattern",
    "elliott", "15m", "1440",
)


def is_retired(sleeve: str) -> bool:
    name = (sleeve or "").lower()
    if not name:
        return False
    if name.startswith("meanrev") or name.startswith("regime") or "hold" in name or "trendhold" in name:
        # 15m / daily variants of the survivors are still retired.
        return any(tag in name for tag in ("15m", "1440", "pump", "futures"))
    return any(tag in name for tag in RETIRED_MARKERS)


def load_jsonl(path: Path | str) -> list[dict]:
    out: list[dict] = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def dedup_trades(rows: list[dict]) -> list[dict]:
    """One row per closed trade (rich 'fill' rows win over learner-synced rows)."""
    best: dict[str, dict] = {}
    for r in rows:
        try:
            k = f"{r.get('symbol')}|{float(r.get('ts', 0)):.3f}|{float(r.get('pnl', 0)):.6f}"
        except (TypeError, ValueError):
            continue
        if k not in best or r.get("source") == "fill":
            best[k] = r
    return sorted(best.values(), key=lambda r: float(r.get("ts", 0)))


def check_strategy(trades: list[dict], *, min_trades: int = MIN_TRADES, conf: float = CONF,
                    sleeve: str = "", benchmark_return_pct: float | None = None,
                    seed: float | None = None) -> dict:
    bps = [float(t["net_bps"]) for t in trades if t.get("net_bps") is not None]
    pnl = sum(float(t.get("pnl", 0.0) or 0.0) for t in trades)
    n = len(bps)
    mean, lo, _hi = mean_bounds(bps, conf)
    rest = sorted(bps)[:-2] if n > 2 else []
    ex2 = (sum(rest) / len(rest)) if rest else None
    checks = {
        "min_trades": {"pass": n >= min_trades, "value": n, "need": f">= {min_trades}"},
        "mean_positive": {"pass": mean is not None and mean > 0, "value": _r(mean), "need": "> 0 bps"},
        "lower_bound_positive": {"pass": lo is not None and lo > 0, "value": _r(lo),
                                 "need": f"{int(conf * 100)}% lower bound > 0 bps"},
        "ex_best_2_positive": {"pass": ex2 is not None and ex2 > 0, "value": _r(ex2),
                               "need": "mean without best 2 trades > 0 bps"},
        "beats_cash": {"pass": pnl >= 0 and n > 0, "value": round(pnl, 4), "need": "total realized P&L >= $0"},
    }
    if sleeve:
        retired = is_retired(sleeve)
        checks["not_retired"] = {
            "pass": not retired, "value": sleeve,
            "need": "sleeve not on the 2026-10-04 retired list",
        }
    if benchmark_return_pct is not None:
        strat = (pnl / seed * 100.0) if seed else None
        checks["beats_hold"] = {
            "pass": strat is not None and strat > float(benchmark_return_pct),
            "value": None if strat is None else round(strat, 2),
            "need": f"return > buy-and-hold {float(benchmark_return_pct):.2f}% on the same window",
        }
    return {"trades": n, "passes": all(c["pass"] for c in checks.values()),
            "failed": [k for k, c in checks.items() if not c["pass"]], "checks": checks}


def _r(x):
    return None if x is None else round(float(x), 1)


def build_report(logs_dir: Path | str, *, now: float | None = None) -> dict:
    logs = Path(logs_dir)
    trades = dedup_trades(load_jsonl(logs / "closed_trades.jsonl"))
    by: dict[str, list[dict]] = {}
    for t in trades:
        by.setdefault(str(t.get("sleeve") or "unknown"), []).append(t)
    eq = load_jsonl(logs / "equity.jsonl")
    last = eq[-1] if eq else None
    book = None
    if last:
        seed = float(last.get("seed") or 0) or None
        book = {"equity": last.get("equity"), "seed": seed, "ts": last.get("ts"),
                "vs_cash_pct": round((float(last["equity"]) / seed - 1) * 100, 2) if seed else None}
    now = time.time() if now is None else now
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "report_only": True,
        "note": ("Report only. Nothing here changes PAPER_TRADING / DRY_RUN / ALLOW_LIVE_TRADING; "
                 "promotion to live is a manual decision."),
        "bar": {"min_trades": MIN_TRADES, "confidence": CONF,
                "rules": ["n >= 30 closed paper trades", "mean net bps > 0 and 90% lower bound > 0",
                          "mean without best 2 trades > 0", "total realized P&L >= 0 (at least as good as cash)", "not a retired sleeve", "beats buy-and-hold when a benchmark is supplied"]},
        "strategies": {k: check_strategy(v, sleeve=k) for k, v in sorted(by.items())},
        "paper_book": book,
    }


def write_report(logs_dir: Path | str, *, now: float | None = None) -> dict:
    rep = build_report(logs_dir, now=now)
    out = Path(logs_dir) / "promotion_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(rep, indent=1), encoding="utf-8")
    tmp.replace(out)
    return rep


def render(rep: dict) -> str:
    lines = ["LIVE-PROMOTION CHECK (report only — never changes a lock)"]
    if not rep["strategies"]:
        lines.append("  no closed paper trades yet")
    for k, v in rep["strategies"].items():
        c = v["checks"]
        lines.append(f"  {k:<20} {'PASS' if v['passes'] else 'not yet'}  n={v['trades']}  "
                     f"mean={c['mean_positive']['value']}  lb90={c['lower_bound_positive']['value']}  "
                     f"ex-best-2={c['ex_best_2_positive']['value']}  pnl=${c['beats_cash']['value']}"
                     + (f"  failed: {','.join(v['failed'])}" if v["failed"] else ""))
    if rep.get("paper_book"):
        b = rep["paper_book"]
        lines.append(f"  paper book: equity {b['equity']} vs seed {b['seed']} ({b['vs_cash_pct']}% vs cash)")
    return "\n".join(lines)
