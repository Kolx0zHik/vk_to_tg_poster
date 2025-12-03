from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml


@dataclass
class LogRotationSettings:
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5


@dataclass
class GeneralSettings:
    cron: str = "*/10 * * * *"
    vk_api_version: str = "5.199"
    posts_limit: int = 10
    cache_file: str = "data/cache.json"
    log_file: str = "logs/poster.log"
    log_level: str = "INFO"
    log_rotation: LogRotationSettings = field(default_factory=LogRotationSettings)


@dataclass
class VKSettings:
    token: str


@dataclass
class TelegramSettings:
    bot_token: str
    channel_id: str


@dataclass
class ContentTypes:
    text: bool = True
    photo: bool = True
    video: bool = True
    audio: bool = True
    link: bool = True


@dataclass
class Community:
    id: int
    name: str
    active: bool = True
    content_types: ContentTypes = field(default_factory=ContentTypes)


@dataclass
class Config:
    general: GeneralSettings
    vk: VKSettings
    telegram: TelegramSettings
    communities: List[Community]


class ConfigError(Exception):
    pass


def _load_yaml(path: Path) -> Dict:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _parse_log_rotation(raw: Dict) -> LogRotationSettings:
    return LogRotationSettings(
        max_bytes=int(raw.get("max_bytes", LogRotationSettings.max_bytes)),
        backup_count=int(raw.get("backup_count", LogRotationSettings.backup_count)),
    )


def _parse_general(raw: Dict) -> GeneralSettings:
    rotation = _parse_log_rotation(raw.get("log_rotation", {}))
    return GeneralSettings(
        cron=raw.get("cron", GeneralSettings.cron),
        vk_api_version=str(raw.get("vk_api_version", GeneralSettings.vk_api_version)),
        posts_limit=int(raw.get("posts_limit", GeneralSettings.posts_limit)),
        cache_file=raw.get("cache_file", GeneralSettings.cache_file),
        log_file=raw.get("log_file", GeneralSettings.log_file),
        log_level=raw.get("log_level", GeneralSettings.log_level),
        log_rotation=rotation,
    )


def _parse_vk(raw: Dict) -> VKSettings:
    token = os.getenv("VK_API_TOKEN", raw.get("token", ""))
    if not token:
        raise ConfigError("VK API token is required. Set VK_API_TOKEN or provide vk.token in config.")
    return VKSettings(token=token)


def _parse_telegram(raw: Dict) -> TelegramSettings:
    token = os.getenv("TELEGRAM_BOT_TOKEN", raw.get("bot_token", ""))
    if not token:
        raise ConfigError(
            "Telegram bot token is required. Set TELEGRAM_BOT_TOKEN or provide telegram.bot_token in config."
        )
    channel = raw.get("channel_id")
    if not channel:
        raise ConfigError("Telegram channel_id is required in config under telegram.channel_id.")
    return TelegramSettings(bot_token=token, channel_id=str(channel))


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
        content_types = _parse_content_types(raw.get("content_types"))
        communities.append(
            Community(
                id=int(raw.get("id")),
                name=str(raw.get("name", "")),
                active=bool(raw.get("active", True)),
                content_types=content_types,
            )
        )
    return communities


def load_config(path: str | Path) -> Config:
    raw = _load_yaml(Path(path))
    general = _parse_general(raw.get("general", {}))
    vk = _parse_vk(raw.get("vk", {}))
    telegram = _parse_telegram(raw.get("telegram", {}))
    communities = _parse_communities(raw.get("communities"))
    if not communities:
        raise ConfigError("Config must define at least one community under `communities`.")
    return Config(general=general, vk=vk, telegram=telegram, communities=communities)
