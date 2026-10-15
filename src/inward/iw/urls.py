from django.urls import path

from inward.iw import views

app_name = "iw"
urlpatterns = [
    path("", views.iw_list, name="list"),
    path("new/", views.iw_new, name="new"),
    path("<int:pk>/", views.iw_detail, name="detail"),
    path("<int:pk>/edit/", views.iw_edit, name="edit"),
    path("<int:pk>/void/", views.iw_void, name="void"),
    path("<int:pk>/discard/", views.iw_discard, name="discard"),
]
