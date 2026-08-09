"""Discover and rank today's hot topics without writing anything."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from news.pipeline import topics as topic_discovery
from news.pipeline.tavily_client import NewsSearch, TavilyNotConfigured


class Command(BaseCommand):
    help = "Search for today's trending stories and store them as topics."

    def add_arguments(self, parser):
        parser.add_argument("--target", type=int, default=settings.DAILY_ARTICLE_TARGET)
        parser.add_argument(
            "--days",
            type=int,
            default=2,
            help="How far back to look for news (default: 2 days).",
        )

    def handle(self, *args, **options):
        try:
            search = NewsSearch()
        except TavilyNotConfigured as exc:
            raise CommandError(str(exc)) from exc

        discovered = topic_discovery.discover(
            search, target=options["target"], days=options["days"]
        )
        if not discovered:
            self.stdout.write(
                self.style.WARNING(
                    "No new topics. Everything found today was already covered."
                )
            )
            return

        self.stdout.write(self.style.SUCCESS(f"Stored {len(discovered)} topics:"))
        for topic in sorted(discovered, key=lambda t: t.heat_score, reverse=True):
            self.stdout.write(
                f"  [{topic.heat_score:6.2f}] {topic.category.name:<18} "
                f"{topic.domain_count} outlets  {topic.label[:80]}"
            )
