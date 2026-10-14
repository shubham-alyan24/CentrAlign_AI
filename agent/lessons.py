"""Long-term memory across runs: short, general lessons distilled from errors (e.g. a field's format).
Loaded into every future run's prompt so the operator does not repeat the same mistakes."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class LessonStore:
    def __init__(self, path: Path, limit: int = 20):
        self.path = Path(path)
        self.limit = limit

    def _read(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def load(self) -> list[str]:
        return [x["lesson"] for x in self._read()][-self.limit:]

    def add(self, lessons: list[str], run_id: str) -> list[str]:
        data = self._read()
        known = {x["lesson"].strip().lower() for x in data}
        added = []
        for l in lessons:
            l = " ".join(l.split())
            if l and l.lower() not in known:
                data.append({"lesson": l, "run_id": run_id, "learned_at": datetime.now().isoformat(timespec="seconds")})
                known.add(l.lower())
                added.append(l)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data[-self.limit:], indent=2), encoding="utf-8")
        return added
