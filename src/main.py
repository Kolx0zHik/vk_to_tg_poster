from __future__ import annotations

import os

from .cache import Cache
from .config import ConfigError, load_config
from .logger import configure_logging
from .pipeline import process_communities
from .scheduler import CronScheduler
from .tg_client import TelegramClient
from .vk_client import VKClient


def build_app(config_path: str):
    config = load_config(config_path)
    logger = configure_logging(config.general)
    logger.info("Config loaded from %s", os.path.abspath(config_path))

    cache = Cache(config.general.cache_file)
    vk_client = VKClient(config.vk.token, api_version=config.general.vk_api_version)
    tg_client = TelegramClient(config.telegram.bot_token, config.telegram.channel_id)
    return config, logger, cache, vk_client, tg_client


def run_once(config_path: str) -> None:
    config, logger, cache, vk_client, tg_client = build_app(config_path)
    process_communities(config, vk_client, tg_client, cache)
    logger.info("Run completed once.")


def run_with_scheduler(config_path: str) -> None:
    config, logger, cache, vk_client, tg_client = build_app(config_path)
    scheduler = CronScheduler(config.general.cron, lambda: process_communities(config, vk_client, tg_client, cache), logger)
    scheduler.start()


def main() -> None:
    config_path = os.getenv("CONFIG_PATH", "config/config.yaml")
    run_mode = os.getenv("RUN_MODE", "scheduled")

    try:
        if run_mode == "once":
            run_once(config_path)
        else:
            run_with_scheduler(config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
