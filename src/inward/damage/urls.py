from django.urls import path

from inward.damage import views

app_name = "damage"
urlpatterns = [
    path("", views.report_list, name="list"),
    path("new/", views.report_new, name="new"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/lines/add/", views.line_add, name="line_add"),
    path("<int:pk>/lines/<int:line_pk>/remove/", views.line_remove, name="line_remove"),
    path("<int:pk>/lines/<int:line_pk>/photos/", views.photo_add, name="photo_add"),
    path("<int:pk>/photos/<int:photo_pk>/remove/", views.photo_remove, name="photo_remove"),
    path("<int:pk>/photos/<int:photo_pk>/<str:size>/", views.photo, name="photo"),
    path("<int:pk>/reply/", views.reply_file, name="reply"),
    path("<int:pk>/submit/", views.submit, name="submit"),
    path("<int:pk>/cancel/", views.cancel, name="cancel"),
    path("<int:pk>/trx/add/", views.trx_add, name="trx_add"),
    path("<int:pk>/trx/<int:trx_pk>/remove/", views.trx_remove, name="trx_remove"),
    path("<int:pk>/reconcile/", views.recon, name="recon"),
    path("<int:pk>/accept-reason/", views.accept_reason, name="accept_reason"),
    path("<int:pk>/reasons/add/", views.category_add, name="category_add"),
]
