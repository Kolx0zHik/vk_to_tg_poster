from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Attachment:
    type: str
    url: str
    title: Optional[str] = None
    likes: Optional[int] = None
    views: Optional[int] = None


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
