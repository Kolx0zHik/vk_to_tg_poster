"""Structured run journal for the web UI.

The scheduler records one compact entry per run: the overall result, per-community
counters and the notable post events (published / skipped / failed). The panel
renders this as a human-readable history instead of a raw log tail.

The file lives next to ``cache.json`` and is owned by the scheduler; ``src.web``
only reads it, the same way it treats ``cache.json``. Every string written to the
journal goes through ``redact_secrets`` so a token in an error message cannot leak
into the file or the API.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from .logger import redact_secrets

JOURNAL_VERSION = 1
# 500 runs cover ~2 days at the default 10-minute cron (144 runs/day) and are
# enough for the "за сутки / за 2 дня" period filters in the panel.
MAX_RUNS = 500
MAX_EVENTS_PER_COMMUNITY = 30


def journal_path_for(cache_file: str | Path) -> Path:
    """Journal file that sits next to the cache file."""
    return Path(cache_file).with_name("runs.json")


def redact_tree(value: Any) -> Any:
    """Recursively redact secrets from a JSON-like structure."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [redact_tree(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_tree(item) for key, item in value.items()}
    return value


class RunJournal:
    """Append-only history of runs, newest first, capped at ``MAX_RUNS``."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _read(self) -> Dict[str, Any]:
        empty = {"meta": {"version": JOURNAL_VERSION}, "runs": []}
        if not self.path.exists():
            return empty
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return empty
        if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
            return empty
        return data

    def record(self, run: Dict[str, Any]) -> None:
        data = self._read()
        existing = [item for item in data.get("runs", []) if isinstance(item, dict)]
        data["meta"] = {"version": JOURNAL_VERSION}
        data["runs"] = [redact_tree(run)] + existing
        data["runs"] = data["runs"][:MAX_RUNS]
        self._write(data)

    def recent(self, limit: int = 10) -> List[Dict[str, Any]]:
        if limit <= 0:
            return []
        runs = [item for item in self._read().get("runs", []) if isinstance(item, dict)]
        return runs[:limit]

    def _write(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)