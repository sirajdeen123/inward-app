"""Screen 05 views - receipts, LSCCR No, SED TRX and IRR reconciliation (thin; rules in erp.py / recon.py)."""
from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from inward.accounts.services import PermissionDenied
from inward.iw import closure, erp, recon
from inward.iw.models import ErpReceipt, Iw, SedTrx, ShipLink
from inward.iw.services import IwError
from inward.web.views import needs, upload_done


@needs("RECEIPT_ENTRY")
def erp_page(request, pk):
    iw = get_object_or_404(Iw.objects.select_related("load_type"), pk=pk)
    from inward.accounts.services import modes_for
    run = iw.recon_runs.select_related("run_by").first()
    live = recon.reconcile(iw) if iw.status in erp.ERP_STAGES or iw.status == Iw.Status.CLOSED else []
    close_gates = closure.gates(iw, live) if iw.status in erp.ERP_STAGES else []
    sc = closure.scans(iw)
    return render(request, "iw/erp.html", {
        "iw": iw, "open": iw.status in erp.ERP_STAGES, "close_gates": close_gates,
        "ready": bool(close_gates) and all(g.ok for g in close_gates),
        "scan_rows": [(role, label, sc[role]) for role, label in closure.SCAN_ROLES.items()],
        "lr_required": closure.lr_required(iw), "closures": list(iw.closures.select_related("closed_by", "report")),
        "can_reopen": bool(modes_for(request.user, "IW_REOPEN") & {"REQUEST", "APPROVE"}),
        "can_scan": "GRANT" in modes_for(request.user, "SCAN_UPLOAD"),
        "ships": iw.ship_links.select_related("ship").prefetch_related("receipts").order_by("linked_at"),
        "vendors": iw.vendor_consignments.prefetch_related("receipts"), "memos": erp.memos(iw), "run": run,
        "live": live})


def _back(pk):
    return redirect("iw:erp", pk=pk)


@require_POST
@needs("RECEIPT_ENTRY")
def receipt_add(request, pk):
    try:
        r = erp.add_receipt(request.user, pk, request.POST.get("ref", ""), request.POST.get("kind", ""),
                            request.POST.get("receipt_no", ""))
        messages.success(request, f"{r.get_kind_display()} {r.receipt_no} recorded - IRR verification pending.")
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return _back(pk)


@require_POST
@needs("RECEIPT_ENTRY")
def receipt_remove(request, pk, receipt_pk):
    get_object_or_404(ErpReceipt, pk=receipt_pk, iw_id=pk)
    try:
        erp.remove_receipt(request.user, receipt_pk)
        messages.success(request, "Receipt removed.")
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return _back(pk)


@require_POST
@needs("SED_TRX_LSCRR")
def lscrr_set(request, pk, link_pk):
    get_object_or_404(ShipLink, pk=link_pk, iw_id=pk)
    try:
        link = erp.set_lscrr(request.user, link_pk, request.POST.get("lscrr_no", ""), request.POST.get("reason", ""))
        messages.success(request, f"Ship {link.ship_id}: LSCCR {link.lscrr_no} recorded.")
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return _back(pk)


@require_POST
@needs("SED_TRX_LSCRR")
def trx_add(request, pk):
    try:
        t = erp.add_sed_trx(request.user, pk, request.POST.get("memo", ""), request.POST.get("trx_no", ""))
        messages.success(request, f"TRX {t.trx_no} recorded for {t.source_label} {t.get_kind_display().lower()}.")
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return _back(pk)


@require_POST
@needs("SED_TRX_LSCRR")
def trx_remove(request, pk, trx_pk):
    get_object_or_404(SedTrx, pk=trx_pk, iw_id=pk)
    try:
        erp.remove_sed_trx(request.user, trx_pk)
        messages.success(request, "TRX removed.")
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return _back(pk)


@require_POST
@needs("IMPORT_RECONCILE")
def recon_run(request, pk):
    try:
        rr = recon.run(request.user, pk)
        if rr.all_ok:
            messages.success(request, "Everything reconciles with the IRR.")
        else:
            messages.warning(request, "Reconciliation saved - some items are not fully matched yet (see below).")
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return _back(pk)


