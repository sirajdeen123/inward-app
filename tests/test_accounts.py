"""Sign-in security, two-factor and maker-checker (SoT 12.1, 14.5)."""
import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse
from django_otp.oath import totp

from inward.accounts import services, twofactor
from inward.accounts.models import ApprovalRequest

PASSWORD = "Correct-Horse-Battery-9"
pytestmark = pytest.mark.django_db


def _code(device) -> str:
    return f"{totp(device.bin_key, device.step, device.t0, device.digits, device.drift):06d}"


def _sign_in(client, username):
    return client.post(reverse("accounts:login"), {"username": username, "password": PASSWORD})


def _enrol(client, user):
    client.get(reverse("accounts:two_factor"))
    device = twofactor.pending_totp(user)
    return client.post(reverse("accounts:two_factor"), {"code": _code(device)}), device


def test_every_screen_needs_two_factor(client, seeded, make_user):
    user = make_user("clerk1", "DEPOT_CLERK")
    assert client.get(reverse("web:home")).status_code == 302  # not signed in
    resp = _sign_in(client, "clerk1")
    assert resp.status_code == 302 and reverse("accounts:two_factor") in resp["Location"]
    resp = client.get(reverse("web:home"))
    assert resp.status_code == 302 and reverse("accounts:two_factor") in resp["Location"]
    resp, _ = _enrol(client, user)
    assert resp.status_code == 200 and len(resp.context["codes"]) == 10  # recovery codes shown once
    assert client.get(reverse("web:home")).status_code == 200


def test_trusted_device_skips_code_and_recovery_code_works_once(client, seeded, make_user):
    user = make_user("clerk2", "DEPOT_CLERK")
    _sign_in(client, "clerk2")
    resp, device = _enrol(client, user)
    codes = resp.context["codes"]
    client.post(reverse("accounts:logout"))

    _sign_in(client, "clerk2")
    resp = client.post(reverse("accounts:two_factor"), {"code": codes[0], "trust": "on"})
    assert resp.status_code == 302
    name = twofactor.trusted_cookie(user)
    assert name in resp.cookies and resp.cookies[name]["httponly"]
    client.post(reverse("accounts:logout"))

    resp = _sign_in(client, "clerk2")  # trusted cookie still in the client -> straight in
    assert resp.status_code == 302 and reverse("accounts:two_factor") not in resp["Location"]
    client.post(reverse("accounts:logout"))

    client.cookies.pop(name)
    _sign_in(client, "clerk2")
    resp = client.post(reverse("accounts:two_factor"), {"code": codes[0]})  # used already
    assert resp.status_code == 200 and "did not match" in resp.content.decode()


def test_account_locks_after_five_failures(client, seeded, make_user):
    make_user("clerk3", "DEPOT_CLERK")
    for _ in range(5):
        client.post(reverse("accounts:login"), {"username": "clerk3", "password": "wrong-password-x"})
    resp = _sign_in(client, "clerk3")
    assert resp.status_code in (403, 429)
    assert "locked" in resp.content.decode().lower()


def test_maker_checker(seeded, make_user):
    clerk = make_user("clerk4", "DEPOT_CLERK")
    mgr = make_user("cfa1", "CFA_MANAGER")
    services.require(clerk, "IW_EDIT")
    with pytest.raises(services.PermissionDenied):
        services.require(clerk, "MAIL_PENDING_RELEASE")
    req = services.request_approval(clerk, "IW_REOPEN", "iw", "IW-2627-000001", "LR scan was the wrong file")
    with pytest.raises(services.PermissionDenied):
        services.decide(clerk, req.id, True)
    assert services.decide(mgr, req.id, True, "ok").status == ApprovalRequest.Status.APPROVED
    with pytest.raises(services.PermissionDenied):
        services.assign_role(mgr, mgr, "ADMINISTRATOR")
    # the database refuses self-approval even if the service layer were bypassed
    req2 = services.request_approval(clerk, "IW_VOID", "iw", "IW-2627-000002", "duplicate IW")
    req2.decided_by = clerk
    with pytest.raises(IntegrityError), transaction.atomic():
        req2.save()


def test_staff_flag_only_for_cfa_and_admin(seeded, make_user):
    assert make_user("cfa2", "CFA_MANAGER").is_staff
    assert make_user("admin1", "ADMINISTRATOR").is_staff
    assert not make_user("clerk5", "DEPOT_CLERK").is_staff


def test_password_policy(seeded):
    from django.core.exceptions import ValidationError
    with pytest.raises(ValidationError):
        services.create_user(None, username="short", full_name="Short", email="", password="abc123",
                             role_code="DEPOT_CLERK")


def test_security_headers(client):
    resp = client.get(reverse("accounts:login"))
    assert "default-src 'self'" in resp["Content-Security-Policy"]
    assert resp["X-Frame-Options"] == "DENY"
    assert resp["X-Content-Type-Options"] == "nosniff"


def test_two_users_in_one_browser_keep_their_trust(client, seeded, make_user):
    """UAT 18-Oct-2026: the clerk's trusted cookie replaced the manager's, so the manager was asked every time."""
    from django_otp.oath import totp
    users = [make_user("trust_a", "DEPOT_CLERK"), make_user("trust_b", "CFA_MANAGER")]
    for u in users:
        dev = twofactor.pending_totp(u)
        dev.confirmed = True
        dev.save()
        _sign_in(client, u.username)
        code = format(totp(dev.bin_key, dev.step, dev.t0, dev.digits, dev.drift), "06d")
        assert client.post(reverse("accounts:two_factor"), {"code": code, "trust": "on"}).status_code == 302
        client.post(reverse("accounts:logout"))
    for u in users:                     # both are remembered in the same browser
        resp = _sign_in(client, u.username)
        assert reverse("accounts:two_factor") not in resp["Location"], u.username
        client.post(reverse("accounts:logout"))
