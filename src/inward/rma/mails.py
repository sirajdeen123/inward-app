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
from inward.rma.models import ApproverMail, Grn, Request, StageEvent

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


def _queue_approver(g: dict, rws: list, kind: str, day: date, reason: str = "", user=None) -> ApproverMail | None:
    """One mail to the whole Pending At group - every member in To, any one of them can approve."""
    from inward.iw import comms
    from inward.iw.models import MailMessage
    to = [a.email.lower() for a in g["approvers"]]
    sos = sorted({row.sales_officer[1].lower() for row in rws if row.sales_officer and row.sales_officer[1]})
    cc = [c for c in approval_cc() + sos if c not in to]
    value = sum((row.r.total_value for row in rws), ZERO)
    if kind == "URGENT":
        first = rws[0].r
        ids = ", ".join(row.r.request_id for row in rws[:5]) + (" ..." if len(rws) > 5 else "")
        dealer = first.dealer_name if len({row.r.dealer_code for row in rws}) == 1 else f"{len(rws)} requests"
        subject = (f"URGENT - material return approval needed - request {ids} - {dealer} - Rs {value:,.0f} - "
                   f"Depot {depot_code()}")
    elif kind == "NEW":
        ids = ", ".join(row.r.request_id for row in rws[:5]) + (" ..." if len(rws) > 5 else "")
        subject = f"New material return pending your approval - request {ids} - Rs {value:,.0f} - Depot {depot_code()}"
    else:
        subject = (f"Material returns pending your approval - {len(rws)} request(s) - Rs {value:,.0f} - "
                   f"Depot {depot_code()} - {day:%d-%b-%Y}")
    with transaction.atomic():
        try:
            am = ApproverMail.objects.create(approver=g["approvers"][0], group=g["key"], kind=kind, day=day,
                                             requests=[row.r.request_id for row in rws], reason=reason[:300],
                                             sent_by=user)
        except IntegrityError:
            return None
        m = comms._queue(None, MailMessage.Purpose.RMA_APPROVAL, None, f"RMA-APPR {g['key']} {day:%Y%m%d} {kind}",
                         to, cc, subject, "rma/mail/approval.html",
                         {"g": g, "rows": rws, "kind": kind, "day": day, "value": value, "as_on": as_on(),
                          "reason": reason}, [], user or _user())
        am.mail = m
        am.save(update_fields=["mail"])
    return am


# ---------------------------------------------------------------- urgent reminder, sent by hand (22-Oct-2026)

URGENT_REASONS = ["Dealer account is locked until this return is approved", "Dealer follow-up / escalation",
                  "Credit note needed urgently", "Other"]
URGENT_GAP_HOURS = 2


def urgent_groups(only_request: str = "") -> list[dict]:
    """Pending-approval groups (all approvers of a request together) with their requests, for the urgent screen."""
    rws = svc.rows(Request.objects.filter(closed_at__isnull=True))
    groups = svc.pending_groups(rws)
    if only_request:
        groups = [dict(g, rows=[x for x in g["rows"] if x.r.request_id == only_request]) for g in groups]
        groups = [g for g in groups if g["rows"]]
    since = timezone.now() - timedelta(hours=URGENT_GAP_HOURS)
    recent = list(ApproverMail.objects.filter(kind="URGENT", created_at__gte=since))
    for g in groups:
        g["value"] = sum((x.r.total_value for x in g["rows"]), ZERO)
        for x in g["rows"]:
            x.urgent_recent = next((m for m in recent if m.group == g["key"] and x.r.request_id in m.requests), None)
    return groups


def send_urgent(user, picks: dict[str, set[str]], reason_kind: str, reason_text: str, force: bool = False) -> list:
    """picks: group key -> request ids. One URGENT mail per chosen group with only the chosen requests."""
    from inward.accounts.services import require
    from inward.core import audit
    require(user, "RMA_URGENT_MAIL")
    if reason_kind not in URGENT_REASONS:
        raise svc.RmaError("Choose why it is urgent.")
    text = " ".join(str(reason_text or "").split())
    if reason_kind == "Other" and len(text) < 5:
        raise svc.RmaError("Write why it is urgent.")
    reason = reason_kind if reason_kind != "Other" else text
    if text and reason_kind != "Other":
        reason = f"{reason_kind} - {text}"
    groups = {g["key"]: g for g in urgent_groups()}
    chosen = {k: v for k, v in picks.items() if v}
    if not chosen:
        raise svc.RmaError("Tick at least one approver and request.")
    plan = []
    for key, ids in chosen.items():
        g = groups.get(key)
        if g is None:
            raise svc.RmaError("The pending approvers have changed - reload the page.")
        rows = [x for x in g["rows"] if x.r.request_id in ids]
        if len(rows) != len(ids):
            raise svc.RmaError("A ticked request is no longer pending with these approvers - reload the page.")
        repeat = [x for x in rows if x.urgent_recent]
        if repeat and not force:
            x = repeat[0]
            raise svc.RmaError(f"Request {x.r.request_id} was already sent as urgent to "
                               f"{' / '.join(a.name for a in g['approvers'])} at "
                               f"{timezone.localtime(x.urgent_recent.created_at):%I:%M %p} - tick 'send again anyway' "
                               "to repeat it within 2 hours.")
        plan.append((g, rows))
    day = timezone.localdate()
    sent = []
    for g, rows in plan:
        am = _queue_approver(g, rows, "URGENT", day, reason=reason, user=user)
        sent.append(am)
        audit.record("RMA_URGENT_REMINDER", "rma_approver_mail", am.pk, user=user,
                     after={"to": am.mail.to, "cc": am.mail.cc, "requests": am.requests, "reason": reason})
    return sent


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
    n = 0
    for g in svc.pending_groups(rws):
        if ApproverMail.objects.filter(group=g["key"], kind="DIGEST", day=day).exists():
            continue
        if _queue_approver(g, g["rows"], "DIGEST", day):
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
        for g in svc.pending_groups(rws):
            if _queue_approver(g, g["rows"], "NEW", day):
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
    from inward.rma import autofetch
    cn_today, cn_yesterday = autofetch.today_status(day), autofetch.today_status(day - timedelta(days=1))
    return {"day": day, "by_stage": by_stage, "no_grn": no_grn, "cn_today": cn_today, "cn_yesterday": cn_yesterday, "groups": groups, "unmapped": unmapped, "approval": approval,
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
