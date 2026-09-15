import re
from typing import Optional
from urllib.parse import unquote


MAX_KEY_LENGTH = 64

_VK_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*vk\.(?:com|ru)$", re.IGNORECASE)
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_CLEAN_RE = re.compile(r"[^a-z0-9._-]+")


def normalize_community_key(raw: str) -> str:
    """Canonical, lowercased community key for cache lookups and VK API calls.

    Accepts full URLs (vk.com / vk.ru / m.vk.com / new.vk.com ...), club/public/event
    ids, id-prefixed and numeric ids, ``@screenname``, and plain screen names.
    """
    value = unquote((raw or "").strip())
    if not value:
        return ""

    value = _SCHEME_RE.sub("", value)
    value = value.split("?", 1)[0].split("#", 1)[0].strip()

    if "/" in value:
        host, rest = value.split("/", 1)
        if _VK_HOST_RE.match(host) or ("." in host and " " not in host):
            value = rest

    value = value.strip("/")
    if "/" in value:
        value = value.split("/", 1)[0]

    value = value.lower().lstrip("@")
    value = _CLEAN_RE.sub("", value)
    return value[:MAX_KEY_LENGTH]


def parse_owner_id(raw: str) -> Optional[int]:
    """Return a numeric VK owner id when it can be derived without an API call."""
    key = normalize_community_key(raw)
    if not key:
        return None

    for prefix in ("club", "public", "event"):
        if key.startswith(prefix) and key[len(prefix) :].isdigit():
            return -int(key[len(prefix) :])

    if key.startswith("id") and key[2:].isdigit():
        return int(key[2:])

    if key.lstrip("-").isdigit():
        return int(key)

    return None


def normalize_display_id(raw: str) -> str:
    """Value stored in config for a community: numeric id when possible, else screen name."""
    key = normalize_community_key(raw)
    if not key:
        return ""
    owner_id = parse_owner_id(key)
    if owner_id is None:
        return key
    return str(owner_id)
