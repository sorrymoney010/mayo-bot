from __future__ import annotations

import json
from pathlib import Path

from .models import DecisionRecord


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: DecisionRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")

