import json
from pathlib import Path
from typing import Dict, Set


class Cache:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._store: Dict[str, Set[str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._store = {}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._store = {k: set(v) for k, v in data.items()}
        except Exception:
            # In case of corrupted cache we start fresh.
            self._store = {}

    def _persist(self) -> None:
        serializable = {k: list(v) for k, v in self._store.items()}
        self.path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")

    def is_duplicate(self, community_id: int, post_hash: str) -> bool:
        key = str(community_id)
        return post_hash in self._store.get(key, set())

    def remember(self, community_id: int, post_hash: str) -> None:
        key = str(community_id)
        if key not in self._store:
            self._store[key] = set()
        self._store[key].add(post_hash)
        self._persist()
