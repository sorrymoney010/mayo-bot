#!/bin/bash
# Reboot-safe auto-restart for the two USER-APPROVED CAPIT-24 processes ONLY:
#   paper/capit_binanceus (paper runner)  and  shadow/capit24 (shadow signal loop).
# Never starts the old main paper loop, supervisors, health loop or ensure_box.
# Idempotent: each start.sh refuses to launch a second copy. Skips a process whose dir has
# NO_AUTOSTART (written by its stop.sh). Honors no keys, no .env: everything runs under env -i.
# Usage: scripts/capit_autostart.sh          # check both now (used by the update routine)
#        scripts/capit_autostart.sh --boot   # only once per box boot (used by ~/.profile / ~/.bashrc hook)
R=/workspace/mayo-bot
LOG=$R/logs/capit_autostart.log
BOOT_ID=$(cat /tmp/sand-box-boot-id 2>/dev/null || cat /proc/sys/kernel/random/boot_id 2>/dev/null)
MARK=$HOME/.cache/capit_autostart.boot
exec 8>/tmp/capit_autostart.lock
flock -n 8 || exit 0
if [ "$1" = "--boot" ]; then
  [ -n "$BOOT_ID" ] && [ "$(cat "$MARK" 2>/dev/null)" = "$BOOT_ID" ] && exit 0
  mkdir -p "$(dirname "$MARK")"; echo "$BOOT_ID" > "$MARK"
fi
# CAPIT-24L main-plan runner (paper/capit24l_binanceus) is deliberately NOT in this list yet.
# Add it here ONLY after the owner OKs starting it (docs/MAIN_PLAN_CAPIT24L_BINANCEUS.md).
for d in paper/capit_binanceus shadow/capit24; do
  if [ -f "$R/$d/NO_AUTOSTART" ]; then msg="skip (NO_AUTOSTART)"
  else msg=$(env -i PATH=/usr/bin:/bin HOME=/home/box /bin/bash "$R/$d/start.sh" --auto 2>&1 8>&- | tail -1); fi
  echo "$(date '+%F %T %Z') ${1:-check} $d: $msg" >> "$LOG" 2>/dev/null
  [ -t 1 ] && echo "$d: $msg"
done
exit 0
