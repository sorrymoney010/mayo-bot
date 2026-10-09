#!/usr/bin/env bash
# Box health loop (stands in for cron, which this box does not have). Every 300 s:
#   1. revive dead supervisors (pure bash, works even with no Python at all)
#   2. verify/rebuild the venv (box_venv.sh; locked) and run box_health.py with it
#   3. optional failover heartbeat (box_heartbeat.sh; inert unless configured)
# Single instance via flock logs/box_health_loop.lock. Clean env (no keys). Never exits.
set -u
source /workspace/mayo-bot/deploy/box/box_env.sh
cd "$REPO" || exit 1
mkdir -p logs
exec 8>>"$REPO/logs/box_health_loop.lock"
flock -n 8 || exit 0
echo $$ > logs/box_health_loop.pid
blog logs/box_health.log "health loop start pid=$$"
while true; do
  ensure_supervisors 8>&-
  if ensure_venv health_loop 8>&-; then
    timeout 1200 env -i "${BASE_ENV[@]}" "$PY" "$REPO/deploy/box/box_health.py" >> logs/box_health.log 2>&1 8>&- </dev/null
  else
    blog logs/box_health.log "HEAL python unavailable (see logs/box_venv_rebuild.log); supervisors revived by bash, retrying in 300s"
  fi
  "$REPO/deploy/box/box_heartbeat.sh" publish >/dev/null 2>&1 8>&- </dev/null || true
  sleep 300 8>&- 9>&-
done
