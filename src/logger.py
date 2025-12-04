import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import GeneralSettings


def configure_logging(settings: GeneralSettings) -> logging.Logger:
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

    logger.debug("Logging configured with file %s", log_path)
    return logger
