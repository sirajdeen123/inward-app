"""Screen 05 views - receipts, LSCRR, SED TRX and IRR reconciliation (thin; rules in erp.py / recon.py)."""
from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from inward.accounts.services import PermissionDenied
from inward.iw import erp, recon
from inward.iw.models import ErpReceipt, Iw, SedTrx, ShipLink
from inward.iw.services import IwError
from inward.web.views import needs


@needs("RECEIPT_ENTRY")
def erp_page(request, pk):
    iw = get_object_or_404(Iw.objects.select_related("load_type"), pk=pk)
    run = iw.recon_runs.select_related("run_by").first()
    return render(request, "iw/erp.html", {
        "iw": iw, "open": iw.status in erp.ERP_STAGES,
        "ships": iw.ship_links.select_related("ship").prefetch_related("receipts").order_by("linked_at"),
        "vendors": iw.vendor_consignments.prefetch_related("receipts"), "memos": erp.memos(iw), "run": run,
        "live": recon.reconcile(iw) if iw.status in erp.ERP_STAGES else []})


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
        link = erp.set_lscrr(request.user, link_pk, request.POST.get("applied") == "yes",
                             request.POST.get("lscrr_no", ""), request.POST.get("reason", ""))
        messages.success(request, f"Ship {link.ship_id}: LSCRR {'applied ' + link.lscrr_no if link.lscrr_applied else 'not applied'}.")
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
