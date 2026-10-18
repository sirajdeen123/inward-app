from django.urls import path

from inward.irr import views

app_name = "irr"
urlpatterns = [
    path("fetch/", views.irr_fetch_now, name="fetch"),
    path("auto-settings/", views.irr_auto_settings, name="auto_settings"),
    path("", views.irr_home, name="home"),
    path("upload/", views.irr_upload, name="upload"),
]
