import logging
from typing import List

from .cache import Cache
from .config import Config, ContentTypes
from .models import Post
from .tg_client import TelegramClient
from .vk_client import VKClient


logger = logging.getLogger("poster.pipeline")


def _dedup_key(post: Post) -> str:
    """
    Build a deduplication key:
    - Prefer original post ids from copy_history (source_owner_id/source_post_id) if present.
    - Fallback to current post owner/id.
    """
    owner = post.source_owner_id if post.source_owner_id is not None else post.owner_id
    pid = post.source_post_id if post.source_post_id is not None else post.id
    return f"{owner}_{pid}"


def _should_publish(post: Post, allowed: ContentTypes) -> bool:
    if allowed.text and post.text.strip():
        return True
    for att in post.attachments:
        if getattr(allowed, att.type, False):
            return True
    return False


def _contains_blocked(post: Post, blocked_keywords: List[str]) -> bool:
    if not blocked_keywords:
        return False
    text_parts = [post.text or ""]
    for att in post.attachments:
        if att.title:
            text_parts.append(att.title)
    haystack = " ".join(text_parts).lower()
    for kw in blocked_keywords:
        if kw.lower() in haystack and kw.strip():
            return True
    return False


def _normalize_owner_id(raw_id: str, vk_client: VKClient) -> int | None:
    value = (raw_id or "").strip()
    if not value:
        return None
    lower = value.lower()

    if lower.startswith("https://vk.com/"):
        lower = lower.replace("https://vk.com/", "")
    if lower.startswith("http://vk.com/"):
        lower = lower.replace("http://vk.com/", "")
    lower = lower.strip("/")

    # club12345, public12345, event12345 -> negative ids
    for prefix in ("club", "public", "event"):
        if lower.startswith(prefix) and lower[len(prefix) :].isdigit():
            return -int(lower[len(prefix) :])

    if lower.startswith("id") and lower[2:].isdigit():
        return int(lower[2:])

    # numeric owner id with sign
    if lower.lstrip("-").isdigit():
        return int(lower)

    # screen name -> resolve via API
    try:
        obj_type, object_id = vk_client.resolve_screen_name(lower)
        if obj_type in {"group", "page", "event"}:
            return -object_id
        return object_id
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to resolve VK community id '%s': %s", raw_id, exc)
        return None


def process_communities(config: Config, vk_client: VKClient, tg_client: TelegramClient, cache: Cache) -> None:
    for community in config.communities:
        if not community.active:
            logger.debug("Сообщество %s выключено, пропускаем", community.name)
            continue

        owner_id = _normalize_owner_id(community.id, vk_client)
        if owner_id is None:
            logger.warning("Не удалось определить ID сообщества '%s', пропускаем", community.id)
            continue

        logger.debug("Запрашиваем посты из %s (owner_id=%s)", community.name, owner_id)
        try:
            posts = vk_client.fetch_posts(owner_id, config.general.posts_limit)
            logger.info("Получено %s постов из %s", len(posts), community.name)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Не удалось получить посты для %s: %s", community.name, exc)
            continue

        last_ts, last_id = cache.get_last_seen(owner_id)
        new_posts: list[Post] = []
        for post in reversed(posts):  # старые сначала
            ts = getattr(post, "date", None) or 0
            if last_ts:
                if ts < last_ts:
                    continue
                if ts == last_ts and post.id <= (last_id or 0):
                    continue
            new_posts.append(post)

        # Process oldest first to keep order.
        for post in new_posts:
            if _contains_blocked(post, config.general.blocked_keywords):
                logger.info("Пост %s пропущен по стоп-словам в %s", post.id, community.name)
                continue
            if not _should_publish(post, community.content_types):
                continue
            digest = _dedup_key(post)
            if cache.is_duplicate(digest):
                logger.debug("Пост %s уже публиковался для %s, дубликат", post.id, community.name)
                continue
            try:
                tg_client.send_post(post, community.content_types)
                cache.remember(owner_id, digest, getattr(post, "date", None))
                cache.update_last_seen(owner_id, post.id, getattr(post, "date", None))
                logger.info("Опубликован пост %s из %s", post.id, community.name)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Не удалось опубликовать пост %s из %s: %s", post.id, community.name, exc)
