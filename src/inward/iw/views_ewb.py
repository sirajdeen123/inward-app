"""Outbound transhipment - E-way bill Transporter ID update screen (6-Nov-2026; rules in ewb.py)."""
from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from inward.accounts.services import PermissionDenied, modes_for
from inward.iw import ewb
from inward.iw.models import Iw, MailMessage, OutboundShip
from inward.iw.services import IwError
from inward.web.views import needs


@needs("EWB_MAIL")
def ewb_page(request, pk):
    iw = get_object_or_404(Iw.objects.select_related("load_type", "source_org"), pk=pk)
    st = ewb.state(iw)
    p = ewb.preview(iw, reminder=bool(st.covered)) if st.needed and not st.missing_ewb else None
    release_modes = modes_for(request.user, "EWB_MRA_RELEASE")
    mails = MailMessage.objects.filter(iw=iw, purpose=MailMessage.Purpose.EWB_TRANSPORTER).order_by("-id")
    return render(request, "iw/ewb.html", {
        "iw": iw, "st": st, "p": p, "events": iw.ewb_events.select_related("by", "mail"), "mails": mails,
        "open": iw.status not in ewb.CLOSED, "reasons": ewb.RELEASE_REASONS,
        "can_release": bool(release_modes & {"REQUEST", "APPROVE"}) and not st.mra_ok and not st.pending_release})


@require_POST
@needs("EWB_MAIL")
def ewb_set(request, ship_pk):
    ship = get_object_or_404(OutboundShip, pk=ship_pk)
    try:
        ewb.set_ewb(request.user, ship.pk, request.POST.get("ewb_no", ""))
        messages.success(request, f"E-way bill No of Ship {ship.ship_id} saved.")
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return redirect(request.POST.get("back") or request.META.get("HTTP_REFERER") or "iw:ewb", pk=ship.iw_id) \
        if not (request.POST.get("back") or request.META.get("HTTP_REFERER")) else redirect(
        request.POST.get("back") or request.META["HTTP_REFERER"])


@require_POST
@needs("EWB_MAIL")
def ewb_send(request, pk):
    reminder = bool(request.POST.get("reminder"))
    try:
        m = ewb.send(request.user, pk, reminder=reminder)
        messages.success(request, ("Reminder" if reminder else "Transporter ID update request") +
                         f" queued to {', '.join(m.to)} ({m.stamp}).")
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return redirect("iw:ewb", pk=pk)


@require_POST
@needs("EWB_MAIL")
def ewb_confirm(request, pk):
    try:
        ewb.confirm(request.user, pk, request.POST.get("note", ""))
        messages.success(request, "Recorded: the source updated the Transporter ID in the E-way bill(s).")
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return redirect("iw:ewb", pk=pk)


@require_POST
@needs("EWB_MRA_RELEASE", modes=("REQUEST", "APPROVE"))
def ewb_release(request, pk):
    iw = get_object_or_404(Iw.objects.select_related("load_type"), pk=pk)
    try:
        req = ewb.request_release(request.user, iw, request.POST.get("reason_kind", ""), request.POST.get("remark", ""))
        if req.status == req.Status.APPROVED:
            messages.success(request, "MRA released without the E-way bill mail - the MRA can be issued. The mail and the "
                                      "source's update are still needed before closure.")
        else:
            messages.success(request, "MRA release requested - the CFA Manager must approve it (Approvals).")
    except (IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return redirect("iw:ewb", pk=pk)
