import json
import time
from pathlib import Path
from typing import Dict, List, Optional


class Cache:
    """
    Состояние кеша:
    - dedup: список объектов {hash, ts} с очисткой старше TTL (сутки)
    - last_seen: по каждому owner_id храним последнюю дату/ид поста
    """

    DEDUP_TTL = 24 * 3600

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._store: Dict = {"dedup": [], "last_seen": {}}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._store = {"dedup": [], "last_seen": {}}
        else:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._store = {
                    "dedup": data.get("dedup", []),
                    "last_seen": data.get("last_seen", {}),
                }
            except Exception:
                self._store = {"dedup": [], "last_seen": {}}
        self._purge()

    def _persist(self) -> None:
        self.path.write_text(json.dumps(self._store, ensure_ascii=False, indent=2), encoding="utf-8")

    def _purge(self) -> None:
        now_ts = int(time.time())
        ttl = now_ts - self.DEDUP_TTL
        dedup: List[Dict] = self._store.get("dedup", [])
        dedup = [item for item in dedup if item.get("ts", 0) >= ttl]
        self._store["dedup"] = dedup

    def is_duplicate(self, post_hash: str) -> bool:
        self._purge()
        return any(item.get("hash") == post_hash for item in self._store.get("dedup", []))

    def remember(self, community_id: int, post_hash: str, post_ts: Optional[int] = None) -> None:
        self._purge()
        ts = post_ts or int(time.time())
        self._store.setdefault("dedup", []).append({"hash": post_hash, "ts": ts})
        self._persist()

    def update_last_seen(self, community_id: int, post_id: int, post_ts: Optional[int]) -> None:
        ts = post_ts or int(time.time())
        self._store.setdefault("last_seen", {})[str(community_id)] = {"ts": ts, "post_id": post_id}
        self._persist()

    def get_last_seen(self, community_id: int) -> tuple[Optional[int], Optional[int]]:
        entry = self._store.get("last_seen", {}).get(str(community_id), {})
        return entry.get("ts"), entry.get("post_id")
