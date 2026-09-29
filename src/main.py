from __future__ import annotations

import os
import time
from datetime import datetime

from croniter import croniter

from .backfill import BackfillRequests, requests_path_for
from .cache import Cache
from .config import ConfigError, load_config
from .envfile import load_env_file
from .journal import RunJournal, journal_path_for
from .logger import apply_timezone, configure_logging
from .pipeline import process_communities
from .tg_client import TelegramClient
from .version import get_version
from .vk_client import VKClient


def _run_record(started: float, summaries: list, ok: bool, error: str = "") -> dict:
    finished = time.time()
    record = {
        "started": int(started),
        "finished": int(finished),
        "duration": round(finished - started, 1),
        "ok": ok,
        "version": get_version(),
        "communities": summaries,
    }
    if error:
        record["error"] = error
    return record


def _execute_run(config) -> None:
    """Publish for all communities and append a record to the run journal."""
    journal = RunJournal(journal_path_for(config.general.cache_file))
    started = time.time()
    try:
        summaries = process_communities(
            config,
            VKClient(os.getenv("VK_API_TOKEN", ""), config.general.vk_api_version),
            TelegramClient(os.getenv("TELEGRAM_BOT_TOKEN", ""), config.telegram.channel_id),
            Cache(config.general.cache_file),
            BackfillRequests(requests_path_for(config.general.cache_file)),
        )
    except Exception as exc:  # noqa: BLE001
        journal.record(_run_record(started, [], ok=False, error=str(exc)))
        raise
    journal.record(_run_record(started, summaries, ok=all(item["status"] != "error" for item in summaries)))


def run_job(config_path: str, logger) -> None:
    load_env_file(config_path)
    config = load_config(config_path, require_tokens=False, require_channel=False, allow_missing=True)
    vk_token = os.getenv("VK_API_TOKEN", "")
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not vk_token or not tg_token or not config.telegram.channel_id:
        logger.warning("Токены VK/Telegram или канал не заданы, публикация пропущена")
        return
    _execute_run(config)
    logger.info("Запуск завершён.")


def run_once(config_path: str, logger) -> None:
    run_job(config_path, logger)


def run_with_scheduler(cron_expr: str, config_path: str, logger) -> None:
    logger.info("Запуск по расписанию с cron: %s", cron_expr)
    while True:
        now = datetime.now()
        it = croniter(cron_expr, now)
        next_run = it.get_next(datetime)
        sleep_for = max(0.0, (next_run - datetime.now()).total_seconds())
        logger.debug("Следующий запуск в %s (через %.1fs)", next_run, sleep_for)
        time.sleep(sleep_for)
        try:
            # reload config to pick up updated cron/content/token changes
            load_env_file(config_path)
            cfg = load_config(config_path, require_tokens=False, require_channel=False, allow_missing=True)
            apply_timezone(cfg.general.timezone)
            if not os.getenv("VK_API_TOKEN", "") or not os.getenv("TELEGRAM_BOT_TOKEN", "") or not cfg.telegram.channel_id:
                logger.warning("Токены VK/Telegram или канал не заданы, публикация пропущена")
            else:
                _execute_run(cfg)
            # refresh cron/timezone from file for next iteration
            try:
                next_cfg = load_config(config_path, require_tokens=False, require_channel=False, allow_missing=True)
                cron_expr = next_cfg.general.cron
                apply_timezone(next_cfg.general.timezone)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Не удалось перечитать cron из конфига: %s (оставляем прошлое: %s)", exc, cron_expr)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Сбой планового запуска: %s", exc)


def main() -> None:
    config_path = os.getenv("CONFIG_PATH", "data/config.yaml")
    run_mode = os.getenv("RUN_MODE", "scheduled")

    try:
        config = load_config(config_path, require_tokens=False, require_channel=False, allow_missing=True)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}")
        raise SystemExit(1)

    logger = configure_logging(config.general)
    logger.info("Версия проекта: %s", get_version())
    logger.info("Конфигурация загружена из %s", os.path.abspath(config_path))

    try:
        if run_mode == "once":
            run_once(config_path, logger)
        else:
            run_with_scheduler(config.general.cron, config_path, logger)
    except ConfigError as exc:
        logger.error("Ошибка конфигурации при запуске: %s", exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
