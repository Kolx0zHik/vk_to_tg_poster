from __future__ import annotations

import json
import logging
import time
from typing import List, Optional

import requests

from .config import ContentTypes
from .models import Attachment, Post

logger = logging.getLogger("poster.tg")
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
CAPTION_CONTINUATION = "...\n\n<b>Продолжение текста читайте в источнике.</b>"
MAX_PHOTO_BYTES = 10 * 1024 * 1024
DOWNLOAD_TIMEOUT = 30


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


def _safe_offset(text: str, offset: int) -> int:
    """Move a cut offset left so an HTML entity or tag is not split in half."""
    if offset >= len(text):
        return len(text)
    amp = text.rfind("&", 0, offset)
    if amp != -1:
        semicolon = text.find(";", amp)
        if semicolon >= offset:
            offset = amp
    tag = text.rfind("<", 0, offset)
    if tag != -1:
        closing = text.find(">", tag)
        if closing >= offset:
            offset = tag
    return offset


def _break_offset(text: str, limit: int) -> int:
    """Pick the preferred cut offset: paragraph, line, sentence, word, then hard cut."""
    if limit >= len(text):
        return len(text)
    window = text[:limit]
    for separator in ("\n\n", "\n"):
        index = window.rfind(separator)
        if index > 0:
            return index + len(separator)
    sentence = 0
    for separator in (". ", "! ", "? ", "… "):
        index = window.rfind(separator)
        if index > sentence:
            sentence = index + len(separator)
    if sentence > 0:
        return sentence
    space = window.rfind(" ")
    if space > 0:
        return space + 1
    return limit


