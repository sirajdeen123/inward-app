from django.conf import settings
from django.contrib import admin
from django.shortcuts import redirect
from django.urls import include, path

admin.site.site_header = "Stock Inward · Depot 035 · RAINBOW LOGISTICS (CFA)"
admin.site.site_title = "Stock Inward administration"


def _app_login(request):
    """Masters & users has no sign-in of its own: everyone signs in on the app page (with 2-step code); the admin
    login only accepted manager accounts, so a cashier signing in there was refused (UAT 18-Oct-2026)."""
    return redirect(f"/accounts/login/?next=/{settings.INWARD['ADMIN_URL']}")

urlpatterns = [
    path(f"{settings.INWARD['ADMIN_URL']}login/", _app_login),
    path(f"{settings.INWARD['ADMIN_URL']}logout/", lambda request: redirect("/")),   # sign out from the app menu
    path(settings.INWARD["ADMIN_URL"], admin.site.urls),  # non-default admin address (SoT 14.5)
    path("accounts/", include("inward.accounts.urls")),
    path("sit/", include("inward.sit.urls")),
    path("iw/", include("inward.iw.urls")),
    path("irr/", include("inward.irr.urls")),
    path("cash/", include("inward.cash.urls")),
    path("damage/", include("inward.damage.urls")),
    path("irr-recon/", include("inward.irrecon.urls")),
    path("masters/", include("inward.core.urls")),
    path("", include("inward.web.urls")),
]
