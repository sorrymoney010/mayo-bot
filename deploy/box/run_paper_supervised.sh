#!/usr/bin/env bash
# Box supervisor for the PAPER loop (replaces launchd com.mayo.kraken.paper).
# * single supervisor: flock on logs/box_paper_supervisor.lock
# * runs scripts/paper_trader_loop.py under `env -i` (no Kraken keys, paper locks forced)
# * before every launch: ensure_venv (verify imports; rebuild the runtime venv offline under a
#   lock if missing/broken; logs/box_venv_rebuild.log)
# * NEVER gives up: any failure (no python, crash, .env guard) -> retry with capped backoff
#   (30s doubling to 300s; reset after 10 min uptime)
# * if a paper loop is already alive (holds logs/paper_trader.lock, e.g. orphaned after this
#   supervisor was killed) it is ADOPTED: we wait for it instead of starting a second one.
# * logs/BOX_STOP present -> do not (re)start the paper loop.
# * optional failover gate (box_heartbeat.sh gate; inert unless heartbeat is configured)
set -u
source /workspace/mayo-bot/deploy/box/box_env.sh
cd "$REPO" || exit 1
mkdir -p logs
SLOG="$REPO/logs/box_paper_supervisor.log"
exec 9>>"$REPO/logs/box_paper_supervisor.lock"
flock -n 9 || exit 0
echo $$ > logs/box_paper_supervisor.pid
blog "$SLOG" "supervisor start pid=$$"
trap 'blog "$SLOG" "supervisor exit pid=$$ (child keeps running; next supervisor adopts it)"; exit 0' TERM INT
paper_lock_held() { ( exec 7>>"$REPO/logs/paper_trader.lock"; flock -n 7 ) 9>&- && return 1 || return 0; }
backoff=30
while true; do
  if [[ -f logs/BOX_STOP || -f logs/BOX_FENCED ]]; then sleep 30 9>&- 8>&-; continue; fi
  if paper_lock_held; then
    blog "$SLOG" "paper loop already running (lock pid $(cat logs/paper_trader.lock 2>/dev/null)); adopting, waiting for it"
    while paper_lock_held; do sleep 10 9>&- 8>&-; ensure_health_loop; done
    blog "$SLOG" "adopted paper loop exited"
    sleep 5 9>&- 8>&-; continue
  fi
  env_file_guard "$SLOG" || { sleep 300 9>&- 8>&-; continue; }
  if ! ensure_venv paper_supervisor 9>&-; then
    blog "$SLOG" "no working python (see logs/box_venv_rebuild.log); retry in ${backoff}s"
    sleep "$backoff" 9>&- 8>&-; backoff=$(next_backoff "$backoff"); continue
  fi
  if ! "$REPO/deploy/box/box_heartbeat.sh" gate >> "$SLOG" 2>&1 9>&- </dev/null; then
    sleep 60 9>&- 8>&-; continue   # failover hand-back in progress (Mac may still own the book)
  fi
  started=$(date +%s)
  env -i "${PAPER_ENV[@]}" "$PY" "$REPO/scripts/paper_trader_loop.py" \
      >> "$REPO/logs/paper_trader.box.out.log" 2>&1 9>&- </dev/null &
  child=$!
  echo "$child" > logs/box_paper.pid
  blog "$SLOG" "started paper_trader_loop.py pid=$child python=$PY"
  watch_child "$child"; rc=$?
  ran=$(( $(date +%s) - started ))
  if (( ran > 600 )); then backoff=30; fi
  blog "$SLOG" "paper_trader_loop.py pid=$child exited rc=$rc after ${ran}s; restart in ${backoff}s"
  sleep "$backoff" 9>&- 8>&-
  backoff=$(next_backoff "$backoff")
done
