"""Screen 05 part C - final documents, closure gate, reopen (SoT 8.7, 9, table 11).

Closure needs: Screen 04 complete; every inbound Ship (normal + excess receipts), vendor PO receipt and SED memo
reconciled against the IRR at the moment of closing; the mandatory mails SENT (also after a
Mail-Pending Release); the signed MRA scan; the transporter LR scan (not for Others (Non-Stock) without an LR);
no unresolved critical exception. Closing generates the Final IRR Reconciliation Report (PDF) and freezes a
closure snapshot. A closed IW is read-only; reopening is requested with a reason and approved by the CFA
Manager, and the next closure is a new snapshot. An outstanding unloading ledger never blocks closure.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from inward.accounts.models import ApprovalRequest, User
from inward.accounts.services import require
from inward.core import audit, branding, documents, pdf
from inward.core.models import DocumentLink, ExceptionEvent
from inward.iw import erp, recon
from inward.iw.models import Iw, IwClosure, MailMessage
from inward.iw.services import IwError

SCAN_ROLES = {"signed_mra": "Signed MRA (driver + depot)", "lr_scan": "Transporter LR"}
SCAN_TYPES = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png"}
CLOSABLE = {Iw.Status.ERP_IN_PROGRESS, Iw.Status.RECON_COMPLETE, Iw.Status.DOCUMENTS_COMPLETE, Iw.Status.REOPENED,
            Iw.Status.MRA_ISSUED}


@dataclass
class Gate:
    label: str
    ok: bool
    detail: str = ""


def scans(iw: Iw) -> dict[str, list]:
    out = {role: [] for role in SCAN_ROLES}
    for link in DocumentLink.objects.filter(entity_type="iw", entity_id=str(iw.pk), role__in=list(SCAN_ROLES)
                                            ).select_related("document").order_by("document__created_at"):
        out[link.role].append(link.document)
    return out


def lr_required(iw: Iw) -> bool:
    return not (iw.load_type.is_non_stock and not iw.lr_no)


@transaction.atomic
def add_scans(user: User, iw_pk: int, role: str, files: list[tuple[bytes, str, str]]) -> list:
    """files: (data, filename, content type). A PDF is stored as it is; the photos of one upload are converted
    into ONE PDF (one page per photo, decision 17-Oct-2026), so every final document on file is a PDF."""
    require(user, "IW_CLOSE")
    iw = Iw.objects.get(pk=iw_pk)
    if role not in SCAN_ROLES:
        raise IwError("Unknown document.")
    if iw.status in (Iw.Status.CLOSED, Iw.Status.VOID):
        raise IwError("The IW is closed - reopen it to add documents.")
    if iw.status not in erp.ERP_STAGES:
        raise IwError("Scans are added after the MRA is issued.")
    pdfs, photos = [], []
    for data, filename, content_type in files:
        ctype = (content_type or "").split(";")[0].lower()
        if ctype not in SCAN_TYPES or not data:
            raise IwError(f"{filename or 'File'}: upload a PDF or a photo (JPEG / PNG).")
        if len(data) > 20 * 1024 * 1024:
            raise IwError(f"{filename or 'File'} is larger than 20 MB.")
        (pdfs if ctype == "application/pdf" else photos).append((data, filename))
    if not pdfs and not photos:
        raise IwError("Choose a scan, photo or PDF.")
    stamp = timezone.localtime(timezone.now()).strftime("%Y%m%d-%H%M%S")
    stored = []
    for data, filename in pdfs:
        stored.append((data, filename or f"{role}.pdf", [filename]))
    if photos:
        try:
            data = pdf.images_to_pdf([d for d, _ in photos])
        except pdf.ImageError as exc:
            raise IwError(str(exc)) from exc
        stored.append((data, f"{iw.iw_no or iw.pk} {SCAN_ROLES[role]} {stamp}.pdf", [n for _, n in photos]))
    docs = []
    for data, filename, originals in stored:
        doc = documents.store(data, filename=filename, content_type="application/pdf", doc_type=role.upper(),
                              user=user, links=[("iw", iw.pk, role)])
        audit.record("SCAN_ADDED", "iw", iw.pk, user=user, iw_id=iw.pk,
                     after={"role": role, "document": str(doc.pk), "sha256": doc.sha256, "uploaded": originals,
                            "converted_to_pdf": filename not in originals})
        docs.append(doc)
    _refresh(iw)
    return docs


def add_scan(user: User, iw_pk: int, role: str, data: bytes, filename: str, content_type: str):
    return add_scans(user, iw_pk, role, [(data, filename, content_type)])[0]


def _critical(iw: Iw) -> list[ExceptionEvent]:
    ships = [l.ship_id for l in iw.ship_links.all()]
    return list(ExceptionEvent.objects.filter(severity="CRITICAL", resolved_at__isnull=True)
                .filter(Q(entity_type="ship", entity_id__in=ships) | Q(entity_type="iw", entity_id=str(iw.pk))))


def gates(iw: Iw, results=None) -> list[Gate]:
    results = recon.reconcile(iw) if results is None else results
    out = [Gate("Screen 04 complete (MRA issued)", iw.status in erp.ERP_STAGES or iw.status == Iw.Status.CLOSED)]
    groups = [("Inbound Ships (normal + excess receipts) fully reconciled", ("SHIP", "EXCESS")),
              ("Vendor PO receipts fully reconciled", ("PO",)),
              ("SED memos reconciled", ("SED",))]
    for label, kinds in groups:
        items = [r for r in results if r.kind in kinds]
        if items:
            bad = [f"{r.track}: {r.status}" for r in items if not r.ok]
            out.append(Gate(label, not bad, "; ".join(bad)))
    rev = iw.mras.first()
    mails = list(rev.mails.filter(purpose__in=[MailMessage.Purpose.MRA, MailMessage.Purpose.SED_NOTIFY])) if rev else []
    unsent = [m.get_purpose_display() for m in mails if m.status != MailMessage.Status.SENT]
    out.append(Gate("Mandatory mails SENT", bool(mails) and not unsent,
                    ", ".join(unsent) if unsent else ("" if mails else "MRA mail not sent")))
    sc = scans(iw)
    out.append(Gate("Signed MRA scan", bool(sc["signed_mra"])))
    if lr_required(iw):
        out.append(Gate("Transporter LR scan", bool(sc["lr_scan"])))
    crit = _critical(iw)
    out.append(Gate("No unresolved critical exception", not crit, "; ".join(e.message[:80] for e in crit[:3])))
    return out


def _refresh(iw: Iw) -> None:
    """RECON_COMPLETE + scans present -> DOCUMENTS_COMPLETE (display only; closure re-checks everything)."""
    iw = Iw.objects.get(pk=iw.pk)
    sc = scans(iw)
    docs_ok = bool(sc["signed_mra"]) and (bool(sc["lr_scan"]) or not lr_required(iw))
    if iw.status == Iw.Status.RECON_COMPLETE and docs_ok:
        iw.status = Iw.Status.DOCUMENTS_COMPLETE
        iw.version += 1
        iw.save(update_fields=["status", "version", "updated_at"])


def report_html(iw: Iw, results, gate_list, number: int, closed_by: str, closed_at) -> str:
    content = (iw.mras.first().content if iw.mras.exists() else {}) or {}
    return pdf.render_html("iw/recon_report.html", {
        "iw": iw, "c": content, "results": [r.as_dict() for r in results], "gates": gate_list, "number": number,
        "closed_by": closed_by, "closed_at": timezone.localtime(closed_at).strftime("%d-%b-%Y %H:%M"),
        "logo": branding.logo_data_uri(), "receipts": iw.receipts.select_related("ship_link", "vendor_consignment"),
        "trx": iw.sed_trx.all()})


@transaction.atomic
def close(user: User, iw_pk: int) -> IwClosure:
    require(user, "IW_CLOSE")
    iw = Iw.objects.select_for_update(of=("self",)).select_related("load_type").get(pk=iw_pk)
    if iw.status not in CLOSABLE:
        raise IwError("This IW cannot be closed in its present state.")
    results = recon.reconcile(iw)
    gate_list = gates(iw, results)
    bad = [g for g in gate_list if not g.ok]
    if bad:
        raise IwError("Not ready to close: " + "; ".join(g.label + (f" ({g.detail})" if g.detail else "") for g in bad))
    number = iw.closures.count() + 1
    now = timezone.now()
    from inward.iw import audit_memo
    audit_memo.regenerate(iw, user, f"closure C{number}")  # final audit copy with TRX numbers and IRR result
    html = report_html(iw, results, gate_list, number, user.full_name or user.username, now)
    data = pdf.html_to_pdf(html)
    doc = None
    if data is not None:
        doc = documents.store(data, filename=f"IRR Reconciliation {iw.iw_no} C{number}.pdf",
                              content_type="application/pdf", doc_type="IRR_RECON_REPORT", user=user,
                              links=[("iw", iw.pk, "irr_recon_report")])
    snap = IwClosure.objects.create(iw=iw, number=number, gates=[g.__dict__ for g in gate_list],
                                    recon=[r.as_dict() for r in results], report=doc, closed_by=user)
    iw.status = Iw.Status.CLOSED
    iw.version += 1
    iw.save(update_fields=["status", "version", "updated_at"])
    from inward.iw.reports import send_irr_report
    send_irr_report(snap, user)  # Admin Officer, CFA Manager, Clerk (SoT table 15)
    audit.record("IW_CLOSED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"closure": number, "report": str(doc.pk) if doc else None,
                        "results": {r.track: r.status for r in results}})
    return snap


def request_reopen(user: User, iw: Iw, reason: str) -> ApprovalRequest:
    if iw.status != Iw.Status.CLOSED:
        raise IwError("Only a closed IW can be reopened.")
    if ApprovalRequest.objects.filter(permission_id="IW_REOPEN", entity_type="iw", entity_id=str(iw.pk),
                                      status=ApprovalRequest.Status.PENDING).exists():
        raise IwError("A reopen request for this IW is already waiting for approval.")
    try:
        from inward.accounts.approvals import submit
        return submit(user, "IW_REOPEN", "iw", iw.pk, reason, payload={"iw_no": iw.iw_no})
    except ValueError as exc:
        raise IwError(str(exc)) from exc


def apply_reopen(req: ApprovalRequest, approver: User) -> None:
    iw = Iw.objects.select_for_update().get(pk=int(req.entity_id))
    if iw.status != Iw.Status.CLOSED:
        raise IwError(f"{iw.iw_no} is not closed.")
    last = iw.closures.first()
    if last:
        last.reopened_at, last.reopen_reason = timezone.now(), req.reason
        last.save(update_fields=["reopened_at", "reopen_reason"])
    iw.status = Iw.Status.REOPENED
    iw.version += 1
    iw.save(update_fields=["status", "version", "updated_at"])
    audit.record("IW_REOPENED", "iw", iw.pk, user=approver, iw_id=iw.pk,
                 after={"reason": req.reason, "requested_by": req.requested_by_id}, approval_request=req.pk)

