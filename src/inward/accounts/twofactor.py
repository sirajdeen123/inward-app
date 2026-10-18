"""Two-factor sign-in for every user (SoT 14.5): authenticator app (TOTP), one-time recovery codes,
trusted devices that may skip the code for TRUSTED_DEVICE_DAYS, and audited resets."""
from __future__ import annotations

import hashlib
import io
import secrets
from datetime import timedelta

import qrcode
import qrcode.image.svg
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from inward.accounts.models import TrustedDevice, User
from inward.core import audit

TRUSTED_COOKIE = "inward_trusted"


def trusted_cookie(user: User) -> str:
    """One cookie per user: several people (or one tester with several users) share a browser, and a single
    cookie was overwritten by the last user who ticked "trust", so the others were asked again (UAT 18-Oct-2026)."""
    return f"{TRUSTED_COOKIE}_{user.pk}"
RECOVERY_CODES = 10


def confirmed_totp(user: User) -> TOTPDevice | None:
    return TOTPDevice.objects.filter(user=user, confirmed=True).first()


def pending_totp(user: User) -> TOTPDevice:
    """The unconfirmed device being enrolled (created once, reused until confirmed)."""
    device = TOTPDevice.objects.filter(user=user, confirmed=False).first()
    if device is None:
        device = TOTPDevice.objects.create(user=user, name="Authenticator app", confirmed=False)
    return device


def qr_svg(device: TOTPDevice) -> str:
    img = qrcode.make(device.config_url, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return svg[svg.index("<svg"):]  # drop the XML declaration so it can sit inside HTML


@transaction.atomic
def confirm_enrolment(user: User, token: str) -> list[str] | None:
    """Confirm the authenticator with a first code; returns the one-time recovery codes (shown once)."""
    device = pending_totp(user)
    if not device.verify_token(token.strip()):
        return None
    device.confirmed = True
    device.save()
    StaticDevice.objects.filter(user=user).delete()
    static = StaticDevice.objects.create(user=user, name="Recovery codes", confirmed=True)
    codes = ["-".join(secrets.token_hex(2) for _ in range(3)) for _ in range(RECOVERY_CODES)]  # xxxx-xxxx-xxxx
    for code in codes:
        StaticToken.objects.create(device=static, token=code)
    audit.record("2FA_ENROLLED", "app_user", user.pk, user=user)
    return codes


def verify(user: User, token: str):
    """Return the device that accepted the code (authenticator or recovery code), else None."""
    token = token.strip().replace(" ", "").lower()
    totp = confirmed_totp(user)
    if totp and totp.verify_token(token):
        return totp
    for static in StaticDevice.objects.filter(user=user, confirmed=True):
        if static.verify_token(token):  # a recovery code works once
            audit.record("2FA_RECOVERY_CODE_USED", "app_user", user.pk, user=user)
            return static
    return None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def trust_this_device(user: User, user_agent: str) -> str:
    token = secrets.token_urlsafe(32)
    TrustedDevice.objects.create(user=user, token_hash=_hash(token), user_agent=user_agent[:300],
                                 expires_at=timezone.now() + timedelta(days=settings.TRUSTED_DEVICE_DAYS))
    audit.record("2FA_DEVICE_TRUSTED", "app_user", user.pk, user=user, user_agent=user_agent[:120])
    return token


def is_trusted(user: User, token: str | None) -> bool:
    if not token:
        return False
    return TrustedDevice.objects.filter(user=user, token_hash=_hash(token), revoked=False,
                                        expires_at__gt=timezone.now()).exists()


@transaction.atomic
def reset(target: User, approved_by: User) -> None:
    """Remove all second factors and trusted devices; the user enrols again at next sign-in.
    Callers must first obtain an approved TWO_FACTOR_RESET request (maker-checker)."""
    TOTPDevice.objects.filter(user=target).delete()
    StaticDevice.objects.filter(user=target).delete()
    TrustedDevice.objects.filter(user=target).update(revoked=True)
    audit.record("2FA_RESET", "app_user", target.pk, user=approved_by)
