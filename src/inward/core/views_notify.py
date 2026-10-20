"""Notification e-mails (Notification Master, SoT 7.6 / 13 / 23) for the CFA Manager, inside the app.

One e-mail per role (Depot Manager, Admin Officer, CFA Manager, Clerk) is applied to every list that role is on;
the table shows which mails each role receives. Changes are audit-logged."""
from __future__ import annotations

import re

from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect, render

from inward.core import audit
from inward.core.models import NotificationRecipient
from inward.web.views import needs

PURPOSES = {
    "SED_IMMEDIATE": "Internal SED notification - with every MRA that has Short / Excess / Damage",
    "SED_APPROVAL": "SED approval request - the Depot Manager approves by replying from this address",
    "DAMAGE_APPROVAL": "Internal damage approval - the Depot Manager or Admin Officer approves by replying",
    "IRR_EXCEPTIONS": "IRR Reconciliation Summary (daily, as soon as the IRR mail is imported)",
    "DAILY_ESCALATION": "Daily status of incomplete IWs (09:30)",
    "DAILY_UNLOADING": "Yesterday's unloading collection (09:00)",
    "MONTHLY_MIS": "Monthly MIS (1st of the month)",
    "SED_REPORT": "Monthly SED report (1st of the month)",
    "IRR_RECON_REPORT": "IRR Reconciliation Report (when an IW is closed)",
}
EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
MRA_CC = "MRA_CC"   # default Cc of every MRA mail (21-Oct-2026); one row per address, not a role


def save_mra_cc(user, text: str) -> list[str]:
    """Replace the default MRA Cc list; returns the saved addresses. Raises ValueError for a bad address."""
    addrs = []
    for a in re.split(r"[\s,;]+", text.strip().lower()):
        if not a:
            continue
        if not EMAIL.match(a):
            raise ValueError(f"Default MRA Cc: '{a}' is not a valid e-mail address.")
        if a not in addrs:
            addrs.append(a)
    before = list(NotificationRecipient.objects.filter(purpose=MRA_CC).order_by("id").values_list("email", flat=True))
    if before != addrs:
        NotificationRecipient.objects.filter(purpose=MRA_CC).delete()
        NotificationRecipient.objects.bulk_create([
            NotificationRecipient(purpose=MRA_CC, role_label="MRA default Cc", kind="CC", email=a, mandatory=False)
            for a in addrs])
        audit.record("MRA_DEFAULT_CC_CHANGED", "notification_recipient", MRA_CC, user=user,
                     before={"emails": before}, after={"emails": addrs})
    return addrs


@needs("EMAIL_VERIFY")
def notification_emails(request):
    rows = list(NotificationRecipient.objects.filter(active=True).exclude(purpose=MRA_CC)
                .order_by("role_label", "purpose"))
    mra_cc = ", ".join(NotificationRecipient.objects.filter(purpose=MRA_CC).order_by("id")
                       .values_list("email", flat=True))
    roles = sorted({r.role_label for r in rows})
    if request.method == "POST":
        changes, errors = {}, []
        for i, role in enumerate(roles):
            value = request.POST.get(f"role_{i}", "").strip().lower()
            if value and not EMAIL.match(value):
                errors.append(f"{role}: '{value}' is not a valid e-mail address.")
            changes[role] = value
        mra_cc = request.POST.get("mra_cc", "")
        if not errors:
            try:
                with transaction.atomic():
                    save_mra_cc(request.user, mra_cc)
            except ValueError as exc:
                errors.append(str(exc))
        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            with transaction.atomic():
                for role, value in changes.items():
                    before = sorted({r.email for r in rows if r.role_label == role})
                    if before != [value]:
                        NotificationRecipient.objects.filter(role_label=role, active=True).update(email=value)
                        audit.record("NOTIFICATION_EMAIL_CHANGED", "notification_recipient", role, user=request.user,
                                     before={"emails": before}, after={"email": value})
            messages.success(request, "Notification e-mails saved.")
            return redirect("core:notifications")
    current = {role: next((r.email for r in rows if r.role_label == role and r.email), "") for role in roles}
    uses = {role: [(PURPOSES.get(r.purpose, r.purpose), r.get_kind_display(), r.mandatory) for r in rows
                   if r.role_label == role] for role in roles}
    return render(request, "core/notifications.html", {
        "people": [(i, role, current[role], uses[role]) for i, role in enumerate(roles)],
        "missing": [role for role in roles if not current[role]], "mra_cc": mra_cc})
