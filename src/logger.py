import logging
import os
import re
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Tuple

from .config import DEFAULT_TIMEZONE, ConfigError, GeneralSettings, validate_timezone


REDACTED = "<redacted>"

_REDACT_PATTERNS: Tuple[Tuple[re.Pattern, str], ...] = (
    # URL query parameters: access_token=..., ?oauth=..., &token=..., etc.
    (re.compile(r"(?i)(access_token=)[^&\s'\"]+"), r"\1" + REDACTED),
    (re.compile(r"(?i)([?&](?:oauth|token|bot_token|sig|key)=)[^&\s'\"]+"), r"\1" + REDACTED),
    # VK error payload: [{'key': 'oauth', 'value': '...'}]
    (
        re.compile(
            r"(?i)('key':\s*'(?:access_token|oauth|token|bot_token|sig|key)'\s*,\s*'value':\s*')[^']*(')"
        ),
        r"\1" + REDACTED + r"\2",
    ),
    (
        re.compile(
            r'(?i)("key":\s*"(?:access_token|oauth|token|bot_token|sig|key)"\s*,\s*"value":\s*")[^"]*(")'
        ),
        r"\1" + REDACTED + r"\2",
    ),
    # Telegram bot token embedded in a URL or a bare "123456:ABC..." value
    (re.compile(r"(?i)(api\.telegram\.org/bot)[^/\s'\"]+"), r"\1" + REDACTED),
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), REDACTED),
    # Bare VK access tokens (vk1.a.<...>)
    (re.compile(r"\bvk1\.[A-Za-z0-9]\.[A-Za-z0-9_-]{10,}"), REDACTED),
)


def redact_secrets(text: str) -> str:
    """Remove tokens and secrets from a log message before it is written or served."""
    if not text:
        return text
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


class CompactFileFormatter(RedactingFormatter):
    def format(self, record: logging.LogRecord) -> str:
        exc_info = record.exc_info
        exc_text = record.exc_text
        stack_info = record.stack_info
        try:
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            return super().format(record)
        finally:
            record.exc_info = exc_info
            record.exc_text = exc_text
            record.stack_info = stack_info


def apply_timezone(tz: str) -> str:
    """Set the process timezone from a config value (fallback: default)."""
    try:
        name = validate_timezone(tz)
    except ConfigError:
        name = DEFAULT_TIMEZONE
    os.environ["TZ"] = name
    try:
        time.tzset()
    except AttributeError:
        # tzset is not available on some platforms (e.g., Windows containers)
        pass
    return name


def configure_logging(settings: GeneralSettings) -> logging.Logger:
    # Local timezone for logs and schedule, taken from config (default Europe/Moscow)
    apply_timezone(settings.timezone)

    log_path = Path(settings.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    _cleanup_old_logs(log_path, settings.log_retention_days)

    logger = logging.getLogger("poster")
    requested_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    file_level = max(logging.INFO, requested_level)
    console_level = logging.WARNING
    logger.setLevel(min(file_level, console_level))
    logger.propagate = False
    formatter = RedactingFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    formatter.converter = time.localtime  # логируем в локальном часовом поясе
    file_formatter = CompactFileFormatter("%(asctime)s [%(levelname)s] %(message)s")
    file_formatter.converter = time.localtime

    for existing in list(logger.handlers):
        logger.removeHandler(existing)
        existing.close()

    handler = RotatingFileHandler(
        log_path,
        maxBytes=settings.log_rotation.max_bytes,
        backupCount=settings.log_rotation.backup_count,
        encoding="utf-8",
    )
    handler.setLevel(file_level)
    handler.setFormatter(file_formatter)
    logger.addHandler(handler)

    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(formatter)
    logger.addHandler(console)

    logger.debug("Логирование настроено, файл: %s", log_path)
    return logger


def _cleanup_old_logs(log_path: Path, retention_days: int) -> None:
    if retention_days <= 0:
        return
    cutoff = time.time() - (retention_days * 24 * 3600)
    log_dir = log_path.parent
    prefix = log_path.name
    for entry in log_dir.iterdir():
        if not entry.is_file():
            continue
        if not entry.name.startswith(prefix):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            if entry == log_path:
                entry.write_text("", encoding="utf-8")
            else:
                entry.unlink(missing_ok=True)
