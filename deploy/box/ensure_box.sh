#!/usr/bin/env bash
# Idempotent, non-blocking: make sure the mayo box health loop AND both supervisors are running.
# Called from ~/.bashrc and ~/.profile so the first shell after a box restart revives them,
# and safe to run by hand at any time. Never starts a second instance (flock everywhere; the
# paper loop holds its own lock). Needs no Python: the supervisors verify/rebuild the venv
# themselves (box_venv.sh, locked, logs/box_venv_rebuild.log), so a missing/broken .venv or a
# replaced system Python no longer leaves the bot dead.
#   ensure_box.sh          -> start whatever is down, return immediately
#   ensure_box.sh --status -> also print the PIDs and venv state
source /workspace/mayo-bot/deploy/box/box_env.sh
mkdir -p "$REPO/logs"
ensure_health_loop
ensure_supervisors
if [[ "${1:-}" == "--status" ]]; then
  sleep 2
  for f in box_health_loop box_paper_supervisor box_ticks_supervisor; do
    p=$(cat "$REPO/logs/$f.pid" 2>/dev/null); lock_free "$REPO/logs/$f.lock" && s=DOWN || s=up
    echo "$f: $s pid=${p:-?}"
  done
  echo "paper loop pids: $(pgrep -f -- "$PAPER_PROC_RE" | tr '\n' ' ')"
  echo "tick collector pids: $(pgrep -f -- "$TICKS_PROC_RE" | tr '\n' ' ')"
  venv_ok "$RUNTIME_PY" && echo "runtime venv: ok ($RUNTIME_PY)" || echo "runtime venv: BROKEN/missing (supervisors will rebuild)"
fi
exit 0
