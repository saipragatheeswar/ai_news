from django.conf import settings
from django.contrib.syndication.views import Feed
from django.urls import reverse

from news.models import Article


class LatestArticlesFeed(Feed):
    title = f"{settings.SITE_NAME} - latest stories"
    description = settings.SITE_TAGLINE

    def link(self):
        return reverse("news:home")

    def items(self):
        return (
            Article.objects.filter(status=Article.Status.PUBLISHED)
            .select_related("category")
            .order_by("-published_at")[:30]
        )

    def item_title(self, item):
        return item.title

    def item_description(self, item):
        return item.summary

    def item_pubdate(self, item):
        return item.published_at

    def item_categories(self, item):
        return [item.category.name]
