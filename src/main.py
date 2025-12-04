from __future__ import annotations

import os
import time
from datetime import datetime

from croniter import croniter

from .cache import Cache
from .config import ConfigError, load_config
from .logger import configure_logging
from .pipeline import process_communities
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
    logger.info("Starting scheduler with cron: %s", cron_expr)
    while True:
        now = datetime.now()
        it = croniter(cron_expr, now)
        next_run = it.get_next(datetime)
        sleep_for = max(1, int((next_run - datetime.now()).total_seconds()))
        logger.debug("Next run at %s (in %ss)", next_run, sleep_for)
        time.sleep(sleep_for)
        try:
            # reload config to pick up updated cron/content/token changes
            cfg = load_config(config_path)
            process_communities(cfg, VKClient(cfg.vk.token, cfg.general.vk_api_version), TelegramClient(cfg.telegram.bot_token, cfg.telegram.channel_id), Cache(cfg.general.cache_file))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Scheduled run failed: %s", exc)
        # refresh cron from file for next iteration
        try:
            cron_expr = load_config(config_path).general.cron
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to reload cron from config: %s (keeping previous: %s)", exc, cron_expr)


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
