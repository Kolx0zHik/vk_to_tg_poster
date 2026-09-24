import logging
import os
import time
from typing import List

from .backfill import MAX_BACKFILL_POSTS, BackfillRequests, compute_baseline, normalize_mode
from .cache import Cache
from .config import Config, ContentTypes
from .dedup import DedupError, SemanticDedup
from .models import Post
from .tg_client import TelegramClient
from .vk_client import VKClient
from .vk_ids import normalize_community_key, parse_owner_id


logger = logging.getLogger("poster.pipeline")

MAX_FETCH_PAGES = 5
BACKFILL_PAGE_SIZE = 100


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


def _post_text(post: Post) -> str:
    """Raw text used for semantic comparison (attachments' titles too)."""
    parts = [post.text or ""]
    for att in post.attachments:
        if att.title:
            parts.append(att.title)
    return "\n".join(p for p in parts if p.strip()).strip()


def _candidate_pool(cache: Cache, window_days: int, limit: int = 20) -> tuple[List[dict], List[dict]]:
    """Published posts within the window as candidates for the LLM.

    Records are mapped to the ``{"date", "text"}`` shape the user prompt uses
    (ADR-021); ``published_candidates`` already returns the global pool (all
    source communities) per ADR-018. The original cache items are returned
    alongside so the verdict's ``matched`` number can be mapped back.
    """
    since_ts = int(time.time()) - max(1, window_days) * 86400
    items = cache.published_candidates(since_ts, limit=limit)
    candidates = [
        {
            "date": int(item.get("date") or 0),
            "text": str(item.get("text") or "").strip(),
        }
        for item in items
    ]
    return items, candidates


def _dedup_check(
    post: Post,
    cache: Cache,
    dedup: SemanticDedup,
    window_days: int,
    debug_log: bool = False,
) -> tuple[bool, str]:
    """Return (is_duplicate, reason) for a post against the candidate pool.

    Fail-open: on any error we log a warning and treat the post as NOT a
    duplicate so publication is never blocked by the checker.

    When ``debug_log`` is set, also log the "not a duplicate" verdict for
    every text post (the "duplicate" line is already logged unconditionally by
    ``_publish_pending``, so it is not repeated here).
    """
    pool_items, candidates = _candidate_pool(cache, window_days)
    if not candidates:
        if debug_log:
            logger.info("ИИ-проверка поста %s: кандидатов в окне нет, пропущено", post.id)
        return False, ""

    new_post = {
        "text": _post_text(post),
        "date": post.date or 0,
    }
    try:
        result = dedup.check(new_post, candidates)
    except DedupError as exc:
        logger.warning("Семантическая проверка пропущена (%s): %s", post.id, exc)
        return False, ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("Семантическая проверка не удалась (%s): %s", post.id, exc)
        return False, ""
    reason = result.reason
    if result.is_duplicate and result.matched_index:
        idx = result.matched_index
        if 1 <= idx <= len(pool_items):
            matched = pool_items[idx - 1]
            suffix = f"кандидат {idx} (пост {matched['post_id']} из {matched['owner_id']})"
            reason = f"{reason}; {suffix}" if reason else suffix
    if debug_log and not result.is_duplicate:
        logger.info(
            "ИИ-проверка поста %s: не дубль (причина: %s; кандидатов: %s)",
            post.id,
            reason or "—",
            len(candidates),
        )
    return result.is_duplicate, reason


