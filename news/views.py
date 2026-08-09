from django.conf import settings
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.generic import DetailView, ListView

from news.models import Article, Category, PipelineRun


def published_articles():
    return (
        Article.objects.filter(status=Article.Status.PUBLISHED)
        .select_related("category")
        .order_by("-published_at")
    )


class HomeView(ListView):
    template_name = "news/home.html"
    context_object_name = "articles"
    paginate_by = 12

    def get_queryset(self):
        return published_articles()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        page = context.get("page_obj")
        articles = list(context["articles"])

        # The newest story runs large, but only at the top of the first page.
        on_first_page = page is None or page.number == 1
        context["lead"] = articles[0] if on_first_page and articles else None
        context["articles"] = articles[1:] if context["lead"] else articles

        context["categories"] = _active_categories()
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
        context["categories"] = _active_categories()
        return context


class ArticleView(DetailView):
    template_name = "news/article_detail.html"
    context_object_name = "article"

    def get_queryset(self):
        queryset = Article.objects.select_related("category").prefetch_related(
            "attributions"
        )
        if self.request.user.is_staff:
            return queryset
        return queryset.filter(status=Article.Status.PUBLISHED)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["categories"] = _active_categories()
        context["related"] = (
            published_articles()
            .filter(category=self.object.category)
            .exclude(pk=self.object.pk)[:4]
        )
        return context


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
        },
    )


def pipeline_status(request):
    """Lightweight operational view of the last week of runs."""
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
