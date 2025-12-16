import logging
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger


class CronScheduler:
    def __init__(self, cron_expr: str, job, logger: logging.Logger):
        self.cron_expr = cron_expr
        self.job = job
        self.logger = logger
        self.scheduler = BlockingScheduler()

    def start(self) -> None:
        trigger = CronTrigger.from_crontab(self.cron_expr)
        self.scheduler.add_job(self._run_job, trigger)
        self.logger.info("Запускаем планировщик с cron: %s", self.cron_expr)
        try:
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            self.logger.info("Планировщик остановлен.")

    def _run_job(self) -> None:
        self.logger.info("Выполняем задачу по расписанию")
        try:
            self.job()
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Ошибка при выполнении задачи: %s", exc)
