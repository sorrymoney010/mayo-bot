#!/usr/bin/env python3
"""One box health pass (every 300 s from deploy/box/health_loop.sh). Linux stand-in for
launchd com.mayo.kraken.watchdog; reuses dublin_bot.watchdog's log/lock/tick parsing,
gap-fill and the daily scorecard. Never edits config, env or the safety locks, never
places orders, needs no keys. Appends one line per pass to logs/box_health.log."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path("/workspace/mayo-bot")
sys.path.insert(0, str(REPO / "src"))
from dublin_bot.scorecard import write_scorecard  # noqa: E402
from dublin_bot.watchdog import (  # noqa: E402
    GAPFILL_EVERY, PAPER_MAX_AGE, TICKS_MAX_AGE, WAKE_GAP, Watchdog, locks_ok,
    paper_log_state, tick_state,
)

LOGS = REPO / "logs"
DATA = REPO / "data"
STATE = LOGS / ".box_health_state.json"
BOX = REPO / "deploy" / "box"
COOLDOWN = 15 * 60
BAD = re.compile(r"lockout|EAPI:Invalid|Invalid key|Invalid nonce|API key|private endpoint|"
                 r"Permission denied|EGeneral:Temporary lockout", re.I)
FORBIDDEN_ENV = ("API_KEY", "API_SECRET", "SECRET", "PRIVATE_KEY", "PASSPHRASE", "TOKEN")  # KRAKEN_TIER is fine


def log(msg: str) -> None:
    line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} {msg}"
    with (LOGS / "box_health.log").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def lock_held(path: Path) -> bool:
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True


def pids(pattern: str) -> list[int]:
    out = subprocess.run(["pgrep", "-f", "--", pattern], capture_output=True, text=True).stdout
    me = os.getpid()
    return [int(p) for p in out.split() if int(p) != me]


def proc_env(pid: int) -> dict:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        return dict(x.decode("utf-8", "replace").split("=", 1) for x in raw if b"=" in x)
    except OSError:
        return {}


def proc_age(pid: int) -> float:
    try:
        out = subprocess.run(["ps", "-o", "etimes=", "-p", str(pid)], capture_output=True,
                             text=True).stdout.strip()
        return float(out)
    except ValueError:
        return 0.0


def start(script: str) -> None:
    subprocess.Popen(["setsid", "nohup", str(BOX / script)], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, close_fds=True)


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def main() -> None:
    now = time.time()
    st = load_state()
    woke = st.get("last_run") is not None and now - float(st["last_run"]) > WAKE_GAP
    problems: list[str] = []
    actions: list[str] = []
    stop = (LOGS / "BOX_STOP").exists()

    # 1. supervisors
    for name, lock, script in (("paper_sup", "box_paper_supervisor.lock", "run_paper_supervised.sh"),
                               ("ticks_sup", "box_ticks_supervisor.lock", "run_ticks_supervised.sh")):
        if not lock_held(LOGS / lock):
            problems.append(f"{name} down")
            start(script)
            actions.append(f"started {script}")
    if actions:
        time.sleep(30)
        now = time.time()

    # 2. paper loop: exactly one, fresh, locks ok, clean env
    pp = pids(f"^[^ ]*/python[^ ]* {REPO}/scripts/paper_trader_loop.py")  # any interpreter path
    pl = paper_log_state(LOGS / "paper_trader.log")
    fresh = max([t for t in (pl["last_cycle"], pl["last_start"]) if t], default=None)
    p_age = None if fresh is None else now - fresh
    lk = locks_ok(pl["locks"])
    if lk is False:
        problems.append("locks OFF")
        log(f"ALERT locks look OFF in latest START line: {pl['locks']}")
    if not stop and len(pp) != 1:
        problems.append(f"paper procs={len(pp)}")
    env_ok = True
    for pid in pp:
        env = proc_env(pid)
        bad = [k for k in env if any(f in k.upper() for f in FORBIDDEN_ENV)]
        want = {"PAPER_TRADING": "true", "DRY_RUN": "true", "ALLOW_LIVE_TRADING": "false",
                "PAPER_USE_LEDGER_EQUITY": "true"}
        wrong = [k for k, v in want.items() if env.get(k) != v]
        if bad or wrong:
            env_ok = False
            problems.append(f"paper pid {pid} env bad={bad} wrong={wrong}")
            log(f"ALERT paper pid {pid} env forbidden={bad} wrong_locks={wrong}")
    if (len(pp) == 1 and (p_age is None or p_age > PAPER_MAX_AGE) and proc_age(pp[0]) > PAPER_MAX_AGE
            and now - float(st.get("act_paper", 0)) > COOLDOWN):
        st["act_paper"] = now
        os.kill(pp[0], 15)
        actions.append(f"paper stale ({p_age}); TERM pid {pp[0]} (supervisor restarts it)")

    # 3. private-endpoint / key / lockout errors in new paper log lines
    plog = LOGS / "paper_trader.log"
    off = int(st.get("plog_off", 0))
    try:
        size = plog.stat().st_size
        if size < off:
            off = 0
        with plog.open("rb") as fh:
            fh.seek(off)
            new = fh.read().decode("utf-8", "replace").splitlines()
        st["plog_off"] = size
    except OSError:
        new = []
    errs = [ln for ln in new if BAD.search(ln)]
    n_err = sum(1 for ln in new if " ERROR " in ln)
    if errs:
        problems.append(f"key/private/lockout lines={len(errs)}")
        log(f"ALERT {len(errs)} key/private/lockout lines, first: {errs[0][:300]}")

    # 4. ticks
    tp = pids(f"^[^ ]*/python[^ ]* {REPO}/scripts/tick_collector.py --data-dir {DATA}")
    ts = tick_state(DATA)
    t_age = None if ts["heartbeat"] is None else now - ts["heartbeat"]
    t_fresh = t_age is not None and t_age < TICKS_MAX_AGE
    if len(tp) != 1:
        problems.append(f"tick procs={len(tp)}")
    if not t_fresh:
        problems.append(f"ticks stale ({t_age})")
        if (len(tp) == 1 and proc_age(tp[0]) > 300
                and now - float(st.get("act_ticks", 0)) > COOLDOWN):
            st["act_ticks"] = now
            os.kill(tp[0], 15)
            actions.append(f"TERM stale tick collector {tp[0]}")

    # 5. gap fill + daily scorecard (repo watchdog logic)
    wd = Watchdog(repo=REPO, data_dir=DATA, python=sys.executable)
    if ((woke or not t_fresh or ts["gaps_open"] > 0)
            and now - float(st.get("last_gapfill", 0)) >= GAPFILL_EVERY):
        st["last_gapfill"] = now
        actions.append(wd.gap_fill())
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    if st.get("scorecard_day") != today:
        try:
            write_scorecard(LOGS)
            st["scorecard_day"] = today
            actions.append("scorecard written")
        except Exception as e:  # noqa: BLE001
            actions.append(f"scorecard ERROR {type(e).__name__}: {e}")

    st["last_run"] = now
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(STATE)
    log(("HEALTHY" if not problems else "HEAL")
        + f" paper=pids:{pp},cycle_age:{'-' if p_age is None else int(p_age)}s,"
        f"locks:{'ok' if lk else 'unknown' if lk is None else 'OFF'},env:{'ok' if env_ok else 'BAD'},"
        f"new_ERROR_lines:{n_err} ticks=pids:{tp},hb_age:{'-' if t_age is None else int(t_age)}s,"
        f"gaps_open:{ts['gaps_open']} box_stop={stop} woke={woke}"
        + (f" problems={problems}" if problems else "")
        + (f" actions={actions}" if actions else ""))


if __name__ == "__main__":
    main()
