"""Admin acts as the editorial desk: review held articles, publish or reject."""

from django.contrib import admin, messages
from django.utils import timezone
from django.utils.html import format_html

from news.models import (
    Article,
    ArticleImage,
    Attribution,
    Category,
    PipelineRun,
    Source,
    Topic,
)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "ordering", "article_count")
    list_editable = ("is_active", "ordering")
    prepopulated_fields = {"slug": ("name",)}

    @admin.display(description="articles")
    def article_count(self, obj):
        return obj.articles.count()


class SourceInline(admin.TabularInline):
    model = Source
    extra = 0
    fields = ("domain", "title", "url", "relevance", "published_at")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj):
        return False


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = (
        "label",
        "category",
        "heat_score",
        "domain_count",
        "status",
        "discovered_for",
    )
    list_filter = ("status", "category", "discovered_for")
    search_fields = ("label", "query")
    readonly_fields = ("fingerprint", "created_at", "heat_score")
    inlines = [SourceInline]
    date_hierarchy = "discovered_for"


class AttributionInline(admin.TabularInline):
    model = Attribution
    extra = 0
    fields = ("domain", "title", "url")


class ArticleImageInline(admin.TabularInline):
    model = ArticleImage
    extra = 0
    fields = ("preview", "file", "alt_text", "credit", "licence", "source_page")
    readonly_fields = ("preview",)

    @admin.display(description="preview")
    def preview(self, obj):
        if not obj.file:
            return "-"
        return format_html(
            '<img src="{}" style="height:60px;border-radius:4px">', obj.file.url
        )


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "category",
        "status_badge",
        "word_count",
        "view_count",
        "originality",
        "flag_count",
        "published_at",
    )
    list_filter = ("status", "category", "published_at")
    search_fields = ("title", "summary", "body")
    readonly_fields = (
        "topic",
        "safety_flags",
        "review_notes",
        "max_ngram_overlap",
        "longest_common_run",
        "attempts",
        "model_name",
        "word_count",
        "view_count",
        "last_viewed_at",
        "created_at",
        "updated_at",
    )
    inlines = [ArticleImageInline, AttributionInline]
    actions = ["approve_and_publish", "reject", "send_back_to_review"]
    date_hierarchy = "created_at"
    fieldsets = (
        (
            None,
            {"fields": ("title", "slug", "category", "summary", "body", "status")},
        ),
        (
            "Editorial checks",
            {
                "fields": (
                    "safety_flags",
                    "review_notes",
                    "max_ngram_overlap",
                    "longest_common_run",
                )
            },
        ),
        (
            "Provenance",
            {
                "classes": ("collapse",),
                "fields": (
                    "topic",
                    "model_name",
                    "attempts",
                    "word_count",
                    "view_count",
                    "last_viewed_at",
                    "published_at",
                    "created_at",
                    "updated_at",
                ),
            },
        ),
    )
    prepopulated_fields = {"slug": ("title",)}

    @admin.display(description="status", ordering="status")
    def status_badge(self, obj):
        colours = {
            Article.Status.PUBLISHED: "#137333",
            Article.Status.NEEDS_REVIEW: "#b06000",
            Article.Status.REJECTED: "#c5221f",
            Article.Status.DRAFT: "#5f6368",
        }
        return format_html(
            '<b style="color:{}">{}</b>',
            colours.get(obj.status, "#5f6368"),
            obj.get_status_display(),
        )

    @admin.display(description="overlap")
    def originality(self, obj):
        return f"{obj.max_ngram_overlap:.1%} / run {obj.longest_common_run}"

    @admin.display(description="flags")
    def flag_count(self, obj):
        count = len(obj.safety_flags or [])
        if not count:
            return "-"
        return format_html('<span style="color:#b06000">{}</span>', count)

    @admin.action(description="Approve and publish selected articles")
    def approve_and_publish(self, request, queryset):
        published = 0
        blocked = 0
        for article in queryset.exclude(status=Article.Status.PUBLISHED):
            if _has_hard_block(article):
                blocked += 1
            article.status = Article.Status.PUBLISHED
            article.published_at = article.published_at or timezone.now()
            article.save()
            published += 1

        self.message_user(
            request, f"Published {published} article(s).", messages.SUCCESS
        )
        if blocked:
            self.message_user(
                request,
                f"{blocked} of those had hard safety blocks against them. "
                "Re-read them and unpublish anything that should not be live.",
                messages.WARNING,
            )

    @admin.action(description="Reject selected articles")
    def reject(self, request, queryset):
        count = queryset.update(status=Article.Status.REJECTED, published_at=None)
        self.message_user(request, f"Rejected {count} article(s).", messages.WARNING)

    @admin.action(description="Move selected articles back to review queue")
    def send_back_to_review(self, request, queryset):
        count = queryset.update(status=Article.Status.NEEDS_REVIEW, published_at=None)
        self.message_user(request, f"{count} article(s) moved to review.")


@admin.register(PipelineRun)
class PipelineRunAdmin(admin.ModelAdmin):
    list_display = (
        "run_for",
        "status",
        "target_count",
        "articles_published",
        "articles_held",
        "articles_rejected",
        "runtime",
        "started_at",
    )
    list_filter = ("status", "run_for")
    readonly_fields = [f.name for f in PipelineRun._meta.fields]

    @admin.display(description="runtime")
    def runtime(self, obj):
        seconds = obj.duration_seconds
        if seconds is None:
            return "running..."
        return f"{seconds / 60:.1f} min"

    def has_add_permission(self, request):
        return False


def _has_hard_block(article: Article) -> bool:
    return any(
        isinstance(flag, dict) and flag.get("severity") == "block"
        for flag in article.safety_flags or []
    )


admin.site.site_header = "AI News Desk"
admin.site.site_title = "AI News Desk"
admin.site.index_title = "Editorial control"
