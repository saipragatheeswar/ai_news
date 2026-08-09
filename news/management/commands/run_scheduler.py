"""Long-running scheduler that triggers the daily run at a fixed local time.

Use this when you want a single always-on process. If you would rather let the
operating system handle timing, skip this and point Windows Task Scheduler (or
cron) at `manage.py run_daily` instead.
"""

import logging
import signal

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand

logger = logging.getLogger("news.scheduler")


class Command(BaseCommand):
    help = "Run the daily article pipeline on a schedule (blocking process)."

    def add_arguments(self, parser):
        parser.add_argument("--hour", type=int, default=settings.SCHEDULE_HOUR)
        parser.add_argument("--minute", type=int, default=settings.SCHEDULE_MINUTE)
        parser.add_argument(
            "--now",
            action="store_true",
            help="Also run once immediately on startup.",
        )

    def handle(self, *args, **options):
        hour, minute = options["hour"], options["minute"]
        scheduler = BlockingScheduler(timezone=settings.TIME_ZONE)
        scheduler.add_job(
            _run_pipeline,
            CronTrigger(hour=hour, minute=minute),
            id="daily_news",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Scheduled daily run at {hour:02d}:{minute:02d} {settings.TIME_ZONE}. "
                "Press Ctrl+C to stop."
            )
        )

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: scheduler.shutdown(wait=False))
            except (ValueError, OSError):
                pass

        if options["now"]:
            _run_pipeline()

        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            self.stdout.write("Scheduler stopped.")


def _run_pipeline() -> None:
    logger.info("scheduler triggering daily run")
    try:
        call_command("run_daily")
    except Exception:
        logger.exception("scheduled run failed")
