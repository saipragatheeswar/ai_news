"""Produce the day's articles. This is the command a scheduler should call."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from news.pipeline.llm import LocalModelError
from news.pipeline.orchestrator import Pipeline
from news.pipeline.tavily_client import TavilyNotConfigured


class Command(BaseCommand):
    help = "Discover today's hot topics and publish original articles about them."

    def add_arguments(self, parser):
        parser.add_argument(
            "--target",
            type=int,
            default=settings.DAILY_ARTICLE_TARGET,
            help="How many articles to publish (default: DAILY_ARTICLE_TARGET).",
        )
        parser.add_argument(
            "--hold-all",
            action="store_true",
            help="Send every article to the review queue instead of publishing.",
        )

    def handle(self, *args, **options):
        target = options["target"]
        auto_publish = False if options["hold_all"] else None

        try:
            pipeline = Pipeline(auto_publish=auto_publish)
        except (TavilyNotConfigured, LocalModelError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f"Target: {target} articles. Model: {pipeline.model.model}")
        result = pipeline.run_daily(target=target)

        self.stdout.write("")
        self.stdout.write(f"  topics discovered : {result.topics_discovered}")
        self.stdout.write(self.style.SUCCESS(f"  published         : {result.published}"))
        self.stdout.write(self.style.WARNING(f"  held for review   : {result.held}"))
        self.stdout.write(f"  rejected/skipped  : {result.rejected}")
        if result.failed:
            self.stdout.write(self.style.ERROR(f"  errors            : {result.failed}"))

        if result.published < target:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    f"Only {result.published}/{target} published. Check the review "
                    "queue in the admin, and see logs/pipeline.log for gate details."
                )
            )
