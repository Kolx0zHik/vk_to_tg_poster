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
    is_pinned: bool = False
    attachments: List[Attachment] = field(default_factory=list)

    @property
    def vk_link(self) -> str:
        return f"https://vk.com/wall{self.owner_id}_{self.id}"
