from django.urls import path

from inward.irrecon import views

app_name = "irrecon"
urlpatterns = [
    path("", views.home, name="home"),
    path("run/", views.run_now, name="run"),
    path("mark/", views.mark, name="mark"),
    path("unmark/<int:row_id>/", views.unmark, name="unmark"),
]
