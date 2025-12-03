from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Attachment:
    type: str
    url: str
    title: Optional[str] = None


@dataclass
class Post:
    id: int
    owner_id: int
    text: str
    attachments: List[Attachment] = field(default_factory=list)
