from django.urls import path

from inward.sit import views

app_name = "sit"
urlpatterns = [
    path("", views.sit_home, name="home"),
    path("fetch/", views.sit_fetch, name="fetch"),
    path("upload/", views.sit_upload, name="upload"),
    path("ships/", views.ship_list, name="ships"),
    path("ships/pre-go-live/", views.mark_pre_go_live, name="mark_pre_go_live"),
    path("ships/<str:ship_id>/", views.ship_detail, name="ship"),
    path("imports/", views.import_list, name="imports"),
    path("imports/<int:pk>/", views.import_detail, name="import"),
]
