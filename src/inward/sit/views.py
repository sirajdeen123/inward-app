from __future__ import annotations

import tempfile
from pathlib import Path

from django.contrib import messages
from django.core.paginator import Paginator
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from inward.core.models import BusinessLineWarehouse
from inward.importers.common import ImportFailed
from inward.sit import services
from inward.sit.models import SitChangeEvent, SitImport, SitLine, SitShip
from inward.web.views import PreviewForm, needs

EVENT_LABELS = {
    "NEW_SHIP": "New Ship", "SKU_REDUCED": "Quantity reduced (partial processing)",
    "SKU_REMOVED": "SKU processed", "SHIP_REMOVED_PROCESSED": "Ship fully processed",
    "SHIP_REMOVED_NO_RECEIPT": "Removed - Stock Receipt No not entered",
    "SHIP_REMOVED_NEVER_LINKED": "Removed - never linked to an IW",
    "SHIP_OUT_OF_WINDOW": "Outside report window",
    "QTY_INCREASED": "Quantity increased", "SKU_ADDED": "SKU added to existing Ship",
}


class UploadForm(PreviewForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        del self.fields["kind"]


def _decorate(events):
    for e in events:
        e.label = EVENT_LABELS.get(e.event, e.event)
    return events


@needs("SEARCH_REPORTS")
def sit_home(request):
    latest = SitImport.objects.select_related("batch", "batch__uploaded_by").first()
    counts = dict(SitShip.objects.values_list("status").annotate(n=Count("ship_id")))
    events = []
    if latest:
        events = _decorate(list(latest.events.all()[:300]))
    return render(request, "sit/home.html", {
        "latest": latest, "events": events, "upload_form": UploadForm(),
        "status_counts": [(s.label, counts.get(s.value, 0), s.value) for s in SitShip.Status],
        "pre_go_live": SitShip.objects.filter(pre_go_live=True).count(),
    })


@require_POST
@needs("IMPORT_RECONCILE")
def sit_fetch(request):
    try:
        result = services.fetch_from_gmail(request.user)
    except ImportFailed as exc:
        messages.error(request, f"Import rejected: {exc}")
        return redirect("sit:home")
    except Exception as exc:  # noqa: BLE001 - mail server problems are shown to the user, not a 500 page
        messages.error(request, f"Could not read Gmail: {exc}")
        return redirect("sit:home")
    if result.found:
        messages.info(request, f"Latest SIT e-mail: '{result.subject}' received {result.received_at:%d-%b-%Y %H:%M}.")
    for imp in result.imported:
        messages.success(request, "Imported: " + services.describe(imp))
    for text in result.messages:
        messages.warning(request, text)
    return redirect("sit:home")


@require_POST
@needs("IMPORT_RECONCILE")
def sit_upload(request):
    form = UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        for errors in form.errors.values():
            for e in errors:
                messages.error(request, e)
        return redirect("sit:home")
    upload = form.cleaned_data["file"]
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(upload.name).suffix.lower()) as tmp:
        for chunk in upload.chunks():
            tmp.write(chunk)
    path = Path(tmp.name)
    try:
        imp = services.import_sit_file(path, source="MANUAL", user=request.user, original_name=upload.name)
        messages.success(request, f"Imported {upload.name}: " + services.describe(imp))
    except (services.SitImportRejected, ImportFailed) as exc:
        messages.warning(request, str(exc))
    finally:
        path.unlink(missing_ok=True)
    return redirect("sit:home")


def ship_totals(lines) -> dict:
    """SKU count, pieces, LTR and KG of a Ship; LTR and KG are never added together."""
    def blank():
        return {"skus": 0, "pcs": Decimal(0), "ltr": Decimal(0), "kg": Decimal(0)}
    total, by_wh, empty = blank(), {}, blank()
    for ln in lines:
        buckets = [empty] if ln.is_empty_packing else [total, by_wh.setdefault(ln.warehouse, blank())]
        for b in buckets:
            b["skus"] += 1
            b["pcs"] += ln.quantity
            if ln.measure_family == "LTR":
                b["ltr"] += ln.measure
            elif ln.measure_family == "KG":
                b["kg"] += ln.measure
    return {"total": total, "warehouses": sorted(by_wh.items()), "empty": empty}


@needs("SEARCH_REPORTS")
def ship_list(request):
    qs = SitShip.objects.all()
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    if q:
        qs = qs.filter(Q(ship_id__icontains=q) | Q(from_org_code__iexact=q) | Q(from_org_name__icontains=q))
    if status:
        qs = qs.filter(status=status)
    if request.GET.get("pre_go_live") == "1":
        qs = qs.filter(pre_go_live=True)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    rows = (SitLine.objects.filter(ship_id__in=[s.ship_id for s in page],
                                   sit_import_id__in={s.last_seen_id for s in page}, is_empty_packing=False)
            .values("ship_id", "sit_import_id")
            .annotate(skus=Count("id"), pcs=Sum("quantity"),
                      ltr=Sum("measure", filter=Q(measure_family="LTR")),
                      kg=Sum("measure", filter=Q(measure_family="KG"))))
    sizes = {(r["ship_id"], r["sit_import_id"]): r for r in rows}
    for s in page:
        s.size = sizes.get((s.ship_id, s.last_seen_id), {})
    return render(request, "sit/ships.html", {"page": page, "q": q, "status": status,
                                              "statuses": SitShip.Status.choices,
                                              "pre_go_live_filter": request.GET.get("pre_go_live") == "1"})


@require_POST
@needs("IMPORT_RECONCILE")
def mark_pre_go_live(request):
    ids = request.POST.getlist("ship_id")
    n = services.mark_pre_go_live(request.user, ids)
    messages.success(request, f"{n} Ship(s) marked Pre-go-live.")
    return redirect(request.POST.get("back") or "sit:ships")


@needs("SEARCH_REPORTS")
def ship_detail(request, ship_id):
    ship = get_object_or_404(SitShip, pk=ship_id)
    warehouse = dict(BusinessLineWarehouse.objects.values_list("business_line", "warehouse"))
    lines = list(ship.last_seen.lines.filter(ship_id=ship_id).order_by("business_line", "pack_size", "sku"))
    for ln in lines:
        ln.warehouse = warehouse.get(ln.business_line, "UNMAPPED") if not ln.is_empty_packing else "EMPTY PACKING"
    lines.sort(key=lambda ln: (ln.is_empty_packing, ln.warehouse, ln.pack_size or 0, ln.sku))
    still_in_sit = ship.status in (SitShip.Status.IN_SIT, SitShip.Status.PARTIAL)
    events = _decorate(list(SitChangeEvent.objects.filter(ship_id=ship_id).select_related("sit_import")
                            .order_by("-sit_import_id", "sku")))
    return render(request, "sit/ship_detail.html", {"ship": ship, "lines": lines, "events": events,
                                                    "still_in_sit": still_in_sit, "totals": ship_totals(lines)})


@needs("SEARCH_REPORTS")
def import_list(request):
    page = Paginator(SitImport.objects.select_related("batch", "batch__uploaded_by"), 30).get_page(request.GET.get("page"))
    return render(request, "sit/imports.html", {"page": page})


@needs("SEARCH_REPORTS")
def import_detail(request, pk):
    imp = get_object_or_404(SitImport.objects.select_related("batch"), pk=pk)
    return render(request, "sit/import_detail.html", {"imp": imp, "events": _decorate(list(imp.events.all())),
                                                      "issues": imp.issues.all()})
