#!/usr/bin/env python3
"""Watchdog: alert on the Dublin bot's FIRST real live entry (BUY) fill.

Pure stdlib. Scans logs/audit.jsonl for new ``order_submitted`` BUY events.
Dry-run never reaches ``order_submitted``, so any such event is a real order.
Alerts ONCE, then stays silent forever. State (byte offset + alerted flag) is
kept in logs/.watchdog_first_fill.json so a watchdog restart doesn't re-alert
and the file offset advances correctly across ticks.

Run from anywhere (uses __file__ to locate the repo). Designed for cron with
no_agent=True: emits output ONLY when the first fill is detected; otherwise
prints nothing (empty stdout = no delivery).
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AUDIT = REPO / "logs" / "audit.jsonl"
STATE = REPO / "logs" / ".watchdog_first_fill.json"


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"offset": 0, "alerted": False}


def save_state(s: dict) -> None:
    STATE.write_text(json.dumps(s), encoding="utf-8")


def main() -> None:
    st = load_state()
    if st.get("alerted"):
        return  # already alerted once; stay silent forever
    if not AUDIT.exists():
        return

    offset = int(st.get("offset", 0))
    found = None
    with AUDIT.open("r", encoding="utf-8") as f:
        f.seek(offset)
        buf = ""
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                new_offset = AUDIT.stat().st_size - len(buf.encode("utf-8"))
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("event") != "order_submitted":
                    continue
                p = rec.get("payload", {})
                if str(p.get("side") or "").lower() != "buy":
                    continue
                found = rec
                st["offset"] = new_offset
                break
            if found:
                break
        # advance past any trailing partial line not yet newline-terminated
        if not found:
            tail = (buf + f.read()).rstrip("\n")
            st["offset"] = AUDIT.stat().st_size - len(tail.encode("utf-8")) if tail else AUDIT.stat().st_size

    if found:
        st["alerted"] = True
        save_state(st)
        p = found.get("payload", {})
        ts = found.get("timestamp", "unknown")
        pair = p.get("pair", "?")
        vol = p.get("volume", "?")
        oid = p.get("order_id", "?")
        print(
            "FIRST LIVE FILL on the Dublin bot\n"
            f"  time : {ts}\n"
            f"  pair : {pair}\n"
            f"  side : BUY (entry)\n"
            f"  vol  : {vol}\n"
            f"  id   : {oid}\n"
            "A Kraken bracket stop-loss is attached to this position.\n"
            "(This is the bot trading your own account, not an outbound agent message.)"
        )
        return
    save_state(st)


if __name__ == "__main__":
    main()
