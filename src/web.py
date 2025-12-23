from __future__ import annotations

import os
import json
import time
import re
from urllib.parse import unquote
from pathlib import Path
from typing import List, Optional

import yaml
import requests
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, Field, field_validator

from .config import ConfigError, config_to_dict, load_config, parse_config_dict, save_config_dict

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", BASE_DIR / "config/config.yaml"))
EXAMPLE_CONFIG = BASE_DIR / "config/config.example.yaml"
AVATAR_CACHE = BASE_DIR / "data/avatars.json"
AVATAR_TTL_SECONDS = 24 * 3600
CLIENT_DIR = BASE_DIR / "client"

app = FastAPI(title="VK → Telegram Poster", version="0.1.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class LogRotationModel(BaseModel):
    max_bytes: int = Field(10 * 1024 * 1024, ge=1024)
    backup_count: int = Field(5, ge=1)


class GeneralModel(BaseModel):
    cron: str
    vk_api_version: str = "5.199"
    posts_limit: int = Field(10, ge=1, le=100)
    cache_file: str = "data/cache.json"
    log_file: str = "logs/poster.log"
    log_level: str = Field("INFO", pattern=r"(?i)^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    log_rotation: LogRotationModel = LogRotationModel()
    blocked_keywords: List[str] = Field(default_factory=list)
    refresh_avatars: bool = True

    @field_validator("cron")
    @classmethod
    def cron_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Cron выражение не должно быть пустым")
        return value


class TokenModel(BaseModel):
    token: str = ""


class TelegramModel(BaseModel):
    channel_id: str = ""
    bot_token: str = ""


class ContentTypesModel(BaseModel):
    text: bool = True
    photo: bool = True
    video: bool = True
    audio: bool = True
    link: bool = True


class CommunityModel(BaseModel):
    id: str
    name: str
    active: bool = True
    content_types: ContentTypesModel = ContentTypesModel()

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Имя сообщества не может быть пустым")
        return value


class SaveRequest(BaseModel):
    general: GeneralModel
    vk: TokenModel = TokenModel()
    telegram: TelegramModel
    communities: List[CommunityModel] = Field(default_factory=list)

    @field_validator("communities")
    @classmethod
    def unique_ids(cls, value: List[CommunityModel]) -> List[CommunityModel]:
        ids = [c.id.strip().lower() for c in value]
        if len(ids) != len(set(ids)):
            raise ValueError("ID сообществ должны быть уникальны")
        return value


def _read_raw_config(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _normalize_owner_id(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    lower = value.lower()
    # убираем параметры и якоря
    lower = lower.split("?", 1)[0].split("#", 1)[0]
    for prefix in ("https://vk.com/", "http://vk.com/"):
        if lower.startswith(prefix):
            lower = lower.replace(prefix, "")
    lower = lower.strip("/")
    if "/" in lower:
        lower = lower.split("/", 1)[0]
    lower = unquote(lower)
    lower = re.sub(r"[^a-z0-9._-]+", "", lower)
    if not lower:
        return ""
    for prefix in ("club", "public", "event"):
        if lower.startswith(prefix) and lower[len(prefix) :].isdigit():
            return f"-{lower[len(prefix) :]}"
    if lower.startswith("id") and lower[2:].isdigit():
        return lower[2:]
    if lower.lstrip("-").isdigit():
        return lower
    return lower[:64]


def _fetch_vk_info(value: str) -> dict | None:
    """
    Возвращает словарь с name и photo для сообщества/пользователя VK.
    Требует VK_API_TOKEN в окружении или token в config.yaml.
    """
    norm = _normalize_owner_id(value)
    if not norm:
        return None

    # токен и версия API
    try:
        cfg = load_config(
            CONFIG_PATH,
            require_tokens=False,
            require_channel=False,
            require_communities=False,
            allow_missing=True,
        )
        token = os.getenv("VK_API_TOKEN", cfg.vk.token)
        api_version = cfg.general.vk_api_version
        refresh_avatars = cfg.general.refresh_avatars
    except Exception:
        token = os.getenv("VK_API_TOKEN", "")
        api_version = "5.199"
        refresh_avatars = True

    if not token:
        return None

    def _call(method: str, params: dict):
        base = {"access_token": token, "v": api_version}
        resp = requests.get(f"https://api.vk.com/method/{method}", params={**base, **params}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(data["error"])
        response = data.get("response") or {}
        # Некоторые методы оборачивают в {"groups": [...]} или {"profiles": [...]}
        if isinstance(response, dict):
            if "groups" in response:
                return response.get("groups") or []
            if "profiles" in response:
                return response.get("profiles") or []
        return response

    def _group_info(group_id: str) -> dict | None:
        resp = _call("groups.getById", {"group_id": group_id, "fields": "photo_200,photo_100,name"})
        if isinstance(resp, list) and resp:
            item = resp[0]
            gid = item.get("id")
            screen = item.get("screen_name") or ""
            return {
                "id": screen or (f"-{gid}" if gid else f"-{group_id}"),
                "name": item.get("name") or "",
                "photo": item.get("photo_200") or item.get("photo_100"),
            }
        return None

    def _user_info(user_id: str) -> dict | None:
        resp = _call("users.get", {"user_ids": user_id, "fields": "photo_200,photo_100,first_name,last_name"})
        if isinstance(resp, list) and resp:
            item = resp[0]
            return {
                "id": str(item.get("id") or user_id),
                "name": f"{item.get('first_name','')} {item.get('last_name','')}".strip(),
                "photo": item.get("photo_200") or item.get("photo_100"),
            }
        return None

    try:
        # 1) resolve screen name
        resolved = _call("utils.resolveScreenName", {"screen_name": norm.lstrip("-")})
        obj_type = resolved.get("type")
        obj_id = resolved.get("object_id")
        if obj_type in {"group", "page", "event"} and obj_id:
            info = _group_info(str(obj_id))
            if info:
                return info
        if obj_type == "user" and obj_id:
            info = _user_info(str(obj_id))
            if info:
                return info

        # 2) если не удалось — пробуем как числовой id группы
        if norm.lstrip("-").isdigit():
            info = _group_info(norm.lstrip("-"))
            if info:
                return info

        # 3) fallback: groups.getById с переданным значением как screen_name
        info = _group_info(norm)
        if info:
            return info
    except Exception:
        return None
    return None


def _load_ui_config() -> dict:
    try:
        config = load_config(
            CONFIG_PATH,
            require_tokens=False,
            require_channel=False,
            require_communities=False,
            allow_missing=True,
        )
    except ConfigError:
        fallback = EXAMPLE_CONFIG if EXAMPLE_CONFIG.exists() else CONFIG_PATH
        config = load_config(
            fallback,
            require_tokens=False,
            require_channel=False,
            require_communities=False,
            allow_missing=True,
        )
    return config_to_dict(config)


def _read_avatar_cache() -> dict:
    if not AVATAR_CACHE.exists():
        return {}
    try:
        with AVATAR_CACHE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_avatar_cache(data: dict) -> None:
    AVATAR_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with AVATAR_CACHE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _cleanup_cache(config_dict: dict) -> None:
    # Пока не трогаем last_seen при изменении сообществ, чтобы не потерять состояние
    return


def _tail_lines(path: Path, lines: int) -> list[str]:
    if lines <= 0:
        return []
    block_size = 8192
    buffer = b""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        while pos > 0 and buffer.count(b"\n") <= lines:
            read_size = block_size if pos >= block_size else pos
            pos -= read_size
            f.seek(pos)
            buffer = f.read(read_size) + buffer
    text = buffer.decode("utf-8", errors="ignore")
    parts = text.splitlines(keepends=True)
    return parts[-lines:] if len(parts) > lines else parts


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    index_path = CLIENT_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>client/index.html не найден</h1>")


@app.get("/style.css")
async def client_styles() -> FileResponse:
    return FileResponse(CLIENT_DIR / "style.css")


@app.get("/script.js")
async def client_script() -> FileResponse:
    return FileResponse(CLIENT_DIR / "script.js")


@app.get("/api/config")
async def get_config() -> dict:
    data = _load_ui_config()
    vk_token_set = bool(data.get("vk", {}).get("token") or os.getenv("VK_API_TOKEN"))
    tg_token_set = bool(data.get("telegram", {}).get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN"))
    data["vk"] = {"token_set": vk_token_set}
    data["telegram"] = {"channel_id": data.get("telegram", {}).get("channel_id", ""), "bot_token_set": tg_token_set}
    data["avatar_cache"] = _read_avatar_cache()
    return data


@app.post("/api/config")
async def save_config(payload: SaveRequest) -> dict:
    current = _read_raw_config(CONFIG_PATH)

    merged = {
        "general": payload.general.model_dump(),
        "vk": {"token": payload.vk.token or current.get("vk", {}).get("token", "")},
        "telegram": {
            "channel_id": payload.telegram.channel_id,
            "bot_token": payload.telegram.bot_token or current.get("telegram", {}).get("bot_token", ""),
        },
        "communities": [community.model_dump() for community in payload.communities],
    }

    try:
        # Validate structure; tokens may be пустыми, но канал обязателен.
        parse_config_dict(
            merged,
            require_tokens=False,
            require_channel=False,
            require_communities=False,
        )
    except ConfigError as exc:
        raise HTTPException(
            status_code=400,
            detail={"message": str(exc), "field": "telegram.channel_id"},
        )

    save_config_dict(merged, CONFIG_PATH)
    _cleanup_cache(merged)
    return {"ok": True}


@app.get("/api/community_info")
async def community_info(value: str) -> dict:
    info = _fetch_vk_info(value)
    if not info:
        return {"id": value, "name": "", "photo": None}
    cache = _read_avatar_cache()
    cache_key = (info.get("id") or value).strip().lower()
    cache[cache_key] = {
        "photo": info.get("photo"),
        "name": info.get("name") or "",
        "fetched_at": int(time.time()),
    }
    _save_avatar_cache(cache)
    return {"id": info.get("id") or value, "name": info.get("name") or "", "photo": info.get("photo")}

@app.get("/api/logs")
async def get_logs(lines: int = 200) -> dict:
    try:
        cfg = load_config(
            CONFIG_PATH,
            require_tokens=False,
            require_channel=False,
            require_communities=False,
            allow_missing=True,
        )
        log_path = Path(cfg.general.log_file)
    except Exception:
        log_path = Path("logs/poster.log")

    if not log_path.exists():
        return {"lines": [], "path": str(log_path)}

    tail = _tail_lines(log_path, lines)
    return {"lines": tail, "path": str(log_path)}
