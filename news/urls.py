from django.urls import path

from news import views
from news.feeds import LatestArticlesFeed

app_name = "news"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),
    path("about/", views.about, name="about"),
    path("privacy/", views.privacy, name="privacy"),
    path("terms/", views.terms, name="terms"),
    path("contact/", views.contact, name="contact"),
    path("search/", views.SearchView.as_view(), name="search"),
    path("status/", views.pipeline_status, name="status"),
    path("desk/", views.desk, name="desk"),
    path("desk/<slug:slug>/action/", views.desk_action, name="desk_action"),
    path("feed/", LatestArticlesFeed(), name="feed"),
    path("category/<slug:slug>/", views.CategoryView.as_view(), name="category"),
    path("story/<slug:slug>/", views.ArticleView.as_view(), name="article"),
]
