import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import GeneralSettings


class CompactFileFormatter(logging.Formatter):
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


def configure_logging(settings: GeneralSettings) -> logging.Logger:
    # Ensure local timezone (default to Europe/Moscow if not provided)
    tz = os.environ.get("TZ", "Europe/Moscow")
    os.environ["TZ"] = tz
    try:
        time.tzset()
    except AttributeError:
        # tzset is not available on some platforms (e.g., Windows containers)
        pass

    log_path = Path(settings.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    _cleanup_old_logs(log_path, settings.log_retention_days)

    logger = logging.getLogger("poster")
    requested_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    file_level = max(logging.INFO, requested_level)
    console_level = logging.WARNING
    logger.setLevel(min(file_level, console_level))
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
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