def _split_text(text: str, limit: int = MESSAGE_LIMIT) -> List[str]:
    """Split a ready-to-send message body into chunks that fit the Telegram limit."""
    remaining = text.strip()
    chunks: List[str] = []
    while len(remaining) > limit:
        offset = _safe_offset(remaining, _break_offset(remaining, limit))
        if offset <= 0:
            offset = limit
        chunk = remaining[:offset].strip()
        if not chunk:
            offset = limit
            chunk = remaining[:offset]
        chunks.append(chunk)
        remaining = remaining[offset:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _build_photo_caption(text: str, max_len: int = CAPTION_LIMIT) -> str:
    text_html = _escape_html(text)
    if len(text_html) <= max_len:
        return text_html

    budget = max_len - len(CAPTION_CONTINUATION)
    if budget <= 0:
        return CAPTION_CONTINUATION[:max_len]

    low = 0
    high = len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if len(_escape_html(text[:mid]).rstrip()) <= budget:
            low = mid
        else:
            high = mid - 1

    prefix = text[:low].rstrip()
    if low < len(text) and prefix and not text[low : low + 1].isspace():
        words = prefix.rsplit(None, 1)
        if len(words) == 2:
            prefix = words[0]

    return f"{_escape_html(prefix)}{CAPTION_CONTINUATION}"


def _truncate_html_text(text_html: str, max_len: int = CAPTION_LIMIT) -> str:
    """Truncate an already escaped HTML caption to the Telegram caption limit."""
    if len(text_html) <= max_len:
        return text_html

    budget = max_len - len(CAPTION_CONTINUATION)
    if budget <= 0:
        return CAPTION_CONTINUATION[:max_len]

    offset = _safe_offset(text_html, _break_offset(text_html, budget))
    if offset <= 0:
        offset = budget
    return f"{text_html[:offset].rstrip()}{CAPTION_CONTINUATION}"


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

    def _post(self, method: str, data: dict, json_mode: bool = False, files: dict | None = None) -> None:
        url = f"{self.base_url}/{method}"
        if json_mode:
            resp = self.session.post(url, json=data, timeout=20)
        elif files:
            resp = self.session.post(url, data=data, files=files, timeout=60)
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

    def _post_with_retry(
        self, method: str, data: dict, json_mode: bool = False, files: dict | None = None
    ) -> None:
        try:
            self._post(method, data, json_mode=json_mode, files=files)
        except self.RateLimitError as exc:
            delay = exc.retry_after or 3
            logger.warning("Ограничение Telegram на %s, повтор через %s с", method, delay)
            time.sleep(delay)
            self._post(method, data, json_mode=json_mode, files=files)

    def _download_media(self, url: str, max_bytes: int) -> bytes:
        """Download media locally so it can be uploaded when Telegram cannot fetch it."""
        try:
            response = self.session.get(url, timeout=DOWNLOAD_TIMEOUT)
            response.raise_for_status()
            content = response.content
        except requests.RequestException as exc:
            raise RuntimeError(f"Не удалось скачать медиа: {type(exc).__name__}") from None
        if len(content) > max_bytes:
            raise RuntimeError(f"Медиа больше лимита ({len(content)} байт)")
        return content

    def send_text(
        self,
        text: str,
        vk_url: Optional[str] = None,
        use_keyboard: bool = True,
        parse_mode: Optional[str] = None,
        disable_preview: bool = False,
    ) -> None:
        logger.debug("Отправка текстового сообщения в Telegram")
        chunks = _split_text(text)
        for index, chunk in enumerate(chunks):
            data = {
                "chat_id": self.channel_id,
                "text": chunk,
                "disable_web_page_preview": disable_preview,
            }
            if parse_mode:
                data["parse_mode"] = parse_mode
            if vk_url and use_keyboard and index == len(chunks) - 1:
                data["reply_markup"] = _vk_link_keyboard(vk_url)
            self._post_with_retry("sendMessage", data)

    def send_photo(
        self,
        photo_url: str,
        caption: str | None = None,
        vk_url: Optional[str] = None,
        parse_mode: Optional[str] = None,
    ) -> None:
        logger.debug("Отправка фото в Telegram")
        data = {"chat_id": self.channel_id}
        if caption:
            data["caption"] = caption
        if parse_mode:
            data["parse_mode"] = parse_mode
        if vk_url:
            data["reply_markup"] = _vk_link_keyboard(vk_url)
        try:
            content = self._download_media(photo_url, MAX_PHOTO_BYTES)
        except RuntimeError:
            logger.warning("Не удалось скачать фото, пробуем отправить по URL")
            data["photo"] = photo_url
            self._post_with_retry("sendPhoto", data)
        else:
            self._post_with_retry("sendPhoto", data, files={"photo": ("photo.jpg", content)})

    def send_video(
        self,
        video_url: str,
        caption: str | None = None,
        vk_url: Optional[str] = None,
        parse_mode: Optional[str] = None,
    ) -> None:
        logger.debug("Отправка видео в Telegram")
        data = {"chat_id": self.channel_id, "video": video_url}
        if caption:
            data["caption"] = caption
        if parse_mode:
            data["parse_mode"] = parse_mode
        if vk_url:
            data["reply_markup"] = _vk_link_keyboard(vk_url)
        self._post_with_retry("sendVideo", data)

    def send_audio(
        self,
        audio_url: str,
        caption: str | None = None,
        vk_url: Optional[str] = None,
        parse_mode: Optional[str] = None,
    ) -> None:
        logger.debug("Отправка аудио в Telegram")
        data = {"chat_id": self.channel_id, "audio": audio_url}
        if caption:
            data["caption"] = caption
        if parse_mode:
            data["parse_mode"] = parse_mode
        if vk_url:
            data["reply_markup"] = _vk_link_keyboard(vk_url)
        self._post_with_retry("sendAudio", data)

    def send_link(self, link_url: str, title: str | None = None, vk_url: Optional[str] = None) -> None:
        text = f"{title or ''}\n{link_url}" if title else link_url
        self.send_text(text.strip(), vk_url=vk_url)

    def send_media_group(self, media: List[dict]) -> None:
        logger.debug("Отправка медиагруппы в Telegram (%s элементов)", len(media))
        try:
            payload, files = self._build_media_group_upload(media)
        except RuntimeError:
            logger.warning("Не удалось скачать медиагруппу, пробуем отправить по URL")
            data = {"chat_id": self.channel_id, "media": media}
            self._post_with_retry("sendMediaGroup", data, json_mode=True)
        else:
            self._post_with_retry("sendMediaGroup", payload, files=files)

    def _build_media_group_upload(self, media: List[dict]) -> tuple[dict, dict]:
        files: dict = {}
        upload_media: List[dict] = []
        for index, item in enumerate(media):
            url = item.get("media", "")
            content = self._download_media(url, MAX_PHOTO_BYTES)
            attach = f"file{index}"
            files[attach] = (f"photo{index}.jpg", content)
            upload_media.append({"type": item.get("type", "photo"), "media": f"attach://{attach}"})
        payload = {
            "chat_id": self.channel_id,
            "media": json.dumps(upload_media, ensure_ascii=False),
        }
        return payload, files

    def send_post(self, post: Post, allowed: ContentTypes) -> None:
        vk_url = post.vk_link
        attachments = self._filter_attachments(post.attachments, allowed)

        photos = [a for a in attachments if a.type == "photo"]
        videos = [a for a in attachments if a.type == "video"]
        audios = [a for a in attachments if a.type == "audio"]
        links = [a for a in attachments if a.type == "link"]

        text_used = False

        # Single photo: отправляем фото с caption (если есть текст) и кнопкой.
        if photos and len(photos) == 1:
            caption = _build_photo_caption(post.text) if (allowed.text and post.text) else None
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
                self.send_text(_escape_html(post.text), vk_url=vk_url, parse_mode="HTML")
                text_used = True

        # Видео/аудио
        for video in videos:
            stats_parts = []
            if video.views is not None:
                stats_parts.append(f"Просмотры: {video.views}")
            if video.likes is not None:
                stats_parts.append(f"Лайки: {video.likes}")
            stats_text = " | ".join(stats_parts)

            if video.url and video.url.endswith((".mp4", ".mov", ".mkv")):
                caption_parts = []
                if allowed.text and post.text and not text_used:
                    reserve = len(stats_text) + 2 if stats_text else 0
                    caption_parts.append(
                        _truncate_html_text(_escape_html(post.text), CAPTION_LIMIT - reserve)
                    )
                if stats_text:
                    caption_parts.append(stats_text)
                caption = "\n\n".join(part for part in caption_parts if part)
                self.send_video(
                    video.url,
                    caption=caption if caption else None,
                    vk_url=vk_url,
                    parse_mode="HTML" if caption else None,
                )
                text_used = text_used or bool(post.text)
            else:
                link_url = video.url or vk_url
                base_text = post.text if (allowed.text and post.text and not text_used) else ""
                link_text = _escape_html(video.title) if video.title else "Видео"
                if link_url:
                    text_body_parts = []
                    if base_text:
                        text_body_parts.append(_escape_html(base_text))
                    link_html = f'<a href="{link_url}">{link_text}</a>'
                    text_body_parts.append(link_html)
                    if stats_text:
                        text_body_parts.append(stats_text)
                    text_body = "\n\n".join(text_body_parts)
                    self.send_text(text_body, vk_url=vk_url, parse_mode="HTML", disable_preview=False)
                    text_used = text_used or bool(base_text)
                else:
                    residual = [link_text]
                    if stats_text:
                        residual.append(stats_text)
                    self.send_text("\n".join(residual), vk_url=vk_url)

        for audio in audios:
            if audio.url:
                if allowed.text and post.text and not text_used:
                    caption = _truncate_html_text(_escape_html(post.text))
                else:
                    caption = _truncate_html_text(_escape_html(audio.title)) if audio.title else None
                self.send_audio(
                    audio.url,
                    caption=caption,
                    vk_url=vk_url,
                    parse_mode="HTML" if caption else None,
                )
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
