"""Data model for discovered topics, gathered sources and generated articles."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from django.db import models
from django.urls import reverse
from django.utils import timezone
from slugify import slugify


class Category(models.Model):
    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(max_length=80, unique=True)
    description = models.CharField(max_length=300, blank=True)
    is_active = models.BooleanField(default=True)
    ordering = models.PositiveSmallIntegerField(default=100)

    class Meta:
        verbose_name_plural = "categories"
        ordering = ["ordering", "name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:80]
        super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        return reverse("news:category", args=[self.slug])


class Topic(models.Model):
    """A candidate story discovered from search, before it becomes an article."""

    class Status(models.TextChoices):
        DISCOVERED = "discovered", "Discovered"
        SELECTED = "selected", "Selected for writing"
        WRITTEN = "written", "Article written"
        SKIPPED = "skipped", "Skipped"
        FAILED = "failed", "Failed"

    label = models.CharField(max_length=300)
    fingerprint = models.CharField(max_length=64, db_index=True)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name="topics"
    )
    query = models.CharField(max_length=300, blank=True)
    heat_score = models.FloatField(default=0.0)
    source_count = models.PositiveSmallIntegerField(default=0)
    domain_count = models.PositiveSmallIntegerField(default=0)
    discovered_for = models.DateField(default=timezone.localdate, db_index=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DISCOVERED
    )
    skip_reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-heat_score", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["fingerprint", "discovered_for"],
                name="unique_topic_fingerprint_per_day",
            )
        ]

    def __str__(self) -> str:
        return self.label

    @staticmethod
    def build_fingerprint(label: str) -> str:
        """Stable key for a topic so the same story is not covered twice."""
        normalised = " ".join(sorted(_keywords(label)))
        return hashlib.sha256(normalised.encode("utf-8")).hexdigest()

    def save(self, *args, **kwargs):
        if not self.fingerprint:
            self.fingerprint = self.build_fingerprint(self.label)
        super().save(*args, **kwargs)


class Source(models.Model):
    """A single search result backing a topic. Never republished verbatim."""

    topic = models.ForeignKey(Topic, on_delete=models.CASCADE, related_name="sources")
    url = models.URLField(max_length=1000)
    domain = models.CharField(max_length=200, blank=True)
    title = models.CharField(max_length=500, blank=True)
    snippet = models.TextField(blank=True)
    raw_content = models.TextField(blank=True)
    relevance = models.FloatField(default=0.0)
    published_at = models.DateTimeField(null=True, blank=True)
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-relevance"]
        constraints = [
            models.UniqueConstraint(
                fields=["topic", "url"], name="unique_source_url_per_topic"
            )
        ]

    def __str__(self) -> str:
        return self.title or self.url

    @property
    def reference_text(self) -> str:
        return f"{self.title}\n{self.raw_content or self.snippet}".strip()


def article_image_path(instance, filename: str) -> str:
    day = (instance.article.published_at or timezone.now()).strftime("%Y/%m/%d")
    extension = Path(filename).suffix.lower() or ".jpg"
    return f"articles/{day}/{uuid.uuid4().hex}{extension}"


class Article(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        NEEDS_REVIEW = "needs_review", "Needs human review"
        PUBLISHED = "published", "Published"
        REJECTED = "rejected", "Rejected"

    topic = models.ForeignKey(
        Topic, on_delete=models.SET_NULL, null=True, blank=True, related_name="articles"
    )
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name="articles"
    )
    title = models.CharField(max_length=250)
    slug = models.SlugField(max_length=270, unique=True)
    summary = models.CharField(max_length=400, blank=True)
    body = models.TextField(help_text="Plain text; blank lines separate paragraphs.")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True
    )

    # Compliance and originality bookkeeping.
    safety_flags = models.JSONField(default=list, blank=True)
    review_notes = models.TextField(blank=True)
    max_ngram_overlap = models.FloatField(default=0.0)
    longest_common_run = models.PositiveSmallIntegerField(default=0)
    attempts = models.PositiveSmallIntegerField(default=1)

    model_name = models.CharField(max_length=120, blank=True)
    word_count = models.PositiveIntegerField(default=0)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Readership, used to decide what is worth keeping past the retention window.
    view_count = models.PositiveIntegerField(default=0, db_index=True)
    last_viewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-published_at", "-created_at"]
        indexes = [models.Index(fields=["status", "-published_at"])]

    def __str__(self) -> str:
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(Article, self.title)
        # Normalize line endings so paragraph breaks always survive storage.
        if self.body:
            normalised = self.body.replace("\r\n", "\n").replace("\r", "\n")
            # Collapse 3+ blank lines; keep blank-line paragraph separators.
            while "\n\n\n" in normalised:
                normalised = normalised.replace("\n\n\n", "\n\n")
            self.body = normalised.strip()
        self.word_count = len(self.body.split())
        if self.status == self.Status.PUBLISHED and self.published_at is None:
            self.published_at = timezone.now()
        super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        return reverse("news:article", args=[self.slug])

    @property
    def paragraphs(self) -> list[str]:
        """Split body into display paragraphs.

        Writers sometimes emit ``\\r\\n`` or single newlines; treat blank lines
        as paragraph breaks and fall back to single newlines when needed.
        """
        text = (self.body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            return []
        blocks = [p.strip() for p in text.split("\n\n") if p.strip()]
        if len(blocks) > 1:
            return blocks
        # One blob with single newlines (common from small models).
        lines = [p.strip() for p in text.split("\n") if p.strip()]
        return lines if len(lines) > 1 else blocks

    @property
    def is_live(self) -> bool:
        return self.status == self.Status.PUBLISHED

    @property
    def lead_image(self):
        return self.images.first()

    @property
    def reading_minutes(self) -> int:
        return max(1, round(self.word_count / 200))

    def register_view(self) -> None:
        """Counted with an UPDATE so concurrent reads cannot clobber each other."""
        Article.objects.filter(pk=self.pk).update(
            view_count=models.F("view_count") + 1, last_viewed_at=timezone.now()
        )


class ArticleImage(models.Model):
    """An openly-licensed image stored on our own server.

    We never copy pictures from the outlets we summarise: news photography is
    almost always agency-licensed and is the most aggressively enforced form of
    copyright online. Everything here carries a licence we can actually point
    to, and the credit line is rendered with the image.
    """

    article = models.ForeignKey(
        Article, on_delete=models.CASCADE, related_name="images"
    )
    file = models.ImageField(upload_to=article_image_path)
    alt_text = models.CharField(max_length=300, blank=True)
    credit = models.CharField(max_length=200, blank=True)
    licence = models.CharField(max_length=80, blank=True)
    licence_url = models.URLField(max_length=500, blank=True)
    source_page = models.URLField(max_length=1000, blank=True)
    provider = models.CharField(max_length=60, blank=True)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return self.alt_text or Path(self.file.name).name

    @property
    def attribution(self) -> str:
        parts = [p for p in (self.credit, self.licence) if p]
        return " / ".join(parts)

    def delete(self, *args, **kwargs):
        self.file.delete(save=False)
        super().delete(*args, **kwargs)


class Attribution(models.Model):
    """Public credit line pointing readers back to the reporting we relied on."""

    article = models.ForeignKey(
        Article, on_delete=models.CASCADE, related_name="attributions"
    )
    title = models.CharField(max_length=500, blank=True)
    url = models.URLField(max_length=1000)
    domain = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["domain"]
        constraints = [
            models.UniqueConstraint(
                fields=["article", "url"], name="unique_attribution_per_article"
            )
        ]

    def __str__(self) -> str:
        return self.domain or self.url


class PipelineRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"

    run_for = models.DateField(default=timezone.localdate, db_index=True)
    target_count = models.PositiveSmallIntegerField(default=0)
    topics_discovered = models.PositiveSmallIntegerField(default=0)
    articles_published = models.PositiveSmallIntegerField(default=0)
    articles_held = models.PositiveSmallIntegerField(default=0)
    articles_rejected = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.RUNNING
    )
    notes = models.TextField(blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"Run {self.run_for} ({self.status})"

    @property
    def duration_seconds(self) -> float | None:
        if not self.finished_at:
            return None
        return (self.finished_at - self.started_at).total_seconds()


_STOPWORDS = {
    "a", "about", "after", "against", "all", "also", "amid", "an", "and", "announces",
    "another", "any", "are", "as", "at", "back", "be", "been", "before", "being",
    "best", "big", "but", "by", "can", "could", "day", "did", "do", "does", "down",
    "during", "first", "for", "from", "get", "gets", "had", "has", "have", "he",
    "her", "here", "him", "his", "how", "i", "if", "in", "into", "is", "it", "its",
    "just", "latest", "live", "may", "might", "more", "most", "new", "news", "no",
    "not", "now", "of", "off", "on", "one", "only", "or", "other", "our", "out",
    "over", "report", "reports", "said", "says", "she", "should", "so", "some",
    "still", "such", "than", "that", "the", "their", "them", "then", "there",
    "these", "they", "this", "those", "to", "today", "top", "two", "under", "up",
    "update", "updates", "us", "very", "was", "we", "were", "what", "when", "where",
    "which", "while", "who", "why", "will", "with", "would", "you", "your",
}


def _keywords(text: str) -> set[str]:
    words = {
        "".join(ch for ch in word.lower() if ch.isalnum())
        for word in text.split()
    }
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS} or {
        w for w in words if w
    }


def _unique_slug(model, title: str) -> str:
    base = slugify(title)[:240] or "article"
    candidate = base
    suffix = 2
    while model.objects.filter(slug=candidate).exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate
