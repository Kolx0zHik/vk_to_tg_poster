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
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

from .config import ConfigError, config_to_dict, load_config, parse_config_dict, save_config_dict

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", BASE_DIR / "config/config.yaml"))
EXAMPLE_CONFIG = BASE_DIR / "config/config.example.yaml"
AVATAR_CACHE = BASE_DIR / "data/avatars.json"
AVATAR_TTL_SECONDS = 24 * 3600

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
    return HTMLResponse(content=INDEX_HTML)


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


INDEX_HTML = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>VK → Telegram Poster · Настройки</title>
  <link rel="icon" type="image/png" href="/static/logo.png" />
  <link rel="apple-touch-icon" href="/static/logo.png" />
  <link rel="preconnect" href="https://fonts.gstatic.com" />
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: radial-gradient(120% 120% at 20% 10%, #f6fbff 0%, #eef3fb 40%, #e6eef8 75%, #e1eaf6 100%);
      --card: rgba(255,255,255,0.75);
      --stroke: rgba(22, 43, 76, 0.12);
      --accent: #34c38f;
      --accent-2: #3a86ff;
      --danger: #ff6b6b;
      --text: #0f1c2e;
      --muted: #4e647f;
      --shadow: 0 18px 36px rgba(24, 44, 78, 0.12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: 'Space Grotesk', system-ui, -apple-system, sans-serif;
      background: var(--bg);
      color: var(--text);
      padding: 24px;
    }
    .page {
      max-width: 1100px;
      margin: 0 auto;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }
    .hero {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 20px;
      border: 1px solid var(--stroke);
      background: linear-gradient(135deg, rgba(58,134,255,0.14), rgba(52,195,143,0.12));
      border-radius: 18px;
      box-shadow: var(--shadow);
    }
    .hero h1 {
      margin: 0 0 6px;
      font-size: 24px;
      letter-spacing: -0.02em;
    }
    .hero p { margin: 0; color: var(--muted); }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .brand img {
      width: 48px;
      height: 48px;
      border-radius: 14px;
      border: 1px solid var(--stroke);
      background: rgba(255,255,255,0.08);
      box-shadow: var(--shadow);
    }
    .badge-row { display: flex; gap: 10px; flex-wrap: wrap; }
    .badge {
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 13px;
      border: 1px solid var(--stroke);
      background: rgba(255,255,255,0.6);
    }
    .pill {
      background: rgba(52,195,143,0.16);
      border-color: rgba(52,195,143,0.35);
      color: #0b3a2b;
    }
    .row {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 14px;
      position: relative;
      z-index: 2;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--stroke);
      border-radius: 16px;
      padding: 16px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
      overflow: visible;
      position: relative;
      z-index: 1;
    }
    .card.raise { z-index: 10; }
    .card h3 {
      margin: 0 0 12px;
      font-size: 16px;
      letter-spacing: -0.01em;
    }
    label {
      display: block;
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 6px;
    }
    input[type="text"], input[type="number"], input[type="password"], select {
      width: 100%;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid var(--stroke);
      background: rgba(255,255,255,0.8);
      color: var(--text);
      font-size: 14px;
      outline: none;
      transition: border-color 0.2s ease, transform 0.15s ease;
    }
    input[type=number]::-webkit-outer-spin-button,
    input[type=number]::-webkit-inner-spin-button {
      -webkit-appearance: none;
      margin: 0;
    }
    input[type=number] { -moz-appearance: textfield; }
    input:focus, select:focus {
      border-color: var(--accent-2);
      transform: translateY(-1px);
    }
    select {
      appearance: none;
      background:
        linear-gradient(45deg, transparent 50%, rgba(78,100,127,0.9) 50%),
        linear-gradient(135deg, rgba(78,100,127,0.9) 50%, transparent 50%),
        linear-gradient(135deg, rgba(255,255,255,0.95), rgba(236,242,251,0.95));
      background-position:
        right 16px center,
        right 10px center,
        0 0;
      background-size: 6px 6px, 6px 6px, 100% 100%;
      background-repeat: no-repeat;
      padding-right: 36px;
      color: var(--text);
      position: relative;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.6);
    }
    select:hover {
      border-color: rgba(58,134,255,0.4);
    }
    select:focus {
      box-shadow: 0 0 0 3px rgba(58,134,255,0.15);
    }
    select option { color: #0a1221; background: #f4f6fb; }
    .custom-select {
      position: relative;
      width: 100%;
    }
    .custom-select.open { z-index: 5; }
    .custom-select select {
      position: absolute;
      opacity: 0;
      pointer-events: none;
      height: 0;
      width: 0;
    }
    .select-display {
      display: flex;
      align-items: center;
      justify-content: space-between;
      width: 100%;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid var(--stroke);
      background: linear-gradient(135deg, rgba(255,255,255,0.95), rgba(236,242,251,0.95));
      color: var(--text);
      font-size: 14px;
      cursor: pointer;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.6);
      transition: border-color 0.2s ease, box-shadow 0.2s ease, transform 0.15s ease;
    }
    .select-display::after {
      content: '';
      width: 8px;
      height: 8px;
      margin-left: 10px;
      border-right: 2px solid rgba(78,100,127,0.9);
      border-bottom: 2px solid rgba(78,100,127,0.9);
      transform: rotate(45deg);
    }
    .select-display:hover {
      border-color: rgba(58,134,255,0.4);
    }
    .custom-select.open .select-display {
      border-color: var(--accent-2);
      box-shadow: 0 0 0 3px rgba(58,134,255,0.12);
      transform: translateY(-1px);
    }
    .select-options {
      position: absolute;
      top: calc(100% + 6px);
      left: 0;
      right: 0;
      border-radius: 14px;
      border: 1px solid var(--stroke);
      background: linear-gradient(135deg, rgba(255,255,255,0.95), rgba(236,242,251,0.95));
      box-shadow: var(--shadow);
      padding: 6px;
      display: none;
      z-index: 20;
      font-size: 14px;
    }
    .custom-select.open .select-options { display: grid; }
    .select-option {
      padding: 10px 12px;
      border-radius: 10px;
      cursor: pointer;
      color: var(--text);
      transition: background 0.15s ease, color 0.15s ease;
    }
    .select-option:hover {
      background: rgba(58,134,255,0.1);
    }
    .select-option.selected {
      background: rgba(52,195,143,0.16);
      color: #0b3a2b;
      font-weight: 600;
    }
    .toggle {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      padding: 6px 10px;
      border-radius: 12px;
      background: rgba(255,255,255,0.7);
      border: 1px solid var(--stroke);
      cursor: pointer;
      user-select: none;
    }
    .toggle input { display: none; }
    .dot {
      width: 34px; height: 18px;
      border-radius: 999px;
      border: 1px solid var(--stroke);
      background: rgba(15,28,46,0.08);
      position: relative;
      transition: background 0.2s ease, border-color 0.2s ease;
    }
    .dot::after {
      content: '';
      position: absolute;
      top: 2px; left: 2px;
      width: 12px; height: 12px;
      border-radius: 50%;
      background: #8aa1bc;
      transition: transform 0.2s ease, background 0.2s ease;
    }
    .toggle input:checked + .dot {
      background: rgba(52,195,143,0.2);
      border-color: rgba(52,195,143,0.6);
    }
    .toggle input:checked + .dot::after {
      transform: translateX(16px);
      background: var(--accent);
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 10px 14px;
    }
    .row-inline { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    .error {
      color: #b52a3b;
      font-size: 13px;
      margin-top: 4px;
    }
    .invalid {
      border-color: rgba(255,107,107,0.7) !important;
      box-shadow: 0 0 0 1px rgba(255,107,107,0.25);
    }
    .alert {
      position: fixed;
      top: 40px;
      left: 50%;
      transform: translateX(-50%);
      padding: 12px 16px;
      border-radius: 12px;
      background: rgba(255,255,255,0.95);
      color: #7a1725;
      border: 1px solid rgba(255,107,107,0.4);
      box-shadow: var(--shadow);
      opacity: 0;
      transition: opacity 0.2s ease;
      z-index: 1000;
    }
    .alert.show { opacity: 1; }
    .btn {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 12px 16px;
      border-radius: 12px;
      border: 1px solid var(--stroke);
      background: linear-gradient(135deg, rgba(58,134,255,0.18), rgba(52,195,143,0.12));
      color: var(--text);
      font-weight: 600;
      cursor: pointer;
      transition: transform 0.15s ease, box-shadow 0.2s ease;
      box-shadow: var(--shadow);
    }
    .btn:hover { transform: translateY(-1px) scale(1.01); }
    .btn.save {
      background: linear-gradient(135deg, #3a86ff, #34c38f);
      color: #07101d;
      border: none;
      box-shadow: 0 12px 30px rgba(58,134,255,0.25);
    }
    .btn.secondary {
      padding: 10px 14px;
      background: rgba(255,255,255,0.75);
      border: 1px solid var(--stroke);
      box-shadow: none;
      color: var(--text);
    }
    .btn.danger {
      background: linear-gradient(135deg, rgba(255,107,107,0.2), rgba(255,107,107,0.32));
      border-color: rgba(255,107,107,0.45);
      color: #7a1725;
    }
    .communities { display: grid; gap: 12px; }
    .community {
      border: 1px solid var(--stroke);
      border-radius: 14px;
      padding: 12px;
      background: linear-gradient(135deg, rgba(255,255,255,0.95), rgba(236,242,251,0.95));
      box-shadow: var(--shadow);
    }
    .community summary {
      cursor: pointer;
      font-weight: 600;
      letter-spacing: -0.01em;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      list-style: none;
      align-items: center;
      padding: 6px 4px;
    }
    .summary-title {
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }
    .avatar {
      width: 34px;
      height: 34px;
      border-radius: 50%;
      background: linear-gradient(135deg, rgba(255,255,255,0.9), rgba(241,245,251,0.85));
      border: 1px solid var(--stroke);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      font-size: 13px;
      color: var(--text);
      box-shadow: var(--shadow);
    }
    .avatar img { width: 100%; height: 100%; border-radius: 50%; object-fit: cover; display: block; }
    .badge-mini {
      padding: 4px 8px;
      border-radius: 10px;
      border: 1px solid var(--stroke);
      background: rgba(255,255,255,0.75);
      font-size: 12px;
      color: var(--muted);
    }
    .badge-mini.pill {
      background: rgba(52,195,143,0.18);
      border-color: rgba(52,195,143,0.45);
      color: #0b3a2b;
    }
    .community details > div { margin-top: 12px; }
    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      margin-top: 6px;
      margin-bottom: 4px;
    }
    .community {
      border: 1px solid var(--stroke);
      border-radius: 14px;
      padding: 12px;
      background: linear-gradient(135deg, rgba(255,255,255,0.95), rgba(236,242,251,0.95));
      display: grid;
      gap: 10px;
      box-shadow: var(--shadow);
    }
    .toast {
      position: fixed;
      bottom: 20px;
      right: 20px;
      padding: 12px 14px;
      border-radius: 12px;
      background: rgba(255,255,255,0.95);
      color: #0b3a2b;
      border: 1px solid rgba(52,195,143,0.45);
      box-shadow: var(--shadow);
      opacity: 0;
      transform: translateY(10px);
      transition: opacity 0.2s ease, transform 0.2s ease;
    }
    .toast.show { opacity: 1; transform: translateY(0); }
    .hint { color: var(--muted); font-size: 13px; margin-top: 4px; }
    @media (max-width: 720px) {
      .hero { flex-direction: column; align-items: flex-start; }
    }
    .modal {
      position: fixed;
      inset: 0;
      background: rgba(8, 16, 28, 0.35);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 1500;
    }
    .modal.show { display: flex; }
    .modal-card {
      background: rgba(255,255,255,0.92);
      border: 1px solid var(--stroke);
      border-radius: 16px;
      padding: 18px;
      width: min(540px, 92vw);
      box-shadow: var(--shadow);
      display: grid;
      gap: 12px;
    }
    .modal-card.small { width: min(360px, 92vw); }
    .avatar-large {
      width: 72px;
      height: 72px;
      border-radius: 50%;
      background: linear-gradient(135deg, rgba(255,255,255,0.9), rgba(241,245,251,0.85));
      border: 1px solid var(--stroke);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      font-size: 18px;
      color: var(--text);
      box-shadow: var(--shadow);
      object-fit: cover;
    }
    .avatar-large img { width: 100%; height: 100%; border-radius: 50%; object-fit: cover; display: block; }
    .vk-title { margin: 4px 0 0; font-size: 16px; font-weight: 600; }
    .vk-sub { margin: 0; color: var(--muted); font-size: 13px; }
    .modal-actions { display: flex; justify-content: flex-end; gap: 10px; flex-wrap: wrap; }
  </style>
</head>
<body>
    <div class="page">
    <div class="hero">
      <div class="brand">
        <img src="/static/logo.png" alt="VK → Telegram Poster logo">
        <div>
          <h1>VK → Telegram Poster</h1>
          <div class="badge-row" id="statusBadges"></div>
        </div>
      </div>
      <button class="btn save" id="saveBtn">Сохранить</button>
    </div>

    <div class="row">
      <div class="card">
        <h3>Общие параметры</h3>
        <label for="cronSimple">Частота проверки</label>
        <div class="custom-select" data-select="cronSimple">
          <select id="cronSimple">
            <option value="*/5 * * * *">Каждые 5 минут</option>
            <option value="*/10 * * * *">Каждые 10 минут</option>
            <option value="*/15 * * * *">Каждые 15 минут</option>
            <option value="*/30 * * * *">Каждые 30 минут</option>
            <option value="0 * * * *">Каждый час</option>
            <option value="custom">Кастомное cron-выражение</option>
          </select>
          <div class="select-display" data-role="display">Каждые 10 минут</div>
          <div class="select-options" data-role="options">
            <div class="select-option" data-value="*/5 * * * *">Каждые 5 минут</div>
            <div class="select-option" data-value="*/10 * * * *">Каждые 10 минут</div>
            <div class="select-option" data-value="*/15 * * * *">Каждые 15 минут</div>
            <div class="select-option" data-value="*/30 * * * *">Каждые 30 минут</div>
            <div class="select-option" data-value="0 * * * *">Каждый час</div>
            <div class="select-option" data-value="custom">Кастомное cron-выражение</div>
          </div>
        </div>
        <div id="cronCustomBlock" style="margin-top:10px; display:none;">
          <label for="cron">Кастомный cron</label>
          <input id="cron" type="text" placeholder="*/10 * * * *" />
          <div class="error" id="cronError" style="display:none;"></div>
        </div>
        <label for="limit" style="margin-top:12px; display:block;">Максимум постов за один опрос</label>
        <input id="limit" type="number" min="1" max="100" />
        <label for="logRetention" style="margin-top:12px; display:block;">Сколько дней хранить логи</label>
        <div class="custom-select" data-select="logRetention">
          <select id="logRetention">
            <option value="1">1 день</option>
            <option value="2">2 дня</option>
            <option value="3">3 дня</option>
            <option value="7">7 дней</option>
            <option value="14">14 дней</option>
            <option value="30">30 дней</option>
          </select>
          <div class="select-display" data-role="display">2 дня</div>
          <div class="select-options" data-role="options">
            <div class="select-option" data-value="1">1 день</div>
            <div class="select-option" data-value="2">2 дня</div>
            <div class="select-option" data-value="3">3 дня</div>
            <div class="select-option" data-value="7">7 дней</div>
            <div class="select-option" data-value="14">14 дней</div>
            <div class="select-option" data-value="30">30 дней</div>
          </div>
        </div>
        <div class="row-inline" style="margin-top:10px;">
          <label class="toggle">
            <input type="checkbox" id="refreshAvatars">
            <span class="dot"></span>
            <span>Обновлять аватарки сообществ раз в сутки</span>
          </label>
        </div>
        <label for="blocked" style="margin-top:12px; display:block;">Стоп-слова (каждое с новой строки)</label>
        <textarea id="blocked" style="width:100%; min-height:90px; padding:10px 12px; border-radius:12px; border:1px solid var(--stroke); background: rgba(255,255,255,0.05); color:var(--text); font-size:14px; resize: vertical;"></textarea>
        <div class="hint">Файлы логов и кеш пути: задаются в config.yaml, остаются без изменений.</div>
      </div>

      <div class="card">
        <h3>Токены</h3>
        <label for="vkToken">VK сервисный токен (не обязательно, если в env)</label>
        <input id="vkToken" type="password" placeholder="vk1.a...." />
        <label for="tgChannel">Канал Telegram</label>
        <input id="tgChannel" type="text" placeholder="@channel или ID" />
        <div class="error" id="tgChannelError" style="display:none;"></div>
        <label for="tgToken">Telegram Bot Token (не обязательно, если в env)</label>
        <input id="tgToken" type="password" placeholder="1234:ABC..." />
        <div class="hint">Токены не отображаются, чтобы не светить секреты. Новое значение заменит сохранённое.</div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <h3>Сообщества</h3>
        <div style="display:flex; gap:8px; flex-wrap:wrap;">
          <button class="btn secondary" id="addCommunity">+ Добавить сообщество</button>
        </div>
      </div>
      <div class="communities" id="communities"></div>
    </div>

    <div class="card">
      <div class="card-header">
        <h3>Логи</h3>
        <button class="btn save" id="loadLogs">Показать последние строки</button>
      </div>
      <pre id="logsBox" style="max-height:320px; overflow:auto; background:rgba(0,0,0,0.35); padding:12px; border-radius:12px; border:1px solid var(--stroke); color:#d5dcef; font-family: monospace; font-size:13px;"></pre>
      <div class="hint">Загружается по кнопке, чтобы не расходовать ресурсы постоянно.</div>
    </div>
  </div>

  <div class="toast" id="toast"></div>
  <div class="alert" id="alert"></div>

  <div class="modal" id="lookupModal">
    <div class="modal-card small">
      <h3>Новое сообщество</h3>
      <label for="lookupInput">Ссылка или короткое имя VK</label>
      <input type="text" id="lookupInput" placeholder="https://vk.com/club123, poputiuren" />
      <div class="hint">Попробуем получить название и аватар автоматически.</div>
      <div class="modal-actions">
        <button class="btn secondary" id="lookupCancel" type="button">Отмена</button>
        <button class="btn save" id="lookupNext" type="button">Далее</button>
      </div>
    </div>
  </div>

  <div class="modal" id="communityModal">
    <div class="modal-card">
      <h3>Новое сообщество</h3>
      <div style="display:flex; align-items:center; gap:12px;">
        <div id="modalAvatar" class="avatar-large">?</div>
        <div>
          <p class="vk-title" id="modalVkName"></p>
          <p class="vk-sub" id="modalVkId"></p>
        </div>
      </div>
      <div class="row">
        <div>
          <label>ID/ссылка/короткое имя</label>
          <input type="text" id="modalId" placeholder="club123, https://vk.com/club123, poputiuren" />
        </div>
        <div>
          <label>Имя</label>
          <input type="text" id="modalName" placeholder="Имя для удобства" />
        </div>
        <div style="display:flex; align-items:flex-end;">
          <label class="toggle" style="margin:0;">
            <input type="checkbox" id="modalActive" checked>
            <span class="dot"></span>
            <span>Активно</span>
          </label>
        </div>
      </div>
      <div class="grid">
        <label class="toggle">
          <input type="checkbox" id="modal-text" checked>
          <span class="dot"></span>
          <span>text</span>
        </label>
        <label class="toggle">
          <input type="checkbox" id="modal-photo" checked>
          <span class="dot"></span>
          <span>photo</span>
        </label>
        <label class="toggle">
          <input type="checkbox" id="modal-video" checked>
          <span class="dot"></span>
          <span>video</span>
        </label>
        <label class="toggle">
          <input type="checkbox" id="modal-audio">
          <span class="dot"></span>
          <span>audio</span>
        </label>
        <label class="toggle">
          <input type="checkbox" id="modal-link" checked>
          <span class="dot"></span>
          <span>link</span>
        </label>
      </div>
      <div class="modal-actions">
        <button class="btn secondary" id="modalCancel" type="button">Отмена</button>
        <button class="btn save" id="modalAdd" type="button">Добавить</button>
      </div>
    </div>
  </div>

  <script>
    const communitiesEl = document.getElementById('communities');
    const statusBadges = document.getElementById('statusBadges');
    let communities = [];
    const modal = document.getElementById('communityModal');
    const lookupModal = document.getElementById('lookupModal');
    const modalAvatar = document.getElementById('modalAvatar');
    const modalVkName = document.getElementById('modalVkName');
    const modalVkId = document.getElementById('modalVkId');
    let prefillInfo = null;
    let saveTimer = null;
    let avatarCache = {};

    function showToast(text, isError = false) {
      const toast = document.getElementById('toast');
      toast.textContent = text;
      toast.style.borderColor = isError ? 'rgba(255,127,157,0.5)' : 'rgba(124,231,160,0.4)';
      toast.classList.add('show');
      setTimeout(() => toast.classList.remove('show'), 2600);
    }

    function showAlert(text) {
      const el = document.getElementById('alert');
      el.textContent = text;
      el.classList.add('show');
      setTimeout(() => el.classList.remove('show'), 2400);
    }

    function badge(text, state) {
      return `<span class="badge ${state ? 'pill' : ''}">${text}</span>`;
    }

    function renderBadges(data) {
      statusBadges.innerHTML = [
        badge('Cron: ' + (data.general?.cron || '—'), true),
        badge('VK token: ' + (data.vk?.token_set ? 'установлен' : 'нет'), data.vk?.token_set),
        badge('TG token: ' + (data.telegram?.bot_token_set ? 'установлен' : 'нет'), data.telegram?.bot_token_set),
        badge('Сообществ: ' + (data.communities?.length || 0), true),
      ].join('');
    }

    function renderCommunities() {
      communitiesEl.innerHTML = '';
      communities.forEach((c, idx) => {
        const wrapper = document.createElement('div');
        wrapper.className = 'community';
        const avatarEl = (() => {
          if (c.icon) {
            return `<span class="avatar"><img src="${c.icon}" alt="icon"></span>`;
          }
          const letter = (c.name || c.id || '?').trim().charAt(0).toUpperCase() || '?';
          return `<span class="avatar">${letter}</span>`;
        })();
        wrapper.innerHTML = `
          <details>
            <summary>
              <span class="summary-title">
                ${avatarEl}
                <span>${c.name || 'Без имени'}</span>
              </span>
              ${c.active ? '<span class="badge-mini pill">Активно</span>' : '<span class="badge-mini">Выключено</span>'}
            </summary>
            <div class="row" style="margin-top:10px;">
              <div>
                <label>ID/ссылка/короткое имя</label>
                <input type="text" data-field="id" value="${c.id}" placeholder="club123, https://vk.com/club123, poputiuren" />
              </div>
              <div>
                <label>Имя</label>
                <input type="text" data-field="name" value="${c.name}" />
              </div>
              <div style="display:flex; align-items:flex-end;">
                <label class="toggle" style="margin:0;">
                  <input type="checkbox" data-field="active" ${c.active ? 'checked' : ''}>
                  <span class="dot"></span>
                  <span>Активно</span>
                </label>
              </div>
            </div>
            <div class="grid">
              ${['text','photo','video','audio','link'].map(type => `
                <label class="toggle">
                  <input type="checkbox" data-field="${type}" ${c.content_types[type] ? 'checked' : ''}>
                  <span class="dot"></span>
                  <span>${type}</span>
                </label>
              `).join('')}
            </div>
            <div style="display:flex; justify-content:flex-end; margin-top:8px;">
              <button class="btn danger" data-remove="${idx}">Удалить</button>
            </div>
          </details>
        `;
        communitiesEl.appendChild(wrapper);
      });
    }

    function collectCommunities() {
      const cards = [...communitiesEl.querySelectorAll('.community')];
      return cards.map(card => {
        const obj = {
          id: card.querySelector('input[data-field="id"]').value.trim(),
          name: card.querySelector('input[data-field="name"]').value,
          active: card.querySelector('input[data-field="active"]').checked,
          content_types: {}
        };
        ['text','photo','video','audio','link'].forEach(type => {
          obj.content_types[type] = card.querySelector('input[data-field="'+type+'"]').checked;
        });
        return obj;
      });
    }

    function openModal(prefill = null) {
      prefillInfo = prefill;
      const idVal = prefill?.id || '';
      const nameVal = prefill?.name || '';
      const avatarUrl = prefill?.photo || '';

      document.getElementById('modalId').value = idVal;
      document.getElementById('modalName').value = nameVal || idVal || 'new_community';
      document.getElementById('modalActive').checked = true;
      ['text','photo','video','audio','link'].forEach((type) => {
        const el = document.getElementById(`modal-${type}`);
        if (el) el.checked = ['text','photo','video','link'].includes(type);
      });
      modalVkName.textContent = nameVal || 'Название неизвестно';
      modalVkId.textContent = idVal ? `ID: ${idVal}` : '';
      modalAvatar.innerHTML = '';
      if (avatarUrl) {
        const img = document.createElement('img');
        img.src = avatarUrl;
        img.alt = 'avatar';
        modalAvatar.appendChild(img);
      } else {
        const letter = (nameVal || idVal || '?').trim().charAt(0).toUpperCase() || '?';
        modalAvatar.textContent = letter;
      }
      modal.classList.add('show');
    }

    function closeModal() {
      modal.classList.remove('show');
    }

    function addFromModal() {
      const idVal = document.getElementById('modalId').value.trim();
      const nameVal = document.getElementById('modalName').value.trim() || 'new_community';
      const active = document.getElementById('modalActive').checked;
      const content = {};
      ['text','photo','video','audio','link'].forEach((type) => {
        const el = document.getElementById(`modal-${type}`);
        content[type] = el ? el.checked : false;
      });
      if (!idVal) {
        showAlert('Укажите ID/ссылку сообщества');
        return;
      }
      communities.push({
        id: idVal,
        name: nameVal,
        active,
        icon: prefillInfo?.photo || '',
        content_types: content,
      });
      renderCommunities();
      closeModal();
      scheduleSave();
    }

    function openLookup() {
      document.getElementById('lookupInput').value = '';
      lookupModal.classList.add('show');
    }

    async function fetchCommunityInfo(value) {
      const res = await fetch(`/api/community_info?value=${encodeURIComponent(value)}`);
      if (!res.ok) throw new Error('fail');
      return res.json();
    }

    async function proceedLookup() {
      const raw = document.getElementById('lookupInput').value.trim();
      if (!raw) {
        showAlert('Введите ссылку или имя сообщества');
        return;
      }
      try {
        const info = await fetchCommunityInfo(raw);
        lookupModal.classList.remove('show');
        openModal({ id: info.id || raw, name: info.name || '', photo: info.photo || '' });
      } catch (err) {
        showToast('Не удалось получить данные. Заполните вручную.', true);
        lookupModal.classList.remove('show');
        openModal({ id: raw, name: '', photo: '' });
      }
    }

    function attachHandlers() {
      initCustomSelects();
      communitiesEl.addEventListener('click', (e) => {
        const removeIdx = e.target.getAttribute('data-remove');
        if (removeIdx !== null) {
          communities.splice(parseInt(removeIdx, 10), 1);
          renderCommunities();
          scheduleSave();
        }
      });
      communitiesEl.addEventListener('change', (e) => {
        if (e.target.matches('input[data-field]')) {
          const card = e.target.closest('.community');
          const idx = [...communitiesEl.children].indexOf(card);
          if (idx >= 0) {
            communities[idx].id = card.querySelector('input[data-field="id"]').value.trim();
            communities[idx].name = card.querySelector('input[data-field="name"]').value.trim();
            communities[idx].active = card.querySelector('input[data-field="active"]').checked;
            ['text','photo','video','audio','link'].forEach(type => {
              communities[idx].content_types[type] = card.querySelector(`input[data-field="${type}"]`).checked;
            });
            // сохраняем иконку, если была
            const current = communities[idx];
            if (current && current.icon && !card.querySelector('.avatar img')) {
              // no-op, icon already stored
            }
          }
          scheduleSave();
        }
      });
      document.getElementById('addCommunity').addEventListener('click', openLookup);
      document.getElementById('saveBtn').addEventListener('click', () => saveConfig(false));
      document.getElementById('refreshAvatars').addEventListener('change', () => scheduleSave());
      document.getElementById('logRetention').addEventListener('change', () => scheduleSave());
      document.getElementById('cronSimple').addEventListener('change', (e) => {
        const value = e.target.value;
        const customBlock = document.getElementById('cronCustomBlock');
        const cronInput = document.getElementById('cron');
        if (value === 'custom') {
          customBlock.style.display = 'block';
          cronInput.focus();
        } else {
          customBlock.style.display = 'none';
          cronInput.value = value;
        }
      });

      const logsBtn = document.getElementById('loadLogs');
      const logsBox = document.getElementById('logsBox');
      logsBtn.addEventListener('click', async () => {
        if (logsBox.dataset.visible === 'true') {
          logsBox.textContent = '';
          logsBox.dataset.visible = 'false';
          logsBtn.textContent = 'Показать последние строки';
          return;
        }
        logsBtn.disabled = true;
        logsBtn.textContent = 'Загрузка...';
        try {
          const res = await fetch('/api/logs?lines=50');
          const data = await res.json();
          logsBox.textContent = (data.lines || []).join('');
          logsBox.dataset.visible = 'true';
          logsBtn.textContent = 'Скрыть логи';
        } catch (err) {
          logsBox.textContent = 'Не удалось загрузить логи';
        } finally {
          logsBtn.disabled = false;
        }
      });

      document.getElementById('modalCancel').addEventListener('click', closeModal);
      document.getElementById('modalAdd').addEventListener('click', addFromModal);
      document.getElementById('lookupCancel').addEventListener('click', () => lookupModal.classList.remove('show'));
      document.getElementById('lookupNext').addEventListener('click', proceedLookup);
      modal.addEventListener('click', (e) => {
        if (e.target === modal) closeModal();
      });
      lookupModal.addEventListener('click', (e) => {
        if (e.target === lookupModal) lookupModal.classList.remove('show');
      });
    }

    function updateCustomSelectDisplay(selectEl) {
      const wrapper = selectEl.closest('.custom-select');
      if (!wrapper) return;
      const display = wrapper.querySelector('[data-role="display"]');
      const options = wrapper.querySelectorAll('.select-option');
      const selectedOption = [...selectEl.options].find((opt) => opt.value === selectEl.value);
      if (display && selectedOption) display.textContent = selectedOption.textContent;
      options.forEach((opt) => {
        opt.classList.toggle('selected', opt.getAttribute('data-value') === selectEl.value);
      });
    }

    function closeAllCustomSelects(except = null) {
      document.querySelectorAll('.custom-select.open').forEach((el) => {
        if (el !== except) el.classList.remove('open');
      });
      document.querySelectorAll('.card.raise').forEach((card) => {
        card.classList.remove('raise');
      });
    }

    function initCustomSelects() {
      document.querySelectorAll('.custom-select').forEach((wrapper) => {
        const selectEl = wrapper.querySelector('select');
        const display = wrapper.querySelector('[data-role="display"]');
        const options = wrapper.querySelectorAll('.select-option');
        updateCustomSelectDisplay(selectEl);

        display.addEventListener('click', (e) => {
          e.stopPropagation();
          const isOpen = wrapper.classList.contains('open');
          closeAllCustomSelects(wrapper);
          wrapper.classList.toggle('open', !isOpen);
          const card = wrapper.closest('.card');
          if (card && !isOpen) card.classList.add('raise');
        });

        options.forEach((opt) => {
          opt.addEventListener('click', () => {
            const value = opt.getAttribute('data-value');
            selectEl.value = value;
            updateCustomSelectDisplay(selectEl);
            selectEl.dispatchEvent(new Event('change', { bubbles: true }));
            wrapper.classList.remove('open');
            const card = wrapper.closest('.card');
            if (card) card.classList.remove('raise');
          });
        });
      });

      document.addEventListener('click', () => closeAllCustomSelects());
    }

    function syncCronUI(cronValue) {
      const simple = document.getElementById('cronSimple');
      const customBlock = document.getElementById('cronCustomBlock');
      const cronInput = document.getElementById('cron');
      const known = ['*/5 * * * *','*/10 * * * *','*/15 * * * *','*/30 * * * *','0 * * * *'];
      if (known.includes(cronValue)) {
        simple.value = cronValue;
        customBlock.style.display = 'none';
      } else {
        simple.value = 'custom';
        customBlock.style.display = 'block';
      }
      cronInput.value = cronValue;
      updateCustomSelectDisplay(simple);
    }

    async function loadConfig() {
      try {
        const res = await fetch('/api/config');
        const data = await res.json();
        avatarCache = data.avatar_cache || {};
        syncCronUI(data.general?.cron || '*/10 * * * *');
        document.getElementById('limit').value = data.general?.posts_limit || 10;
        const logRetentionSelect = document.getElementById('logRetention');
        logRetentionSelect.value = (data.general?.log_retention_days || 7);
        updateCustomSelectDisplay(logRetentionSelect);
        document.getElementById('blocked').value = (data.general?.blocked_keywords || []).join('\\n');
        document.getElementById('refreshAvatars').checked = data.general?.refresh_avatars !== false;
        const vkField = document.getElementById('vkToken');
        const tgField = document.getElementById('tgToken');
        const tgChannelField = document.getElementById('tgChannel');

        vkField.value = data.vk?.token_set ? '********' : '';
        vkField.dataset.masked = data.vk?.token_set ? 'true' : 'false';

        tgField.value = data.telegram?.bot_token_set ? '********' : '';
        tgField.dataset.masked = data.telegram?.bot_token_set ? 'true' : 'false';

        tgChannelField.value = data.telegram?.channel_id || '';
        communities = data.communities || [];
        await enrichIcons(data.general?.refresh_avatars !== false);
        renderCommunities();
        renderBadges(data);
      } catch (err) {
        showToast('Не удалось загрузить конфиг', true);
      }
    }

    async function enrichIcons(allowRefresh = true) {
      const tasks = (communities || []).map(async (c) => {
        // Попытка взять из кэша
        const key = (c.id || '').toLowerCase();
        const cached = avatarCache[key];
        const now = Math.floor(Date.now() / 1000);
        if (cached) {
          c.icon = cached.photo || c.icon;
          if (!c.name && cached.name) c.name = cached.name;
        }
        const is_stale = !cached || (cached.fetched_at && (now - cached.fetched_at > 24 * 3600));
        if (!allowRefresh || !is_stale) return;
        try {
          const info = await fetchCommunityInfo(c.id);
          if (info?.photo) c.icon = info.photo;
          if (!c.name && info?.name) c.name = info.name;
          if (info?.photo || info?.name) {
            avatarCache[key] = { photo: info.photo, name: info.name, fetched_at: Math.floor(Date.now() / 1000) };
          }
        } catch (e) {
          // ignore
        }
      });
      await Promise.all(tasks);
    }

    function clearFieldErrors() {
      ['tgChannel','cron'].forEach((id) => {
        document.getElementById(id).classList.remove('invalid');
      });
      ['tgChannelError','cronError'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) {
          el.style.display = 'none';
          el.textContent = '';
        }
      });
    }

    function setFieldError(id, message) {
      const input = document.getElementById(id);
      const error = document.getElementById(id + 'Error');
      if (input) input.classList.add('invalid');
      if (error) {
        error.textContent = message;
        error.style.display = 'block';
      } else {
        showAlert(message);
      }
    }

    function scheduleSave() {
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => saveConfig(true), 500);
    }

    async function saveConfig(silent = false) {
      clearFieldErrors();
      const vkTokenInput = document.getElementById('vkToken');
      const tgTokenInput = document.getElementById('tgToken');
      const payload = {
        general: {
          cron: document.getElementById('cron').value || document.getElementById('cronSimple').value,
          posts_limit: parseInt(document.getElementById('limit').value, 10),
          vk_api_version: '5.199',
          cache_file: 'data/cache.json',
          log_file: 'logs/poster.log',
          log_level: 'DEBUG',
          log_rotation: { max_bytes: 10485760, backup_count: 7 },
          log_retention_days: parseInt(document.getElementById('logRetention').value, 10) || 7,
          blocked_keywords: document.getElementById('blocked').value
            .split('\\n')
            .map((s) => s.trim())
            .filter((s) => s.length > 0),
          refresh_avatars: document.getElementById('refreshAvatars').checked,
        },
        vk: { token: (vkTokenInput.dataset.masked === 'true' && vkTokenInput.value === '********') ? '' : vkTokenInput.value.trim() },
        telegram: {
          channel_id: document.getElementById('tgChannel').value.trim(),
          bot_token: (tgTokenInput.dataset.masked === 'true' && tgTokenInput.value === '********') ? '' : tgTokenInput.value.trim(),
        },
        communities: collectCommunities(),
      };
      try {
        const res = await fetch('/api/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (!res.ok) {
          const detail = await res.json().catch(() => ({}));
          let message = 'Ошибка сохранения';
          if (detail?.detail) {
            if (typeof detail.detail === 'string') {
              message = detail.detail;
            } else if (Array.isArray(detail.detail)) {
              message = detail.detail.map((d) => d.msg || JSON.stringify(d)).join('; ');
              const field = detail.detail[0]?.loc?.slice(-1)[0];
              if (field === 'channel_id') setFieldError('tgChannel', 'Укажите канал Telegram');
              if (field === 'cron') setFieldError('cron', message);
            } else if (detail.detail.field) {
              if (detail.detail.field === 'telegram.channel_id') {
                setFieldError('tgChannel', detail.detail.message || 'Укажите канал Telegram');
              }
              if (detail.detail.field === 'general.cron') {
                setFieldError('cron', detail.detail.message || 'Неверный cron');
              }
            }
          }
          throw new Error(message);
        }
        if (!silent) {
          showToast('Конфиг сохранён');
          loadConfig();
        }
      } catch (err) {
        showAlert(err.message || 'Ошибка сохранения');
      }
    }

    attachHandlers();
    loadConfig();
  </script>
</body>
</html>
"""
