"""Screen 09 - Reports, escalations and MIS (SoT 13, table 15). Every report can be viewed on screen; scheduled
ones go through the same mail queue (Sent Mail evidence) to the Notification Master recipients, once per period:

- Daily status of incomplete IWs, 09:30 (Admin Officer, CFA Manager): open / aged IWs, SIT partial / abnormal /
  never-linked Ships, ERP pending, IRR pending / not found / review, open SED, SED approvals pending or rejected,
  mail-pending releases, key claim values. Exceptions get first / last escalated and a counter (no repeated spam).
- Yesterday's unloading collection, 09:00 (CFA Manager only).
- Monthly MIS and SED report (Admin Officer, CFA Manager, Clerk) for the previous month, on the 1st.
- IRR Reconciliation Report at closure (with the PDF).
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, F, Sum
from django.utils import timezone

from inward.core.models import ExceptionEvent, FeatureFlag
from inward.iw import comms, ops, unloading
from inward.iw.models import (
    ErpReceipt, Iw, IwClosure, MailMessage, MailRelease, ReportRun, SedApproval, SedLine, TransporterLedgerEntry,
    UnloadingRecord,
)

S = Iw.Status
AGED_HOURS = 48
P = MailMessage.Purpose


def _bounds(d1: date, d2: date):
    tz = timezone.get_current_timezone()
    return datetime.combine(d1, time.min, tz), datetime.combine(d2 + timedelta(days=1), time.min, tz)

# ---------------------------------------------------------------- content


def daily_escalation(day: date | None = None) -> dict:
    day = day or timezone.localdate()
    rows = ops.open_iws()
    aged = [r for r in rows if r.age_hours >= AGED_HOURS]
    erp_pending = [r for r in rows if r.iw.status in (S.MRA_ISSUED, S.ERP_IN_PROGRESS, S.REOPENED)]
    irr_problem = []
    for r in erp_pending:
        run = r.iw.recon_runs.first()
        for res in (run.results if run else []):
            if res["status"] in ("NOT_FOUND", "REVIEW", "PARTIAL"):
                irr_problem.append((r.iw, res["track"], res["status"]))
    open_sed = [r for r in rows if r.iw.sed_lines.exists() and r.iw.status != S.CLOSED]
    approvals = [a for a in ops.approval_board() if a.status in ("REQUESTED", "REJECTED", "MANUAL_PENDING")]
    from inward.sit.models import SitShip
    ships = SitShip.objects.filter(pre_go_live=False, status__in=[SitShip.Status.PARTIAL, SitShip.Status.REMOVED_NO_RECEIPT,
                                                                   SitShip.Status.REMOVED_NEVER_LINKED]).order_by("status", "ship_id")
    releases = [r for r in MailRelease.objects.select_related("iw") if r.iw.mails.exclude(status=MailMessage.Status.SENT)
                .filter(purpose__in=[P.MRA, P.SED_NOTIFY]).exists()]
    claim = sum((ln.claimable for r in open_sed for ln in r.iw.sed_lines.all()), Decimal(0))
    return {"day": day, "open": rows, "aged": aged, "erp_pending": erp_pending, "irr_problem": irr_problem,
            "open_sed": open_sed, "approvals": approvals, "ships": list(ships[:200]), "releases": releases,
            "claim_open": claim, "mail": ops.mail_alert(), "unverified": comms.unverified_emails(),
            "damage": list(__import__("inward.damage.models", fromlist=["DamageReport"]).DamageReport.objects
                           .exclude(status__in=["RECONCILED", "CANCELLED"]).order_by("created_at"))}


def daily_unloading(day: date | None = None) -> dict:
    day = day or timezone.localdate()
    start, end = _bounds(day, day)
    recs = list(UnloadingRecord.objects.filter(recorded_at__gte=start, recorded_at__lt=end)
                .select_related("iw", "iw__transporter").order_by("recorded_at"))
    receipts = list(TransporterLedgerEntry.objects.filter(kind="RECEIPT", entry_date=day).select_related("transporter"))
    total = lambda f: sum((getattr(r, f) for r in recs), Decimal(0))  # noqa: E731
    return {"day": day, "records": recs, "charged": total("charge"), "paid": total("paid_amount"),
            "posted": sum((r.outstanding for r in recs), Decimal(0)), "receipts": receipts,
            "received": sum((r.amount for r in receipts), Decimal(0)),
            "ledger": [v for v in unloading.balances() if v.balance]}


def sed_report(d1: date, d2: date) -> dict:
    start, end = _bounds(d1, d2)
    lines = list(SedLine.objects.filter(iw__verified_at__gte=start, iw__verified_at__lt=end)
                 .exclude(iw__status=S.VOID).select_related("iw", "iw__source_org", "iw__transporter")
                 .order_by("iw__verified_at", "iw_id", "source_key"))
    appr = {a.iw_id: a for a in SedApproval.objects.filter(iw_id__in={ln.iw_id for ln in lines}).order_by("created_at")}
    t = {"short": Decimal(0), "excess": Decimal(0), "damage": Decimal(0), "value": Decimal(0)}
    for ln in lines:
        t["short"] += ln.short_qty
        t["excess"] += ln.excess_qty
        t["damage"] += ln.damage_qty
        t["value"] += ln.claimable
    return {"from": d1, "to": d2, "lines": lines, "approvals": appr, "totals": t}


def monthly_mis(year: int, month: int) -> dict:
    d1 = date(year, month, 1)
    d2 = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
    start, end = _bounds(d1, d2)
    iws = Iw.objects.filter(arrival_at__gte=start, arrival_at__lt=end).exclude(status__in=[S.VOID, S.DRAFT])
    by_type = list(iws.values("load_type__name").annotate(n=Count("id")).order_by("-n"))
    closures = IwClosure.objects.filter(closed_at__gte=start, closed_at__lt=end, number=1).select_related("iw")
    days = [(c.closed_at - c.iw.arrival_at).total_seconds() / 86400 for c in closures]
    runs = [iw.recon_runs.first() for iw in iws]
    statuses = [res["status"] for r in runs if r for res in r.results]
    sed = sed_report(d1, d2)
    unl = UnloadingRecord.objects.filter(iw__in=iws)
    agg = unl.aggregate(charged=Sum("charge"), paid=Sum("paid_amount"))
    non_stock = list(iws.filter(load_type__is_non_stock=True).prefetch_related("non_stock_refs__sender",
                                                                              "non_stock_refs__lines"))
    return {"month": d1, "iws": iws.count(), "by_type": by_type, "closed": closures.count(),
            "avg_close_days": (sum(days) / len(days)) if days else None,
            "max_close_days": max(days) if days else None,
            "recon": {s: statuses.count(s) for s in ("FULL", "PARTIAL", "REVIEW", "NOT_FOUND", "PENDING", "LSCRR_COVERED")},
            "sed": sed["totals"], "sed_iws": len({ln.iw_id for ln in sed["lines"]}),
            "unloading": {"charged": agg["charged"] or Decimal(0), "paid": agg["paid"] or Decimal(0),
                          "exempt": unl.filter(status=UnloadingRecord.Status.EXEMPT).count()},
            "receipts": ErpReceipt.objects.filter(iw__in=iws).count(), "non_stock": non_stock}


def sed_xlsx(d1: date, d2: date) -> bytes:
    from openpyxl import Workbook
    data = sed_report(d1, d2)
    wb = Workbook()
    ws = wb.active
    ws.title = "SED"
    ws.append(["IW", "Verified", "Vehicle", "Source", "Transporter", "Reference", "SKU", "Floor SKU", "Description",
               "Doc pcs", "Short", "Excess", "Damage", "Damage kind", "Short Rs", "Damage Rs", "Claimable Rs", "Excess Rs (not claimable)",
               "Remarks", "DM approval"])
    for ln in data["lines"]:
        ap = data["approvals"].get(ln.iw_id)
        ws.append([ln.iw.iw_no, timezone.localtime(ln.iw.verified_at).strftime("%d-%m-%Y") if ln.iw.verified_at else "",
                   ln.iw.vehicle_no, str(ln.iw.source_org or ""), str(ln.iw.transporter or ""), ln.source_label, ln.sku,
                   ln.floor_sku, ln.description, float(ln.doc_pieces), float(ln.short_qty), float(ln.excess_qty),
                   float(ln.damage_qty), ln.get_damage_kind_display() if ln.damage_qty else "",
                   float(ln.short_value or 0), float(ln.damage_value or 0), float(ln.claimable), float(ln.excess_value or 0),
                   " / ".join(x for x in (ln.short_remarks, ln.excess_remarks, ln.damage_remarks) if x),
                   ap.get_status_display() if ap else ""])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()

# ---------------------------------------------------------------- exception lifecycle


def escalate_exceptions(now=None) -> int:
    """Stamp first / last escalated and count on every unresolved exception included in today's report."""
    now = now or timezone.now()
    q = ExceptionEvent.objects.filter(resolved_at__isnull=True)
    q.filter(first_escalated_at__isnull=True).update(first_escalated_at=now)
    return q.update(last_escalated_at=now, escalation_count=F("escalation_count") + 1)

