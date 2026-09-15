import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import Post


class Cache:
    """Durable state for the posting pipeline.

    Post accounting is keyed by ``Post.dedup_key`` and each record has a status:

    - ``pending``   -- fetched and waiting to be published (also failed posts awaiting retry)
    - ``published`` -- successfully sent to Telegram
    - ``skipped``   -- intentionally not sent (blocked keyword, disallowed type, below baseline)
    - ``dead``      -- failed after ``PENDING_MAX_ATTEMPTS`` attempts, not retried anymore

    A per-community ``baseline`` marks everything at or below it as already handled;
    it is used when migrating the legacy ``dedup``/``last_seen`` cache format so that
    previously published posts are not sent again.
    """

    SCHEMA_VERSION = 2
    OWNER_ID_TTL = 30 * 24 * 3600
    PENDING_MAX_ATTEMPTS = 5

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._store: Dict = self._empty()
        self._dirty = False
        self._load()

    # ------------------------------------------------------------------ storage

    def _empty(self) -> Dict:
        return {
            "meta": {"version": self.SCHEMA_VERSION},
            "posts": {},
            "communities": {},
            "owner_ids": {},
        }

    def _load(self) -> None:
        if not self.path.exists():
            self._store = self._empty()
            return
        try:
            raw_text = self.path.read_text(encoding="utf-8")
            data = json.loads(raw_text)
        except Exception:
            self._store = self._empty()
            return

        if isinstance(data, dict) and "posts" in data:
            self._store = {
                "meta": data.get("meta", {"version": self.SCHEMA_VERSION}),
                "posts": data.get("posts", {}) or {},
                "communities": data.get("communities", {}) or {},
                "owner_ids": data.get("owner_ids", {}) or {},
            }
        else:
            self._store = self._migrate_legacy(data, raw_text)
            self._persist()

        self._purge_owner_ids()

    def _migrate_legacy(self, data: Dict, raw_text: str) -> Dict:
        """Convert the legacy ``dedup``/``last_seen`` state without re-sending posts."""
        store = self._empty()
        try:
            backup = self.path.with_name(self.path.name + ".v1.bak")
            if not backup.exists():
                backup.write_text(raw_text, encoding="utf-8")
        except OSError:
            pass

        now = int(time.time())
        for item in data.get("dedup", []) or []:
            key = item.get("hash")
            if not key:
                continue
            owner_id, post_id = self._split_key(str(key))
            store["posts"][key] = {
                "status": "published",
                "owner_id": owner_id,
                "post_id": post_id,
                "date": 0,
                "attempts": 0,
                "ts": int(item.get("ts", now) or now),
            }

        for owner, entry in (data.get("last_seen", {}) or {}).items():
            store["communities"][str(owner)] = {
                "baseline_date": int(entry.get("ts", 0) or 0),
                "baseline_post_id": int(entry.get("post_id", 0) or 0),
            }

        store["owner_ids"] = data.get("owner_ids", {}) or {}
        return store

    @staticmethod
    def _split_key(key: str) -> Tuple[Optional[int], Optional[int]]:
        if "_" not in key:
            return None, None
        owner, post_id = key.rsplit("_", 1)
        try:
            return int(owner), int(post_id)
        except ValueError:
            return None, None

    def _persist(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(self._store, ensure_ascii=False, indent=2)
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        self._dirty = False

    def flush(self) -> None:
        if self._dirty:
            self._persist()

    def _purge_owner_ids(self) -> None:
        now = int(time.time())
        owner_ids: Dict = self._store.get("owner_ids", {})
        fresh = {
            key: value
            for key, value in owner_ids.items()
            if now - int(value.get("ts", 0) or 0) <= self.OWNER_ID_TTL
        }
        if len(fresh) != len(owner_ids):
            self._store["owner_ids"] = fresh
            self._persist()

    # ----------------------------------------------------------------- owner ids

    def get_owner_id(self, key: str) -> Optional[int]:
        entry = self._store.get("owner_ids", {}).get(key)
        if not entry:
            return None
        return entry.get("owner_id")

    def set_owner_id(self, key: str, owner_id: int, persist: bool = True) -> None:
        self._store.setdefault("owner_ids", {})[key] = {
            "owner_id": owner_id,
            "ts": int(time.time()),
        }
        self._dirty = True
        if persist:
            self._persist()

    # ------------------------------------------------------------- post records

    def _baseline(self, owner_id: int) -> Optional[Tuple[int, int]]:
        entry = self._store.get("communities", {}).get(str(owner_id))
        if not entry:
            return None
        return int(entry.get("baseline_date", 0) or 0), int(entry.get("baseline_post_id", 0) or 0)

    def is_known(self, owner_id: int, post: Post) -> bool:
        if post.dedup_key in self._store.get("posts", {}):
            return True
        baseline = self._baseline(owner_id)
        if baseline is None or post.date is None:
            return False
        return (post.date, post.id) <= baseline

    def record_post(self, owner_id: int, post: Post) -> str:
        """Persist a fetched post. Returns ``known``, ``baseline`` or ``new``."""
        key = post.dedup_key
        posts = self._store.setdefault("posts", {})
        if key in posts:
            return "known"

        baseline = self._baseline(owner_id)
        now = int(time.time())
        record = {
            "status": "pending",
            "owner_id": owner_id,
            "post_id": post.id,
            "date": post.date or 0,
            "attempts": 0,
            "ts": now,
        }
        result = "new"
        if baseline is not None and post.date is not None and (post.date, post.id) <= baseline:
            record["status"] = "skipped"
            result = "baseline"
        else:
            record["payload"] = post.to_dict()
        posts[key] = record
        self._dirty = True
        self._persist()
        return result

    def pending_posts(self, owner_id: int, limit: Optional[int] = None) -> List[Tuple[str, Post]]:
        entries = [
            (key, record)
            for key, record in self._store.get("posts", {}).items()
            if record.get("status") == "pending" and int(record.get("owner_id", 0)) == owner_id
        ]
        entries.sort(key=lambda item: (item[1].get("date", 0), item[1].get("post_id", 0)))
        if limit is not None:
            entries = entries[:limit]

        result: List[Tuple[str, Post]] = []
        for key, record in entries:
            payload = record.get("payload")
            if payload:
                result.append((key, Post.from_dict(payload)))
        return result

    def mark_published(self, key: str) -> None:
        self._update_status(key, "published")

    def mark_skipped(self, key: str) -> None:
        self._update_status(key, "skipped")

    def mark_failed(self, key: str) -> str:
        """Record a failed publish. Returns the resulting status."""
        record = self._store.get("posts", {}).get(key)
        if not record:
            return "unknown"
        record["attempts"] = int(record.get("attempts", 0)) + 1
        record["ts"] = int(time.time())
        if record["attempts"] >= self.PENDING_MAX_ATTEMPTS:
            record["status"] = "dead"
            record.pop("payload", None)
            status = "dead"
        else:
            status = "pending"
        self._dirty = True
        self._persist()
        return status

    def _update_status(self, key: str, status: str) -> None:
        record = self._store.get("posts", {}).get(key)
        if not record:
            return
        record["status"] = status
        record["ts"] = int(time.time())
        record.pop("payload", None)
        self._dirty = True
        self._persist()
