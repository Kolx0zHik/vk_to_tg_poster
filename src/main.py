from __future__ import annotations

import os

from .cache import Cache
from .config import ConfigError, load_config
from .logger import configure_logging
from .pipeline import process_communities
from .scheduler import CronScheduler
from .tg_client import TelegramClient
from .vk_client import VKClient


def run_job(config_path: str, logger) -> None:
    config = load_config(config_path)
    cache = Cache(config.general.cache_file)
    vk_client = VKClient(config.vk.token, api_version=config.general.vk_api_version)
    tg_client = TelegramClient(config.telegram.bot_token, config.telegram.channel_id)
    process_communities(config, vk_client, tg_client, cache)
    logger.info("Run completed once.")


def run_once(config_path: str, logger) -> None:
    run_job(config_path, logger)


def run_with_scheduler(cron_expr: str, config_path: str, logger) -> None:
    scheduler = CronScheduler(cron_expr, lambda: run_job(config_path, logger), logger)
    scheduler.start()


def main() -> None:
    config_path = os.getenv("CONFIG_PATH", "config/config.yaml")
    run_mode = os.getenv("RUN_MODE", "scheduled")

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        raise SystemExit(1)

    logger = configure_logging(config.general)
    logger.info("Config loaded from %s", os.path.abspath(config_path))

    try:
        if run_mode == "once":
            run_once(config_path, logger)
        else:
            run_with_scheduler(config.general.cron, config_path, logger)
    except ConfigError as exc:
        logger.error("Configuration error during run: %s", exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
