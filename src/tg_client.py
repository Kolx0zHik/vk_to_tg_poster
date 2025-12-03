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

    def _post(self, method: str, data: dict, json_mode: bool = False) -> None:
        url = f"{self.base_url}/{method}"
        if json_mode:
            resp = self.session.post(url, json=data, timeout=20)
        else:
            resp = self.session.post(url, data=data, timeout=20)
        if not resp.ok:
            raise RuntimeError(f"Telegram API error ({method}): {resp.status_code} {resp.text}")
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API returned error for {method}: {payload}")

    def send_text(self, text: str, vk_url: Optional[str] = None, use_keyboard: bool = True) -> None:
        logger.debug("Sending text message to Telegram")
        data = {"chat_id": self.channel_id, "text": text, "disable_web_page_preview": False}
        if vk_url and use_keyboard:
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

    def send_media_group(self, media: List[dict]) -> None:
        logger.debug("Sending media group to Telegram (%s items)", len(media))
        data = {"chat_id": self.channel_id, "media": media}
        self._post("sendMediaGroup", data, json_mode=True)

    def send_post(self, post: Post, allowed: ContentTypes) -> None:
        vk_url = post.vk_link
        attachments = self._filter_attachments(post.attachments, allowed)

        photos = [a for a in attachments if a.type == "photo"]
        videos = [a for a in attachments if a.type == "video"]
        audios = [a for a in attachments if a.type == "audio"]
        links = [a for a in attachments if a.type == "link"]

        text_used = False

        # Single photo: отправляем с текстом в caption и кнопкой.
        if photos and len(photos) == 1:
            caption = post.text if allowed.text else None
            self.send_photo(photos[0].url, caption=caption, vk_url=vk_url)
            text_used = bool(caption)
        # Множественные фото: отправляем альбом, caption в первом, потом отдельное сообщение с кнопкой.
        elif len(photos) > 1:
            caption = post.text if allowed.text else None
            media = []
            for idx, photo in enumerate(photos):
                item = {"type": "photo", "media": photo.url}
                if idx == 0 and caption:
                    item["caption"] = caption
                media.append(item)
            self.send_media_group(media)
            # После альбома Telegram не поддерживает inline-кнопки, поэтому даём ссылку текстом.
            self.send_text(f"Открыть пост в VK: {vk_url}", use_keyboard=False)
            text_used = bool(caption)

        # Видео/аудио
        for video in videos:
            if video.url and video.url.endswith((".mp4", ".mov", ".mkv")):
                self.send_video(video.url, caption=post.text if (allowed.text and not text_used) else video.title, vk_url=vk_url)
                text_used = text_used or bool(post.text)
            else:
                self.send_link(video.url or vk_url, title=video.title or "Видео", vk_url=vk_url)

        for audio in audios:
            if audio.url:
                self.send_audio(audio.url, caption=post.text if (allowed.text and not text_used) else audio.title, vk_url=vk_url)
                text_used = text_used or bool(post.text)
            else:
                self.send_link(vk_url, title=audio.title or "Аудио", vk_url=vk_url)

        # Текст, если ещё не использовали и нет фото/медиа с подписью.
        if allowed.text and post.text and not text_used and not photos and not videos and not audios:
            self.send_text(post.text, vk_url=vk_url)

        # Ссылки отдельными сообщениями.
        for link in links:
            self.send_link(link.url, title=link.title, vk_url=vk_url)

    @staticmethod
    def _filter_attachments(attachments: List[Attachment], allowed: ContentTypes) -> List[Attachment]:
        allowed_map = {
            "photo": allowed.photo,
            "video": allowed.video,
            "audio": allowed.audio,
            "link": allowed.link,
        }
        return [att for att in attachments if allowed_map.get(att.type, False)]
