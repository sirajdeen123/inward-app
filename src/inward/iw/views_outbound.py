"""Screen 03 part B views - outbound Despatch rows and vendor challans (thin; rules in outbound.py / vendor.py)."""
from __future__ import annotations

import tempfile
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
    dests = outbound.report_destinations(iw)
    return render(request, "iw/outbound.html", {
        "iw": iw, "state": outbound.state(iw), "destinations": dests,
        "default_destination": f"{dests[0][0]} : {dests[0][1]}" if len(dests) == 1 else "",
        "orgs": Organisation.objects.filter(active=True).order_by("code"),
        "reports": iw.despatch_reports.order_by("uploaded_at")})


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
def outbound_add_ship(request, pk):
    try:
        s = outbound.add_ship(request.user, pk, ship_id=request.POST.get("ship_id", ""),
                              destination=request.POST.get("destination", ""), doc_ltr=request.POST.get("doc_ltr", ""),
                              doc_kg=request.POST.get("doc_kg", ""), doc_nop=request.POST.get("doc_nop", ""))
        messages.success(request, f"Outbound Ship {s.ship_id} added - now tick its rows.")
        return redirect("iw:outbound_rows", ship_pk=s.pk)
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return redirect("iw:outbound", pk=pk)


@needs("MATERIAL_LINK")
def outbound_rows(request, ship_pk):
    ship = get_object_or_404(OutboundShip.objects.select_related("destination", "iw", "iw__load_type"), pk=ship_pk)
    rows = outbound.available_rows(ship)
    offered = outbound.Totals.of(rows)
    error = None
    if request.method == "POST":
        picked = {int(v) for v in request.POST.getlist("row")}
        acks = {int(v) for v in request.POST.getlist("ack")}
        try:
            outbound.save_selection(request.user, ship.pk, picked, acks)
            messages.success(request, f"Outbound Ship {ship.ship_id} saved - ticked rows tally with the document.")
            return redirect("iw:outbound", pk=ship.iw_id)
        except IwError as exc:
            error = str(exc)
            ticked = picked
    elif request.GET.get("suggest"):
        found = outbound.suggest(ship, rows)
        if found is None:
            messages.warning(request, "No exact combination found - tick the rows from the Ship document.")
            ticked = set()
        else:
            messages.info(request, "Suggested rows are ticked. Check them against the Ship document before saving.")
            ticked = set(found)
    else:
        ticked = {r.pk for r in rows if r.outbound_ship_id == ship.pk}
        if not ticked and outbound.matches(ship, offered):
            ticked = {r.pk for r in rows if not r.reused_on}  # report holds exactly this Ship
    for r in rows:
        r.ticked = r.pk in ticked
    return render(request, "iw/outbound_rows.html", {
        "ship": ship, "iw": ship.iw, "rows": rows, "offered": offered, "error": error,
        "exact": outbound.matches(ship, offered), "enough": outbound.covers(ship, offered),
        "ticked_totals": outbound.Totals.of([r for r in rows if r.ticked])})


@require_POST
@needs("MATERIAL_LINK")
def outbound_reopen(request, ship_pk):
    ship = get_object_or_404(OutboundShip, pk=ship_pk)
    try:
        outbound.reopen(request.user, ship.pk)
    except IwError as exc:
        messages.error(request, str(exc))
        return redirect("iw:outbound", pk=ship.iw_id)
    return redirect("iw:outbound_rows", ship_pk=ship.pk)


@require_POST
@needs("MATERIAL_LINK")
def outbound_remove_ship(request, ship_pk):
    ship = get_object_or_404(OutboundShip, pk=ship_pk)
    try:
        outbound.remove_ship(request.user, ship.pk)
        messages.success(request, f"Outbound Ship {ship.ship_id} removed; its rows are free again.")
    except IwError as exc:
        messages.error(request, str(exc))
    return redirect("iw:outbound", pk=ship.iw_id)


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
                                                  request.POST.get("vendor_code", ""), request.POST.get("challan_no", ""),
                                                  request.POST.get("warehouse", ""))
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
                                         challan_no=request.POST.get("challan_no", ""), lines=lines,
                                         warehouse=request.POST.get("warehouse", ""))
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
