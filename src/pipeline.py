import logging
from typing import List

from .cache import Cache
from .config import Config, ContentTypes
from .models import Post
from .tg_client import TelegramClient
from .vk_client import VKClient


logger = logging.getLogger("poster.pipeline")


def _dedup_key(post: Post) -> str:
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
            logger.debug("Community %s is disabled, skipping", community.name)
            continue

        owner_id = _normalize_owner_id(community.id, vk_client)
        if owner_id is None:
            logger.warning("Cannot resolve community id '%s', skipping", community.id)
            continue

        try:
            posts = vk_client.fetch_posts(owner_id, config.general.posts_limit)
            logger.info("Fetched %s posts from %s", len(posts), community.name)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to fetch posts for %s: %s", community.name, exc)
            continue

        # Process oldest first to keep order.
        for post in reversed(posts):
            if not _should_publish(post, community.content_types):
                continue
            digest = _dedup_key(post)
            if cache.is_duplicate(owner_id, digest):
                logger.debug("Post %s already processed for community %s", post.id, community.name)
                continue
            try:
                tg_client.send_post(post, community.content_types)
                cache.remember(owner_id, digest)
                logger.info("Published post %s from %s", post.id, community.name)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to publish post %s from %s: %s", post.id, community.name, exc)
