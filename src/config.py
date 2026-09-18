from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class LogRotationSettings:
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5


@dataclass
class SemanticDedupSettings:
    enabled: bool = False
    window_days: int = 4


@dataclass
class GeneralSettings:
    cron: str = "*/10 * * * *"
    vk_api_version: str = "5.199"
    posts_limit: int = 10
    cache_file: str = "data/cache.json"
    log_file: str = "data/logs/poster.log"
    log_level: str = "INFO"
    log_rotation: LogRotationSettings = field(default_factory=LogRotationSettings)
    blocked_keywords: List[str] = field(default_factory=list)
    refresh_avatars: bool = True
    log_retention_days: int = 2
    semantic_dedup: SemanticDedupSettings = field(default_factory=SemanticDedupSettings)


@dataclass
class VKSettings:
    pass


@dataclass
class TelegramSettings:
    channel_id: str = ""


@dataclass
class LLMSettings:
    base_url: str = ""
    model: str = ""
    prompt: str = ""


@dataclass
class ContentTypes:
    text: bool = True
    photo: bool = True
    video: bool = True
    audio: bool = True
    link: bool = True


@dataclass
class Community:
    id: str
    name: str
    active: bool = True
    content_types: ContentTypes = field(default_factory=ContentTypes)


@dataclass
class Config:
    general: GeneralSettings
    vk: VKSettings
    telegram: TelegramSettings
    communities: List[Community]
    llm: LLMSettings = field(default_factory=LLMSettings)


class ConfigError(Exception):
    pass


def default_config() -> Config:
    return Config(
        general=GeneralSettings(),
        vk=VKSettings(),
        telegram=TelegramSettings(),
        llm=LLMSettings(),
        communities=[],
    )


