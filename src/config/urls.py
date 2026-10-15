from django.conf import settings
from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "Stock Inward · Depot 035 · RAINBOW LOGISTICS (CFA)"
admin.site.site_title = "Stock Inward administration"

urlpatterns = [
    path(settings.INWARD["ADMIN_URL"], admin.site.urls),  # non-default admin address (SoT 14.5)
    path("accounts/", include("inward.accounts.urls")),
    path("sit/", include("inward.sit.urls")),
    path("iw/", include("inward.iw.urls")),
    path("", include("inward.web.urls")),
]
