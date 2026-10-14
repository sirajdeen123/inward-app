from django.urls import path

from inward.web import views

app_name = "web"
urlpatterns = [
    path("", views.home, name="home"),
    path("imports/preview/", views.import_preview, name="import_preview"),
    path("system/health/", views.health, name="health"),
    path("healthz", views.healthz, name="healthz"),
]
