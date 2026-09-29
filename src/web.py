from __future__ import annotations

import os
import json
import re
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
from .config import ConfigError, config_to_dict, load_config, parse_config_dict, save_config_dict, validate_timezone
from .envfile import load_env_file
from .journal import RunJournal, journal_path_for, redact_tree
from .logger import redact_secrets
from .version import get_version
from .vk_ids import normalize_display_id

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", BASE_DIR / "data/config.yaml"))
AVATAR_CACHE = BASE_DIR / "data/avatars.json"
AVATAR_TTL_SECONDS = 24 * 3600
LLM_MODELS_TIMEOUT = 15

app = FastAPI(title="VK → Telegram Poster", version=get_version())
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

load_env_file(CONFIG_PATH)


class LogRotationModel(BaseModel):
    max_bytes: int = Field(10 * 1024 * 1024, ge=1024)
    backup_count: int = Field(5, ge=1)


class SemanticDedupModel(BaseModel):
    enabled: bool = False
    window_days: int = Field(4, ge=1, le=30)
    debug_log: bool = False


class GeneralModel(BaseModel):
    cron: str
    vk_api_version: str = "5.199"
    posts_limit: int = Field(10, ge=1, le=100)
    cache_file: str = "data/cache.json"
    log_file: str = "data/logs/poster.log"
    log_level: str = Field("INFO", pattern=r"(?i)^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    timezone: str = "Europe/Moscow"
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

    @field_validator("timezone")
    @classmethod
    def timezone_valid(cls, value: str) -> str:
        try:
            return validate_timezone(value)
        except ConfigError as exc:
            raise ValueError(str(exc)) from None


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


_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+\[(?P<level>[A-Z]+)\]\s?(?P<message>.*)$"
)
_LEVEL_ORDER = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def parse_log_lines(raw_lines: list[str]) -> list[dict]:
    """Turn compact log lines into ``{ts, level, message}`` records.

    Continuation lines (a traceback body, a multi-line message) are appended to
    the previous record so the panel can render each event as one block.
    """
    entries: list[dict] = []
    for raw in raw_lines:
        line = raw.rstrip("\n")
        match = _LOG_LINE_RE.match(line)
        if match:
            entries.append(
                {
                    "ts": match.group("ts"),
                    "level": match.group("level"),
                    "message": match.group("message"),
                }
            )
        elif entries:
            entries[-1]["message"] += "\n" + line
        elif line:
            entries.append({"ts": "", "level": "", "message": line})
    return entries


def filter_log_entries(entries: list[dict], level: str, query: str) -> list[dict]:
    """Filter parsed log records by minimum level and a case-insensitive substring."""
    wanted = (level or "").strip().upper()
    if wanted in _LEVEL_ORDER:
        threshold = _LEVEL_ORDER.index(wanted)
        entries = [
            entry
            for entry in entries
            if entry.get("level") in _LEVEL_ORDER and _LEVEL_ORDER.index(entry["level"]) >= threshold
        ]
    needle = (query or "").strip().lower()
    if needle:
        entries = [entry for entry in entries if needle in str(entry.get("message", "")).lower()]
    return entries


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
    if payload.general.semantic_dedup.enabled and not payload.llm.prompt.strip():
        raise HTTPException(
            status_code=400,
            detail={"message": "Для ИИ-проверки дублей нужно задать системный промпт", "field": "llm.prompt"},
        )

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


def _normalize_llm_base_url(value: str) -> str:
    value = (value or "").strip().rstrip("/")
    if value.endswith("/chat/completions"):
        value = value[: -len("/chat/completions")].rstrip("/")
    return value


@app.get("/api/llm/models")
async def llm_models(base_url: str = "") -> dict:
    """List models from the configured OpenAI-compatible provider using LLM_API_KEY.

    The provider host is never taken from the query string: the key is only ever
    sent to the host saved in ``llm.base_url``. A client-supplied ``base_url``
    that differs from the saved one is rejected, so the panel cannot be used to
    leak the key to an arbitrary host (see ADR-024).
    """
    load_env_file(CONFIG_PATH)
    key = os.getenv("LLM_API_KEY", "").strip()
    if not key:
        raise HTTPException(status_code=401, detail={"message": "Ключ LLM_API_KEY не задан в .env"})

    try:
        cfg = load_config(
            CONFIG_PATH,
            require_tokens=False,
            require_channel=False,
            require_communities=False,
            allow_missing=True,
        )
        configured_base_url = _normalize_llm_base_url(cfg.llm.base_url)
    except Exception:
        configured_base_url = ""
    if not configured_base_url:
        raise HTTPException(status_code=400, detail={"message": "Укажите Base URL провайдера и сохраните настройки"})

    requested = _normalize_llm_base_url(base_url) if base_url.strip() else configured_base_url
    if requested != configured_base_url:
        raise HTTPException(
            status_code=400,
            detail={"message": "Сначала сохраните Base URL, затем загрузите список моделей"},
        )
    target = configured_base_url
    if not target.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail={"message": "Base URL должен начинаться с http:// или https://"})

    url = f"{target.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        resp = requests.get(url, headers=headers, timeout=LLM_MODELS_TIMEOUT)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail={"message": f"Не удалось подключиться к провайдеру: {type(exc).__name__}"}) from None
    if resp.status_code in {401, 403}:
        raise HTTPException(status_code=401, detail={"message": "Провайдер отклонил ключ LLM_API_KEY"})
    if not resp.ok:
        raise HTTPException(status_code=502, detail={"message": f"Провайдер вернул HTTP {resp.status_code}"})
    try:
        data = resp.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail={"message": "Провайдер вернул не-JSON ответ"}) from None

    raw = data.get("data") if isinstance(data, dict) else None
    models = []
    if isinstance(raw, list):
        for item in raw:
            model_id = item.get("id") if isinstance(item, dict) else item
            if isinstance(model_id, str) and model_id.strip():
                models.append(model_id.strip())
    return {"models": sorted(set(models)), "base_url": target.rstrip("/")}


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
async def get_logs(lines: int = 200, level: str = "", q: str = "") -> dict:
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
        return {"lines": [], "entries": [], "path": str(log_path)}

    tail = _tail_lines(log_path, lines)
    entries = filter_log_entries(parse_log_lines(tail), level, q)
    return {
        "lines": [redact_secrets(line) for line in tail],
        "entries": [
            {
                "ts": entry.get("ts", ""),
                "level": entry.get("level", ""),
                "message": redact_secrets(entry.get("message", "")),
            }
            for entry in entries
        ],
        "path": str(log_path),
    }


@app.get("/api/journal")
async def get_journal(runs: int = 10) -> dict:
    try:
        cfg = load_config(
            CONFIG_PATH,
            require_tokens=False,
            require_channel=False,
            require_communities=False,
            allow_missing=True,
        )
        journal_path = journal_path_for(cfg.general.cache_file)
    except Exception:
        journal_path = journal_path_for("data/cache.json")

    limit = max(1, min(int(runs), 50))
    items = RunJournal(journal_path).recent(limit=limit)
    return {"runs": redact_tree(items), "path": str(journal_path)}
