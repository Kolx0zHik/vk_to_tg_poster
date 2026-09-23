"""Semantic duplicate detection via an OpenAI-compatible chat completions API.

Kept deliberately small and dependency-free (uses ``requests`` only).  The
caller decides what to do with the answer; the checker itself never blocks
publication: every exception is converted into ``DedupError`` so the pipeline
can fail open (publish anyway).

The user message owns only the data shape (ADR-021): the new post and a
numbered, dated candidate list plus the answer contract line.  Dedup rules
themselves live exclusively in the operator's system prompt (ADR-020).
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
# Candidate block budget in characters. Truncation happens on whole-candidate
# boundaries: trailing candidates are dropped entirely; the last kept candidate
# may be cut short with a marker (never inside the numbered prefix).
CANDIDATES_CHAR_BUDGET = 8000
# Skip the trailing candidate entirely unless at least this many chars of its
# line still fit — a few words are useless for the verdict but cost tokens.
CANDIDATE_TAIL_MIN = 200
# Cap for the new post text in the prompt (candidates are already stored capped
# by PublishedText.MAX_LEN in src/cache.py; the new post is not).
NEW_POST_TEXT_MAX = 2000


def debug_log_enabled() -> bool:
    """Temporary test knob: dump raw LLM answers into the log on INFO."""
    return os.getenv("LLM_DEBUG_LOG", "").strip().lower() in {"1", "true", "yes", "on"}

@dataclass
class DedupResult:
    is_duplicate: bool
    reason: str
    # 1-based candidate number the model matched, 0 when absent or invalid.
    matched_index: int = 0


class DedupError(Exception):
    """Raised when the LLM endpoint cannot produce a decision."""


ANSWER_CONTRACT = (
    "Ответь строго одним JSON-объектом без пояснений: "
    '{"is_duplicate": true или false, "reason": "короткое пояснение", '
    '"matched": номер кандидата из списка или 0}'
)


def _fmt_date(ts) -> str:
    try:
        ts = int(ts or 0)
    except (TypeError, ValueError):
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""


def _candidate_line(idx: int, candidate: dict) -> str:
    text = str(candidate.get("text") or "").strip()
    date = _fmt_date(candidate.get("date"))
    return f"{idx}. [{date}] {text}" if date else f"{idx}. {text}"


def _build_user_prompt(new_post: dict, candidates: List[dict]) -> str:
    """Render data only: the new post, then the numbered candidates within budget."""
    date = _fmt_date(new_post.get("date"))
    header = f"Новый пост [{date}]:" if date else "Новый пост:"
    new_text = str(new_post.get("text") or "").strip()[:NEW_POST_TEXT_MAX]
    used = 0
    lines: List[str] = []
    for idx, candidate in enumerate(candidates, start=1):
        line = _candidate_line(idx, candidate)
        remaining = CANDIDATES_CHAR_BUDGET - used
        if len(line) <= remaining:
            lines.append(line)
            used += len(line) + 2
            continue
        if remaining >= CANDIDATE_TAIL_MIN:
            lines.append(line[:remaining].rstrip() + " …")
        break
    body = "\n".join(lines) if lines else "(пусто)"
    return (
        "Проверь, является ли новый пост дубликатом одного из кандидатов.\n\n"
        f"{header}\n{new_text}\n\n"
        f"Кандидаты:\n{body}\n\n"
        f"{ANSWER_CONTRACT}"
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


def _parse_matched(value, candidate_count: int) -> int:
    """Coerce ``matched`` to a valid 1-based candidate number, else 0.

    Tolerates JSON numbers (int or float) and their string forms; booleans and
    junk always yield 0.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        idx = value
    elif isinstance(value, float):
        idx = int(value) if value.is_integer() else 0
    elif isinstance(value, str):
        try:
            idx = int(value.strip())
        except ValueError:
            return 0
    else:
        return 0
    return idx if 1 <= idx <= candidate_count else 0


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on", "да"}
    if isinstance(value, int):
        return bool(value)
    return bool(value)


def _parse_result(raw: dict, candidate_count: int) -> DedupResult:
    try:
        is_dup = _as_bool(raw.get("is_duplicate", False))
        reason = str(raw.get("reason", "") or "").strip()
    except (AttributeError, TypeError) as exc:
        raise DedupError(f"Некорректный формат ответа LLM: {exc}") from None
    return DedupResult(
        is_duplicate=is_dup,
        reason=reason,
        matched_index=_parse_matched(raw.get("matched"), candidate_count),
    )


class SemanticDedup:
    """Client for the semantic duplicate checker.

    The system prompt is mandatory: there is no built-in fallback, so an empty
    ``prompt`` makes the checker unavailable (the pipeline then skips the
    semantic check entirely).
    """

    def __init__(self, base_url: str, model: str, api_key: str = "", prompt: str = "", timeout: int = LLM_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.prompt = prompt.strip()
        self.timeout = timeout
        self.session = requests.Session()

    def _is_configured(self) -> bool:
        return bool(self.base_url and self.model and self.api_key and self.prompt)

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
        """Ask the model whether ``new_post`` (``{"text", "date"}``) duplicates any candidate."""
        if not self._is_configured():
            raise DedupError("LLM не настроен: задайте base_url, model, системный промпт и LLM_API_KEY")

        messages = [
            {"role": "system", "content": self.prompt},
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
            return _parse_result(_extract_json(content), len(candidates))
        except DedupError:
            if debug_log_enabled():
                flat = " ".join(str(content).split())[:LLM_DEBUG_MAX_CHARS]
                logger.info("LLM ответ (кандидатов %s): %s", len(candidates), flat)
            raise
