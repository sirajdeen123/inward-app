"""Screen 06 - Operations Control Centre (SoT 10): today's figures, open IWs with age and progress, the exact next
action per IW ('My Pending Actions'), SIT monitoring, SED and unloading positions, the SED approval board and the
mail-engine warning. Read-only; every figure comes from the database (never from files)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, Sum, Value
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone

from inward.iw import checklist, closure, comms, erp, mra, sed
from inward.iw.models import (
    ErpReceipt, Iw, IwClosure, MailMessage, MraRevision, SedApproval, SedLine, TransporterLedgerEntry, UnloadingRecord,
)
from inward.iw.outbound import state as outbound_state

S = Iw.Status
OPEN = [S.DRAFT, S.OPEN, S.MATERIAL_LINKED, S.UNLOADING_IN_PROGRESS, S.PHYSICALLY_VERIFIED, S.MRA_ISSUED,
        S.ERP_IN_PROGRESS, S.RECON_COMPLETE, S.DOCUMENTS_COMPLETE, S.REOPENED]
STAGES = [("Linked", {S.MATERIAL_LINKED}), ("Unloading", {S.UNLOADING_IN_PROGRESS}),
          ("Verified", {S.PHYSICALLY_VERIFIED}), ("MRA", {S.MRA_ISSUED}), ("ERP", {S.ERP_IN_PROGRESS, S.REOPENED}),
          ("Reconciled", {S.RECON_COMPLETE, S.DOCUMENTS_COMPLETE}), ("Closed", {S.CLOSED})]
MONEY = DecimalField(max_digits=16, decimal_places=2)
ORDER = [S.DRAFT, S.OPEN, S.MATERIAL_LINKED, S.UNLOADING_IN_PROGRESS, S.PHYSICALLY_VERIFIED, S.MRA_ISSUED,
         S.ERP_IN_PROGRESS, S.REOPENED, S.RECON_COMPLETE, S.DOCUMENTS_COMPLETE, S.CLOSED]


@dataclass
class Action:
    text: str
    url: str
    track: str = ""


def progress(iw: Iw) -> list[tuple[str, str]]:
    """[(stage, done / current / todo)] for the per-IW progress strip."""
    pos = ORDER.index(iw.status) if iw.status in ORDER else 0
    out = []
    for label, statuses in STAGES:
        last = max(ORDER.index(s) for s in statuses)
        out.append((label, "current" if iw.status in statuses else "done" if pos > last else "todo"))
    return out


def actions(iw: Iw) -> list[Action]:
    """The exact missing actions of one IW, with the screen that resolves each."""
    u = lambda name: reverse(f"iw:{name}", args=[iw.pk])  # noqa: E731
    st = iw.status
    if st == S.DRAFT:
        return [Action("Create the IW (issue its number)", u("edit"))]
    if st == S.OPEN:
        return [Action("Link material (Ships / challans / other items)", u("detail"))]
    if st == S.MATERIAL_LINKED:
        if iw.load_type.has_outbound and not outbound_state(iw).ready:
            return [Action("Tally every outbound Ship", u("outbound"), "Outbound")]
        return [Action("Generate the unloading checklist", u("checklist"), "Checklist")]
    if st == S.UNLOADING_IN_PROGRESS:
        b = sed.verification_blockers(iw)
        return [Action(x, u("unloading"), "G04-DMG" if "photo" in x else "Checklist") for x in b] or [
            Action("Record SED (if any) and confirm physical verification", u("unloading"), "Verification")]
    if st == S.PHYSICALLY_VERIFIED:
        out = [Action(x, u("charges") if ("package" in x.lower() or "unloading" in x.lower()) else u("unloading"),
                      "G04-ULC" if "unloading" in x.lower() else "MRA") for x in mra.blockers(iw)]
        if out:
            return out
        last = iw.mras.first()
        if last is None or not mra.is_current(iw, last):
            return [Action("Issue the MRA" if last is None else f"Issue a new MRA revision (changed after {last.label})",
                           u("mra"), "MRA")]
        mails = list(last.mails.all())
        if not mails:
            return [Action(f"Send MRA {last.label} and the SED mails", u("mra"), "G04-MAIL")]
        return [Action(f"{g.label}: {g.detail or 'pending'}", u("mra"), g.code) for g in comms.gates(iw) if not g.ok]
    if st in erp.ERP_STAGES:
        out = []
        for link in iw.ship_links.all():
            if not link.receipts.filter(kind=ErpReceipt.Kind.NORMAL).exists():
                out.append(Action(f"Ship {link.ship_id}: enter the Stock Receipt No", u("erp"), "ERP"))
        for c in iw.vendor_consignments.all():
            if not c.receipts.exists():
                out.append(Action(f"Challan {c.challan_no}: enter the PO Receipt No", u("erp"), "ERP"))
        no_lsccr = set()
        for m in erp.memos(iw):
            if m.direct and not m.lsccr_no:
                if m.source_key not in no_lsccr:
                    no_lsccr.add(m.source_key)
                    out.append(Action(f"{m.source_label}: record the LSCCR No", u("erp"), "SED"))
            elif not m.trx:
                out.append(Action(f"{m.source_label} {m.kind.lower()} memo: enter the Oracle TRX No"
                                  + (f" created by LSCCR {m.lsccr_no}" if m.direct else ""), u("erp"), "SED"))
        if out:
            return out
        run = iw.recon_runs.first()
        if run is None or not run.all_ok:
            bad = [r["track"] for r in (run.results if run else []) if r["status"] not in ("FULL", "LSCRR_COVERED")]
            return [Action("Import the latest IRR and run the reconciliation" + (f" ({', '.join(bad[:3])})" if bad else ""),
                           u("erp"), "IRR")]
        gates = [g for g in closure.gates(iw, []) if not g.ok and "reconciled" not in g.label]
        return [Action(g.label + (f": {g.detail}" if g.detail else ""), u("erp"), "Closure") for g in gates] or [
            Action("Close the IW", u("erp"), "Closure")]
    return []


@dataclass
class OpenIw:
    iw: Iw
    age_hours: float
    progress: list
    actions: list = field(default_factory=list)

    @property
    def age_text(self) -> str:
        h = self.age_hours
        return f"{h:.0f} h" if h < 48 else f"{h / 24:.0f} days"


def open_iws(with_actions: bool = True) -> list[OpenIw]:
    now = timezone.now()
    out = []
    for iw in (Iw.objects.filter(status__in=OPEN).select_related("load_type", "source_org", "transporter")
               .order_by("arrival_at")):
        o = OpenIw(iw, (now - iw.arrival_at).total_seconds() / 3600, progress(iw))
        if with_actions:
            try:
                o.actions = actions(iw)
            except Exception as exc:  # noqa: BLE001 - one bad IW must never break the dashboard
                o.actions = [Action(f"Check this IW ({type(exc).__name__})", reverse("iw:detail", args=[iw.pk]))]
        out.append(o)
    return out


def today() -> dict:
    day = timezone.localdate()
    start = timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.min.time()))
    end = start + timedelta(days=1)
    return {"arrivals": Iw.objects.filter(arrival_at__gte=start, arrival_at__lt=end).exclude(status=S.VOID).count(),
            "created": Iw.objects.filter(created_at__gte=start, created_at__lt=end).exclude(iw_no=None).count(),
            "mras": MraRevision.objects.filter(issued_at__gte=start, issued_at__lt=end, revision=1).count(),
            "closed": IwClosure.objects.filter(closed_at__gte=start, closed_at__lt=end).count()}


def counts() -> dict:
    by = dict(Iw.objects.filter(status__in=OPEN).values_list("status").annotate(n=Count("id")))
    return {"open": sum(by.values()), "erp_pending": by.get(S.MRA_ISSUED, 0) + by.get(S.ERP_IN_PROGRESS, 0)
            + by.get(S.REOPENED, 0), "unloading": by.get(S.UNLOADING_IN_PROGRESS, 0) + by.get(S.MATERIAL_LINKED, 0),
            "to_close": by.get(S.RECON_COMPLETE, 0) + by.get(S.DOCUMENTS_COMPLETE, 0), "by_status": by}


def sit_watch() -> dict:
    from inward.sit.models import SitShip
    st = SitShip.Status
    q = SitShip.objects.filter(pre_go_live=False)
    return {"partial": q.filter(status=st.PARTIAL).count(),
            "removed_no_receipt": q.filter(status=st.REMOVED_NO_RECEIPT).count(),
            "never_linked": q.filter(status=st.REMOVED_NEVER_LINKED).count(),
            "in_sit": q.filter(status=st.IN_SIT).count()}


def sed_month() -> dict:
    day = timezone.localdate()
    start = timezone.make_aware(timezone.datetime(day.year, day.month, 1))
    q = SedLine.objects.filter(iw__verified_at__gte=start).exclude(iw__status=S.VOID)
    agg = q.aggregate(short=Sum("short_qty"), excess=Sum("excess_qty"), damage=Sum("damage_qty"),
                      sv=Sum("short_value"), dv=Sum("damage_value"))
    by_source = (q.values("iw__source_org__code", "iw__source_org__name")
                 .annotate(n=Count("id"), v=Coalesce(Sum("short_value"), Value(0), output_field=MONEY)
                           + Coalesce(Sum("damage_value"), Value(0), output_field=MONEY)).order_by("-v")[:8])
    return {"lines": q.count(), "iws": q.values("iw").distinct().count(), "short": agg["short"] or 0,
            "excess": agg["excess"] or 0, "damage": agg["damage"] or 0,
            "claimable": (agg["sv"] or Decimal(0)) + (agg["dv"] or Decimal(0)), "by_source": list(by_source)}


def unloading_position() -> dict:
    day = timezone.localdate()
    start = timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.min.time()))
    todays = UnloadingRecord.objects.filter(recorded_at__gte=start)
    charges = TransporterLedgerEntry.objects.filter(kind="CHARGE").aggregate(s=Sum("amount"))["s"] or Decimal(0)
    receipts = TransporterLedgerEntry.objects.filter(kind="RECEIPT").aggregate(s=Sum("amount"))["s"] or Decimal(0)
    return {"today_charge": todays.aggregate(s=Sum("charge"))["s"] or Decimal(0),
            "today_paid": todays.aggregate(s=Sum("paid_amount"))["s"] or Decimal(0),
            "ledger_outstanding": charges - receipts,
            "exempt_pending": UnloadingRecord.objects.filter(status=UnloadingRecord.Status.EXEMPT_REQUESTED).count()}


def approval_board() -> list[SedApproval]:
    latest = {}
    for ap in SedApproval.objects.select_related("iw", "mra", "request_mail").order_by("iw_id", "-created_at"):
        latest.setdefault(ap.iw_id, ap)
    rank = {"REJECTED": 0, "MANUAL_PENDING": 1, "REQUESTED": 2, "MANUAL_OK": 3, "APPROVED": 4}
    return sorted(latest.values(), key=lambda a: (rank.get(a.status, 9), a.created_at))


def mail_alert() -> dict:
    stuck = MailMessage.objects.exclude(status=MailMessage.Status.SENT)
    old = stuck.filter(created_at__lt=timezone.now() - timedelta(minutes=30))
    failed = stuck.filter(status__in=[MailMessage.Status.FAILED, MailMessage.Status.FAILED_UNCONFIRMED])
    return {"failed": failed.count(), "late": old.count(), "pending": stuck.count(),
            "show": failed.exists() or old.exists()}


def dashboard() -> dict:
    rows = open_iws()
    return {"today": today(), "counts": counts(), "open": rows, "sit": sit_watch(), "sed": sed_month(),
            "unl": unloading_position(), "board": approval_board()[:20], "mail": mail_alert(),
            "stale_checklists": sum(1 for r in rows if r.iw.status == S.UNLOADING_IN_PROGRESS
                                    and checklist.is_stale(r.iw, r.iw.checklists.first()))}


def mail_banner() -> dict | None:
    """For every page (context processor): red banner when mandatory mail is failing or late."""
    a = mail_alert()
    return a if a["show"] else None


def my_actions(user) -> list[tuple[Iw, Action]]:
    from inward.accounts.services import modes_for
    out = []
    for r in open_iws():
        for a in r.actions:
            out.append((r.iw, a))
    if "GRANT" in modes_for(user, "EMAIL_VERIFY"):
        n = len(comms.unverified_emails())
        if n:
            out.insert(0, (None, Action(f"{n} clerk-entered e-mail(s) to verify", reverse("iw:email_verify"), "E-mail")))
    return out

