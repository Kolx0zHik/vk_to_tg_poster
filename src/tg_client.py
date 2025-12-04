from __future__ import annotations

import json
import logging
import time
from typing import List, Optional

import requests

from .config import ContentTypes
from .models import Attachment, Post

logger = logging.getLogger("poster.tg")


def _vk_link_keyboard(url: str) -> str:
    keyboard = {"inline_keyboard": [[{"text": "Открыть пост в VK", "url": url}]]}
    return json.dumps(keyboard, ensure_ascii=False)


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_caption_with_link(text: str, vk_url: str, max_len: int = 1024) -> str:
    link_html = f'<a href="{vk_url}">Открыть пост в VK</a>'
    if not text:
        return link_html
    text_html = _escape_html(text)
    reserve = len(link_html) + 2  # for \n\n
    if len(text_html) + reserve > max_len:
        text_html = text_html[: max_len - reserve - 3] + "..."
    return f"{text_html}\n\n{link_html}"


class TelegramClient:
    def __init__(self, bot_token: str, channel_id: str):
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.session = requests.Session()

    class RateLimitError(Exception):
        def __init__(self, retry_after: int | None, payload: dict):
            super().__init__("Telegram rate limit")
            self.retry_after = retry_after
            self.payload = payload

    def _post(self, method: str, data: dict, json_mode: bool = False) -> None:
        url = f"{self.base_url}/{method}"
        if json_mode:
            resp = self.session.post(url, json=data, timeout=20)
        else:
            resp = self.session.post(url, data=data, timeout=20)
        if not resp.ok:
            payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code == 429:
                retry_after = payload.get("parameters", {}).get("retry_after")
                raise self.RateLimitError(retry_after=retry_after, payload=payload)
            raise RuntimeError(f"Telegram API error ({method}): {resp.status_code} {resp.text}")
        payload = resp.json()
        if not payload.get("ok"):
            if payload.get("error_code") == 429:
                retry_after = payload.get("parameters", {}).get("retry_after")
                raise self.RateLimitError(retry_after=retry_after, payload=payload)
            raise RuntimeError(f"Telegram API returned error for {method}: {payload}")

    def _post_with_retry(self, method: str, data: dict, json_mode: bool = False) -> None:
        try:
            self._post(method, data, json_mode=json_mode)
        except self.RateLimitError as exc:
            delay = exc.retry_after or 3
            logger.warning("Rate limited on %s, retrying after %ss", method, delay)
            time.sleep(delay)
            self._post(method, data, json_mode=json_mode)

    def send_text(self, text: str, vk_url: Optional[str] = None, use_keyboard: bool = True) -> None:
        logger.debug("Sending text message to Telegram")
        data = {"chat_id": self.channel_id, "text": text, "disable_web_page_preview": False}
        if vk_url and use_keyboard:
            data["reply_markup"] = _vk_link_keyboard(vk_url)
        self._post_with_retry("sendMessage", data)

    def send_photo(
        self,
        photo_url: str,
        caption: str | None = None,
        vk_url: Optional[str] = None,
        parse_mode: Optional[str] = None,
    ) -> None:
        logger.debug("Sending photo to Telegram")
        data = {"chat_id": self.channel_id, "photo": photo_url}
        if caption:
            data["caption"] = caption
        if parse_mode:
            data["parse_mode"] = parse_mode
        if vk_url:
            data["reply_markup"] = _vk_link_keyboard(vk_url)
        self._post_with_retry("sendPhoto", data)

    def send_video(self, video_url: str, caption: str | None = None, vk_url: Optional[str] = None) -> None:
        logger.debug("Sending video to Telegram")
        data = {"chat_id": self.channel_id, "video": video_url}
        if caption:
            data["caption"] = caption
        if vk_url:
            data["reply_markup"] = _vk_link_keyboard(vk_url)
        self._post_with_retry("sendVideo", data)

    def send_audio(self, audio_url: str, caption: str | None = None, vk_url: Optional[str] = None) -> None:
        logger.debug("Sending audio to Telegram")
        data = {"chat_id": self.channel_id, "audio": audio_url}
        if caption:
            data["caption"] = caption
        if vk_url:
            data["reply_markup"] = _vk_link_keyboard(vk_url)
        self._post_with_retry("sendAudio", data)

    def send_link(self, link_url: str, title: str | None = None, vk_url: Optional[str] = None) -> None:
        text = f"{title or ''}\n{link_url}" if title else link_url
        self.send_text(text.strip(), vk_url=vk_url)

    def send_media_group(self, media: List[dict]) -> None:
        logger.debug("Sending media group to Telegram (%s items)", len(media))
        data = {"chat_id": self.channel_id, "media": media}
        self._post_with_retry("sendMediaGroup", data, json_mode=True)

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
            caption = _escape_html(post.text) if (allowed.text and post.text) else None
            self.send_photo(
                photos[0].url,
                caption=caption,
                vk_url=vk_url,
                parse_mode="HTML" if caption else None,
            )
            text_used = bool(caption)
        # Множественные фото: отправляем альбом без caption, затем текст отдельным сообщением с кнопкой.
        elif len(photos) > 1:
            media = [{"type": "photo", "media": photo.url} for photo in photos]
            self.send_media_group(media)
            # Отдельным сообщением отправляем текст + кнопку на VK.
            if allowed.text and post.text:
                self.send_text(_escape_html(post.text), vk_url=vk_url)
                text_used = True

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
