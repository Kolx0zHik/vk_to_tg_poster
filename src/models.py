from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Attachment:
    type: str
    url: str
    title: Optional[str] = None
    likes: Optional[int] = None
    views: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Attachment":
        return cls(
            type=raw.get("type", ""),
            url=raw.get("url", ""),
            title=raw.get("title"),
            likes=raw.get("likes"),
            views=raw.get("views"),
        )


@dataclass
class Post:
    id: int
    owner_id: int
    text: str
    date: int | None = None
    is_pinned: bool = False
    attachments: List[Attachment] = field(default_factory=list)
    source_owner_id: Optional[int] = None
    source_post_id: Optional[int] = None

    @property
    def vk_link(self) -> str:
        src_owner = self.source_owner_id if self.source_owner_id is not None else self.owner_id
        src_id = self.source_post_id if self.source_post_id is not None else self.id
        return f"https://vk.com/wall{src_owner}_{src_id}"

    @property
    def dedup_key(self) -> str:
        """Stable identity of the original post (repost source when available)."""
        src_owner = self.source_owner_id if self.source_owner_id is not None else self.owner_id
        src_id = self.source_post_id if self.source_post_id is not None else self.id
        return f"{src_owner}_{src_id}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Post":
        return cls(
            id=int(raw["id"]),
            owner_id=int(raw["owner_id"]),
            text=raw.get("text", "") or "",
            date=raw.get("date"),
            is_pinned=bool(raw.get("is_pinned", False)),
            attachments=[Attachment.from_dict(item) for item in raw.get("attachments", []) or []],
            source_owner_id=raw.get("source_owner_id"),
            source_post_id=raw.get("source_post_id"),
        )
