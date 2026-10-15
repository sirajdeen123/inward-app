"""Screen 03 part B views - outbound Despatch rows and vendor challans (thin; rules in outbound.py / vendor.py)."""
from __future__ import annotations

import tempfile
from collections import defaultdict
from pathlib import Path

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from inward.accounts.services import PermissionDenied
from inward.core.models import Organisation, Vendor
from inward.importers.common import ImportFailed
from inward.iw import outbound, vendor
from inward.iw.models import Iw, OutboundShip, VendorConsignment
from inward.iw.services import IwError
from inward.web.views import PreviewForm, needs

VENDOR_ROWS = 8


def _save_upload(upload) -> Path:
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(upload.name).suffix.lower()) as tmp:
        for chunk in upload.chunks():
            tmp.write(chunk)
    return Path(tmp.name)


def _checked_upload(request):
    form = PreviewForm({"kind": "SIT"}, request.FILES)
    if not form.is_valid():
        raise IwError(" ".join(str(e) for errs in form.errors.values() for e in errs))
    return form.cleaned_data["file"]


@needs("MATERIAL_LINK")
def outbound_page(request, pk):
    iw = get_object_or_404(Iw.objects.select_related("load_type"), pk=pk)
    st = outbound.state(iw)
    rows = list(outbound.visible_rows(iw).select_related("outbound_ship"))
    reuse = outbound.reused_rows(iw)
    for r in rows:
        r.reused_on = reuse.get(r.pk)
        r.choice = "X" if r.excluded else (str(r.outbound_ship_id) if r.outbound_ship_id else "")
    ships = list(iw.outbound_ships.select_related("destination"))
    per_dest = defaultdict(list)
    for s in ships:
        per_dest[s.destination_id].append(s)
    single_dest = [(d, ss[0]) for d, ss in per_dest.items() if len(ss) == 1]
    return render(request, "iw/outbound.html", {
        "iw": iw, "state": st, "rows": rows, "ships": ships, "single_dest": single_dest,
        "orgs": Organisation.objects.filter(active=True).order_by("code"), "multi_ship": len(ships) > 1})


@require_POST
@needs("MATERIAL_LINK")
def outbound_add_ship(request, pk):
    try:
        s = outbound.add_ship(request.user, pk, ship_id=request.POST.get("ship_id", ""),
                              destination=request.POST.get("destination", ""), doc_nop=request.POST.get("doc_nop", ""),
                              doc_ltr=request.POST.get("doc_ltr", ""), doc_kg=request.POST.get("doc_kg", ""))
        messages.success(request, f"Outbound Ship {s.ship_id} added.")
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return redirect("iw:outbound", pk=pk)


@require_POST
@needs("MATERIAL_LINK")
def outbound_remove_ship(request, ship_pk):
    ship = get_object_or_404(OutboundShip, pk=ship_pk)
    try:
        outbound.remove_ship(request.user, ship.pk)
        messages.success(request, f"Outbound Ship {ship.ship_id} removed; its rows are undecided again.")
    except IwError as exc:
        messages.error(request, str(exc))
    return redirect("iw:outbound", pk=ship.iw_id)


@require_POST
@needs("MATERIAL_LINK")
def outbound_upload(request, pk):
    try:
        upload = _checked_upload(request)
        path = _save_upload(upload)
        try:
            rep = outbound.upload_report(request.user, pk, path, upload.name)
        finally:
            path.unlink(missing_ok=True)
        messages.success(request, f"Despatch report {upload.name} loaded: {rep.rows.count()} rows.")
    except (IwError, ImportFailed) as exc:
        messages.error(request, str(exc))
    return redirect("iw:outbound", pk=pk)


@require_POST
@needs("MATERIAL_LINK")
def outbound_assign(request, pk):
    choices = {int(k[4:]): v for k, v in request.POST.items() if k.startswith("row_")}
    acks = {int(k[4:]) for k in request.POST if k.startswith("ack_")}
    try:
        if request.POST.get("all_for"):
            n = outbound.assign_all_for_destination(request.user, pk, request.POST["all_for"])
            messages.success(request, f"{n} row(s) assigned.")
        else:
            outbound.assign(request.user, pk, choices, acks)
            messages.success(request, "Assignment saved.")
    except IwError as exc:
        messages.error(request, str(exc))
    return redirect("iw:outbound", pk=pk)


@needs("MATERIAL_LINK")
def vendor_page(request, pk):
    iw = get_object_or_404(Iw.objects.select_related("load_type"), pk=pk)
    error = None
    if request.method == "POST":
        try:
            if request.POST.get("mode") == "portal":
                upload = _checked_upload(request)
                path = _save_upload(upload)
                try:
                    cons = vendor.add_from_portal(request.user, iw.pk, path, upload.name,
                                                  request.POST.get("vendor_code", ""), request.POST.get("challan_no", ""))
                except IwError as exc:
                    report = vendor.read_portal(path)
                    found = sorted({f"{ln.vendor_code} / {ln.challan_no}" for ln in report.lines})
                    raise IwError(f"{exc} Challans in this file: {', '.join(found) or 'none for this depot'}.") from exc
                finally:
                    path.unlink(missing_ok=True)
            else:
                lines = [(request.POST.get(f"sku_{i}", ""), request.POST.get(f"desc_{i}", ""),
                          request.POST.get(f"uom_{i}", ""), request.POST.get(f"nop_{i}", ""),
                          request.POST.get(f"qty_{i}", "")) for i in range(VENDOR_ROWS)]
                cons = vendor.add_manual(request.user, iw.pk, vendor_code=request.POST.get("vendor_code", ""),
                                         vendor_name=request.POST.get("vendor_name", ""),
                                         po_no=request.POST.get("po_no", ""),
                                         challan_no=request.POST.get("challan_no", ""), lines=lines)
            messages.success(request, f"Challan {cons.challan_no} of {cons.vendor_id} added "
                                      f"({cons.lines.count()} line(s), {cons.get_source_display().lower()}).")
            return redirect("iw:detail", pk=iw.pk)
        except (IwError, ImportFailed) as exc:
            error = str(exc)
    return render(request, "iw/vendor.html", {"iw": iw, "error": error, "post": request.POST,
                                              "rows": range(VENDOR_ROWS), "vendors": Vendor.objects.filter(active=True)})


@require_POST
@needs("MATERIAL_LINK")
def vendor_remove(request, cons_pk):
    cons = get_object_or_404(VendorConsignment, pk=cons_pk)
    try:
        vendor.remove(request.user, cons.pk)
        messages.success(request, f"Challan {cons.challan_no} removed.")
    except IwError as exc:
        messages.error(request, str(exc))
    return redirect("iw:detail", pk=cons.iw_id)
