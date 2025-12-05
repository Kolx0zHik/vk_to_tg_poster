import json
import re
from datetime import date
from pathlib import Path
from typing import Dict, Set


class Cache:
    """
    Deduplication cache:
    - Keeps a single bucket per current date (YYYY-MM-DD), old buckets are dropped on load.
    - Keys are owner_id_post_id strings; legacy hashes are discarded.
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._store: Dict[str, Set[str]] = {}
        self.current_bucket = str(date.today())
        self._load()

    @staticmethod
    def _is_id_key(value: str) -> bool:
        return bool(re.match(r"-?\d+_-?\d+$", value))

    def _load(self) -> None:
        if not self.path.exists():
            self._store = {}
        else:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._store = {k: set(v) for k, v in data.items()}
            except Exception:
                # In case of corrupted cache we start fresh.
                self._store = {}

        # Keep only current date bucket
        self._store = {k: v for k, v in self._store.items() if k == self.current_bucket}
        if self.current_bucket not in self._store:
            self._store[self.current_bucket] = set()

        # drop legacy hashes; keep only id-based keys
        for k, v in list(self._store.items()):
            self._store[k] = {val for val in v if self._is_id_key(str(val))}

    def _persist(self) -> None:
        serializable = {k: list(v) for k, v in self._store.items()}
        self.path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")

    def is_duplicate(self, community_id: int, post_hash: str) -> bool:
        bucket = self.current_bucket
        key = str(community_id)
        return post_hash in self._store.get(bucket, set()) or post_hash in self._store.get(key, set())

    def remember(self, community_id: int, post_hash: str) -> None:
        bucket = self.current_bucket
        key = str(community_id)
        if bucket not in self._store:
            self._store[bucket] = set()
        self._store[bucket].add(post_hash)
        if key not in self._store:
            self._store[key] = set()
        self._store[key].add(post_hash)
        self._persist()
