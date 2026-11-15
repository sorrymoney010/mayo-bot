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
  5. profit factor >= 1.10 (gross wins / gross losses on net_bps after fees;
     falls back to pnl when net_bps is missing; PF is inf when n > 0 and there
     are no losses)
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

from .learner import mean_bounds

MIN_TRADES = 30
CONF = 0.90
MIN_PF = 1.10
# Prefer these, in order, as a stable trade identifier when present.
ID_FIELDS = ("trade_id", "id", "order_id", "txid", "lot_id")


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


def stable_trade_id(row: dict) -> str | None:
    """Return a stable id from the row, or None if it has none."""
    for f in ID_FIELDS:
        v = row.get(f)
        if v is not None and str(v).strip() != "":
            return str(v)
    return None


def legacy_trade_key(row: dict) -> str:
    return f"{row.get('symbol')}|{float(row.get('ts', 0)):.3f}|{float(row.get('pnl', 0)):.6f}"


def dedup_trades(rows: list[dict]) -> list[dict]:
    """One row per closed trade (rich 'fill' rows win over learner-synced rows).

    Key on a stable identifier (trade_id, id, order_id, txid, lot_id) when
    present. Rows with no id fall back to symbol|ts|pnl. A no-id row that
    shares that legacy key with an identified row is treated as the same
    trade (historical lean+rich pairs). Two rows with *different* ids stay
    distinct even if their legacy keys collide.
    """
    best: dict[str, dict] = {}
    legacy_to_k: dict[str, str] = {}
    for r in rows:
        try:
            legacy = legacy_trade_key(r)
        except (TypeError, ValueError):
            continue
        tid = stable_trade_id(r)
        if tid is not None:
            k = f"id:{tid}"
            prev_k = legacy_to_k.get(legacy)
            if prev_k and prev_k.startswith("legacy:") and prev_k in best:
                old = best.pop(prev_k)
                if k not in best or (old.get("source") == "fill" and r.get("source") != "fill"):
                    best[k] = old
            if prev_k is None or prev_k.startswith("legacy:"):
                legacy_to_k[legacy] = k
        else:
            k = legacy_to_k.get(legacy, f"legacy:{legacy}")
            legacy_to_k.setdefault(legacy, k)
        if k not in best or r.get("source") == "fill":
            best[k] = r
    return sorted(best.values(), key=lambda r: float(r.get("ts", 0)))


def profit_factor(values: list[float]) -> float | None:
    """Gross wins / gross losses. inf if n > 0 and no losses; None if empty."""
    if not values:
        return None
    wins = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v < 0)
    if losses == 0:
        return math.inf
    return wins / losses


def _pf_vals(trades: list[dict], bps: list[float]) -> list[float]:
    if bps:
        return bps
    out: list[float] = []
    for t in trades:
        try:
            out.append(float(t.get("pnl", 0.0) or 0.0))
        except (TypeError, ValueError):
            continue
    return out


def _pf_out(x):
    if x is None:
        return None
    if x == math.inf:
        return "inf"
    return round(float(x), 2)


def check_strategy(trades: list[dict], *, min_trades: int = MIN_TRADES, conf: float = CONF,
                   min_pf: float = MIN_PF) -> dict:
    bps = [float(t["net_bps"]) for t in trades if t.get("net_bps") is not None]
    pnl = sum(float(t.get("pnl", 0.0) or 0.0) for t in trades)
    n = len(bps)
    mean, lo, _hi = mean_bounds(bps, conf)
    rest = sorted(bps)[:-2] if n > 2 else []
    ex2 = (sum(rest) / len(rest)) if rest else None
    pf = profit_factor(_pf_vals(trades, bps))
    checks = {
        "min_trades": {"pass": n >= min_trades, "value": n, "need": f">= {min_trades}"},
        "mean_positive": {"pass": mean is not None and mean > 0, "value": _r(mean), "need": "> 0 bps"},
        "lower_bound_positive": {"pass": lo is not None and lo > 0, "value": _r(lo),
                                 "need": f"{int(conf * 100)}% lower bound > 0 bps"},
        "ex_best_2_positive": {"pass": ex2 is not None and ex2 > 0, "value": _r(ex2),
                               "need": "mean without best 2 trades > 0 bps"},
        "beats_cash": {"pass": pnl >= 0 and n > 0, "value": round(pnl, 4), "need": "total realized P&L >= $0"},
        "profit_factor": {"pass": pf is not None and pf >= min_pf, "value": _pf_out(pf),
                          "need": f">= {min_pf}"},
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
        "bar": {"min_trades": MIN_TRADES, "confidence": CONF, "min_profit_factor": MIN_PF,
                "rules": ["n >= 30 closed paper trades", "mean net bps > 0 and 90% lower bound > 0",
                          "mean without best 2 trades > 0", "total realized P&L >= 0 (at least as good as cash)",
                          "profit factor >= 1.10 (gross wins / gross losses after fees)"]},
        "strategies": {k: check_strategy(v) for k, v in sorted(by.items())},
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
                     f"ex-best-2={c['ex_best_2_positive']['value']}  pnl=${c['beats_cash']['value']}  "
                     f"pf={c['profit_factor']['value']}"
                     + (f"  failed: {','.join(v['failed'])}" if v["failed"] else ""))
    if rep.get("paper_book"):
        b = rep["paper_book"]
        lines.append(f"  paper book: equity {b['equity']} vs seed {b['seed']} ({b['vs_cash_pct']}% vs cash)")
    return "\n".join(lines)
