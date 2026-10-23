"""Packaging changes mailed to the CFA Manager (decision 23-Oct-2026).

Every user may add or change SKU packaging now; the CFA Manager is told by mail. Changes are gathered for a few
minutes so one bulk upload or a run of edits becomes one mail; drastic changes are highlighted with the link to
the review screen. The manager's own changes are not mailed back to the manager.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from inward.core.models import PackagingChange

GATHER = timedelta(minutes=5)


def due(now=None) -> int:
    """Called by the mail worker every round: one mail with the changes not yet reported."""
    from inward.accounts.services import modes_for
    from inward.iw import comms
    from inward.iw.models import MailMessage
    now = now or timezone.now()
    pending = list(PackagingChange.objects.filter(notified=False).select_related("changed_by").order_by("changed_at"))
    if not pending:
        return 0
    if now - pending[-1].changed_at < GATHER and now - pending[0].changed_at < GATHER * 6:
        return 0          # still being edited - wait until it has been quiet for a few minutes
    managers: dict[int, bool] = {}

    def by_manager(ch) -> bool:
        uid = ch.changed_by_id
        if uid not in managers:
            managers[uid] = "GRANT" in modes_for(ch.changed_by, "PACKAGING_REVIEW")
        return managers[uid]

    report = [ch for ch in pending if not by_manager(ch) and ch.source != "MANAGER"]
    with transaction.atomic():
        PackagingChange.objects.filter(pk__in=[ch.pk for ch in pending]).update(notified=True)
        if not report:
            return 0
        to, cc, _ = comms.internal("PACKAGING_CHANGE")
        if not to:
            return 0
        drastic = sum(1 for ch in report if ch.drastic)
        subject = (f"SKU packaging changed - {len(report)} change(s)"
                   + (f", {drastic} to review" if drastic else "")
                   + f" - Depot {comms._branding(None)['depot_code']}")
        comms._queue(None, MailMessage.Purpose.PACKAGING_CHANGE, None, f"PKG {now:%Y%m%d%H%M}", to, cc, subject,
                     "core/mail/packaging_change.html", {"changes": report, "drastic": drastic},
                     [], report[0].changed_by)
    return 1
