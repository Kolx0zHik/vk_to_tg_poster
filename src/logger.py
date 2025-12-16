import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import GeneralSettings


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

    logger = logging.getLogger("poster")
    logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    formatter.converter = time.localtime  # логируем в локальном часовом поясе

    handler = RotatingFileHandler(
        log_path,
        maxBytes=settings.log_rotation.max_bytes,
        backupCount=settings.log_rotation.backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    logger.debug("Логирование настроено, файл: %s", log_path)
    return logger
