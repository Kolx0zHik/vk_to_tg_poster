import hashlib
import logging
from typing import List

from .cache import Cache
from .config import Config, ContentTypes
from .models import Post
from .tg_client import TelegramClient
from .vk_client import VKClient


logger = logging.getLogger("poster.pipeline")


def _post_hash(post: Post, allowed: ContentTypes) -> str:
    relevant_attachments = [
        f"{att.type}:{att.url}"
        for att in post.attachments
        if getattr(allowed, att.type, False)
    ]
    payload = f"{post.text}|{'|'.join(sorted(relevant_attachments))}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _should_publish(post: Post, allowed: ContentTypes) -> bool:
    if allowed.text and post.text.strip():
        return True
    for att in post.attachments:
        if getattr(allowed, att.type, False):
            return True
    return False


def process_communities(config: Config, vk_client: VKClient, tg_client: TelegramClient, cache: Cache) -> None:
    for community in config.communities:
        if not community.active:
            logger.debug("Community %s is disabled, skipping", community.name)
            continue
        try:
            posts = vk_client.fetch_posts(community.id, config.general.posts_limit)
            logger.info("Fetched %s posts from %s", len(posts), community.name)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to fetch posts for %s: %s", community.name, exc)
            continue

        # Process oldest first to keep order.
        for post in reversed(posts):
            if not _should_publish(post, community.content_types):
                continue
            digest = _post_hash(post, community.content_types)
            if cache.is_duplicate(community.id, digest):
                logger.debug("Post %s already processed for community %s", post.id, community.name)
                continue
            try:
                tg_client.send_post(post, community.content_types)
                cache.remember(community.id, digest)
                logger.info("Published post %s from %s", post.id, community.name)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to publish post %s from %s: %s", post.id, community.name, exc)
