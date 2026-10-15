from __future__ import annotations

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import authenticate, login, logout
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.views.decorators.http import require_http_methods, require_POST
from django_otp import login as otp_login

from inward.accounts import twofactor
from inward.core import audit


class LoginForm(forms.Form):
    username = forms.CharField(max_length=64)
    password = forms.CharField(widget=forms.PasswordInput)


class CodeForm(forms.Form):
    code = forms.CharField(max_length=20, label="Code from your authenticator app (or a recovery code)",
                           widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric"}))
    trust = forms.BooleanField(required=False, label=f"Trust this device for {settings.TRUSTED_DEVICE_DAYS} days")


def _next(request, default="web:home"):
    target = request.POST.get("next") or request.GET.get("next")
    if target and url_has_allowed_host_and_scheme(target, {request.get_host()}, request.is_secure()):
        return target
    return default


@require_http_methods(["GET", "POST"])
def login_view(request):
    form = LoginForm(request.POST or None)
    error = None
    if request.method == "POST" and form.is_valid():
        user = authenticate(request, username=form.cleaned_data["username"].strip().lower(),
                            password=form.cleaned_data["password"])
        if user is None:
            error = "Sign-in failed. After 5 failed attempts the account is locked for 15 minutes."
        else:
            login(request, user)
            audit.record("LOGIN", "app_user", user.pk, user=user, ip=request.META.get("REMOTE_ADDR"))
            device = twofactor.confirmed_totp(user)
            if device and twofactor.is_trusted(user, request.COOKIES.get(twofactor.TRUSTED_COOKIE)):
                otp_login(request, device)
                return redirect(_next(request))
            target = reverse("accounts:two_factor")
            nxt = _next(request, "")
            return redirect(f"{target}?{urlencode({'next': nxt})}" if nxt else target)
    return render(request, "accounts/login.html", {"form": form, "error": error, "next": _next(request, "")})


@require_http_methods(["GET", "POST"])
def two_factor_view(request):
    user = request.user
    if not user.is_authenticated:
        return redirect("accounts:login")
    if user.is_verified():
        return redirect(_next(request))
    device = twofactor.confirmed_totp(user)
    form = CodeForm(request.POST or None)
    error = None
    if device is None:  # first sign-in: enrol the authenticator app
        pending = twofactor.pending_totp(user)
        if request.method == "POST" and form.is_valid():
            codes = twofactor.confirm_enrolment(user, form.cleaned_data["code"])
            if codes is None:
                error = "That code did not match. Check the time on your phone and try the newest code."
            else:
                otp_login(request, twofactor.confirmed_totp(user))
                return render(request, "accounts/recovery_codes.html", {"codes": codes, "next": _next(request)})
        return render(request, "accounts/two_factor_enrol.html", {
            "form": form, "error": error, "qr_svg": twofactor.qr_svg(pending),
            "secret": pending.bin_key.hex(), "next": _next(request, "")})
    if request.method == "POST" and form.is_valid():
        used = twofactor.verify(user, form.cleaned_data["code"])
        if used is None:
            error = "That code did not match."
            audit.record("2FA_FAILED", "app_user", user.pk, user=user, ip=request.META.get("REMOTE_ADDR"))
        else:
            otp_login(request, used)
            response = redirect(_next(request))
            if form.cleaned_data["trust"]:
                token = twofactor.trust_this_device(user, request.META.get("HTTP_USER_AGENT", ""))
                response.set_cookie(twofactor.TRUSTED_COOKIE, token, max_age=settings.TRUSTED_DEVICE_DAYS * 86400,
                                    secure=request.is_secure(), httponly=True, samesite="Lax")
            return response
    return render(request, "accounts/two_factor_verify.html", {"form": form, "error": error, "next": _next(request, "")})


@login_required
def approvals_view(request):
    from inward.accounts.approvals import pending_for
    return render(request, "accounts/approvals.html", {"pending": pending_for(request.user)})


@require_POST
@login_required
def decide_view(request, pk):
    from inward.accounts.approvals import decide_and_apply
    approve = request.POST.get("decision") == "approve"
    try:
        req = decide_and_apply(request.user, pk, approve, request.POST.get("note", "")[:300])
        messages.success(request, f"Request {req.pk} {req.status.lower()}.")
    except Exception as exc:  # noqa: BLE001 - PermissionDenied, already decided, effect refused
        messages.error(request, f"Not done: {exc}")
    return redirect("accounts:approvals")


@require_POST
def logout_view(request):
    if request.user.is_authenticated:
        audit.record("LOGOUT", "app_user", request.user.pk, user=request.user)
    logout(request)
    return redirect("accounts:login")
