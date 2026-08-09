from django.conf import settings
from django.conf.urls.static import static
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

if settings.DEBUG:
    # In production nginx serves MEDIA_ROOT directly; see the README.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
