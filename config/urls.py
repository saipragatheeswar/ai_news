from django.contrib import admin
from django.contrib.sitemaps.views import sitemap
from django.urls import include, path

from news.sitemaps import ArticleSitemap, CategorySitemap

sitemaps = {"articles": ArticleSitemap, "categories": CategorySitemap}

urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "sitemap.xml",
        sitemap,
        {"sitemaps": sitemaps},
        name="django.contrib.sitemaps.views.sitemap",
    ),
    path("", include("news.urls")),
]
