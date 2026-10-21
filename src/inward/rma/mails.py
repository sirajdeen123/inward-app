"""Material Return mails (decisions 21-Oct-2026), run by the mail worker (comms.run_once):

- Approval pending, to each portal approver (Pending At, mapped to an e-mail on the Approvers screen; a person who
  left -> the replacement), Cc the Admin Officer, the godown and the Sales Officer(s) of the dealers:
    * every morning after 09:30 (flag rma_approval_time) - everything pending with that person, once a day;
    * the same day a request newly reaches a person (after a portal upload later in the day).
- Daily Material Return report after 09:30 (flag rma_daily_time) to the Admin Officer, Cc the CFA Manager and the
  RCM - the RCM copy is switched on / off on Notification e-mails (flag rma_daily_cc_rcm).
Each mail is recorded, so a worker restart never sends it twice.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from inward.core.models import NotificationRecipient
from inward.rma import services as svc
from inward.rma.models import Approver, ApproverMail, Grn, Request, StageEvent

ZERO = Decimal(0)


def _time(key: str, default: str) -> time:
    from inward.iw.reports import _time as t
    return t(key, default)


def _user():
    from inward.iw.reports import _system_user
    return _system_user()


def depot_code() -> str:
    from django.conf import settings
    return settings.INWARD["DEPOT_CODE"]


def approval_cc() -> list[str]:
    """Admin Officer + godown (Notification e-mails -> 'Material return approval pending')."""
    return [r.email.lower() for r in NotificationRecipient.objects.filter(purpose="RMA_APPROVAL", active=True)
            .exclude(email="").order_by("id")]


def cc_rcm() -> bool:
    return bool(svc.flag("rma_daily_cc_rcm", True))


def daily_recipients() -> tuple[list[str], list[str], list[str]]:
    to, cc, missing = [], [], []
    for r in NotificationRecipient.objects.filter(purpose="RMA_DAILY", active=True).order_by("id"):
        if r.role_label == "RCM" and not cc_rcm():
            continue
        if not r.email:
            if r.mandatory:
                missing.append(r.role_label)
            continue
        (to if r.kind == "TO" else cc).append(r.email.lower())
    return to, [c for c in cc if c not in to], missing

# ---------------------------------------------------------------- approver mails


def _queue_approver(a: Approver, rws: list, kind: str, day: date) -> ApproverMail | None:
    from inward.iw import comms
    from inward.iw.models import MailMessage
    sos = sorted({row.sales_officer[1].lower() for row in rws if row.sales_officer and row.sales_officer[1]})
    cc = [c for c in approval_cc() + sos if c != a.email.lower()]
    value = sum((row.r.total_value for row in rws), ZERO)
    if kind == "NEW":
        ids = ", ".join(row.r.request_id for row in rws[:5]) + (" ..." if len(rws) > 5 else "")
        subject = f"New material return pending your approval - request {ids} - Rs {value:,.0f} - Depot {depot_code()}"
    else:
        subject = (f"Material returns pending your approval - {len(rws)} request(s) - Rs {value:,.0f} - "
                   f"Depot {depot_code()} - {day:%d-%b-%Y}")
    with transaction.atomic():
        try:
            am = ApproverMail.objects.create(approver=a, kind=kind, day=day,
                                             requests=[row.r.request_id for row in rws])
        except IntegrityError:
            return None
        m = comms._queue(None, MailMessage.Purpose.RMA_APPROVAL, None, f"RMA-APPR {a.pk} {day:%Y%m%d} {kind}",
                         [a.email.lower()], cc, subject, "rma/mail/approval.html",
                         {"a": a, "rows": rws, "kind": kind, "day": day, "value": value,
                          "as_on": as_on()}, [], _user())
        am.mail = m
        am.save(update_fields=["mail"])
    return am


def as_on():
    b = svc.last_upload("RMA_PORTAL")
    if b is None:
        return None
    return {"report": (b.summary or {}).get("as_on") or "", "uploaded": timezone.localtime(b.uploaded_at)}


def send_digests(day: date) -> int:
    """Once a day (ReportRun RMA_DIGEST): one mail per approver with everything pending with that person."""
    from inward.iw.models import ReportRun
    if svc.last_upload("RMA_PORTAL") is None:
        return 0
    try:
        with transaction.atomic():
            ReportRun.objects.create(kind="RMA_DIGEST", period=day.isoformat())
    except IntegrityError:
        return 0
    rws = svc.rows(Request.objects.filter(closed_at__isnull=True))
    groups, _ = svc.pending_by_approver(rws)
    n = 0
    for g in groups:
        if ApproverMail.objects.filter(approver=g["approver"], kind="DIGEST", day=day).exists():
            continue
        if _queue_approver(g["approver"], sorted(g["rows"], key=lambda x: -(x.days_at_stage or 0)), "DIGEST", day):
            n += 1
    StageEvent.objects.filter(notified=False).update(notified=True)   # the digest covered them
    return n


def send_new_pending(now: datetime) -> int:
    """Requests that reached a new approver since the last mail - after the morning digest only (before it, the
    digest itself tells them)."""
    events = list(StageEvent.objects.filter(notified=False).select_related("request"))
    if not events:
        return 0
    day = now.date()
    if now.time() < _time("rma_approval_time", "09:30"):
        return 0          # the morning digest will tell them
    reqs = {e.request_id for e in events if svc.is_pending(e.request.status) and e.request.pending_at == e.pending_at}
    n = 0
    if reqs:
        rws = svc.rows(Request.objects.filter(pk__in=reqs, closed_at__isnull=True))
        groups, _ = svc.pending_by_approver(rws)
        for g in groups:
            if _queue_approver(g["approver"], g["rows"], "NEW", day):
                n += 1
    StageEvent.objects.filter(pk__in=[e.pk for e in events]).update(notified=True)
    return n


# ---------------------------------------------------------------- daily report


def daily_report(day: date | None = None) -> dict:
    day = day or timezone.localdate()
    open_rows = svc.rows(Request.objects.filter(closed_at__isnull=True), day)
    by_stage = []
    for code, label in svc.STAGES:
        if code == "CLOSED":
            continue
        rs = [x for x in open_rows if x.stage == code]
        by_stage.append({"code": code, "label": label, "count": len(rs),
                         "value": sum((x.r.total_value for x in rs), ZERO),
                         "overdue": sum(1 for x in rs if x.flag == "RED")})
    groups, unmapped = svc.pending_by_approver(open_rows)
    approval = sorted([x for x in open_rows if x.stage == "APPROVAL"], key=lambda x: -(x.days_at_stage or 0))
    yday = day - timedelta(days=1)
    closed = svc.rows(Request.objects.filter(closed_at__date=yday))
    incomplete = [g for g in Grn.objects.filter(lines__condition="").distinct()] + \
        [g for g in Grn.objects.filter(gate_entry_no="").exclude(lines__condition="").distinct()]
    upload = as_on()
    stale = upload is None or upload["uploaded"].date() < day - timedelta(days=1)
    no_grn = [x for x in open_rows if not x.grns and x.stage not in ("REJECTED",)]
    return {"day": day, "by_stage": by_stage, "no_grn": no_grn, "groups": groups, "unmapped": unmapped, "approval": approval,
            "unlinked": svc.unlinked_grns(day), "waiting": [x for x in open_rows if x.stage in ("RMA", "CN", "IRR")],
            "ready": [x for x in open_rows if x.stage == "CLOSE"],
            "rejected": [x for x in open_rows if x.stage == "REJECTED"], "closed": closed,
            "incomplete_grns": incomplete[:50], "as_on": upload, "stale": stale,
            "open_value": sum((x.r.total_value for x in open_rows), ZERO), "open_count": len(open_rows),
            "cc_rcm": cc_rcm()}


def send_daily(day: date):
    from inward.core import documents
    from inward.iw import comms
    from inward.iw.models import MailMessage, ReportRun
    from inward.rma import register
    period = day.isoformat()
    with transaction.atomic():
        try:
            run = ReportRun.objects.create(kind="RMA_DAILY", period=period)
        except IntegrityError:
            return None
        to, cc, missing = daily_recipients()
        if not to:
            run.skipped = "no Admin Officer e-mail in Notification e-mails" + (f" ({', '.join(missing)})" if missing else "")
            run.save(update_fields=["skipped"])
            return run
        if svc.last_upload("RMA_PORTAL") is None and not Grn.objects.exists():
            run.skipped = "Material returns not in use yet (no GRN and no portal report)"
            run.save(update_fields=["skipped"])
            return run
        r = daily_report(day)
        xlsx = documents.store(register.build(), filename=f"Materials Return Register {day:%d-%m-%Y}.xlsx",
                               content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               doc_type="RMA_REGISTER", user=None)
        m = comms._queue(None, MailMessage.Purpose.RMA_DAILY, None, f"RMA_DAILY {period}", to, cc,
                         f"Material returns - daily status - {day:%d-%b-%Y} - {r['open_count']} open, "
                         f"Rs {r['open_value']:,.0f} - Depot {depot_code()}",
                         "rma/mail/daily.html", {"r": r}, [xlsx], _user())
        run.mail = m
        run.save(update_fields=["mail"])
        return run


def due(now=None) -> dict:
    now = timezone.localtime(now or timezone.now())
    out = {}
    if now.time() >= _time("rma_approval_time", "09:30"):
        out["digests"] = send_digests(now.date())
    out["new"] = send_new_pending(now)
    if now.time() >= _time("rma_daily_time", "09:30"):
        run = send_daily(now.date())
        if run is not None:
            out["daily"] = 1
    return {k: v for k, v in out.items() if v}
