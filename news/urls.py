from django.urls import path

from news import views
from news.feeds import LatestArticlesFeed

app_name = "news"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),
    path("about/", views.about, name="about"),
    path("status/", views.pipeline_status, name="status"),
    path("feed/", LatestArticlesFeed(), name="feed"),
    path("category/<slug:slug>/", views.CategoryView.as_view(), name="category"),
    path("story/<slug:slug>/", views.ArticleView.as_view(), name="article"),
]