@require_POST
@needs("SCAN_UPLOAD")
def scan_add(request, pk):
    files = request.FILES.getlist("file")
    try:
        docs = closure.add_scans(request.user, pk, request.POST.get("role", ""),
                                 [(f.read(), f.name, f.content_type) for f in files])
        messages.success(request, "Saved as PDF: " + ", ".join(d.original_filename for d in docs) + ".")
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return upload_done(request, reverse("iw:erp", args=[pk]))


@needs("SEARCH_REPORTS")
def scan_file(request, pk, doc_pk):
    from django.http import Http404, HttpResponse

    from inward.core import documents
    from inward.core.models import DocumentLink
    link = DocumentLink.objects.filter(entity_type="iw", entity_id=str(pk), document_id=doc_pk,
                                       role__in=list(closure.SCAN_ROLES)).select_related("document").first()
    if link is None:
        raise Http404("No such document.")
    resp = HttpResponse(documents.read(link.document), content_type=link.document.content_type)
    resp["Content-Disposition"] = f'inline; filename="{link.document.original_filename}"'
    return resp


@require_POST
@needs("IW_CLOSE")
def close(request, pk):
    try:
        snap = closure.close(request.user, pk)
        messages.success(request, f"IW closed (C{snap.number})." + ("" if snap.report else
                         " The report PDF is made on the server."))
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return _back(pk)


@needs("SEARCH_REPORTS")
def closure_report(request, pk, number):
    from django.http import Http404, HttpResponse

    from inward.core import documents
    snap = get_object_or_404(closure.IwClosure.objects.select_related("report"), iw_id=pk, number=number)
    if snap.report is None:
        raise Http404("No report PDF.")
    resp = HttpResponse(documents.read(snap.report), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{snap.report.original_filename}"'
    return resp


@require_POST
@needs("IW_REOPEN", modes=("REQUEST", "APPROVE"))
def reopen_request(request, pk):
    iw = get_object_or_404(Iw, pk=pk)
    try:
        req = closure.request_reopen(request.user, iw, request.POST.get("reason", ""))
        if req.status == req.Status.APPROVED:
            messages.success(request, "IW reopened (you are the approver - recorded as self-approved).")
        else:
            messages.success(request, "Reopen requested - waiting for the CFA Manager's approval.")
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return _back(pk)


@needs("SEARCH_REPORTS")
def recon_detail(request, pk):
    """Detailed reconciliation report - every SKU with its put-away locator; open or closed IW; ?pdf=1 on the server."""
    from django.http import HttpResponse
    from django.utils import timezone

    from inward.core import branding, pdf
    iw = get_object_or_404(Iw.objects.select_related("load_type", "source_org"), pk=pk)
    if iw.status not in erp.ERP_STAGES and iw.status != Iw.Status.CLOSED:
        messages.error(request, "The reconciliation starts after the MRA is issued.")
        return _back(pk)
    mra = iw.mras.first()
    from inward.iw.mra import other_items
    ctx = {"iw": iw, "d": recon.detail(iw), "items": other_items(iw), "c": (mra.content if mra else {}) or {},
           "logo": branding.logo_data_uri(),
           "closure": iw.closures.select_related("closed_by").first(), "run": iw.recon_runs.select_related("run_by").first(),
           "generated": timezone.localtime(timezone.now()), "by": request.user.username,
           "pdf_available": pdf.available()}
    if request.GET.get("pdf") and pdf.available():
        data = pdf.html_to_pdf(pdf.render_html("iw/recon_detail.html", ctx | {"for_pdf": True}), "inward/recon.css")
        resp = HttpResponse(data, content_type="application/pdf")
        resp["Content-Disposition"] = f'inline; filename="Reconciliation detail {iw.iw_no or iw.pk}.pdf"'
        return resp
    return render(request, "iw/recon_detail.html", ctx)
