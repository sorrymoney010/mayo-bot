"""Per-sleeve closed-trade scorecard (paper book).

Source of truth for closed trades is the learner history in
``logs/learner.json`` (each entry: realized ``pnl`` after fees, ``net_bps`` vs
cost basis, ``strategy`` = sleeve key, ``ts``). The learner keeps only the last
200 trades per coin, so every run first appends new trades to an append-only
``logs/closed_trades.jsonl`` (dedup by trade_id when present, else symbol/ts/pnl) and the scorecard is
computed from that ledger — history survives the learner cap.

Rich rows (``source: "fill"``, written by the learner at the close) carry
signal vs fill price, fees, maker/taker, exit reason, MAE/MFE, hold time; the
scorecard summarises them per sleeve. It also summarises
``shadow_signals.jsonl`` (blocked signals and their hypothetical outcomes),
``equity.jsonl`` (marked-to-market paper book) and the report-only live-promotion
check (``promotion.py``).

Pure stdlib + json; reads no settings, no .env, places nothing.
"""
from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path


def _key(t: dict) -> str:
    return f"{t.get('symbol')}|{float(t.get('ts', 0)):.3f}|{float(t.get('pnl', 0)):.6f}"


def learner_trades(learner_path: Path) -> list[dict]:
    try:
        data = json.loads(Path(learner_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for sym, c in (data.get("coins") or {}).items():
        for h in c.get("history") or []:
            try:
                pnl = float(h.get("pnl"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(pnl):
                continue
            nb = h.get("net_bps")
            row = {"symbol": sym, "ts": float(h.get("ts") or 0.0), "pnl": pnl,
                   "net_bps": None if nb is None else float(nb),
                   "sleeve": str(h.get("strategy") or data.get("strategy_key") or "unknown"),
                   "regime": h.get("regime", "unknown")}
            tid = h.get("trade_id") or h.get("id") or h.get("order_id") or h.get("txid") or h.get("lot_id")
            if tid is not None and str(tid).strip() != "":
                row["trade_id"] = str(tid)
            out.append(row)
    return sorted(out, key=lambda t: t["ts"])


def sync_ledger(learner_path: Path, ledger_path: Path) -> list[dict]:
    """Append unseen learner trades to the jsonl ledger; return the full ledger."""
    ledger_path = Path(ledger_path)
    have: list[dict] = []
    if ledger_path.exists():
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            try:
                have.append(json.loads(line))
            except ValueError:
                continue
    seen = {_key(t) for t in have}
    new = [t for t in learner_trades(learner_path) if _key(t) not in seen]
    if new:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with ledger_path.open("a", encoding="utf-8") as fh:
            for t in new:
                fh.write(json.dumps(t, sort_keys=True) + "\n")
    return sorted(have + new, key=lambda t: t["ts"])


def _stats(trades: list[dict]) -> dict:
    n = len(trades)
    pnl = [t["pnl"] for t in trades]
    bps = [t["net_bps"] for t in trades if t.get("net_bps") is not None]
    return {
        "trades": n,
        "wins": sum(p > 0 for p in pnl),
        "win_pct": round(100.0 * sum(p > 0 for p in pnl) / n, 1) if n else None,
        "net_pnl_usd": round(sum(pnl), 4),
        "avg_net_bps": round(statistics.fmean(bps), 1) if bps else None,
        "median_net_bps": round(statistics.median(bps), 1) if bps else None,
        "best_bps": round(max(bps), 1) if bps else None,
        "worst_bps": round(min(bps), 1) if bps else None,
        "last_close_ts": max((t["ts"] for t in trades), default=None),
    }


def _mean(xs):
    xs = [float(x) for x in xs if isinstance(x, (int, float))]
    return round(statistics.fmean(xs), 2) if xs else None


def _detail(trades: list[dict]) -> dict:
    rich = [t for t in trades if t.get("source") == "fill"]
    if not rich:
        return {"rich_trades": 0}
    return {
        "rich_trades": len(rich),
        "fees_usd": round(sum(float(t.get("fees_usd") or 0) for t in rich), 4),
        "maker_entry_pct": round(100.0 * sum(bool(t.get("entry_maker")) for t in rich) / len(rich), 1),
        "maker_exit_pct": round(100.0 * sum(bool(t.get("exit_maker")) for t in rich) / len(rich), 1),
        "avg_entry_slip_bps": _mean(t.get("entry_slip_bps") for t in rich),
        "avg_exit_slip_bps": _mean(t.get("exit_slip_bps") for t in rich),
        "avg_mae_bps": _mean(t.get("mae_bps") for t in rich),
        "avg_mfe_bps": _mean(t.get("mfe_bps") for t in rich),
        "avg_hold_h": (round(_mean(t.get("hold_s") for t in rich) / 3600, 2)
                       if _mean(t.get("hold_s") for t in rich) is not None else None),
        "exit_reasons": {r: sum(1 for t in rich if t.get("exit_reason") == r)
                         for r in sorted({str(t.get("exit_reason")) for t in rich})},
    }


def shadow_summary(rows: list[dict]) -> dict:
    sig = {r["id"]: r for r in rows if r.get("type") == "signal" and r.get("id")}
    out_rows = {r["id"]: r for r in rows if r.get("type") == "outcome" and r.get("id") in sig}
    groups: dict[str, dict] = {}
    for sid, r in sig.items():
        g = groups.setdefault(f"{r.get('strategy')}|{r.get('gate')}", {"signals": 0, "scored": 0, "open": 0,
                                                                      "other": 0, "bps": []})
        g["signals"] += 1
        o = out_rows.get(sid)
        if o is None:
            g["open"] += 1
        elif o.get("status") in ("scored", "no_fill") and o.get("net_bps") is not None:
            g["scored"] += 1
            g["bps"].append(float(o["net_bps"]))
        else:
            g["other"] += 1
    return {k: {"signals": v["signals"], "scored": v["scored"], "open": v["open"], "unscorable": v["other"],
                "avg_net_bps": round(statistics.fmean(v["bps"]), 1) if v["bps"] else None,
                "sum_net_bps": round(sum(v["bps"]), 1)} for k, v in sorted(groups.items())}


def equity_summary(rows: list[dict]) -> dict:
    eq = [float(r["equity"]) for r in rows if isinstance(r.get("equity"), (int, float))]
    if not eq:
        return {"points": 0}
    peak, mdd = eq[0], 0.0
    for e in eq:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1 if peak > 0 else 0.0)
    seed = rows[-1].get("seed")
    return {"points": len(eq), "first_ts": rows[0].get("ts"), "last_ts": rows[-1].get("ts"),
            "equity": round(eq[-1], 4), "seed": seed,
            "return_pct": round((eq[-1] / float(seed) - 1) * 100, 2) if seed else None,
            "max_dd_pct": round(mdd * 100, 2), "open_positions": rows[-1].get("open_positions")}


def build(trades: list[dict], *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    sleeves: dict[str, list[dict]] = {}
    for t in trades:
        sleeves.setdefault(t["sleeve"], []).append(t)
    out = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
           "date_utc": time.strftime("%Y-%m-%d", time.gmtime(now)),
           "note": "paper book; pnl is realized after fees; net_bps vs cost basis",
           "all": _stats(trades), "last_24h": _stats([t for t in trades if t["ts"] >= now - 86400]),
           "sleeves": {}}
    for name, ts in sorted(sleeves.items()):
        by_sym: dict[str, list[dict]] = {}
        for t in ts:
            by_sym.setdefault(t["symbol"], []).append(t)
        out["sleeves"][name] = {**_stats(ts), "detail": _detail(ts),
                                "by_symbol": {s: _stats(v) for s, v in sorted(by_sym.items())}}
    return out


def write_scorecard(logs_dir: Path, *, now: float | None = None) -> dict:
    logs_dir = Path(logs_dir)
    from .promotion import dedup_trades, load_jsonl, write_report

    trades = dedup_trades(sync_ledger(logs_dir / "learner.json", logs_dir / "closed_trades.jsonl"))
    card = build(trades, now=now)
    card["shadow"] = shadow_summary(load_jsonl(logs_dir / "shadow_signals.jsonl"))
    card["equity"] = equity_summary(load_jsonl(logs_dir / "equity.jsonl"))
    card["promotion"] = {k: {"passes": v["passes"], "failed": v["failed"], "trades": v["trades"]}
                         for k, v in write_report(logs_dir, now=now)["strategies"].items()}
    card["promotion_note"] = "report only — see scripts/promotion_check.py; never changes a lock"
    logs_dir.mkdir(parents=True, exist_ok=True)
    tmp = logs_dir / "scorecard.json.tmp"
    tmp.write_text(json.dumps(card, indent=1), encoding="utf-8")
    tmp.replace(logs_dir / "scorecard.json")
    return card
