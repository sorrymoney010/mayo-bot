"""Automatic, staged promotion of order-flow overlays: candidate -> shadow -> paper.

Stages (one record per ``sleeve|variant`` in ``logs/flow_overlays.json``):

* ``candidate``  scored by the weekly research, did not clear both backtest gates.
* ``shadow``     cleared the backtest bar AND the improvement gate
                 (``flow_research.run_study`` -> ``shadow_candidate``). Promotion to
                 shadow is automatic: shadow = signals/virtual trades only, no fills.
* ``paper``      additionally cleared the bar on >= 30 FORWARD shadow trades (trades whose
                 entry is after the record entered shadow, replayed with frozen rules on
                 ticks that arrived later) and beat the unmodified sleeve on the same
                 forward window. Automatic only when ``auto_paper`` is on (setting
                 ``flow_auto_paper``, default OFF), the overlay is an entry filter (the only
                 kind the paper sleeves can apply today), and the process is in paper mode
                 (PAPER_TRADING=true, DRY_RUN=true, ALLOW_LIVE_TRADING=false). Otherwise the
                 record is marked ``paper_ready`` and waits.
* ``retired``    a paper overlay whose forward paper-window trades (>= 30) fall to a mean
                 <= 0 or below the unmodified sleeve is switched off automatically.

This module never reads or writes a safety lock, an env file, a key or the settings
file, and never places an order. Going LIVE is not a stage here.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np

from .promotion import check_strategy

REGISTRY = Path("logs/flow_overlays.json")
FORWARD = Path("logs/flow_shadow_trades.jsonl")
MIN_FORWARD = 30
STAGES = ("candidate", "shadow", "paper", "retired")


# ── registry io ────────────────────────────────────────────────

def load(path: Path | str = REGISTRY) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(reg: dict, path: Path | str = REGISTRY) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _move(rec: dict, stage: str, why: str, now: float) -> None:
    rec.setdefault("history", []).append({"ts": _iso(now), "from": rec.get("stage"), "to": stage, "why": why})
    rec["stage"] = stage
    rec[f"{stage}_since"] = now


def paper_supported(overlay: dict) -> bool:
    """The paper sleeves can only apply an entry veto today."""
    return bool(overlay.get("entry_filter")) and not overlay.get("exit_trigger") \
        and overlay.get("stop_mode", "intrabar") == "intrabar" and not overlay.get("ptp_atr")


def paper_mode_ok(settings) -> bool:
    if settings is None:
        return False
    return (bool(getattr(settings, "paper_trading", False)) and bool(getattr(settings, "dry_run", False))
            and not bool(getattr(settings, "allow_live_trading", True)))


# ── stage 1: backtest -> shadow ────────────────────────────────

def update_from_backtest(report: dict, *, path: Path | str = REGISTRY, now: float | None = None,
                         report_path: str = "") -> dict:
    """Record every scored variant; move passing candidates to shadow. Returns the registry."""
    from .flow_research import OVERLAYS
    now = time.time() if now is None else float(now)
    reg = load(path)
    for sk, s in (report.get("sleeves") or {}).items():
        for nm, v in (s.get("variants") or {}).items():
            if nm == "baseline":
                continue
            rid = f"{sk}|{nm}"
            ov = OVERLAYS.get(nm)
            rec = reg.setdefault(rid, {"id": rid, "sleeve": sk, "variant": nm, "stage": "candidate",
                                       "created": _iso(now), "candidate_since": now,
                                       "overlay": asdict(ov) if ov else {}, "history": []})
            rec["last_backtest"] = {"report": report_path, "at": _iso(now), "n": v.get("n"),
                                    "mean": v.get("mean"), "lb90": v.get("lb90"),
                                    "bar": bool(v.get("bar", {}).get("passes")),
                                    "improves": bool((v.get("improvement") or {}).get("passes")),
                                    "shadow_candidate": bool(v.get("shadow_candidate"))}
            if rec["stage"] == "candidate" and v.get("shadow_candidate"):
                _move(rec, "shadow", "passed backtest bar + improvement gate", now)
    save(reg, path)
    return reg


# ── stage 2: forward shadow trades ─────────────────────────────

def _read_forward(path: Path | str) -> list[dict]:
    out = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def record_forward_trades(reg: dict, trades_by_rec: dict[str, dict[str, list[dict]]], *,
                          path: Path | str = FORWARD) -> int:
    """Append unseen CLOSED forward trades. ``trades_by_rec[rid] = {"variant": [...], "baseline": [...]}``
    with rows ``{"symbol","entry_time","exit_time","net_bps","pnl","reason"}``."""
    seen = {r.get("id") for r in _read_forward(path)}
    n = 0
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        for rid, kinds in trades_by_rec.items():
            rec = reg.get(rid)
            if not rec or rec.get("stage") not in ("shadow", "paper"):
                continue
            since = float(rec.get("shadow_since") or 0)
            for kind, rows in kinds.items():
                for r in rows:
                    if float(r["entry_time"]) < since or "eod_mark" in str(r.get("reason", "")):
                        continue
                    fid = f"{rid}|{kind}|{r['symbol']}|{int(r['entry_time'])}"
                    if fid in seen:
                        continue
                    seen.add(fid)
                    fh.write(json.dumps({"id": fid, "rec": rid, "kind": kind, **r}) + "\n")
                    n += 1
    return n


def forward_stats(rid: str, *, since: float, path: Path | str = FORWARD) -> dict:
    rows = [r for r in _read_forward(path) if r.get("rec") == rid and float(r.get("entry_time", 0)) >= since]
    var = [r for r in rows if r.get("kind") == "variant"]
    base = [r for r in rows if r.get("kind") == "baseline"]
    bar = check_strategy(var)
    vm = float(np.mean([r["net_bps"] for r in var])) if var else None
    bm = float(np.mean([r["net_bps"] for r in base])) if base else None
    beats = vm is not None and (bm is None or vm > bm)
    return {"n": len(var), "mean": vm, "baseline_n": len(base), "baseline_mean": bm,
            "bar": bar, "beats_baseline": beats,
            "passes": bool(bar["passes"] and len(var) >= MIN_FORWARD and beats)}


# ── stage 3: shadow -> paper, paper -> retired ─────────────────

def advance(*, path: Path | str = REGISTRY, forward_path: Path | str = FORWARD, settings=None,
            auto_paper: bool | None = None, now: float | None = None) -> list[dict]:
    """Apply forward-evidence transitions. Returns a list of changes."""
    now = time.time() if now is None else float(now)
    if auto_paper is None:
        auto_paper = bool(getattr(settings, "flow_auto_paper", False))
    reg = load(path)
    changes = []
    for rid, rec in reg.items():
        st = rec.get("stage")
        if st == "shadow":
            fs = forward_stats(rid, since=float(rec.get("shadow_since") or now), path=forward_path)
            rec["forward"] = {k: v for k, v in fs.items() if k != "bar"} | {"failed": fs["bar"]["failed"]}
            if not fs["passes"]:
                continue
            rec["paper_ready"] = True
            if not paper_supported(rec.get("overlay") or {}):
                rec["paper_blocked"] = "overlay kind not applicable by paper sleeves yet (entry filters only)"
            elif not auto_paper:
                rec["paper_blocked"] = "flow_auto_paper is off"
            elif not paper_mode_ok(settings):
                rec["paper_blocked"] = "process not in paper mode"
            else:
                rec.pop("paper_blocked", None)
                _move(rec, "paper", f"forward shadow n={fs['n']} mean={fs['mean']:.1f} bps passed", now)
                changes.append({"id": rid, "to": "paper"})
        elif st == "paper":
            fs = forward_stats(rid, since=float(rec.get("paper_since") or now), path=forward_path)
            rec["paper_forward"] = {k: v for k, v in fs.items() if k != "bar"}
            if fs["n"] >= MIN_FORWARD and ((fs["mean"] or 0) <= 0 or not fs["beats_baseline"]):
                _move(rec, "retired", f"paper-window forward n={fs['n']} mean={fs['mean']:.1f} failed", now)
                changes.append({"id": rid, "to": "retired"})
            elif not paper_mode_ok(settings) and settings is not None:
                _move(rec, "shadow", "process left paper mode; overlay parked in shadow", now)
                changes.append({"id": rid, "to": "shadow"})
    save(reg, path)
    return changes


def active(stage: str, sleeve: str, *, path: Path | str = REGISTRY) -> list[dict]:
    return [r for r in load(path).values() if r.get("stage") == stage and r.get("sleeve") == sleeve]


# ── live hooks (paper stage only; fail OPEN = unmodified sleeve) ─

_LIVE_CACHE: dict = {}
LIVE_TTL = 120.0


def _live_tf_frame(data_dir: Path, symbol: str, tf: int, now: float, cache_dir: Path | None):
    from .orderflow import build_flow_1m, continuous_1m, fine_features, minute_features, tf_features
    key = (str(data_dir), symbol, tf)
    hit = _LIVE_CACHE.get(key)
    if hit and now - hit[0] < LIVE_TTL:
        return hit[1]
    base = symbol.replace("/", "")
    m1 = build_flow_1m(Path(data_dir) / "ticks" / base, start=now - 9 * 86400, end=now,
                       cache_dir=(cache_dir / base) if cache_dir else None)
    if not len(m1):
        return None
    a = int(m1.index[0])
    b = int(now // 60 * 60)
    c1 = continuous_1m(m1, a, b)
    mf = minute_features(c1)
    fb = fine_features(c1, mf)
    d = tf_features(c1, mf, fb, tf)
    _LIVE_CACHE[key] = (now, d)
    return d


def paper_entry_veto(sleeve: str, symbol: str, *, settings=None, path: Path | str = REGISTRY,
                     now: float | None = None) -> str | None:
    """Reason string to SKIP a paper entry, or None. No paper overlays -> None without any I/O
    beyond reading the registry. Any error -> None (the sleeve runs unmodified)."""
    try:
        recs = [r for r in active("paper", sleeve, path=path) if paper_supported(r.get("overlay") or {})]
        if not recs or not paper_mode_ok(settings):
            return None
        from .orderflow import entry_filter_mask
        tf = int(sleeve.split("@", 1)[1].rstrip("m"))
        now = time.time() if now is None else float(now)
        data_dir = Path(getattr(settings, "pipeline_data_dir", "data"))
        d = _live_tf_frame(data_dir, symbol, tf, now, Path("logs") / "flow_cache")
        if d is None or not len(d):
            return None
        last = d.iloc[[-1]]
        if int(last["time"].iloc[0]) + tf * 60 != (int(now) // (tf * 60)) * (tf * 60):
            return None  # latest closed bar not built yet: do not guess
        for r in recs:
            flt = r["overlay"]["entry_filter"]
            if not bool(entry_filter_mask(last, flt)[0]):
                return f"order-flow overlay {r['variant']} (paper stage) vetoed entry"
        return None
    except Exception:  # noqa: BLE001 — overlays must never break a sleeve
        return None