def _load_yaml(path: Path, allow_missing: bool = False) -> Dict:
    if not path.exists():
        if allow_missing:
            return {}
        raise ConfigError(f"Config file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Не удалось прочитать конфиг {path}: {exc}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError("Корень конфига должен быть словарём (mapping)")
    return data


def _as_int(value: Any, field_name: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{field_name} должно быть числом") from None


def _parse_log_rotation(raw: Dict) -> LogRotationSettings:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError("general.log_rotation должно быть словарём")
    return LogRotationSettings(
        max_bytes=_as_int(raw.get("max_bytes", LogRotationSettings.max_bytes), "log_rotation.max_bytes", LogRotationSettings.max_bytes),
        backup_count=_as_int(
            raw.get("backup_count", LogRotationSettings.backup_count),
            "log_rotation.backup_count",
            LogRotationSettings.backup_count,
        ),
    )


def _parse_semantic_dedup(raw: Dict) -> SemanticDedupSettings:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError("general.semantic_dedup должно быть словарём")
    return SemanticDedupSettings(
        enabled=bool(raw.get("enabled", False)),
        window_days=_as_int(
            raw.get("window_days", SemanticDedupSettings.window_days),
            "semantic_dedup.window_days",
            SemanticDedupSettings.window_days,
        ),
    )


def _parse_general(raw: Dict) -> GeneralSettings:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError("Секция general должна быть словарём")
    rotation = _parse_log_rotation(raw.get("log_rotation", {}))
    semantic_dedup = _parse_semantic_dedup(raw.get("semantic_dedup", {}))
    blocked = raw.get("blocked_keywords", []) or []
    blocked_list = [str(k).strip() for k in blocked if str(k).strip()]

    cron = str(raw.get("cron", GeneralSettings.cron) or "").strip()
    if not cron:
        raise ConfigError("general.cron не должен быть пустым")

    return GeneralSettings(
        cron=cron,
        vk_api_version=str(raw.get("vk_api_version", GeneralSettings.vk_api_version)),
        posts_limit=_as_int(raw.get("posts_limit", GeneralSettings.posts_limit), "general.posts_limit", GeneralSettings.posts_limit),
        cache_file=raw.get("cache_file", GeneralSettings.cache_file),
        log_file=raw.get("log_file", GeneralSettings.log_file),
        log_level=raw.get("log_level", GeneralSettings.log_level),
        log_rotation=rotation,
        blocked_keywords=blocked_list,
        refresh_avatars=bool(raw.get("refresh_avatars", True)),
        log_retention_days=_as_int(raw.get("log_retention_days", 2), "general.log_retention_days", 2),
        semantic_dedup=semantic_dedup,
    )


def _parse_vk(raw: Dict) -> VKSettings:
    return VKSettings()


def _parse_telegram(raw: Dict, require_channel: bool = True) -> TelegramSettings:
    channel = raw.get("channel_id", "") if isinstance(raw, dict) else ""
    if require_channel and not channel:
        raise ConfigError("Telegram channel_id is required in config under telegram.channel_id.")
    return TelegramSettings(channel_id=str(channel))


def _parse_llm(raw: Dict) -> LLMSettings:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError("Секция llm должна быть словарём")
    return LLMSettings(
        base_url=str(raw.get("base_url", "") or "").strip(),
        model=str(raw.get("model", "") or "").strip(),
        prompt=str(raw.get("prompt", "") or ""),
    )


def _parse_content_types(raw: Optional[Dict]) -> ContentTypes:
    raw = raw or {}
    return ContentTypes(
        text=bool(raw.get("text", True)),
        photo=bool(raw.get("photo", True)),
        video=bool(raw.get("video", True)),
        audio=bool(raw.get("audio", True)),
        link=bool(raw.get("link", True)),
    )


def _parse_communities(raw_list: Optional[List[Dict]]) -> List[Community]:
    if not raw_list:
        return []
    communities: List[Community] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            raise ConfigError("Каждое сообщество должно быть словарём")
        community_id = str(raw.get("id") or "").strip()
        if not community_id:
            raise ConfigError("У сообщества не задан id")
        content_types = _parse_content_types(raw.get("content_types"))
        communities.append(
            Community(
                id=community_id,
                name=str(raw.get("name", "")),
                active=bool(raw.get("active", True)),
                content_types=content_types,
            )
        )
    return communities


def parse_config_dict(
    raw: Dict,
    require_tokens: bool = True,
    require_channel: bool = True,
    require_communities: bool = False,
) -> Config:
    general = _parse_general(raw.get("general", {}))
    vk = _parse_vk(raw.get("vk", {}))
    telegram = _parse_telegram(raw.get("telegram", {}), require_channel=require_channel)
    llm = _parse_llm(raw.get("llm", {}))
    communities = _parse_communities(raw.get("communities"))
    if require_communities and not communities:
        raise ConfigError("Config must define at least one community under `communities`.")
    return Config(general=general, vk=vk, telegram=telegram, llm=llm, communities=communities)


def load_config(
    path: str | Path,
    require_tokens: bool = True,
    require_channel: bool = True,
    require_communities: bool = False,
    allow_missing: bool = False,
) -> Config:
    raw = _load_yaml(Path(path), allow_missing=allow_missing)
    return parse_config_dict(
        raw,
        require_tokens=require_tokens,
        require_channel=require_channel,
        require_communities=require_communities,
    )


def config_to_dict(config: Config) -> Dict:
    return {
        "general": {
            "cron": config.general.cron,
            "vk_api_version": config.general.vk_api_version,
            "posts_limit": config.general.posts_limit,
            "cache_file": config.general.cache_file,
            "log_file": config.general.log_file,
            "log_level": config.general.log_level,
            "log_rotation": {
                "max_bytes": config.general.log_rotation.max_bytes,
                "backup_count": config.general.log_rotation.backup_count,
            },
            "blocked_keywords": config.general.blocked_keywords,
            "refresh_avatars": config.general.refresh_avatars,
            "log_retention_days": config.general.log_retention_days,
            "semantic_dedup": {
                "enabled": config.general.semantic_dedup.enabled,
                "window_days": config.general.semantic_dedup.window_days,
            },
        },
        "vk": {},
        "telegram": {"channel_id": config.telegram.channel_id},
        "llm": {"base_url": config.llm.base_url, "model": config.llm.model, "prompt": config.llm.prompt},
        "communities": [
            {
                "id": community.id,
                "name": community.name,
                "active": community.active,
                "content_types": {
                    "text": community.content_types.text,
                    "photo": community.content_types.photo,
                    "video": community.content_types.video,
                    "audio": community.content_types.audio,
                    "link": community.content_types.link,
                },
            }
            for community in config.communities
        ],
    }


def default_config_dict() -> Dict:
    return config_to_dict(default_config())


def save_config_dict(data: Dict, path: str | Path) -> None:
    """Write config atomically so a concurrent reader never sees a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
