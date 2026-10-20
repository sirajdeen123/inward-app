"""Screen 04 part C views - MRA and SED memos (thin; rules in mra.py)."""
from __future__ import annotations

from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from inward.accounts.services import PermissionDenied
from inward.core import documents, pdf
from inward.iw import mra
from inward.iw.models import Iw, MraRevision
from inward.iw.services import IwError
from inward.web.views import needs


@needs("MRA_ISSUE")
def mra_page(request, pk):
    iw = get_object_or_404(Iw.objects.select_related("load_type", "source_org", "transporter"), pk=pk)
    latest = iw.mras.first()
    return render(request, "iw/mra.html", {
        "iw": iw, "blockers": mra.blockers(iw), "revisions": iw.mras.select_related("issued_by", "pdf"),
        "latest": latest, "current": mra.is_current(iw, latest) if latest and not mra.blockers(iw) else bool(latest),
        "pdf_available": pdf.available(), **_comms_context(request, iw)})


@require_POST
@needs("MRA_ISSUE")
def mra_issue(request, pk):
    try:
        rev, created = mra.issue(request.user, pk, request.POST.get("reason", ""),
                                 reconfirm=request.POST.get("reconfirm") == "yes")
        if created:
            messages.success(request, f"MRA {rev.label} issued." + ("" if rev.pdf else
                             " The PDF is made on the server; use 'View / print' on this PC."))
        else:
            messages.info(request, f"Nothing changed since {rev.label} - print that revision.")
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return redirect("iw:mra", pk=pk)


def _revision(pk, revision) -> MraRevision:
    return get_object_or_404(MraRevision.objects.select_related("iw", "issued_by", "pdf"), iw_id=pk, revision=revision)


@needs("SEARCH_REPORTS")
def mra_view(request, pk, revision):
    rev = _revision(pk, revision)
    mra.record_print(request.user, rev, "html")
    return HttpResponse(mra.render(rev.content, rev.revision, rev.issued_at,
                                   rev.issued_by.full_name or rev.issued_by.username, rev.reason))


