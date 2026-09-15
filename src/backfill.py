"""Backfill (baseline) requests for communities.

The web UI records what a community should start from; the scheduler turns that
request into a cache baseline on the next run. Requests live in their own file so
the web process never rewrites ``cache.json``, which the scheduler owns.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import Post

MODES = ("none", "posts", "days")
REQUEST_TTL = 30 * 24 * 3600
MAX_BACKFILL_POSTS = 300


def requests_path_for(cache_file: str | Path) -> Path:
    """Backfill request file that sits next to the cache file."""
    return Path(cache_file).with_name("backfill.json")


def normalize_mode(mode: str) -> str:
    value = (mode or "").strip().lower()
    return value if value in MODES else "none"


def compute_baseline(
    posts: List[Post],
    mode: str,
    value: int,
    now: float,
) -> Optional[Tuple[int, int]]:
    """Baseline for the requested backfill scope.

    Everything at or below the baseline counts as already handled, so the posts
    above it (the requested window) get published. ``(0, 0)`` means "all fetched
    posts", ``None`` means there was nothing to look at.
    """
    if not posts:
        return None

    ordered = sorted(posts, key=lambda post: ((post.date or 0), post.id), reverse=True)
    mode = normalize_mode(mode)

    if mode == "none":
        newest = ordered[0]
        return int(newest.date or 0), int(newest.id)

    if mode == "posts":
        if value <= 0 or len(ordered) <= value:
            return 0, 0
        border = ordered[value]
        return int(border.date or 0), int(border.id)

    cutoff = int(now - max(1, int(value or 0)) * 86400)
    for post in ordered:
        if int(post.date or 0) < cutoff:
            return int(post.date or 0), int(post.id)
    return 0, 0


class BackfillRequests:
    """Pending backfill requests keyed by canonical community key or owner id."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _read(self) -> Dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: Dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def get(self, key: str) -> Optional[dict]:
        entry = self._read().get(str(key))
        return entry if isinstance(entry, dict) else None

    def request(self, key: str, mode: str, value: int = 0) -> dict:
        entry = {
            "mode": normalize_mode(mode),
            "value": max(0, int(value or 0)),
            "ts": int(time.time()),
        }
        data = self._read()
        data[str(key)] = entry
        self._purge(data)
        self._write(data)
        return entry

    def pop(self, key: str) -> Optional[dict]:
        data = self._read()
        entry = data.pop(str(key), None)
        if entry is not None:
            self._write(data)
        return entry

    def _purge(self, data: Dict[str, dict]) -> None:
        now = int(time.time())
        stale = [key for key, entry in data.items() if now - int(entry.get("ts", 0) or 0) > REQUEST_TTL]
        for key in stale:
            data.pop(key, None)
