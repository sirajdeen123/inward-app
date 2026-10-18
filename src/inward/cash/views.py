"""Cash register screens - the cashier sees only these (thin; rules in services.py)."""
from __future__ import annotations

from datetime import date

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from inward.accounts.services import PermissionDenied
from inward.cash import services as cash
from inward.cash.models import CashEntry
from inward.web.views import needs


def _month(request) -> tuple[int, int]:
    text = request.GET.get("month", "")
    try:
        y, m = (int(x) for x in text.split("-"))
        date(y, m, 1)
        return y, m
    except ValueError:
        today = timezone.localdate()
        return today.year, today.month


@needs("CASH_REGISTER")
def register(request):
    y, m = _month(request)
    st = cash.statement(y, m)
    return render(request, "cash/register.html", {
        "pos": cash.position(), "st": st, "month": f"{y:04d}-{m:02d}", "categories": cash.CATEGORIES,
        "manager": cash.is_manager(request.user), "today": timezone.localdate(),
        "has_opening": CashEntry.objects.filter(kind=CashEntry.Kind.OPENING).exclude(state="CANCELLED").exists()})


def _do(request, fn, ok: str):
    try:
        fn()
        messages.success(request, ok)
    except (cash.CashError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return redirect("cash:register")


@require_POST
@needs("CASH_REGISTER")
def receive(request, pk):
    e = get_object_or_404(CashEntry, pk=pk)
    return _do(request, lambda: cash.receive(request.user, pk), f"Rs {e.amount} received from {e.created_by}.")


@require_POST
@needs("CASH_REGISTER")
def expense(request):
    f = request.FILES.get("bill")
    return _do(request, lambda: cash.add_expense(
        request.user, entry_date=request.POST.get("entry_date", ""), category=request.POST.get("category", ""),
        amount=request.POST.get("amount", ""), paid_to=request.POST.get("paid_to", ""),
        description=request.POST.get("description", ""),
        bill=(f.read(), f.name, f.content_type) if f else None), "Expense entered.")


@require_POST
@needs("CASH_REGISTER")
def handover(request):
    return _do(request, lambda: cash.record_handover(request.user, amount=request.POST.get("amount", ""),
                                                      note=request.POST.get("note", "")),
               "Handover recorded - waiting for the other side to confirm.")


@require_POST
@needs("CASH_REGISTER")
def confirm(request, pk):
    return _do(request, lambda: cash.confirm_handover(request.user, pk), "Handover confirmed.")


@require_POST
@needs("CASH_MANAGE")
def opening(request):
    return _do(request, lambda: cash.set_opening(request.user, request.POST.get("amount", ""),
                                                 request.POST.get("entry_date", "")), "Opening balance set.")


@require_POST
@needs("CASH_REGISTER")
def cancel(request, pk):
    return _do(request, lambda: cash.cancel(request.user, pk, request.POST.get("reason", "")), "Entry cancelled.")


@needs("CASH_REGISTER")
def bill(request, pk):
    from inward.core import documents
    e = get_object_or_404(CashEntry.objects.select_related("bill"), pk=pk, bill__isnull=False)
    resp = HttpResponse(documents.read(e.bill), content_type=e.bill.content_type)
    resp["Content-Disposition"] = f'inline; filename="{e.bill.original_filename}"'
    return resp


@needs("CASH_REGISTER")
def statement(request):
    from inward.core import branding, pdf
    y, m = _month(request)
    st = cash.statement(y, m)
    if request.GET.get("xlsx"):
        resp = HttpResponse(cash.statement_xlsx(st),
                            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = f'attachment; filename="Cash statement {y:04d}-{m:02d}.xlsx"'
        return resp
    from inward.web.context import depot_info
    ctx = {"st": st, "logo": branding.logo_data_uri(), "generated": timezone.localtime(timezone.now()), "depot": depot_info(),
           "by": request.user.username, "month": f"{y:04d}-{m:02d}", "pdf_available": pdf.available()}
    if request.GET.get("pdf") and pdf.available():
        data = pdf.html_to_pdf(pdf.render_html("cash/statement.html", ctx | {"for_pdf": True}), "inward/recon.css")
        resp = HttpResponse(data, content_type="application/pdf")
        resp["Content-Disposition"] = f'inline; filename="Cash statement {y:04d}-{m:02d}.pdf"'
        return resp
    return render(request, "cash/statement.html", ctx)
