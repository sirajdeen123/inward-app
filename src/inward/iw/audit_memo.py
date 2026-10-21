"""SED Audit Memo (business owner's requirement, 16-Oct-2026): one document per SED IW that answers an auditor's
questions without anything else - what was short / excess / damaged, who declared it and when, the damage photos,
the MRA and mails with their send evidence, the Depot Manager's approval (request, reminder, reply or manual
evidence, check result), the Oracle TRX numbers and the IRR result, and where every file is kept (document ID,
SHA-256, OneDrive path). It is regenerated as a new version whenever the approval changes and at closure; earlier
versions are kept (each new one supersedes the previous)."""
from __future__ import annotations

from django.utils import timezone

from inward.core import branding, documents, pdf
from inward.core.mirror import path_for
from inward.core.models import Document, DocumentLink
from inward.iw import erp
from inward.iw.models import Iw, MailMessage, SedApproval, SedPhoto

ROLE = "sed_audit_memo"
STORAGE = ("Primary copy: the application's document store on the RAINBOW LOGISTICS server (each file stored once "
           "under its SHA-256 fingerprint and verified on every read). Mirror: the OneDrive path shown for each file. "
           "Encrypted nightly backups: server and OneDrive (Inward Backups).")


def _t(dt) -> str:
    return timezone.localtime(dt).strftime("%d-%b-%Y %I:%M %p") if dt else "-"


def _file(doc: Document | None) -> dict | None:
    if doc is None:
        return None
    return {"name": doc.original_filename, "id": str(doc.pk), "sha256": doc.sha256, "stored": _t(doc.created_at),
            "onedrive": doc.cloud_path or f"{path_for(doc)} (copy pending)"}


def _mail(m: MailMessage | None) -> dict | None:
    if m is None:
        return None
    return {"purpose": m.get_purpose_display(), "stamp": m.stamp, "subject": m.subject, "to": ", ".join(m.to),
            "cc": ", ".join(m.cc), "status": m.get_status_display(), "queued": _t(m.created_at), "sent": _t(m.sent_at),
            "message_id": m.message_id, "eml": _file(m.eml),
            "attachments": [_file(d) for d in m.attachments.all()]}


def build(iw: Iw) -> dict:
    mras = list(iw.mras.select_related("issued_by", "pdf").order_by("revision"))
    latest = mras[-1] if mras else None
    content = latest.content if latest else {}
    run = iw.recon_runs.first()
    results = {r["track"]: r for r in (run.results if run else [])}
    memos = []
    for m in erp.memos(iw):
        res = results.get(f"SED {m.source_label} {m.kind.lower()}")
        memos.append({
            "title": f"{m.kind.title()} memo - {m.source_label}", "kind": m.kind, "direct": m.direct, "lsccr": m.lsccr_no,
            "lines": [{"sku": ln.sku, "floor": ln.floor_sku, "desc": ln.description, "qty": str(q.normalize()),
                       "value": f"{({'SHORT': ln.short_value, 'DAMAGE': ln.damage_value, 'EXCESS': ln.excess_value}[m.kind]) or 0:.2f}",
                       "remarks": {"SHORT": ln.short_remarks, "EXCESS": ln.excess_remarks, "DAMAGE": ln.damage_remarks}[m.kind],
                       "by": ln.updated_by.username if ln.updated_by_id else "", "at": _t(ln.updated_at)}
                      for ln, q in m.lines],
            "trx": [{"no": t.trx_no, "by": t.entered_by.username, "at": _t(t.entered_at)} for t in m.trx],
            "irr": res["status"] if res else "not reconciled yet",
            "irr_notes": res["notes"] if res else []})
    photos = [{"sku": p.line.floor_sku, "source": p.line.source_label, "by": p.uploaded_by.username,
               "at": _t(p.uploaded_at), "file": _file(p.original)}
              for p in SedPhoto.objects.filter(line__iw=iw).select_related("line", "original", "uploaded_by")]
    approvals = []
    for ap in iw.sed_approvals.select_related("mra", "request_mail", "evidence", "evidence_by", "accepted_by").order_by("created_at"):
        reminder = iw.mails.filter(purpose=MailMessage.Purpose.SED_REMINDER, mra=ap.mra).first()
        approvals.append({
            "mra": ap.mra.label, "status": ap.get_status_display(), "ok": ap.status in (SedApproval.Status.APPROVED,
                                                                                         SedApproval.Status.MANUAL_ACCEPTED),
            "requested": _t(ap.created_at), "request": _mail(ap.request_mail), "reminder": _mail(reminder),
            "decided": _t(ap.decided_at), "decision": ap.decision_text, "reply_from": ap.reply_from,
            "check": ("Reply accepted: sent from the registered Depot Manager address and passed Gmail sender "
                      "authentication (SPF/DKIM)" if ap.reply_from else
                      f"Manual evidence uploaded by {ap.evidence_by.username}, accepted by "
                      f"{ap.accepted_by.username}" if ap.accepted_by else
                      f"Manual evidence uploaded by {ap.evidence_by.username} - awaiting CFA Manager check"
                      if ap.evidence_by else ""),
            "evidence": _file(ap.evidence)})
    mails = [_mail(m) for m in iw.mails.filter(purpose__in=[MailMessage.Purpose.MRA, MailMessage.Purpose.SED_NOTIFY,
                                                  MailMessage.Purpose.SED_APPROVAL])
             .select_related("eml").order_by("id")]
    return {"iw": iw, "c": content, "logo": branding.logo_data_uri(), "declaration": iw.sed_declaration or {},
            "mras": [{"label": r.label, "issued": _t(r.issued_at), "by": r.issued_by.username, "reason": r.reason,
                      "file": _file(r.pdf)} for r in mras],
            "memos": memos, "photos": photos, "approvals": approvals, "mails": mails, "storage": STORAGE,
            "recon_at": _t(run.run_at) if run else "-", "generated": _t(timezone.now())}


def versions(iw: Iw) -> list[Document]:
    return [link.document for link in DocumentLink.objects.filter(entity_type="iw", entity_id=str(iw.pk), role=ROLE)
            .select_related("document").order_by("-document__created_at")]


def render(iw: Iw, version: int) -> str:
    return pdf.render_html("iw/sed_audit.html", {**build(iw), "version": version})


def regenerate(iw: Iw, user=None, why: str = "") -> Document | None:
    """New version of the SED Audit Memo PDF (only for IWs with SED, and only where PDFs can be made)."""
    if not iw.sed_lines.exists():
        return None
    prev = versions(iw)
    number = len(prev) + 1
    data = pdf.html_to_pdf(render(iw, number))
    if data is None:
        return None
    doc = documents.store(data, filename=f"SED Audit Memo {iw.iw_no} V{number:02d}.pdf", content_type="application/pdf",
                          doc_type="SED_AUDIT_MEMO", user=user, links=[("iw", iw.pk, ROLE)],
                          supersedes=prev[0] if prev else None)
    from inward.core import audit
    audit.record("SED_AUDIT_MEMO_GENERATED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"version": number, "why": why, "document": str(doc.pk), "sha256": doc.sha256})
    return doc
