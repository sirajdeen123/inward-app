from django.urls import path

from inward.irr import views

app_name = "irr"
urlpatterns = [
    path("", views.irr_home, name="home"),
    path("upload/", views.irr_upload, name="upload"),
]
