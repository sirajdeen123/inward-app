"""Screens 06 / 07 views - Operations Control Centre, My Pending Actions, Universal Search (thin)."""
from __future__ import annotations

from datetime import date

from django.shortcuts import render

from inward.core.models import LoadType
from inward.iw import ops, search
from inward.iw.models import Iw
from inward.web.views import needs


@needs("SEARCH_REPORTS")
def control_centre(request):
    return render(request, "iw/ops.html", ops.dashboard())


@needs("SEARCH_REPORTS")
def pending_actions(request):
    return render(request, "iw/actions.html", {"rows": ops.my_actions(request.user)})


def _date(value: str):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


@needs("SEARCH_REPORTS")
def universal_search(request):
    q = request.GET.get("q", "")
    f = {"date_from": _date(request.GET.get("from", "")), "date_to": _date(request.GET.get("to", "")),
         "status": request.GET.get("status", ""), "load_type": request.GET.get("load_type", "")}
    kind, hits = search.search(q, **f) if q.strip() else ("", [])
    return render(request, "iw/search.html", {
        "q": q, "kind": kind, "hits": hits, "irr": search.irr_lookup(q) if q.strip() else [], "f": request.GET,
        "statuses": Iw.Status.choices, "load_types": LoadType.objects.filter(active=True)})


@needs("SEARCH_REPORTS")
def dossier(request, pk):
    from django.http import FileResponse
    from django.shortcuts import get_object_or_404

    from inward.core import audit
    from inward.iw.dossier import build_zip
    iw = get_object_or_404(Iw.objects.select_related("load_type"), pk=pk)
    audit.record("DOSSIER_DOWNLOADED", "iw", iw.pk, user=request.user, iw_id=iw.pk)
    return FileResponse(build_zip(iw), as_attachment=True, filename=f"Dossier {iw.iw_no or iw.pk}.zip",
                        content_type="application/zip")
