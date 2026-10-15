from django.urls import path

from inward.core import views_packaging

app_name = "core"
urlpatterns = [
    path("packaging/", views_packaging.packaging_home, name="packaging"),
    path("packaging/upload/", views_packaging.packaging_upload, name="packaging_upload"),
    path("packaging/upload/<int:pk>/", views_packaging.packaging_preview, name="packaging_preview"),
    path("packaging/review/", views_packaging.packaging_review, name="packaging_review"),
    path("packaging/<str:sku>/", views_packaging.packaging_sku, name="packaging_sku"),
]
