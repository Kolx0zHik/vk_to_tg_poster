from __future__ import annotations

import os
import json
import time
from pathlib import Path
from typing import List, Optional

import yaml
import requests
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

from .backfill import BackfillRequests, requests_path_for
from .config import ConfigError, config_to_dict, load_config, parse_config_dict, save_config_dict
from .envfile import load_env_file
from .logger import redact_secrets
from .version import get_version
from .vk_ids import normalize_display_id

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", BASE_DIR / "data/config.yaml"))
AVATAR_CACHE = BASE_DIR / "data/avatars.json"
AVATAR_TTL_SECONDS = 24 * 3600

app = FastAPI(title="VK → Telegram Poster", version=get_version())
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

load_env_file(CONFIG_PATH)


class LogRotationModel(BaseModel):
    max_bytes: int = Field(10 * 1024 * 1024, ge=1024)
    backup_count: int = Field(5, ge=1)


class SemanticDedupModel(BaseModel):
    enabled: bool = False
    window_days: int = Field(4, ge=1, le=30)


class GeneralModel(BaseModel):
    cron: str
    vk_api_version: str = "5.199"
    posts_limit: int = Field(10, ge=1, le=100)
    cache_file: str = "data/cache.json"
    log_file: str = "data/logs/poster.log"
    log_level: str = Field("INFO", pattern=r"(?i)^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    log_rotation: LogRotationModel = LogRotationModel()
    blocked_keywords: List[str] = Field(default_factory=list)
    refresh_avatars: bool = True
    log_retention_days: int = Field(2, ge=0)
    semantic_dedup: SemanticDedupModel = SemanticDedupModel()

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


class LLMModel(BaseModel):
    base_url: str = ""
    model: str = ""
    prompt: str = ""


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
    llm: LLMModel = LLMModel()
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
    return normalize_display_id(raw)


def _fetch_vk_info(value: str) -> dict | None:
    """
    Возвращает словарь с name и photo для сообщества/пользователя VK.
    Требует VK_API_TOKEN в окружении (.env).
    """
    norm = _normalize_owner_id(value)
    if not norm:
        return None

    # токен только из env/.env, из конфига секреты убраны
    try:
        cfg = load_config(
            CONFIG_PATH,
            require_tokens=False,
            require_channel=False,
            require_communities=False,
            allow_missing=True,
        )
        token = os.getenv("VK_API_TOKEN", "")
        api_version = cfg.general.vk_api_version
    except Exception:
        token = os.getenv("VK_API_TOKEN", "")
        api_version = "5.199"

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
            return {
                "id": f"-{gid}" if gid else f"-{group_id}",
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
    config = load_config(
        CONFIG_PATH,
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


def _get_cached_community_info(value: str, refresh_avatars: bool) -> dict | None:
    cache_key = _normalize_owner_id(value).strip().lower()
    if not cache_key:
        return None

    cached = _read_avatar_cache().get(cache_key)
    if not cached:
        return None

    fetched_at = int(cached.get("fetched_at") or 0)
    is_fresh = fetched_at > 0 and (int(time.time()) - fetched_at) < AVATAR_TTL_SECONDS
    if refresh_avatars and not is_fresh:
        return None

    return {
        "id": value,
        "name": cached.get("name") or "",
        "photo": cached.get("photo"),
    }


def _cleanup_cache(config_dict: dict) -> None:
    # Пока не трогаем last_seen при изменении сообществ, чтобы не потерять состояние
    return


def _backfill_store() -> BackfillRequests:
    cache_file = "data/cache.json"
    try:
        config_dict = _load_ui_config()
        cache_file = (config_dict.get("general") or {}).get("cache_file") or cache_file
    except Exception:
        pass
    return BackfillRequests(requests_path_for(cache_file))


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
    index_path = BASE_DIR / "static" / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>static/index.html не найден</h1>")


@app.get("/api/config")
async def get_config() -> dict:
    load_env_file(CONFIG_PATH)
    data = _load_ui_config()
    data["version"] = get_version()
    data["vk"] = {"token_set": bool(os.getenv("VK_API_TOKEN"))}
    data["telegram"] = {
        "channel_id": data.get("telegram", {}).get("channel_id", ""),
        "bot_token_set": bool(os.getenv("TELEGRAM_BOT_TOKEN")),
    }
    data["llm_api_key_set"] = bool(os.getenv("LLM_API_KEY"))
    data["avatar_cache"] = _read_avatar_cache()
    return data


@app.post("/api/config")
async def save_config(payload: SaveRequest) -> dict:
    communities = []
    seen_ids = set()
    for community in payload.communities:
        community_id = _normalize_owner_id(community.id)
        if not community_id:
            raise HTTPException(
                status_code=400,
                detail={"message": "У сообщества не задан id", "field": "communities"},
            )
        if community_id in seen_ids:
            raise HTTPException(
                status_code=400,
                detail={"message": f"Сообщество {community_id} указано дважды", "field": "communities"},
            )
        seen_ids.add(community_id)
        entry = community.model_dump()
        entry["id"] = community_id
        communities.append(entry)

    merged = {
        "general": payload.general.model_dump(),
        "vk": {},
        "telegram": {
            "channel_id": payload.telegram.channel_id,
        },
        "llm": {
            "base_url": payload.llm.base_url.strip(),
            "model": payload.llm.model.strip(),
            "prompt": payload.llm.prompt,
        },
        "communities": communities,
    }

    try:
        # Validate structure; tokens now come from the environment only.
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


@app.delete("/api/community/{community_id}")
async def delete_community(community_id: str) -> dict:
    community_id = _normalize_owner_id(community_id)
    if not community_id:
        raise HTTPException(
            status_code=400,
            detail={"message": "Не указано сообщество", "field": "community_id"},
        )
    
    # Загрузить текущий конфиг
    current = _read_raw_config(CONFIG_PATH)
    communities = current.get("communities", [])
    
    # Удалить сообщество
    filtered_communities = [
        c for c in communities 
        if _normalize_owner_id(c.get("id", "")) != community_id
    ]
    
    if len(filtered_communities) == len(communities):
        raise HTTPException(
            status_code=404,
            detail={"message": "Сообщество не найдено", "field": "community_id"},
        )
    
    # Сохранить обновленную конфигурацию
    merged = {
        "general": current.get("general", {}),
        "vk": current.get("vk", {}),
        "telegram": current.get("telegram", {}),
        "llm": current.get("llm", {}),
        "communities": filtered_communities,
    }
    
    try:
        parse_config_dict(
            merged,
            require_tokens=False,
            require_channel=False,
            require_communities=False,
        )
    except ConfigError as exc:
        raise HTTPException(
            status_code=400,
            detail={"message": str(exc)},
        )
    
    save_config_dict(merged, CONFIG_PATH)
    _cleanup_cache(merged)
    return {"ok": True, "deleted_id": community_id}


@app.get("/api/community_info")
async def community_info(value: str) -> dict:
    try:
        cfg = load_config(
            CONFIG_PATH,
            require_tokens=False,
            require_channel=False,
            require_communities=False,
            allow_missing=True,
        )
        refresh_avatars = cfg.general.refresh_avatars
    except Exception:
        refresh_avatars = True

    cached = _get_cached_community_info(value, refresh_avatars=refresh_avatars)
    if cached:
        return cached

    info = _fetch_vk_info(value)
    if not info:
        return cached or {"id": value, "name": "", "photo": None}
    cache = _read_avatar_cache()
    cache_key = _normalize_owner_id(info.get("id") or value).strip().lower()
    cache[cache_key] = {
        "photo": info.get("photo"),
        "name": info.get("name") or "",
        "fetched_at": int(time.time()),
    }
    _save_avatar_cache(cache)
    return {"id": info.get("id") or value, "name": info.get("name") or "", "photo": info.get("photo")}

class BackfillModel(BaseModel):
    id: str
    mode: str = "none"
    value: int = 0


@app.post("/api/backfill")
async def set_backfill(payload: BackfillModel) -> dict:
    community_id = _normalize_owner_id(payload.id)
    if not community_id:
        raise HTTPException(
            status_code=400,
            detail={"message": "Не указано сообщество", "field": "id"},
        )

    mode = (payload.mode or "none").strip().lower()
    if mode not in {"none", "posts", "days"}:
        raise HTTPException(
            status_code=400,
            detail={"message": "Неизвестный режим дозаливки", "field": "mode"},
        )

    raw_value = int(payload.value or 0)
    if mode == "posts":
        value = max(1, min(100, raw_value))
    elif mode == "days":
        value = max(1, min(365, raw_value))
    else:
        value = 0

    _backfill_store().request(community_id, mode, value)
    return {"ok": True, "id": community_id, "mode": mode, "value": value}


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
        log_path = Path("data/logs/poster.log")

    if not log_path.exists():
        return {"lines": [], "path": str(log_path)}

    tail = _tail_lines(log_path, lines)
    return {"lines": [redact_secrets(line) for line in tail], "path": str(log_path)}