# ---------------------------------------------------------------- scheduling and sending


@dataclass
class Spec:
    kind: str
    purpose: str
    template: str
    subject: str


SPECS = {
    "DAILY_ESC": Spec("DAILY_ESC", "DAILY_ESCALATION", "iw/mail/report_daily.html", "Daily status of incomplete IWs"),
    "DAILY_UNL": Spec("DAILY_UNL", "DAILY_UNLOADING", "iw/mail/report_unloading.html", "Daily unloading collection"),
    "MONTHLY_MIS": Spec("MONTHLY_MIS", "MONTHLY_MIS", "iw/mail/report_mis.html", "Monthly MIS"),
    "SED_REPORT": Spec("SED_REPORT", "SED_REPORT", "iw/mail/report_sed.html", "SED report"),
    "IRR_EXC": Spec("IRR_EXC", "IRR_EXCEPTIONS", "irrecon/mail_summary.html", "IRR Reconciliation Summary"),
}


def _time(key: str, default: str) -> time:
    f = FeatureFlag.objects.filter(key=key).first()
    text = str((f.value or {}).get("value") or default) if f else default
    try:
        h, m = (int(x) for x in text.split(":"))
        return time(h, m)
    except ValueError:
        h, m = (int(x) for x in default.split(":"))
        return time(h, m)


