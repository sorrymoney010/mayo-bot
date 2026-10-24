"""Self-healing watchdog for the Mac launchd jobs (paper only).

Every run (launchd ``com.mayo.kraken.watchdog``, StartInterval 300 s):

1. ``com.mayo.kraken.paper`` — must be loaded in ``gui/<uid>`` and fresh: the
   newest ``CYCLE``/``START`` line in ``logs/paper_trader.log`` < 15 min old.
2. ``com.mayo.kraken.ticks`` — must be loaded and fresh: newest collector
   heartbeat (``updated_at`` in ``data/ticks/*/_status.json``) < 2 min old.
3. Not loaded  -> ``launchctl bootstrap gui/<uid> ~/Library/LaunchAgents/<label>.plist``
   (fixes the "job silently unloaded" failure). Loaded but stale ->
   ``launchctl kickstart -k``. Per-job cooldown (15 min) so a job that is
   starting up is never restarted in a loop.
4. After a sleep/wake gap (no watchdog run for > 15 min), when the tick feed
   was stale, or when the collector reports open id gaps: REST gap-fill of
   the last 48 h (``pipeline_backfill.py --fill-gaps --since-hours 48
   --no-compact``), at most every 30 min.
5. Once per UTC day: ``logs/scorecard.json``.

It never edits configuration, environment or plists and never touches the
safety locks; it only reads the paper loop's START line and logs an ALERT if
the locks ever look off. All lines go to ``logs/watchdog.log``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

PAPER = "com.mayo.kraken.paper"
TICKS = "com.mayo.kraken.ticks"
PAPER_MAX_AGE = 5 * 60   # paper loop cycles every 60 s (intraday 15m/30m/1h sleeves)
TICKS_MAX_AGE = 2 * 60
COOLDOWN = 15 * 60
WAKE_GAP = 15 * 60
GAPFILL_EVERY = 30 * 60
GAPFILL_TIMEOUT = 15 * 60
LOG_MAX_BYTES = 5_000_000

_TS = re.compile(r"^\[(\d{4}-\d\d-\d\dT[0-9:.]+(?:[+-]\d\d:\d\d|Z)?)\]\s+(\S+)")

Runner = Callable[[list[str], float], tuple[int, str]]


def default_runner(cmd: list[str], timeout: float) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as e:
        return 127, str(e)


def _tail_lines(path: Path, nbytes: int = 256_000) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - nbytes))
            return fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def _parse_ts(s: str) -> float | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def paper_log_state(log_path: Path) -> dict:
    """Newest CYCLE/START timestamps and the lock flags from the newest START."""
    last_cycle = last_start = None
    start_line = ""
    for line in _tail_lines(log_path):
        m = _TS.match(line)
        if not m:
            continue
        ts = _parse_ts(m.group(1))
        if ts is None:
            continue
        if m.group(2) == "CYCLE":
            last_cycle = ts
        elif m.group(2) == "START":
            last_start, start_line = ts, line
    locks = {k: v for k, v in re.findall(r"\b(locked|paper|dry_run|allow_live)=(\w+)", start_line)}
    return {"last_cycle": last_cycle, "last_start": last_start, "locks": locks}


def locks_ok(locks: dict) -> bool | None:
    if not locks:
        return None
    return (locks.get("locked") == "True" and locks.get("paper") == "True"
            and locks.get("dry_run") == "True" and locks.get("allow_live") == "False")


def tick_state(data_dir: Path) -> dict:
    hb, gaps_open, syms = None, 0, 0
    for p in sorted((data_dir / "ticks").glob("*/_status.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        syms += 1
        u = d.get("updated_at")
        if isinstance(u, (int, float)):
            hb = u if hb is None else max(hb, u)
        gaps_open += int(d.get("gaps_open") or 0)
    return {"heartbeat": hb, "gaps_open": gaps_open, "symbols": syms}


def parse_launchctl_print(rc: int, out: str) -> dict:
    if rc != 0:
        return {"loaded": False, "pid": None, "state": None}
    pid = re.search(r"^\s*pid = (\d+)", out, re.M)
    st = re.search(r"^\s*state = (\S+)", out, re.M)
    return {"loaded": True, "pid": int(pid.group(1)) if pid else None,
            "state": st.group(1) if st else None}


@dataclass
class Watchdog:
    repo: Path
    data_dir: Path
    uid: int = field(default_factory=os.getuid)
    agents_dir: Path = field(default_factory=lambda: Path.home() / "Library" / "LaunchAgents")
    python: str = sys.executable
    run: Runner = default_runner
    now: Callable[[], float] = time.time
    scorecard: Callable[[Path], object] | None = None

    @property
    def logs(self) -> Path:
        return self.repo / "logs"

    @property
    def state_path(self) -> Path:
        return self.logs / ".watchdog_state.json"

    def log(self, msg: str) -> None:
        self.logs.mkdir(parents=True, exist_ok=True)
        p = self.logs / "watchdog.log"
        try:
            if p.stat().st_size > LOG_MAX_BYTES:
                p.replace(p.with_suffix(".log.1"))
        except OSError:
            pass
        line = f"[{datetime.fromtimestamp(self.now()).astimezone().isoformat(timespec='seconds')}] {msg}"
        with p.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


    def load_state(self) -> dict:
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_state(self, st: dict) -> None:
        self.logs.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st), encoding="utf-8")
        tmp.replace(self.state_path)

    # ── launchd ────────────────────────────────────────────────
    def job(self, label: str) -> dict:
        return parse_launchctl_print(*self.run(["launchctl", "print", f"gui/{self.uid}/{label}"], 20))

    def heal(self, label: str, loaded: bool, st: dict, why: str) -> str:
        last = float(st.get("actions", {}).get(label, 0))
        if self.now() - last < COOLDOWN:
            return f"{label}: {why}; cooldown ({int(self.now() - last)}s since last action)"
        st.setdefault("actions", {})[label] = self.now()
        if not loaded:
            plist = self.agents_dir / f"{label}.plist"
            if not plist.exists():
                return f"{label}: {why}; ERROR plist missing at {plist}"
            rc, out = self.run(["launchctl", "bootstrap", f"gui/{self.uid}", str(plist)], 30)
            return f"{label}: {why}; bootstrap rc={rc} {out.strip()[:200]}"
        rc, out = self.run(["launchctl", "kickstart", "-k", f"gui/{self.uid}/{label}"], 30)
        return f"{label}: {why}; kickstart rc={rc} {out.strip()[:200]}"

    def gap_fill(self) -> str:
        cmd = [self.python, str(self.repo / "scripts" / "pipeline_backfill.py"), "--fill-gaps",
               "--since-hours", "48", "--no-compact", "--data-dir", str(self.data_dir)]
        rc, out = self.run(cmd, GAPFILL_TIMEOUT)
        res = [ln for ln in out.splitlines() if "'gaps'" in ln]
        return f"gapfill rc={rc} " + " | ".join(r.split("] ", 1)[-1] for r in res)[:600]

    # ── one pass ───────────────────────────────────────────────
    def check(self) -> dict:
        now = self.now()
        st = self.load_state()
        prev_run = st.get("last_run")
        woke = prev_run is not None and now - float(prev_run) > WAKE_GAP
        actions: list[str] = []
        problems: list[str] = []

        pj = self.job(PAPER)
        pl = paper_log_state(self.logs / "paper_trader.log")
        fresh_ts = max([t for t in (pl["last_cycle"], pl["last_start"]) if t is not None], default=None)
        p_age = None if fresh_ts is None else now - fresh_ts
        p_fresh = p_age is not None and p_age < PAPER_MAX_AGE
        if not pj["loaded"] or pj["pid"] is None or not p_fresh:
            why = ("not loaded" if not pj["loaded"] else "no pid" if pj["pid"] is None
                   else f"stale ({'none' if p_age is None else int(p_age)}s since CYCLE)")
            problems.append(f"paper {why}")
            actions.append(self.heal(PAPER, pj["loaded"], st, why))
        lk = locks_ok(pl["locks"])
        if lk is False:
            problems.append("locks")
            self.log(f"ALERT locks look OFF in latest START line: {pl['locks']} (watchdog never changes locks)")

        tj = self.job(TICKS)
        ts = tick_state(self.data_dir)
        t_age = None if ts["heartbeat"] is None else now - ts["heartbeat"]
        t_fresh = t_age is not None and t_age < TICKS_MAX_AGE
        if not tj["loaded"] or tj["pid"] is None or not t_fresh:
            why = ("not loaded" if not tj["loaded"] else "no pid" if tj["pid"] is None
                   else f"stale ({'none' if t_age is None else int(t_age)}s since heartbeat)")
            problems.append(f"ticks {why}")
            actions.append(self.heal(TICKS, tj["loaded"], st, why))

        need_fill = woke or not t_fresh or ts["gaps_open"] > 0
        if need_fill and now - float(st.get("last_gapfill", 0)) >= GAPFILL_EVERY:
            st["last_gapfill"] = now
            reason = "wake" if woke else "ticks stale" if not t_fresh else f"gaps_open={ts['gaps_open']}"
            actions.append(f"{self.gap_fill()} (reason={reason})")

        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        if self.scorecard is not None and (st.get("scorecard_day") != today
                                           or not (self.logs / "scorecard.json").exists()):
            try:
                self.scorecard(self.logs)
                st["scorecard_day"] = today
                actions.append("scorecard written")
            except Exception as e:  # never let reporting break healing
                actions.append(f"scorecard ERROR {type(e).__name__}: {e}")

        st["last_run"] = now
        self.save_state(st)
        status = "HEALTHY" if not problems else "HEAL"
        self.log(
            f"{status} paper=pid:{pj['pid']},cycle_age:{'-' if p_age is None else int(p_age)}s,"
            f"locks:{'ok' if lk else 'unknown' if lk is None else 'OFF'} "
            f"ticks=pid:{tj['pid']},hb_age:{'-' if t_age is None else int(t_age)}s,"
            f"gaps_open:{ts['gaps_open']} woke={woke}"
            + (f" problems={problems}" if problems else "")
            + (f" actions={actions}" if actions else ""))
        return {"status": status, "problems": problems, "actions": actions, "woke": woke,
                "paper_age": p_age, "ticks_age": t_age, "locks_ok": lk}
