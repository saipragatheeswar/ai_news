from django.contrib.sitemaps import Sitemap

from news.models import Article, Category


class ArticleSitemap(Sitemap):
    changefreq = "daily"
    priority = 0.8
    limit = 500

    def items(self):
        return Article.objects.filter(status=Article.Status.PUBLISHED).order_by(
            "-published_at"
        )

    def lastmod(self, obj):
        return obj.updated_at


class CategorySitemap(Sitemap):
    changefreq = "hourly"
    priority = 0.5

    def items(self):
        return Category.objects.filter(is_active=True)