def _system_user():
    from inward.accounts.models import User
    return User.objects.filter(is_active=True).order_by("id").first()


def queue_report(kind: str, period: str, context: dict, user=None, attachments=()) -> ReportRun:
    """Queue one report for its period (once). Missing recipient e-mails -> recorded as skipped, not an error."""
    spec = SPECS[kind]
    with transaction.atomic():
        try:
            run = ReportRun.objects.create(kind=kind, period=period)
        except IntegrityError:
            return ReportRun.objects.get(kind=kind, period=period)
        to, cc, missing = comms.internal(spec.purpose)
        if not to:
            run.skipped = "no recipient e-mail in the Notification Master" + (f" ({', '.join(missing)})" if missing else "")
            run.save(update_fields=["skipped"])
            return run
        m = comms._queue(None, MailMessage.Purpose(kind), None,
                         f"{kind} {period}", to, cc, f"{spec.subject} - {period} - Depot {comms._branding(None)['depot_code']}",
                         spec.template, context, list(attachments), user or _system_user())
        run.mail = m
        run.save(update_fields=["mail"])
        return run


def due_reports(now=None) -> list[ReportRun]:
    """Called by the worker every round (decision 17-Oct-2026):
    - after 09:00 - yesterday's unloading collection (CFA Manager);
    - after 09:30 - the status of the incomplete IWs this morning (Admin Officer, CFA Manager), with the
      exception escalation;
    - on the 1st after 09:30 - last month's MIS and SED report.
    Each period is sent once, so a worker restart never sends a report twice."""
    now = timezone.localtime(now or timezone.now())
    out = []
    day = now.date()
    if now.time() >= _time("daily_unloading_time", "09:00"):
        prev = (day - timedelta(days=1)).isoformat()
        if not ReportRun.objects.filter(kind="DAILY_UNL", period=prev).exists():
            out.append(queue_report("DAILY_UNL", prev, {"r": daily_unloading(day - timedelta(days=1))}))
    if now.time() < _time("daily_status_time", "09:30"):
        return out
    key = day.isoformat()
    if not ReportRun.objects.filter(kind="DAILY_ESC", period=key).exists():
        out.append(queue_report("DAILY_ESC", key, {"r": daily_escalation(day)}))
        escalate_exceptions()
    if day.day == 1:
        prev = day - timedelta(days=1)
        month = f"{prev:%Y-%m}"
        if not ReportRun.objects.filter(kind="MONTHLY_MIS", period=month).exists():
            out.append(queue_report("MONTHLY_MIS", month, {"r": monthly_mis(prev.year, prev.month)}))
        if not ReportRun.objects.filter(kind="SED_REPORT", period=month).exists():
            d1 = prev.replace(day=1)
            out.append(queue_report("SED_REPORT", month, {"r": sed_report(d1, prev)}))
    return out


def irr_summary(day: date | None = None, fetched: dict | None = None) -> dict:
    """IRR Reconciliation Summary for yesterday (sent after the day's IRR is imported from Gmail): yesterday's
    families and exceptions first, then this month, the whole IRR and the older open exceptions."""
    from inward.irrecon.models import IrrMatch, IrrReconRun
    from inward.irrecon.summary import summary
    day = day or timezone.localdate()
    prev = day - timedelta(days=1)
    open_qs = IrrMatch.objects.filter(status="OPEN").select_related("row").order_by("-row__trx_date", "group")
    return {"day": day, "yesterday": summary(prev, prev), "prev": prev, "all": summary(),
            "month": summary(day.replace(day=1), day), "fetched": fetched,
            "y_open_rows": list(open_qs.filter(row__trx_date=prev)[:200]),
            "open_rows": list(open_qs.exclude(row__trx_date=prev)[:100]),
            "older_open_count": open_qs.exclude(row__trx_date=prev).count(), "open_count": open_qs.count(),
            "run": IrrReconRun.objects.first()}


def send_irr_report(snap: IwClosure, user) -> MailMessage | None:
    """At closure: the Final IRR Reconciliation Report to Admin Officer, CFA Manager and Clerk."""
    to, cc, _ = comms.internal("IRR_RECON_REPORT")
    if not to or snap.report is None:
        return None
    iw = snap.iw
    return comms._queue(iw, P.IRR_RECON, iw.mras.first(), f"{iw.iw_no} IRR-C{snap.number}", to, cc,
                        f"IRR Reconciliation Report {iw.iw_no} - closed - {iw.vehicle_no}", "iw/mail/report_irr.html",
                        {"snap": snap}, [snap.report], user)
