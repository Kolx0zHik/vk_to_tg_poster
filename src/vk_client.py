from __future__ import annotations

import logging
from typing import List

import requests

from .models import Attachment, Post

logger = logging.getLogger("poster.vk")


class VKClient:
    def __init__(self, token: str, api_version: str = "5.199"):
        self.token = token
        self.api_version = api_version
        self.session = requests.Session()

    def fetch_posts(self, owner_id: int, count: int = 10) -> List[Post]:
        params = {
            "owner_id": owner_id,
            "count": count,
            "access_token": self.token,
            "v": self.api_version,
        }
        logger.debug("Requesting VK wall.get for owner_id=%s", owner_id)
        response = self.session.get("https://api.vk.com/method/wall.get", params=params, timeout=15)
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(f"VK API error: {payload['error']}")
        items = payload.get("response", {}).get("items", [])
        posts: List[Post] = []
        for item in items:
            attachments = self._parse_attachments(item.get("attachments", []))
            text = item.get("text", "") or ""
            posts.append(
                Post(
                    id=item["id"],
                    owner_id=item["owner_id"],
                    text=text,
                    attachments=attachments,
                    is_pinned=bool(item.get("is_pinned", 0)),
                )
            )
        return posts

    def _parse_attachments(self, raw_attachments) -> List[Attachment]:
        parsed: List[Attachment] = []
        for att in raw_attachments:
            att_type = att.get("type")
            data = att.get(att_type, {})
            if att_type == "photo":
                sizes = data.get("sizes", [])
                if sizes:
                    # Pick largest resolution
                    sizes = sorted(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0), reverse=True)
                    parsed.append(Attachment(type="photo", url=sizes[0].get("url", "")))
            elif att_type == "video":
                url = data.get("player") or ""
                if not url and data.get("owner_id") and data.get("id"):
                    # Fallback to clickable VK link if direct player URL отсутствует
                    url = f"https://vk.com/video{data.get('owner_id')}_{data.get('id')}"
                    if data.get("access_key"):
                        url += f"?access_key={data.get('access_key')}"
                parsed.append(Attachment(type="video", url=url, title=data.get("title")))
            elif att_type == "audio":
                title = f"{data.get('artist', '')} - {data.get('title', '')}".strip(" -")
                parsed.append(Attachment(type="audio", url=data.get("url", ""), title=title))
            elif att_type == "link":
                parsed.append(Attachment(type="link", url=data.get("url", ""), title=data.get("title")))
        return parsed

    def resolve_screen_name(self, screen_name: str) -> tuple[str, int]:
        params = {
            "screen_name": screen_name,
            "access_token": self.token,
            "v": self.api_version,
        }
        resp = self.session.get("https://api.vk.com/method/utils.resolveScreenName", params=params, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
        if "error" in payload:
            raise RuntimeError(f"VK API error: {payload['error']}")
        resp_obj = payload.get("response") or {}
        object_id = resp_obj.get("object_id")
        object_type = resp_obj.get("type")
        if not object_id or not object_type:
            raise RuntimeError("VK API did not resolve screen name")
        return object_type, int(object_id)
