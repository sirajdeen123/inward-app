"""Screen 09 views - reports on screen, SED Excel, report send history (thin; rules in reports.py)."""
from __future__ import annotations

from datetime import date, timedelta

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from inward.accounts.services import role_codes
from inward.iw import reports
from inward.iw.models import ReportRun
from inward.web.views import needs

KINDS = [("daily", "Daily escalation"), ("unloading", "Daily unloading collection"), ("sed", "SED report"),
         ("mis", "Monthly MIS")]


def _d(value, default: date) -> date:
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


def _params(request):
    today = timezone.localdate()
    kind = request.GET.get("r") or request.POST.get("r") or "daily"
    day = _d(request.GET.get("day") or request.POST.get("day"), today)
    d1 = _d(request.GET.get("from") or request.POST.get("from"), today.replace(day=1))
    d2 = _d(request.GET.get("to") or request.POST.get("to"), today)
    month = request.GET.get("month") or request.POST.get("month") or f"{(today.replace(day=1) - timedelta(days=1)):%Y-%m}"
    return kind, day, d1, d2, month


def _build(kind, day, d1, d2, month):
    if kind == "unloading":
        return "iw/reports/_unloading.html", reports.daily_unloading(day), "DAILY_UNL", day.isoformat()
    if kind == "sed":
        return "iw/reports/_sed.html", reports.sed_report(d1, d2), "SED_REPORT", f"{d1}..{d2}"
    if kind == "mis":
        y, m = (int(x) for x in month.split("-"))
        return "iw/reports/_mis.html", reports.monthly_mis(y, m), "MONTHLY_MIS", month
    return "iw/reports/_daily.html", reports.daily_escalation(day), "DAILY_ESC", day.isoformat()


@needs("SEARCH_REPORTS")
def report_page(request):
    kind, day, d1, d2, month = _params(request)
    if kind == "sed" and request.GET.get("xlsx"):
        resp = HttpResponse(reports.sed_xlsx(d1, d2),
                            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = f'attachment; filename="SED report {d1} to {d2}.xlsx"'
        return resp
    template, data, _, _ = _build(kind, day, d1, d2, month)
    return render(request, "iw/reports.html", {
        "kinds": KINDS, "kind": kind, "template": template, "r": data, "day": day, "d1": d1, "d2": d2,
        "month": month, "runs": ReportRun.objects.select_related("mail").order_by("-created_at")[:20],
        "can_send": bool({"CFA_MANAGER", "ADMINISTRATOR"} & set(role_codes(request.user)))})


@require_POST
@needs("SEARCH_REPORTS")
def report_send(request):
    if not {"CFA_MANAGER", "ADMINISTRATOR"} & set(role_codes(request.user)):
        messages.error(request, "Only the CFA Manager or Administrator can send a report now.")
        return redirect("iw:reports")
    kind, day, d1, d2, month = _params(request)
    _, data, code, period = _build(kind, day, d1, d2, month)
    run = reports.queue_report(code, f"{timezone.now():%m%d%H%M%S}", {"r": data}, request.user)  # manual run
    if run.skipped:
        messages.error(request, f"Not sent: {run.skipped}.")
    else:
        messages.success(request, "Report queued for e-mail to the Notification Master recipients.")
    return redirect(f"{reverse('iw:reports')}?r={kind}")
