from __future__ import annotations

import logging
import time
from typing import List

import requests

from .models import Attachment, Post

logger = logging.getLogger("poster.vk")

# Keep consecutive calls under VK's per-second limit for user tokens (~3 req/s).
VK_REQUEST_INTERVAL = 0.34
VK_MAX_RETRIES = 2
VK_BACKOFF_BASE = 1.0
VK_RETRYABLE_ERROR_CODES = {6, 9, 10}


class VKClient:
    def __init__(self, token: str, api_version: str = "5.199"):
        self.token = token
        self.api_version = api_version
        self.session = requests.Session()
        self._last_request_ts: float | None = None

    def _throttle(self) -> None:
        if VK_REQUEST_INTERVAL <= 0:
            return
        now = time.monotonic()
        if self._last_request_ts is not None:
            wait = VK_REQUEST_INTERVAL - (now - self._last_request_ts)
            if wait > 0:
                time.sleep(wait)
        self._last_request_ts = time.monotonic()

    def _request(
        self, method: str, params: dict, timeout: int, max_retries: int = VK_MAX_RETRIES
    ) -> dict:
        """Call VK API without leaking the token-bearing URL, throttling and retrying
        transient rate-limit (error_code 6/9/10) and network failures."""
        url = f"https://api.vk.com/method/{method}"
        last_error: RuntimeError | None = None
        for attempt in range(max_retries + 1):
            self._throttle()
            try:
                response = self.session.get(url, params=params, timeout=timeout)
            except requests.RequestException as exc:
                last_error = RuntimeError(f"VK request failed ({method}): {type(exc).__name__}")
            else:
                try:
                    response.raise_for_status()
                except requests.HTTPError:
                    raise RuntimeError(f"VK HTTP error ({method}): {response.status_code}") from None
                try:
                    payload = response.json()
                except ValueError:
                    raise RuntimeError(f"VK returned invalid JSON ({method})") from None
                error = payload.get("error") if isinstance(payload, dict) else None
                if not error:
                    return payload
                code = error.get("error_code") if isinstance(error, dict) else None
                if code not in VK_RETRYABLE_ERROR_CODES:
                    raise RuntimeError(f"VK API error: {error}")
                last_error = RuntimeError(f"VK API error: {error}")
                logger.debug("VK %s вернул error_code=%s, повтор", method, code)
            if attempt < max_retries:
                time.sleep(VK_BACKOFF_BASE * (2 ** attempt))
        raise last_error or RuntimeError(f"VK request failed ({method})")

    def fetch_posts(self, owner_id: int, count: int = 10, offset: int = 0) -> List[Post]:
        params = {
            "owner_id": owner_id,
            "count": count,
            "offset": offset,
            "access_token": self.token,
            "v": self.api_version,
        }
        logger.debug("Запрос VK wall.get для owner_id=%s", owner_id)
        payload = self._request("wall.get", params, timeout=15)
        items = payload.get("response", {}).get("items", [])
        posts: List[Post] = []
        for item in items:
            attachments = self._parse_attachments(item.get("attachments", []))
            text = item.get("text", "") or ""
            source_owner_id = None
            source_post_id = None

            copy_history = item.get("copy_history") or []
            if copy_history:
                original = copy_history[0]
                orig_text = original.get("text", "") or ""
                orig_attachments = self._parse_attachments(original.get("attachments", []))
                attachments.extend(orig_attachments)
                source_owner_id = original.get("owner_id")
                source_post_id = original.get("id")
                if text and orig_text:
                    text = f"{text}\n\n{orig_text}"
                elif orig_text and not text:
                    text = orig_text

            posts.append(
                Post(
                    id=item["id"],
                    owner_id=item["owner_id"],
                    date=item.get("date"),
                    text=text,
                    attachments=attachments,
                    is_pinned=bool(item.get("is_pinned", 0)),
                    source_owner_id=source_owner_id,
                    source_post_id=source_post_id,
                )
            )
        return posts

    def _parse_attachments(self, raw_attachments) -> List[Attachment]:
        parsed: List[Attachment] = []
        for att in raw_attachments:
            att_type = att.get("type")
            data = att.get(att_type, {})

            def _count(val):
                if isinstance(val, dict):
                    return val.get("count")
                if isinstance(val, int):
                    return val
                return None

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
                parsed.append(
                    Attachment(
                        type="video",
                        url=url,
                        title=data.get("title"),
                        likes=_count(data.get("likes")),
                        views=_count(data.get("views")),
                    )
                )
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
        payload = self._request("utils.resolveScreenName", params, timeout=10)
        resp_obj = payload.get("response") or {}
        object_id = resp_obj.get("object_id")
        object_type = resp_obj.get("type")
        if not object_id or not object_type:
            raise RuntimeError("VK API did not resolve screen name")
        return object_type, int(object_id)
