# mayo-bot PAPER loop on the box (box = paper-ledger owner since 2026-10-03)

The Mac's com.mayo.kraken.paper + .watchdog are booted out AND `launchctl disable`d (cold
standby). Mac ticks collector still runs. Optional automatic failover: ../mac-failover/.

Everything starts under `env -i` (no Kraken keys; PAPER_TRADING=true DRY_RUN=true
ALLOW_LIVE_TRADING=false PAPER_USE_LEDGER_EQUITY=true MAYO_LEDGER_OWNER=1; see box_env.sh).
A repo `.env` makes every supervisor refuse to start.

## Pieces
- `run_paper_supervised.sh` -> scripts/paper_trader_loop.py; `run_ticks_supervised.sh` ->
  scripts/tick_collector.py (public WS/REST only). Both: single instance (flock), adopt a live
  orphan, verify/rebuild the venv before every launch, restart with capped backoff 30..300 s,
  never give up.
- `health_loop.sh` -> every 300 s: revive supervisors in pure bash (works with no Python),
  ensure the venv, run `box_health.py` (exactly-one paper proc, START locks, clean child env,
  key/lockout lines, freshness, gap-fill, daily scorecard), optional heartbeat. logs/box_health.log
- `ensure_box.sh [--status]` -> non-blocking, idempotent: start health loop + supervisors if
  down. Called from `~/.bashrc` and `~/.profile` (first shell after a box restart).
- `box_venv.sh check|ensure|bootstrap` -> self-healing Python runtime (below).
- `box_heartbeat.sh publish|gate|status` -> optional Mac failover heartbeat; inert unless
  `/workspace/mayo-runtime/heartbeat.conf` exists.

## Python runtime (why it moved out of `.venv`)
Root cause of the 2026-10-04..09 outage: when the box moves to a fresh instance, its file
persistence restores /workspace minus a default ignore list that includes `.venv/`, `venv/`,
`__pycache__/`, `.cache/`, `build/`, `dist/`. The repo `.venv` disappeared, the health loop
could only print `env: .../.venv/bin/python: No such file or directory`, and nothing rebuilt it.

Now the bot runs from `/workspace/mayo-runtime` (names chosen to survive persistence):
`bin/uv` (pinned copy), `python/cpython-3.13.*` (uv standalone CPython, independent of
/usr/bin/python3), `archive/*.tar.gz` (pristine copy of that interpreter), `wheelhouse/`
(every wheel from `requirements.box.lock`), `pyenv/` (the venv). `ensure_venv` imports every
module the loop needs; if that fails it takes `mayo-runtime/.rebuild.lock` and rebuilds
offline (restore interpreter from tarball if needed -> `uv venv` -> `uv pip install --no-index`
from the wheelhouse; falls back to PyPI, then to system python3 + stdlib venv/pip, then to the
legacy repo `.venv`). ~2-4 s offline. Log: logs/box_venv_rebuild.log.
Refresh after changing dependencies: update requirements.box.lock, then
`deploy/box/box_venv.sh bootstrap` (needs network).

## What restarts things (no cron/init/systemd on this box)
Within one box instance the health loop + supervisors restart each other and the children
forever. After a box restart or move, NOTHING on the box runs user code until a shell opens
(an agent's Shell call or a terminal on the box desktop sources ~/.bashrc -> ensure_box.sh).
The box offers no documented boot or periodic hook. Gaps between a restart and the next
shell are what ../mac-failover/ covers.

## Operate
Status: `deploy/box/ensure_box.sh --status`
Stop box paper (stays stopped): `touch logs/BOX_STOP; kill $(cat logs/paper_trader.lock)`
Resume: `rm logs/BOX_STOP` (supervisor restarts it within 30 s)

Bring the Mac back as owner by hand (only AFTER box paper is stopped and no box paper proc
remains; optionally copy box logs/ state back to ~/mayo-bot/logs first so the book continues):
  U=$(id -u)
  launchctl enable gui/$U/com.mayo.kraken.paper
  launchctl enable gui/$U/com.mayo.kraken.watchdog
  launchctl bootstrap gui/$U ~/Library/LaunchAgents/com.mayo.kraken.paper.plist
  launchctl bootstrap gui/$U ~/Library/LaunchAgents/com.mayo.kraken.watchdog.plist
