"""Unloading checklist screens (thin; rules in inward.iw.checklist)."""
from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from inward.accounts.services import PermissionDenied, modes_for
from inward.iw import checklist
from inward.iw.models import Iw
from inward.iw.services import IwError
from inward.web.context import depot_info
from inward.web.views import needs


@needs("SEARCH_REPORTS")
def checklist_page(request, pk):
    iw = get_object_or_404(Iw.objects.select_related("load_type", "source_org", "transporter"), pk=pk)
    b = checklist.build(iw)
    current = iw.checklists.first()
    back = reverse("iw:checklist", args=[iw.pk])
    for ln in b.missing_packaging:
        ln.add_url = reverse("core:packaging_sku", args=[ln.sku]) + "?" + urlencode(
            {"next": back, "desc": ln.description, "size": ln.pack_size or "", "uom": ln.uom})
    return render(request, "iw/checklist.html", {
        "iw": iw, "build": b, "current": current, "older": iw.checklists.all()[1:6],
        "stale": checklist.is_stale(iw, current),
        "can_generate": iw.is_editable and iw.status != Iw.Status.DRAFT
                        and "GRANT" in modes_for(request.user, "PHYSICAL_SED_QTY")})


@require_POST
@needs("PHYSICAL_SED_QTY")
def checklist_generate(request, pk):
    try:
        snap = checklist.generate(request.user, pk)
        messages.success(request, f"Checklist revision {snap.revision} generated.")
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return redirect("iw:checklist", pk=pk)


@needs("SEARCH_REPORTS")
def checklist_print(request, pk, revision):
    iw = get_object_or_404(Iw.objects.select_related("load_type", "source_org", "transporter"), pk=pk)
    snap = get_object_or_404(iw.checklists, revision=revision)
    return render(request, "iw/checklist_print.html", {"iw": iw, "snap": snap, "depot": depot_info()})
