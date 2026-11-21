from django.urls import path

from inward.core import views_masterfiles, views_notify, views_packaging

app_name = "core"
urlpatterns = [
    path("notifications/", views_notify.notification_emails, name="notifications"),
    path("files/", views_masterfiles.home, name="masterfiles"),
    path("files/<str:key>.xlsx", views_masterfiles.download, name="masterfile_download"),
    path("files/<str:key>/upload/", views_masterfiles.upload, name="masterfile_upload"),
    path("files/upload/<int:pk>/", views_masterfiles.preview, name="masterfile_preview"),
    path("files/upload/<int:pk>/decide/", views_masterfiles.decide, name="masterfile_decide"),
    path("packaging/", views_packaging.packaging_home, name="packaging"),
    path("packaging/upload/", views_packaging.packaging_upload, name="packaging_upload"),
    path("packaging/upload/<int:pk>/", views_packaging.packaging_preview, name="packaging_preview"),
    path("packaging/review/", views_packaging.packaging_review, name="packaging_review"),
    path("packaging/download.xlsx", views_packaging.packaging_xlsx, name="packaging_xlsx"),
    path("packaging/<str:sku>/", views_packaging.packaging_sku, name="packaging_sku"),
]