@needs("SEARCH_REPORTS")
def mra_pdf(request, pk, revision):
    rev = _revision(pk, revision)
    if rev.pdf is None:
        raise Http404("No PDF for this revision.")
    mra.record_print(request.user, rev, "pdf")
    resp = HttpResponse(documents.read(rev.pdf), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{rev.pdf.original_filename}"'
    return resp


# ---------------------------------------------------------------- 2D: communications, gates, approval


def _comms_context(request, iw: Iw) -> dict:
    from inward.accounts.services import modes_for
    from inward.iw import audit_memo, comms
    latest = iw.mras.first()
    return {"targets": comms.mra_targets(iw), "default_cc": comms.default_mra_cc(), "send_blockers": comms.send_blockers(iw),
            "mails": latest.mails.select_related("eml").order_by("id") if latest else [],
            "all_mails": iw.mails.select_related("mra").order_by("-id"),
            "gates": comms.gates(iw) if latest else [], "approval": comms.approval(iw),
            "release_blockers": comms.release_blockers(iw),
            "can_release": "GRANT" in modes_for(request.user, "MAIL_PENDING_RELEASE"),
            "can_accept": "GRANT" in modes_for(request.user, "SED_EVIDENCE_ACCEPT"),
            "releases": iw.mail_releases.select_related("released_by"),
            "audit_memos": audit_memo.versions(iw)}


@require_POST
@needs("MRA_ISSUE")
def mra_send(request, pk):
    from inward.iw import comms
    try:
        mails = comms.send_mra(request.user, pk, request.POST.get("extra_cc", ""))
        messages.success(request, f"{len(mails)} e-mail(s) queued. They count as sent once Gmail confirms them "
                                  "(normally within a minute).")
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return redirect("iw:mra", pk=pk)


@require_POST
@needs("MRA_ISSUE")
def mra_capture_email(request, pk):
    from inward.iw import comms
    try:
        comms.capture_email(request.user, pk, request.POST.get("kind", ""), request.POST.get("key", ""),
                            request.POST.get("email", ""))
        messages.success(request, "E-mail saved and used now; it is marked Unverified until the CFA Manager confirms it.")
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return redirect("iw:mra", pk=pk)


@require_POST
@needs("MRA_ISSUE")
def mail_retry(request, pk, mail_pk):
    from inward.iw import comms
    try:
        comms.retry(request.user, mail_pk)
        messages.success(request, "The e-mail is back in the queue.")
    except IwError as exc:
        messages.error(request, str(exc))
    return redirect("iw:mra", pk=pk)


@needs("SEARCH_REPORTS")
def mail_eml(request, pk, mail_pk):
    from inward.iw.models import MailMessage
    m = get_object_or_404(MailMessage.objects.select_related("eml"), pk=mail_pk, iw_id=pk)
    if m.eml is None:
        raise Http404("Not sent yet.")
    resp = HttpResponse(documents.read(m.eml), content_type="message/rfc822")
    resp["Content-Disposition"] = f'attachment; filename="{m.eml.original_filename}"'
    return resp


@require_POST
@needs("MAIL_PENDING_RELEASE")
def mail_release(request, pk):
    from inward.iw import comms
    try:
        comms.release(request.user, pk, request.POST.get("reason", ""))
        messages.success(request, "Released to ERP processing. The mail stays queued; closure waits until it is SENT.")
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return redirect("iw:mra", pk=pk)


@require_POST
@needs("MRA_ISSUE")
def approval_evidence(request, pk, approval_pk):
    from inward.iw import comms
    f = request.FILES.get("file")
    try:
        if not f:
            raise IwError("Choose the approval e-mail (.eml) or PDF.")
        comms.upload_evidence(request.user, approval_pk, f.read(), f.name)
        messages.success(request, "Evidence uploaded - waiting for the CFA Manager's check.")
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return redirect("iw:mra", pk=pk)


@require_POST
@needs("SED_EVIDENCE_ACCEPT")
def approval_decide(request, pk, approval_pk):
    from inward.iw import comms
    try:
        ap = comms.decide_evidence(request.user, approval_pk, request.POST.get("decision") == "accept")
        messages.success(request, f"SED approval: {ap.get_status_display()}.")
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return redirect("iw:mra", pk=pk)


@needs("SEARCH_REPORTS")
def approval_file(request, pk, approval_pk):
    from inward.iw.models import SedApproval
    ap = get_object_or_404(SedApproval.objects.select_related("evidence"), pk=approval_pk, iw_id=pk)
    if ap.evidence is None:
        raise Http404("No evidence.")
    resp = HttpResponse(documents.read(ap.evidence), content_type=ap.evidence.content_type)
    resp["Content-Disposition"] = f'attachment; filename="{ap.evidence.original_filename}"'
    return resp


@needs("EMAIL_VERIFY")
def email_verify(request):
    from inward.iw import comms
    if request.method == "POST":
        try:
            comms.verify_email(request.user, request.POST.get("kind", ""), request.POST.get("pk", ""),
                               request.POST.get("corrected", ""))
            messages.success(request, "E-mail confirmed.")
        except (IwError, PermissionDenied) as exc:
            messages.error(request, str(exc))
        return redirect("iw:email_verify")
    return render(request, "iw/email_verify.html", {"rows": comms.unverified_emails()})


@require_POST
@needs("MRA_ISSUE")
def audit_memo_generate(request, pk):
    from inward.iw import audit_memo
    iw = get_object_or_404(Iw, pk=pk)
    doc = audit_memo.regenerate(iw, request.user, "on request")
    if doc:
        messages.success(request, f"SED Audit Memo {doc.original_filename} generated.")
    elif not iw.sed_lines.exists():
        messages.info(request, "No SED on this IW - the MRA's NIL declaration is the record.")
    else:
        messages.info(request, "The PDF is made on the server; use View on this computer.")
    return redirect("iw:mra", pk=pk)


@needs("SEARCH_REPORTS")
def audit_memo_view(request, pk):
    from inward.iw import audit_memo
    iw = get_object_or_404(Iw, pk=pk)
    return HttpResponse(audit_memo.render(iw, len(audit_memo.versions(iw)) + 1))


@needs("SEARCH_REPORTS")
def audit_memo_file(request, pk, doc_pk):
    from inward.core.models import DocumentLink
    link = get_object_or_404(DocumentLink.objects.select_related("document"), entity_type="iw", entity_id=str(pk),
                             role="sed_audit_memo", document_id=doc_pk)
    resp = HttpResponse(documents.read(link.document), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{link.document.original_filename}"'
    return resp
