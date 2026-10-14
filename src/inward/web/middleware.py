"""Strict browser security headers (SoT 14.5). No inline scripts or styles anywhere in the app."""
from django.conf import settings

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
       "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'; "
       "object-src 'none'")


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not request.path.startswith("/" + settings.INWARD["ADMIN_URL"]) and "Content-Security-Policy" not in response:
            response["Content-Security-Policy"] = CSP
        response["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
        response["Cross-Origin-Opener-Policy"] = "same-origin"
        return response
