from __future__ import annotations

import json
import logging
from typing import List, Optional

import requests

from .config import ContentTypes
from .models import Attachment, Post

logger = logging.getLogger("poster.tg")


def _vk_link_keyboard(url: str) -> str:
    keyboard = {"inline_keyboard": [[{"text": "Открыть пост в VK", "url": url}]]}
    return json.dumps(keyboard, ensure_ascii=False)


class TelegramClient:
    def __init__(self, bot_token: str, channel_id: str):
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.session = requests.Session()

    def _post(self, method: str, data: dict) -> None:
        url = f"{self.base_url}/{method}"
        resp = self.session.post(url, data=data, timeout=20)
        if not resp.ok:
            raise RuntimeError(f"Telegram API error ({method}): {resp.status_code} {resp.text}")
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API returned error for {method}: {payload}")

    def send_text(self, text: str, vk_url: Optional[str] = None) -> None:
        logger.debug("Sending text message to Telegram")
        data = {"chat_id": self.channel_id, "text": text, "disable_web_page_preview": False}
        if vk_url:
            data["reply_markup"] = _vk_link_keyboard(vk_url)
        self._post("sendMessage", data)

    def send_photo(self, photo_url: str, caption: str | None = None, vk_url: Optional[str] = None) -> None:
        logger.debug("Sending photo to Telegram")
        data = {"chat_id": self.channel_id, "photo": photo_url}
        if caption:
            data["caption"] = caption
        if vk_url:
            data["reply_markup"] = _vk_link_keyboard(vk_url)
        self._post("sendPhoto", data)

    def send_video(self, video_url: str, caption: str | None = None, vk_url: Optional[str] = None) -> None:
        logger.debug("Sending video to Telegram")
        data = {"chat_id": self.channel_id, "video": video_url}
        if caption:
            data["caption"] = caption
        if vk_url:
            data["reply_markup"] = _vk_link_keyboard(vk_url)
        self._post("sendVideo", data)

    def send_audio(self, audio_url: str, caption: str | None = None, vk_url: Optional[str] = None) -> None:
        logger.debug("Sending audio to Telegram")
        data = {"chat_id": self.channel_id, "audio": audio_url}
        if caption:
            data["caption"] = caption
        if vk_url:
            data["reply_markup"] = _vk_link_keyboard(vk_url)
        self._post("sendAudio", data)

    def send_link(self, link_url: str, title: str | None = None, vk_url: Optional[str] = None) -> None:
        text = f"{title or ''}\n{link_url}" if title else link_url
        self.send_text(text.strip(), vk_url=vk_url)

    def send_post(self, post: Post, allowed: ContentTypes) -> None:
        vk_url = post.vk_link

        if allowed.text and post.text:
            self.send_text(post.text, vk_url=vk_url)

        attachments = self._filter_attachments(post.attachments, allowed)
        for attachment in attachments:
            if attachment.type == "photo":
                self.send_photo(attachment.url, vk_url=vk_url)
            elif attachment.type == "video":
                # Telegram требует реальный видео-файл; если URL пустой или не mp4 — отправляем ссылкой.
                if attachment.url and attachment.url.endswith((".mp4", ".mov", ".mkv")):
                    self.send_video(attachment.url, caption=attachment.title, vk_url=vk_url)
                else:
                    self.send_link(attachment.url or vk_url, title=attachment.title or "Видео", vk_url=vk_url)
            elif attachment.type == "audio":
                if attachment.url:
                    self.send_audio(attachment.url, caption=attachment.title, vk_url=vk_url)
                else:
                    self.send_link(vk_url, title=attachment.title or "Аудио", vk_url=vk_url)
            elif attachment.type == "link":
                self.send_link(attachment.url, title=attachment.title, vk_url=vk_url)

    @staticmethod
    def _filter_attachments(attachments: List[Attachment], allowed: ContentTypes) -> List[Attachment]:
        allowed_map = {
            "photo": allowed.photo,
            "video": allowed.video,
            "audio": allowed.audio,
            "link": allowed.link,
        }
        return [att for att in attachments if allowed_map.get(att.type, False)]
