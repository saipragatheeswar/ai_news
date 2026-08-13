from collections import OrderedDict
from datetime import timedelta
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView

from news.models import Article, Category, PipelineRun, Topic


def robots_txt(request):
    sitemap_url = request.build_absolute_uri("/sitemap.xml")
    body = f"User-agent: *\nAllow: /\nDisallow: /desk/\nDisallow: /admin/\nDisallow: /status/\n\nSitemap: {sitemap_url}\n"
    return HttpResponse(body, content_type="text/plain")


def ads_txt(request):
    """Publisher authorization for Google AdSense."""
    publisher = (settings.ADSENSE_CLIENT or "").removeprefix("ca-")
    if not publisher:
        return HttpResponse("\n", content_type="text/plain")
    body = f"google.com, {publisher}, DIRECT, f08c47fec0942fa0\n"
    return HttpResponse(body, content_type="text/plain")


def published_articles():
    return (
        Article.objects.filter(status=Article.Status.PUBLISHED)
        .select_related("category")
        .prefetch_related("images")
        .order_by("-published_at")
    )


class HomeView(ListView):
    template_name = "news/home.html"
    context_object_name = "articles"
    paginate_by = 13

    def get_queryset(self):
        return published_articles()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        page = context.get("page_obj")
        articles = list(context["articles"])

        # Newest 10 drive the lead rotator + Live box; sidebar Latest also rotates.
        on_first_page = page is None or page.number == 1
        if on_first_page and articles:
            newest = articles[:10]
            context["rotator"] = newest
            context["live_box"] = newest
            context["lead"] = newest[0]
            context["secondary"] = []
            context["articles"] = articles[10:]
            context["latest"] = newest
        else:
            context["rotator"] = []
            context["live_box"] = []
            context["lead"] = None
            context["secondary"] = []
            context["articles"] = articles
            context["latest"] = published_articles()[:10]

        context["ticker"] = published_articles()[:14]
        context["categories"] = _active_categories()
        context["most_read"] = (
            published_articles()
            .filter(published_at__gte=timezone.now() - timedelta(days=7))
            .order_by("-view_count")[:5]
        )
        context["today_count"] = published_articles().filter(
            published_at__date=timezone.localdate()
        ).count()
        return context


class CategoryView(ListView):
    template_name = "news/category.html"
    context_object_name = "articles"
    paginate_by = 12

    def get_queryset(self):
        self.category = get_object_or_404(Category, slug=self.kwargs["slug"])
        return published_articles().filter(category=self.category)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["category"] = self.category
        context["active_category"] = self.category.slug
        context["categories"] = _active_categories()
        context["latest"] = published_articles().exclude(category=self.category)[:8]
        return context


