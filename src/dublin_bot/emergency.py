from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_MANUAL_STOP_FILE = Path("logs/MANUAL_STOP_ACTIVE")


def emergency_stop_active() -> bool:
    """Check if manual emergency stop has been triggered."""
    return _MANUAL_STOP_FILE.exists()


def activate_emergency_stop() -> bool:
    """Activate emergency stop — blocks all order submission."""
    _MANUAL_STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    _MANUAL_STOP_FILE.write_text(
        json.dumps(
            {
                "activated_at": datetime.now(timezone.utc).isoformat(),
                "by": "dashboard",
            }
        ),
        encoding="utf-8",
    )
    return True


def clear_emergency_stop() -> bool:
    """Clear emergency stop — requires restart and safety re-verification."""
    if _MANUAL_STOP_FILE.exists():
        _MANUAL_STOP_FILE.unlink()
    return True
