"""IRR reconciliation engine screens (separate from the inward flow)."""
from __future__ import annotations

import io
from datetime import date

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from inward.accounts.services import modes_for
from inward.core import audit
from inward.domain.normalize import clean_text
from inward.irr.models import IrrTransaction
from inward.irrecon import engine
from inward.irrecon.models import IrrMark, IrrMatch, IrrReconRun
from inward.web.views import needs

from inward.irrecon.summary import FAMILIES, STATUSES, by_reason, summary


def _d(text):
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def _rows(request):
    f = request.GET
    qs = IrrMatch.objects.select_related("row")
    status = f.get("status", "OPEN")
    if status:
        qs = qs.filter(status=status)
    if f.get("family"):
        qs = qs.filter(family=f["family"])
    if _d(f.get("from")):
        qs = qs.filter(row__trx_date__gte=_d(f["from"]))
    if _d(f.get("to")):
        qs = qs.filter(row__trx_date__lte=_d(f["to"]))
    q = clean_text(f.get("q", "")).upper()
    if q:
        qs = qs.filter(Q(row__trx_number__icontains=q) | Q(row__sku__icontains=q) | Q(group__icontains=q)
                       | Q(row__item_description__icontains=q) | Q(link__icontains=q)
                       | Q(row__reason_description__icontains=q) | Q(row__type__icontains=q))
    return qs.order_by("-row__trx_date", "group", "row__trx_number"), status


@needs("SEARCH_REPORTS")
def home(request):
    qs, status = _rows(request)
    if request.GET.get("xlsx"):
        return _xlsx(qs)
    sm = summary(_d(request.GET.get("from")), _d(request.GET.get("to")))
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    return render(request, "irrecon/home.html", {
        "sm": sm, "reasons": by_reason(_d(request.GET.get("from")), _d(request.GET.get("to"))), "table": sm["table"], "total": sm["total"], "open": sm["open"], "pct": sm["pct"], "presets": _presets(),
        "run": IrrReconRun.objects.select_related("run_by").first(), "page": page, "status": status,
        "families": FAMILIES, "statuses": STATUSES, "f": request.GET,
        "irr_rows": IrrTransaction.objects.count(),
        "can_mark": "GRANT" in modes_for(request.user, "IRR_RECON_MARK")})


@require_POST
@needs("IRR_RECON_MARK")
def run_now(request):
    r = engine.run(request.user)
    open_n = sum(v.get("OPEN", 0) for v in r.summary.values())
    messages.success(request, f"Engine run on {r.rows} IRR rows: {open_n} open exception(s).")
    return redirect("irrecon:home")


@require_POST
@needs("IRR_RECON_MARK")
def mark(request):
    reason = clean_text(request.POST.get("reason", ""))
    ids = [int(x) for x in request.POST.getlist("row") if x.isdigit()]
    if not reason or not ids:
        messages.error(request, "Tick the row(s) and write the reason.")
        return redirect(request.POST.get("back") or "irrecon:home")
    n = 0
    for m in IrrMatch.objects.filter(row_id__in=ids, status=IrrMatch.Status.OPEN).select_related("row"):
        IrrMark.objects.update_or_create(row_id=m.row_id, defaults={"reason": reason[:300], "marked_by": request.user})
        note = f"Marked by {request.user.username} {timezone.localtime():%d-%b-%Y}: {reason}"[:300]
        IrrMatch.objects.filter(pk=m.pk).update(status=IrrMatch.Status.EXPLAINED, note=note)
        audit.record("IRR_EXCEPTION_MARKED", "irr_transaction", m.row_id, user=request.user,
                     after={"trx": m.row.trx_number, "sku": m.row.sku, "family": m.family, "reason": reason})
        n += 1
    messages.success(request, f"{n} row(s) marked as explained.")
    return redirect(request.POST.get("back") or "irrecon:home")


@require_POST
@needs("IRR_RECON_MARK")
def unmark(request, row_id):
    m = IrrMark.objects.filter(row_id=row_id).first()
    if m:
        audit.record("IRR_EXCEPTION_UNMARKED", "irr_transaction", row_id, user=request.user, before={"reason": m.reason})
        m.delete()
        engine.run(request.user)
        messages.success(request, "Mark removed - the row is back in the engine's result.")
    return redirect(request.POST.get("back") or "irrecon:home")


def _presets() -> list[tuple[str, str, str]]:
    from datetime import timedelta
    t = timezone.localdate()
    y = t - timedelta(days=1)
    m1 = t.replace(day=1)
    lm_end = m1 - timedelta(days=1)
    return [("Yesterday", y.isoformat(), y.isoformat()), ("Today", t.isoformat(), t.isoformat()),
            ("Last 7 days", (t - timedelta(days=6)).isoformat(), t.isoformat()),
            ("This month", m1.isoformat(), t.isoformat()),
            ("Last month", lm_end.replace(day=1).isoformat(), lm_end.isoformat()), ("All", "", "")]


def _xlsx(qs) -> HttpResponse:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "IRR reconciliation"
    ws.append(["Date", "Type", "TRX Number", "Issue / Receipt", "SKU", "Description", "Pcs", "LTR", "KG", "From", "To",
               "Lot", "Reason", "Family", "Result", "Group", "Note", "Link"])
    for m in qs[:50000]:
        r = m.row
        ws.append([r.trx_date.strftime("%d-%m-%Y"), r.type, r.trx_number, r.transaction_type, r.sku, r.item_description,
                   float(r.no_of_pack), float(r.volume_ltr or 0), float(r.weight_kg or 0), r.source_subinv,
                   r.dest_subinv, r.lot_number, r.reason_description, m.family, m.get_status_display(), m.group,
                   m.note, m.link])
    out = io.BytesIO()
    wb.save(out)
    resp = HttpResponse(out.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="IRR reconciliation {timezone.localdate():%Y-%m-%d}.xlsx"'
    return resp