class SearchView(ListView):
    template_name = "news/search.html"
    context_object_name = "articles"
    paginate_by = 15

    def get_queryset(self):
        self.query = (self.request.GET.get("q") or "").strip()[:120]
        if len(self.query) < 2:
            return Article.objects.none()
        return published_articles().filter(
            Q(title__icontains=self.query)
            | Q(summary__icontains=self.query)
            | Q(body__icontains=self.query)
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["query"] = getattr(self, "query", "")
        context["search_active"] = True
        context["categories"] = _active_categories()
        return context


class ArticleView(DetailView):
    template_name = "news/article_detail.html"
    context_object_name = "article"

    def get_queryset(self):
        queryset = Article.objects.select_related("category").prefetch_related(
            "attributions", "images"
        )
        if self.request.user.is_staff:
            return queryset
        return queryset.filter(status=Article.Status.PUBLISHED)

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        # Readership decides what survives the retention window, so only count
        # real readers: not staff previewing, not bots we can cheaply spot.
        if self.object.is_live and not request.user.is_staff and not _looks_like_bot(request):
            self.object.register_view()
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        article = self.object
        context["active_category"] = article.category.slug
        context["categories"] = _active_categories()
        context["related"] = (
            published_articles()
            .filter(category=article.category)
            .exclude(pk=article.pk)[:4]
        )
        context["latest"] = published_articles().exclude(pk=article.pk)[:8]
        context["share"] = _share_links(self.request, article)
        return context


def _share_links(request, article) -> dict[str, str]:
    url = request.build_absolute_uri(article.get_absolute_url())
    encoded_url = quote(url, safe="")
    title = quote(article.title, safe="")
    return {
        "url": url,
        "whatsapp": f"https://api.whatsapp.com/send?text={title}%20{encoded_url}",
        "x": f"https://twitter.com/intent/tweet?text={title}&url={encoded_url}",
        "facebook": f"https://www.facebook.com/sharer/sharer.php?u={encoded_url}",
        "linkedin": f"https://www.linkedin.com/sharing/share-offsite/?url={encoded_url}",
        "telegram": f"https://t.me/share/url?url={encoded_url}&text={title}",
        "email": f"mailto:?subject={title}&body={encoded_url}",
    }


def _looks_like_bot(request) -> bool:
    agent = (request.META.get("HTTP_USER_AGENT") or "").lower()
    if not agent:
        return True
    return any(
        token in agent
        for token in ("bot", "crawl", "spider", "slurp", "curl", "wget", "python-requests")
    )


def about(request):
    return render(
        request,
        "news/about.html",
        {
            "categories": _active_categories(),
            "model_name": settings.OLLAMA_MODEL,
            "max_overlap": settings.MAX_NGRAM_OVERLAP,
            "daily_target": settings.DAILY_ARTICLE_TARGET,
            "auto_publish": settings.AUTO_PUBLISH,
            "retention_days": settings.RETENTION_DAYS,
        },
    )


def privacy(request):
    return render(
        request,
        "news/privacy.html",
        {"categories": _active_categories()},
    )


def terms(request):
    return render(
        request,
        "news/terms.html",
        {"categories": _active_categories()},
    )


def contact(request):
    return render(
        request,
        "news/contact.html",
        {"categories": _active_categories()},
    )


# --- Editorial desk -------------------------------------------------------


@staff_member_required
def desk(request):
    """Everything published, grouped by day, with delete in one click."""
    status = request.GET.get("status") or ""
    articles = (
        Article.objects.select_related("category")
        .prefetch_related("images")
        .order_by("-created_at")
    )
    if status in Article.Status.values:
        articles = articles.filter(status=status)

    by_day: "OrderedDict[object, list[Article]]" = OrderedDict()
    for article in articles[:400]:
        day = timezone.localtime(article.created_at).date()
        by_day.setdefault(day, []).append(article)

    totals = {
        row["status"]: row["n"]
        for row in Article.objects.values("status").annotate(n=Count("id"))
    }

    today = timezone.localdate()
    active_run = (
        PipelineRun.objects.filter(status=PipelineRun.Status.RUNNING)
        .order_by("-started_at")
        .first()
    )
    latest_run = PipelineRun.objects.order_by("-started_at").first()
    today_topics = (
        Topic.objects.filter(discovered_for=today)
        .select_related("category")
        .prefetch_related("articles")
        .order_by("-heat_score", "id")
    )
    topic_counts = {
        row["status"]: row["n"]
        for row in today_topics.values("status").annotate(n=Count("id"))
    }

    return render(
        request,
        "news/desk.html",
        {
            "categories": _active_categories(),
            "days": by_day.items(),
            "totals": totals,
            "total_views": Article.objects.aggregate(n=Sum("view_count"))["n"] or 0,
            "runs": PipelineRun.objects.all()[:8],
            "active_run": active_run,
            "latest_run": latest_run,
            "today_topics": today_topics,
            "topic_counts": topic_counts,
            "today": today,
            "status_filter": status,
            "statuses": Article.Status.choices,
            "retention_days": settings.RETENTION_DAYS,
            "retention_min_views": settings.RETENTION_MIN_VIEWS,
            "max_ngram_overlap": settings.MAX_NGRAM_OVERLAP,
        },
    )


@staff_member_required
@require_POST
def desk_action(request, slug):
    article = get_object_or_404(Article, slug=slug)
    action = request.POST.get("action")

    if action == "delete":
        title = article.title
        for image in article.images.all():
            image.delete()
        article.delete()
        messages.success(request, f"Deleted “{title}”.")
    elif action == "publish":
        article.status = Article.Status.PUBLISHED
        article.published_at = article.published_at or timezone.now()
        article.save()
        messages.success(request, f"Published “{article.title}”.")
    elif action == "unpublish":
        article.status = Article.Status.NEEDS_REVIEW
        article.published_at = None
        article.save()
        messages.info(request, f"Moved “{article.title}” back to review.")
    elif action == "reject":
        article.status = Article.Status.REJECTED
        article.published_at = None
        article.save()
        messages.warning(request, f"Rejected “{article.title}”.")
    else:
        messages.error(request, "Unknown action.")

    return redirect(request.POST.get("next") or "news:desk")


@staff_member_required
def pipeline_status(request):
    """Operational view of recent runs — staff only."""
    return render(
        request,
        "news/status.html",
        {
            "categories": _active_categories(),
            "runs": PipelineRun.objects.all()[:14],
            "counts": {
                "published": Article.objects.filter(
                    status=Article.Status.PUBLISHED
                ).count(),
                "review": Article.objects.filter(
                    status=Article.Status.NEEDS_REVIEW
                ).count(),
                "rejected": Article.objects.filter(
                    status=Article.Status.REJECTED
                ).count(),
                "today": published_articles()
                .filter(published_at__date=timezone.localdate())
                .count(),
            },
        },
    )


def _active_categories():
    return (
        Category.objects.filter(is_active=True)
        .annotate(
            live_count=Count(
                "articles", filter=Q(articles__status=Article.Status.PUBLISHED)
            )
        )
        .filter(live_count__gt=0)
    )
