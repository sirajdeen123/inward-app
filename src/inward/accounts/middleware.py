"""Every signed-in user must pass two-factor sign-in before any screen (SoT 14.5)."""
from __future__ import annotations

from django.shortcuts import redirect
from django.urls import reverse

OPEN_PREFIXES = ("/accounts/login/", "/accounts/logout/", "/accounts/two-factor/", "/static/", "/healthz")


class TwoFactorRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and not user.is_verified():
            if not request.path.startswith(OPEN_PREFIXES):
                return redirect(reverse("accounts:two_factor") + f"?next={request.get_full_path()}")
        return self.get_response(request)
