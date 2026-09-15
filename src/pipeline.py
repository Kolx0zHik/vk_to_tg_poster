import logging
from typing import List

from .cache import Cache
from .config import Config, ContentTypes
from .models import Post
from .tg_client import TelegramClient
from .vk_client import VKClient
from .vk_ids import normalize_community_key, parse_owner_id


logger = logging.getLogger("poster.pipeline")

MAX_FETCH_PAGES = 5


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
        if kw.strip() and kw.lower() in haystack:
            return True
    return False


def _resolve_owner_id(raw_id: str, vk_client: VKClient, cache: Cache) -> int | None:
    """Resolve a community owner id using the local parser and a persistent cache
    before falling back to the VK API."""
    value = (raw_id or "").strip()
    if not value:
        return None

    local = parse_owner_id(value)
    if local is not None:
        return local

    key = normalize_community_key(value)
    if key:
        cached = cache.get_owner_id(key)
        if cached is not None:
            return cached

    try:
        obj_type, object_id = vk_client.resolve_screen_name(key or value)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to resolve VK community id '%s': %s", raw_id, exc)
        return None

    owner_id = -object_id if obj_type in {"group", "page", "event"} else object_id
    if key:
        cache.set_owner_id(key, owner_id)
    return owner_id


def _fetch_recent(vk_client: VKClient, cache: Cache, owner_id: int, page_size: int) -> List[Post]:
    """Fetch the newest posts, paging back until we catch up with known posts."""
    fetched: List[Post] = []
    offset = 0
    for _ in range(MAX_FETCH_PAGES):
        batch = vk_client.fetch_posts(owner_id, count=page_size, offset=offset)
        if not batch:
            break
        fetched.extend(batch)
        if all(cache.is_known(owner_id, post) for post in batch):
            break
        if len(batch) < page_size:
            break
        offset += len(batch)
    return fetched


def _record_fetched(cache: Cache, owner_id: int, fetched: List[Post], stats: dict) -> None:
    for post in sorted(fetched, key=lambda item: ((item.date or 0), item.id)):  # oldest first
        result = cache.record_post(owner_id, post)
        if result == "new":
            stats["new"] += 1
        elif result == "known":
            stats["known"] += 1


def _publish_pending(
    cache: Cache,
    owner_id: int,
    community,
    tg_client: TelegramClient,
    general,
    max_per_poll: int,
    stats: dict,
) -> None:
    for key, post in cache.pending_posts(owner_id, limit=max_per_poll):
        if _contains_blocked(post, general.blocked_keywords):
            cache.mark_skipped(key)
            stats["blocked"] += 1
            continue
        if not _should_publish(post, community.content_types):
            cache.mark_skipped(key)
            stats["skipped_by_type"] += 1
            continue
        try:
            tg_client.send_post(post, community.content_types)
        except Exception as exc:  # noqa: BLE001
            status = cache.mark_failed(key)
            stats["failed"] += 1
            logger.error("Не удалось опубликовать пост %s из %s: %s", post.id, community.name, exc)
            if status == "dead":
                logger.warning(
                    "Пост %s из %s отброшен после %s попыток",
                    post.id,
                    community.name,
                    cache.PENDING_MAX_ATTEMPTS,
                )
        else:
            cache.mark_published(key)
            stats["published"] += 1
            logger.debug("Опубликован пост %s из %s", post.id, community.name)


def process_communities(config: Config, vk_client: VKClient, tg_client: TelegramClient, cache: Cache) -> None:
    for community in config.communities:
        stats = {
            "fetched": 0,
            "new": 0,
            "published": 0,
            "known": 0,
            "blocked": 0,
            "skipped_by_type": 0,
            "failed": 0,
        }
        if not community.active:
            logger.debug("Сообщество %s выключено, пропускаем", community.name)
            continue

        owner_id = _resolve_owner_id(community.id, vk_client, cache)
        if owner_id is None:
            logger.warning("Не удалось определить ID сообщества '%s', пропускаем", community.id)
            continue

        max_per_poll = max(1, int(config.general.posts_limit))
        page_size = min(10, max_per_poll)
        logger.debug("Запрашиваем посты из %s (owner_id=%s)", community.name, owner_id)
        try:
            fetched = _fetch_recent(vk_client, cache, owner_id, page_size)
        except Exception as exc:  # noqa: BLE001
            logger.error("Не удалось получить посты для %s: %s", community.name, exc)
            continue

        stats["fetched"] = len(fetched)
        _record_fetched(cache, owner_id, fetched, stats)
        _publish_pending(cache, owner_id, community, tg_client, config.general, max_per_poll, stats)

        pending_left = len(cache.pending_posts(owner_id))
        logger.info(
            "Сообщество %s: fetched=%s new=%s published=%s known=%s blocked=%s "
            "skipped_by_type=%s failed=%s pending=%s",
            community.name,
            stats["fetched"],
            stats["new"],
            stats["published"],
            stats["known"],
            stats["blocked"],
            stats["skipped_by_type"],
            stats["failed"],
            pending_left,
        )
