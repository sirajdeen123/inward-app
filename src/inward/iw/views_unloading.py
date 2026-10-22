"""Screen 04 part B views - package counts, unloading charge, transporter ledger (thin; rules in unloading.py)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from inward.accounts.models import ApprovalRequest
from inward.accounts.services import PermissionDenied
from inward.core.models import Transporter
from inward.iw import unloading
from inward.iw.models import Iw, UnloadingRecord
from inward.iw.sed import rate_on
from inward.iw.services import IwError
from inward.web.views import needs


@needs("UNLOADING_RECORD")
def charges_page(request, pk):
    iw = get_object_or_404(Iw.objects.select_related("load_type", "transporter"), pk=pk)
    error = None
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "weights":
                unloading.save_weights(request.user, iw.pk, {k[3:]: val for k, val in request.POST.items()
                                                             if k.startswith("kg_")})
                messages.success(request, "Gross weights saved - the tonnage below is updated.")
            elif action in ("counts", "reload"):
                values = {k[3:]: v for k, v in request.POST.items() if k.startswith("pc_")}
                unloading.save_counts(request.user, iw.pk, values, reload=action == "reload")
                messages.success(request, "Package counts saved." if action == "counts"
                                 else "Package counts reloaded from the checklist.")
            else:
                rec = unloading.record(request.user, iw.pk, unloading.UnloadingInput(
                    tonnage=request.POST.get("tonnage", ""), status=request.POST.get("status", ""),
                    paid_amount=request.POST.get("paid_amount", ""), payment_mode=request.POST.get("payment_mode", ""),
                    payment_ref=request.POST.get("payment_ref", ""), exempt_reason=request.POST.get("exempt_reason", "")),
                    version=request.POST.get("version") or None)
                messages.success(request, f"Unloading recorded: Rs {rec.charge} - {rec.get_status_display()}.")
                pending = rec.iw.cash_entries.filter(state="PENDING").first()
                if pending:
                    messages.warning(request, f"Hand over Rs {pending.amount} cash to the cashier - it waits in the "
                                              "cash register until the cashier marks it received.")
            return redirect("iw:charges", pk=iw.pk)
        except (IwError, PermissionDenied) as exc:
            error = str(exc)
    rec = getattr(iw, "unloading", None)
    rows = unloading.counts(iw)
    by_wh = [(unloading.WAREHOUSE_LABEL[wh], [r for r in rows if r.warehouse == wh]) for wh in unloading.WAREHOUSE_ORDER]
    arrival = timezone.localtime(iw.arrival_at).date()
    suggested, total_kg = unloading.suggested_tonnage(iw)
    rate = rate_on("UNLOADING_PER_TON", arrival)
    estimate = (suggested * rate).quantize(Decimal("0.01")) if suggested and rate else None
    from inward.iw.coload import charge_iw, members
    lead = charge_iw(iw)
    return render(request, "iw/charges.html", {
        "iw": iw, "rec": rec, "error": error, "post": request.POST if error else None,
        "charged_with": lead if lead.pk != iw.pk else None, "truck": members(iw),
        "lead_rec": getattr(lead, "unloading", None) if lead.pk != iw.pk else None,
        "warehouses": [(label, r) for label, r in by_wh if r], "counts_saved": iw.package_counts.exists(),
        "snap": iw.checklists.first(), "rate": rate, "estimate": estimate, "arrival": arrival,
        "statuses": [(s.value, s.label) for s in UnloadingRecord.Status if s != UnloadingRecord.Status.EXEMPT_REQUESTED],
        "modes": UnloadingRecord.Mode.choices, "settled": unloading.is_settled(iw),
        "weights": unloading.weights(iw), "suggested": suggested, "total_kg": total_kg,
        "exempt_rejected": bool(rec and rec.exempt_request and rec.status == UnloadingRecord.Status.EXEMPT_REQUESTED
                                and rec.exempt_request.status == ApprovalRequest.Status.REJECTED)})


@needs("UNLOADING_RECORD")
def ledger_list(request):
    return render(request, "iw/ledger_list.html", {"views": unloading.balances()})


@needs("UNLOADING_RECORD")
def ledger_detail(request, transporter_pk):
    t = get_object_or_404(Transporter, pk=transporter_pk)
    error = None
    if request.method == "POST":
        try:
            day = request.POST.get("entry_date") or ""
            e = unloading.add_receipt(request.user, t.pk, amount=request.POST.get("amount", ""),
                                      entry_date=date.fromisoformat(day) if day else None,
                                      payment_mode=request.POST.get("payment_mode", ""),
                                      reference=request.POST.get("reference", ""), remarks=request.POST.get("remarks", ""))
            messages.success(request, f"Receipt of Rs {e.amount} recorded for {t.name}.")
            return redirect("iw:ledger_detail", transporter_pk=t.pk)
        except (IwError, PermissionDenied, ValueError) as exc:
            error = str(exc)
    v = unloading.ledger(t)
    open_ids = {e.pk: owed for e, owed in v.open_iws}
    running, rows = 0, []
    for e in v.entries:
        running += e.amount if e.kind == "CHARGE" else -e.amount
        rows.append((e, running, open_ids.get(e.pk)))
    return render(request, "iw/ledger_detail.html", {"v": v, "rows": rows, "error": error, "post": request.POST,
                                                     "modes": UnloadingRecord.Mode.choices,
                                                     "today": timezone.localdate()})
