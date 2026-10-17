"""Screen 03 views - thin: collect input, call inward.iw.linking, show the result."""
from __future__ import annotations

from decimal import Decimal

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from inward.accounts.services import PermissionDenied
from inward.iw import linking
from inward.iw.models import ColourBankItem, Iw, NonStockRef, ShipLink
from inward.iw.services import IwError
from inward.web.views import needs

NON_STOCK_ROWS = 6


def link_totals(lines) -> dict:
    t = {"skus": 0, "pcs": Decimal(0), "ltr": Decimal(0), "kg": Decimal(0), "empty": 0}
    for ln in lines:
        if ln.is_empty_packing:
            t["empty"] += 1
            continue
        t["skus"] += 1
        t["pcs"] += ln.quantity
        if ln.measure_family == "LTR":
            t["ltr"] += ln.measure
        elif ln.measure_family == "KG":
            t["kg"] += ln.measure
    return t


def _bool(value: str):
    return {"yes": True, "no": False}.get(value)


@needs("MATERIAL_LINK")
def link_ship(request, pk):
    iw = get_object_or_404(Iw.objects.select_related("load_type", "source_org"), pk=pk)
    ship_id = (request.POST.get("ship_id") or request.GET.get("ship") or "").strip()
    cand, error = None, None
    if ship_id:
        try:
            cand = linking.ship_candidate(iw, ship_id)
        except IwError as exc:
            error = str(exc)
    if request.method == "POST" and request.POST.get("confirm") and cand:
        split = request.POST.get("split") == "1"
        allocations = {k[6:]: v for k, v in request.POST.items() if k.startswith("alloc_")}
        try:
            link = linking.link_ship(request.user, iw.pk, ship_id, split=split,
                                     split_reason=request.POST.get("split_reason", ""), allocations=allocations,
                                     lscrr_no=request.POST.get("lscrr_no", ""),
                                     lscrr_applied=_bool(request.POST.get("lscrr_applied", "")),
                                     gross_kg=request.POST.get("gross_kg", ""))
            messages.success(request, f"Ship {link.ship_id} linked to {iw}"
                                      f"{' as a split vehicle' if link.is_split else ''}.")
            for w in cand.warnings:
                messages.warning(request, w)
            return redirect("iw:detail", pk=iw.pk)
        except (IwError, PermissionDenied) as exc:
            error = str(exc)
    return render(request, "iw/link_ship.html", {"iw": iw, "ship_id": ship_id, "cand": cand, "error": error,
                                                 "split_requested": request.POST.get("split") == "1",
                                                 "post": request.POST})


@require_POST
@needs("SED_TRX_LSCRR")
def lscrr(request, link_pk):
    link = get_object_or_404(ShipLink, pk=link_pk)
    try:
        linking.update_lscrr(request.user, link.pk, request.POST.get("lscrr_no", ""),
                             _bool(request.POST.get("lscrr_applied", "")))
        messages.success(request, f"LSCRR details saved for Ship {link.ship_id}.")
    except IwError as exc:
        messages.error(request, str(exc))
    return redirect("iw:detail", pk=link.iw_id)


@require_POST
@needs("MATERIAL_LINK")
def unlink_ship(request, link_pk):
    link = get_object_or_404(ShipLink, pk=link_pk)
    try:
        linking.unlink_ship(request.user, link.pk)
        messages.success(request, f"Ship {link.ship_id} removed from the IW.")
    except IwError as exc:
        messages.error(request, str(exc))
    return redirect("iw:detail", pk=link.iw_id)


@require_POST
@needs("MATERIAL_LINK")
def add_colour_bank(request, pk):
    try:
        qty = int(request.POST.get("quantity") or 1)
        linking.add_colour_bank(request.user, pk, direction=request.POST.get("direction", "IN"),
                                challan_no=request.POST.get("challan_no", ""),
                                description=request.POST.get("description", ""),
                                serial_no=request.POST.get("serial_no", ""), asset_no=request.POST.get("asset_no", ""),
                                quantity=qty)
        messages.success(request, "Colour Bank machine added.")
    except (IwError, ValueError) as exc:
        messages.error(request, str(exc))
    return redirect("iw:detail", pk=pk)


@require_POST
@needs("MATERIAL_LINK")
def remove_colour_bank(request, item_pk):
    item = get_object_or_404(ColourBankItem, pk=item_pk)
    try:
        linking.remove_colour_bank(request.user, item.pk)
        messages.success(request, f"Colour Bank challan {item.challan_no} removed.")
    except IwError as exc:
        messages.error(request, str(exc))
    return redirect("iw:detail", pk=item.iw_id)


@needs("MATERIAL_LINK")
def add_non_stock(request, pk):
    iw = get_object_or_404(Iw, pk=pk)
    error = None
    if request.method == "POST":
        lines = [(request.POST.get(f"desc_{i}", ""), request.POST.get(f"unit_{i}", ""), request.POST.get(f"qty_{i}", ""))
                 for i in range(NON_STOCK_ROWS)]
        try:
            linking.add_non_stock(request.user, iw.pk, sender_name=request.POST.get("sender_name", ""),
                                  sender_email=request.POST.get("sender_email", ""),
                                  document_no=request.POST.get("document_no", ""), lines=lines)
            messages.success(request, "Non-Stock items added.")
            return redirect("iw:detail", pk=iw.pk)
        except IwError as exc:
            error = str(exc)
    from inward.core.models import NonStockItem, Sender
    return render(request, "iw/non_stock.html", {"iw": iw, "error": error, "rows": range(NON_STOCK_ROWS),
                                                 "post": request.POST, "senders": Sender.objects.filter(active=True),
                                                 "items": NonStockItem.objects.filter(active=True)})


@require_POST
@needs("MATERIAL_LINK")
def remove_non_stock(request, ref_pk):
    ref = get_object_or_404(NonStockRef, pk=ref_pk)
    try:
        linking.remove_non_stock(request.user, ref.pk)
        messages.success(request, f"Non-Stock document {ref.document_no} removed.")
    except IwError as exc:
        messages.error(request, str(exc))
    return redirect("iw:detail", pk=ref.iw_id)
