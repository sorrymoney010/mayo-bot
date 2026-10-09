#!/usr/bin/env bash
# Box supervisor for the PUBLIC tick collector (replaces launchd com.mayo.kraken.ticks).
# Kraken public WebSocket v2 + public REST only; started under `env -i` (no keys).
# Verifies/rebuilds the venv before every launch (box_venv.sh) and never gives up
# (capped backoff 30..300s). Adopts a live collector started by any interpreter path.
set -u
source /workspace/mayo-bot/deploy/box/box_env.sh
cd "$REPO" || exit 1
mkdir -p logs data
SLOG="$REPO/logs/box_ticks_supervisor.log"
exec 9>>"$REPO/logs/box_ticks_supervisor.lock"
flock -n 9 || exit 0
echo $$ > logs/box_ticks_supervisor.pid
blog "$SLOG" "supervisor start pid=$$"
trap 'blog "$SLOG" "supervisor exit pid=$$ (child keeps running; next supervisor adopts it)"; exit 0' TERM INT
backoff=30
while true; do
  if [[ -f logs/BOX_TICKS_STOP ]]; then sleep 30 9>&- 8>&-; continue; fi
  existing=$(pgrep -f -- "$TICKS_PROC_RE" | head -1)
  if [[ -n "$existing" ]]; then
    blog "$SLOG" "tick collector already running pid=$existing; adopting"
    while kill -0 "$existing" 2>/dev/null; do sleep 10 9>&- 8>&-; ensure_health_loop; done
    blog "$SLOG" "adopted tick collector exited"; sleep 5 9>&- 8>&-; continue
  fi
  env_file_guard "$SLOG" || { sleep 300 9>&- 8>&-; continue; }
  if ! ensure_venv ticks_supervisor 9>&-; then
    blog "$SLOG" "no working python (see logs/box_venv_rebuild.log); retry in ${backoff}s"
    sleep "$backoff" 9>&- 8>&-; backoff=$(next_backoff "$backoff"); continue
  fi
  started=$(date +%s)
  env -i "${BASE_ENV[@]}" "$PY" "$REPO/scripts/tick_collector.py" --data-dir "$REPO/data" \
      --backfill-hours 6 >> "$REPO/logs/tick_collector.log" 2>&1 9>&- </dev/null &
  child=$!
  echo "$child" > logs/box_ticks.pid
  blog "$SLOG" "started tick_collector.py pid=$child python=$PY"
  watch_child "$child"; rc=$?
  ran=$(( $(date +%s) - started ))
  if (( ran > 600 )); then backoff=30; fi
  blog "$SLOG" "tick_collector.py pid=$child exited rc=$rc after ${ran}s; restart in ${backoff}s"
  sleep "$backoff" 9>&- 8>&-
  backoff=$(next_backoff "$backoff")
done
