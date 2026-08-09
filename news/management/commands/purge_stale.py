"""Delete stories nobody read, along with their images, to cap storage growth.

Anything older than the retention window that failed to attract readers is
removed outright: the row, its sources, its attributions and its image files.
Stories with real readership are kept indefinitely.
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from news.models import Article, ArticleImage, Topic


class Command(BaseCommand):
    help = "Purge unread articles and their images once past the retention window."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=settings.RETENTION_DAYS)
        parser.add_argument("--min-views", type=int, default=settings.RETENTION_MIN_VIEWS)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting it.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        min_views = options["min_views"]
        dry_run = options["dry_run"]
        quiet = options["verbosity"] == 0
        cutoff = timezone.now() - timedelta(days=days)

        def say(message, style=None):
            if not quiet:
                self.stdout.write(style(message) if style else message)

        stale = Article.objects.filter(created_at__lt=cutoff, view_count__lt=min_views)
        if settings.RETENTION_KEEP_REVIEWED:
            # Don't delete anything an editor still has to look at.
            stale = stale.exclude(status=Article.Status.NEEDS_REVIEW)

        count = stale.count()
        if not count:
            say(
                f"Nothing to purge: no articles older than {days} days with "
                f"fewer than {min_views} views."
            )
        else:
            say(
                f"{'Would delete' if dry_run else 'Deleting'} {count} article(s) "
                f"older than {days} days with fewer than {min_views} views:"
            )
            for article in stale.order_by("created_at")[:40]:
                say(
                    f"  [{article.view_count} views] {article.created_at:%Y-%m-%d} "
                    f"{article.title[:70]}"
                )
            if not dry_run:
                # Delete images individually so their files leave disk too.
                for image in ArticleImage.objects.filter(article__in=stale):
                    image.delete()
                stale.delete()

        # Topics whose articles are gone are dead weight, and their sources hold
        # the largest text blobs in the database.
        orphan_topics = Topic.objects.filter(
            created_at__lt=cutoff, articles__isnull=True
        )
        orphans = orphan_topics.count()
        if orphans:
            say(
                f"{'Would delete' if dry_run else 'Deleting'} {orphans} "
                "unused topic(s) and their cached source text."
            )
            if not dry_run:
                orphan_topics.delete()

        if not dry_run:
            say("Purge complete.", self.style.SUCCESS)
