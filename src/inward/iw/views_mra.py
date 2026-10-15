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
        "pdf_available": pdf.available()})


@require_POST
@needs("MRA_ISSUE")
def mra_issue(request, pk):
    try:
        rev, created = mra.issue(request.user, pk, request.POST.get("reason", ""))
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
