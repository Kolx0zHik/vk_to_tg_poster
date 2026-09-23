"""Semantic duplicate detection via an OpenAI-compatible chat completions API.

Kept deliberately small and dependency-free (uses ``requests`` only).  The
caller decides what to do with the answer; the checker itself never blocks
publication: every exception is converted into ``DedupError`` so the pipeline
can fail open (publish anyway).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import List, Optional

import requests

logger = logging.getLogger("poster.dedup")

LLM_TIMEOUT = 60
LLM_MAX_RETRIES = 1
LLM_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
LLM_DEBUG_MAX_CHARS = 500


def debug_log_enabled() -> bool:
    """Temporary test knob: dump raw LLM answers into the log on INFO."""
    return os.getenv("LLM_DEBUG_LOG", "").strip().lower() in {"1", "true", "yes", "on"}

DEFAULT_SYSTEM_PROMPT = """Ты — помощник по удалению дубликатов постов в Telegram-канале.
Сравнивай новый пост с кандидатами и решай, является ли он дубликатом по смыслу.

Правила:
1) Дубликат = тот же инфоповод и очень близкий смысл.
2) Помечай дубликат только при высокой уверенности.
3) При сомнении is_duplicate=false.
4) Похожие темы, но разные новости, детали, даты, места, участники или выводы — не дубликат.
5) Сравнивай только посты того же chat_id (канала).
6) Игнорируй кандидатов без raw_text, message_id или chat_id.
7) Никогда не сравнивай новый пост сам с собой: candidate.message_id == new message_id не дубликат.
8) Если найдено несколько похожих кандидатов, выбери самый близкий по смыслу.
9) reason должен коротко объяснять, почему это дубль или почему нет.
10) Иногда что-то куплю и похожее что-то продам — это разное: один покупает, другой продаёт.
11) Иногда смысл одинаковый, но место и даты разные — сравнивай и это тоже.
12) Иногда инфоповод один и тот же, но упоминаются разные обстоятельства — is_duplicate=false.

Отвечай строго в формате JSON с ключами:
- "is_duplicate": true или false,
- "reason": текст до 15 слов на русском,
- "matched_message_id": строка, никогда null; при is_duplicate=false всегда "". """


@dataclass
class DedupResult:
    is_duplicate: bool
    reason: str
    matched_message_id: str


class DedupError(Exception):
    """Raised when the LLM endpoint cannot produce a decision."""


def _build_user_prompt(new_post: dict, candidates: List[dict]) -> str:
    return (
        "Проверь, является ли новый пост дубликатом среди кандидатов того же канала.\n\n"
        "Новый пост:\n"
        f"chat_id: {new_post.get('chat_id', '')}\n"
        f"message_id: {new_post.get('message_id', '')}\n"
        f"date_unix: {new_post.get('date_unix', '')}\n"
        f"raw_text: {new_post.get('raw_text', '')}\n\n"
        "Кандидаты:\n"
        f"{json.dumps(candidates, ensure_ascii=False)[:8000]}\n"
    )


def _extract_json(text: str) -> dict:
    """Parse a strict JSON object out of a chat completion reply."""
    text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass

    # Some models wrap the answer in ```...``` fences or prose around the object.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise DedupError("LLM вернул ответ без JSON-объекта")
    try:
        data = json.loads(match.group(0))
    except Exception as exc:
        raise DedupError(f"Не удалось разобрать JSON от LLM: {exc}") from None
    if not isinstance(data, dict):
        raise DedupError("LLM вернул не объект")
    return data


def _parse_result(raw: dict) -> DedupResult:
    try:
        is_dup = bool(raw.get("is_duplicate", False))
        reason = str(raw.get("reason", "") or "").strip()
        matched = str(raw.get("matched_message_id") or "").strip()
    except (AttributeError, TypeError) as exc:
        raise DedupError(f"Некорректный формат ответа LLM: {exc}") from None
    if is_dup and not matched:
        matched = ""
    return DedupResult(is_duplicate=is_dup, reason=reason, matched_message_id=matched)


class SemanticDedup:
    """Client for the semantic duplicate checker."""

    def __init__(self, base_url: str, model: str, api_key: str = "", prompt: str = "", timeout: int = LLM_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.prompt = prompt.strip()
        self.timeout = timeout
        self.session = requests.Session()

    def _system_prompt(self) -> str:
        return self.prompt or DEFAULT_SYSTEM_PROMPT

    def _is_configured(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)

    def _chat_completion(self, messages: List[dict]) -> dict:
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
        }
        last_exc: Optional[Exception] = None
        for attempt in range(LLM_MAX_RETRIES + 1):
            try:
                resp = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning("Сбой запроса к LLM (%s)", type(exc).__name__)
            else:
                if resp.status_code in LLM_RETRYABLE_STATUS:
                    last_exc = RuntimeError(f"LLM вернул HTTP {resp.status_code}")
                    logger.warning("LLM вернул %s, повтор", resp.status_code)
                elif not resp.ok:
                    raise DedupError(f"LLM API error: HTTP {resp.status_code}")
                else:
                    try:
                        data = resp.json()
                    except ValueError:
                        raise DedupError("LLM вернул не-JSON ответ") from None
                    return data or {}
            if attempt < LLM_MAX_RETRIES:
                time.sleep(1.5 * (attempt + 1))
        raise DedupError(f"LLM недоступен: {last_exc}") from last_exc

    def check(self, new_post: dict, candidates: List[dict]) -> DedupResult:
        """Ask the model whether ``new_post`` duplicates any candidate."""
        if not self._is_configured():
            raise DedupError("LLM не настроен: задайте base_url, model и LLM_API_KEY")

        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": _build_user_prompt(new_post, candidates)},
        ]
        try:
            data = self._chat_completion(messages)
        except DedupError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DedupError(f"Ошибка LLM: {exc}") from exc

        try:
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        except (AttributeError, IndexError, TypeError) as exc:
            raise DedupError(f"LLM вернул неожиданную структуру: {exc}") from None

        if not content:
            raise DedupError("LLM вернул пустой ответ")
        try:
            return _parse_result(_extract_json(content))
        except DedupError:
            if debug_log_enabled():
                flat = " ".join(str(content).split())[:LLM_DEBUG_MAX_CHARS]
                logger.info(
                    "LLM ответ (пост %s, кандидатов %s): %s",
                    new_post.get("message_id", ""),
                    len(candidates),
                    flat,
                )
            raise