def _publish_pending(
    cache: Cache,
    owner_id: int,
    community,
    tg_client: TelegramClient,
    general,
    max_per_poll: int,
    stats: dict,
    dedup: SemanticDedup | None = None,
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
        window_days = general.semantic_dedup.window_days
        if general.semantic_dedup.enabled and dedup is not None and _post_text(post).strip():
            is_dup, reason = _dedup_check(post, cache, dedup, window_days, debug_log=general.semantic_dedup.debug_log)
            if is_dup:
                cache.mark_skipped(key)
                stats["dedup_skipped"] += 1
                logger.info("Пост %s из %s — дубль, пропущен (%s)", post.id, community.name, reason)
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


def _fetch_for_backfill(vk_client: VKClient, owner_id: int, mode: str, value: int, now: float) -> List[Post]:
    """Fetch just enough posts to compute the requested starting baseline."""
    posts: List[Post] = []

    if mode == "none":
        return vk_client.fetch_posts(owner_id, count=1, offset=0)

    if mode == "posts":
        target = min(MAX_BACKFILL_POSTS, max(1, value) + 1)
        offset = 0
        while len(posts) < target:
            batch = vk_client.fetch_posts(
                owner_id,
                count=min(BACKFILL_PAGE_SIZE, target - len(posts)),
                offset=offset,
            )
            if not batch:
                break
            posts.extend(batch)
            if len(batch) < BACKFILL_PAGE_SIZE:
                break
            offset += len(batch)
        return posts

    cutoff = int(now - max(1, value) * 86400)
    offset = 0
    while len(posts) < MAX_BACKFILL_POSTS:
        batch = vk_client.fetch_posts(owner_id, count=BACKFILL_PAGE_SIZE, offset=offset)
        if not batch:
            break
        posts.extend(batch)
        if any(int(post.date or 0) < cutoff for post in batch):
            break
        if len(batch) < BACKFILL_PAGE_SIZE:
            break
        offset += len(batch)
    return posts


def _apply_backfill(
    community,
    owner_id: int,
    vk_client: VKClient,
    cache: Cache,
    requests: BackfillRequests | None,
    stats: dict,
) -> None:
    """Turn a pending backfill request into a cache baseline for this community."""
    if requests is None:
        return

    request_key = None
    entry = None
    for key in (str(owner_id), normalize_community_key(community.id)):
        if not key:
            continue
        entry = requests.get(key)
        if entry:
            request_key = key
            break
    if not entry or request_key is None:
        return

    mode = normalize_mode(entry.get("mode"))
    value = int(entry.get("value") or 0)
    now = time.time()
    try:
        posts = _fetch_for_backfill(vk_client, owner_id, mode, value, now)
    except Exception as exc:  # noqa: BLE001
        logger.error("Не удалось получить посты для дозаливки %s: %s", community.name, exc)
        return

    baseline = compute_baseline(posts, mode, value, now) or (0, 0)
    cache.set_baseline(owner_id, baseline[0], baseline[1])
    requests.pop(request_key)
    stats["backfill"] = len(posts)
    logger.info(
        "Дозаливка %s: режим=%s значение=%s получено=%s база=(%s,%s)",
        community.name,
        mode,
        value,
        len(posts),
        baseline[0],
        baseline[1],
    )


def process_communities(
    config: Config,
    vk_client: VKClient,
    tg_client: TelegramClient,
    cache: Cache,
    backfill: BackfillRequests | None = None,
) -> None:
    cache.prune_published_text(config.general.semantic_dedup.window_days)
    # No built-in prompt: without an explicit system prompt the LLM check is simply
    # not performed (the run continues as if semantic_dedup were disabled).
    dedup_enabled = config.general.semantic_dedup.enabled and bool(config.llm.prompt.strip())
    if config.general.semantic_dedup.enabled and not dedup_enabled:
        logger.warning("ИИ-проверка включена, но системный промпт (llm.prompt) не задан — проверка пропущена")
    for community in config.communities:
        stats = {
            "fetched": 0,
            "new": 0,
            "published": 0,
            "known": 0,
            "blocked": 0,
            "skipped_by_type": 0,
            "dedup_skipped": 0,
            "failed": 0,
            "backfill": 0,
        }
        if not community.active:
            logger.info("Сообщество %s на паузе, пропускаем", community.name)
            continue

        owner_id = _resolve_owner_id(community.id, vk_client, cache)
        if owner_id is None:
            logger.warning("Не удалось определить ID сообщества '%s', пропускаем", community.id)
            continue

        _apply_backfill(community, owner_id, vk_client, cache, backfill, stats)

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

        dedup = None
        if dedup_enabled:
            dedup = SemanticDedup(
                base_url=config.llm.base_url,
                model=config.llm.model,
                api_key=os.getenv("LLM_API_KEY", ""),
                prompt=config.llm.prompt,
                debug_log=config.general.semantic_dedup.debug_log,
            )
        _publish_pending(
            cache,
            owner_id,
            community,
            tg_client,
            config.general,
            max_per_poll,
            stats,
            dedup=dedup,
        )

        pending_left = len(cache.pending_posts(owner_id))
        logger.info(
            "Сообщество %s: fetched=%s new=%s published=%s known=%s blocked=%s "
            "skipped_by_type=%s dedup_skipped=%s failed=%s pending=%s backfill=%s",
            community.name,
            stats["fetched"],
            stats["new"],
            stats["published"],
            stats["known"],
            stats["blocked"],
            stats["skipped_by_type"],
            stats["dedup_skipped"],
            stats["failed"],
            pending_left,
            stats["backfill"],
        )
