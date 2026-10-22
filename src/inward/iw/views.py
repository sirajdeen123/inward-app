from __future__ import annotations

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from inward.accounts.models import ApprovalRequest
from inward.accounts.services import PermissionDenied, modes_for
from inward.core.models import AuditEvent, Organisation, Transporter
from inward.core.versioning import StaleEdit
from inward.domain.normalize import search_key
from inward.iw import services
from inward.iw.forms import IwForm, VoidForm
from inward.iw.linking import LIVE
from inward.iw.outbound import state as outbound_state
from inward.iw.erp import ERP_STAGES
from inward.iw.models import Iw, ShipLink
from inward.sit.models import SitShip
from inward.web.views import needs


def _data(form: IwForm) -> services.IwData:
    c = form.cleaned_data
    return services.IwData(arrival_at=c["arrival_at"], load_type=c["load_type"], source_org=c["source_org"],
                           vehicle_no=c["vehicle_no"], driver_name=c["driver_name"], driver_mobile=c["driver_mobile"],
                           transporter_name=c["transporter"], lr_no=c["lr_no"], remarks=c["remarks"],
                           gate_register_no=c["gate_register_no"], second_trip=c.get("second_trip", False),
                           coload=c.get("coload", False),
                           source_email=c.get("source_email", ""))


def _lists() -> dict:
    from django.contrib.postgres.aggregates import StringAgg
    from django.db.models import Q as _Q
    orgs = (Organisation.objects.filter(active=True).order_by("code")
            .annotate(mra_to=StringAgg("emails__email", ", ", filter=_Q(emails__kind="TO"), default="")))
    return {"orgs": orgs,
            "transporters": Transporter.objects.filter(active=True).order_by("-verified", "name")}


def _warn_duplicate_vehicle(request, vehicle_no: str, exclude_pk=None):
    others = services.open_iws_for_vehicle(vehicle_no, exclude_pk)
    if others.exists():
        messages.warning(request, "Note: this vehicle already has open IW(s): "
                                  + ", ".join(str(i) for i in others[:5]) + ". Check it is not a duplicate entry.")


@needs("SEARCH_REPORTS")
def iw_list(request):
    qs = Iw.objects.select_related("load_type", "source_org", "transporter")
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    if q:
        key = search_key(q)
        qs = qs.filter(Q(iw_no__icontains=q) | Q(vehicle_key__icontains=key) | Q(lr_key__icontains=key))
    if status:
        qs = qs.filter(status=status)
    page = Paginator(qs, 40).get_page(request.GET.get("page"))
    return render(request, "iw/list.html", {"page": page, "q": q, "status": status, "statuses": Iw.Status.choices,
                                            "can_edit": "GRANT" in modes_for(request.user, "IW_EDIT")})


@needs("IW_EDIT")
def iw_new(request):
    form = IwForm(request.POST or None, initial=IwForm.initial_for())
    if request.method == "POST" and form.is_valid():
        try:
            if request.POST.get("action") == "draft":
                iw = services.save_draft(request.user, _data(form))
                messages.success(request, "Draft saved. No IW number is issued until you create the IW.")
            else:
                iw = services.create(request.user, _data(form))
                messages.success(request, f"{iw.iw_no} created.")
                _warn_duplicate_vehicle(request, iw.vehicle_no, iw.pk)
            return redirect("iw:detail", pk=iw.pk)
        except services.IwError as exc:
            form.add_error(None, str(exc))
    return render(request, "iw/form.html", {"form": form, "iw": None, **_lists()})


@needs("IW_EDIT")
def iw_edit(request, pk):
    iw = get_object_or_404(Iw, pk=pk)
    if not iw.is_editable:
        messages.error(request, "This IW can no longer be edited.")
        return redirect("iw:detail", pk=pk)
    form = IwForm(request.POST or None, initial=IwForm.initial_for(iw))
    if request.method == "POST" and form.is_valid():
        try:
            action = request.POST.get("action")
            if iw.status == Iw.Status.DRAFT and action == "draft":
                services.save_draft(request.user, _data(form), iw)
                messages.success(request, "Draft saved.")
            elif iw.status == Iw.Status.DRAFT:
                iw = services.create(request.user, _data(form), draft=iw)
                messages.success(request, f"{iw.iw_no} created.")
                _warn_duplicate_vehicle(request, iw.vehicle_no, iw.pk)
            else:
                iw = services.update(request.user, iw.pk, form.cleaned_data["version"] or 0, _data(form))
                messages.success(request, f"{iw.iw_no} updated.")
            return redirect("iw:detail", pk=iw.pk)
        except (services.IwError, StaleEdit) as exc:
            form.add_error(None, str(exc))
    return render(request, "iw/form.html", {"form": form, "iw": iw, **_lists()})


@needs("SEARCH_REPORTS")
def iw_detail(request, pk):
    iw = get_object_or_404(Iw.objects.select_related("load_type", "source_org", "transporter", "created_by"), pk=pk)
    pending_void = ApprovalRequest.objects.filter(permission_id="IW_VOID", entity_type="iw", entity_id=str(iw.pk),
                                                  status=ApprovalRequest.Status.PENDING).first()
    history = AuditEvent.objects.filter(iw_id=iw.pk).select_related("user").order_by("-occurred_at")[:50]
    from inward.iw.views_link import link_totals
    links = list(iw.ship_links.select_related("ship").prefetch_related("lines"))
    all_lines = []
    for lk in links:
        lk.sorted_lines = sorted(lk.lines.all(), key=lambda ln: (ln.is_empty_packing, ln.business_line,
                                                                  ln.pack_size or 0, ln.sku))
        lk.totals = link_totals(lk.sorted_lines)
        all_lines += lk.sorted_lines
    linked_ids = set(ShipLink.objects.filter(iw__status__in=LIVE).values_list("ship_id", flat=True))
    linkable = (SitShip.objects.filter(status__in=[SitShip.Status.IN_SIT, SitShip.Status.PARTIAL], pre_go_live=False,
                                       has_issues=False).exclude(ship_id__in=linked_ids).order_by("ship_id"))
    void_modes = modes_for(request.user, "IW_VOID")
    from inward.iw.coload import members
    return render(request, "iw/detail.html", {
        "truck": members(iw) if iw.status != Iw.Status.VOID else [],
        "iw": iw, "pending_void": pending_void, "history": history, "void_form": VoidForm(),
        "erp_stages": [s.value for s in ERP_STAGES],
        "ship_links": links, "all_totals": link_totals(all_lines), "linkable_ships": linkable,
        "colour_bank": iw.colour_bank_items.all(), "non_stock": iw.non_stock_refs.select_related("sender"),
        "outbound_state": outbound_state(iw) if iw.load_type.has_outbound else None,
        "vendor_consignments": iw.vendor_consignments.select_related("vendor").prefetch_related("lines"),
        "can_link": iw.is_editable and iw.status != Iw.Status.DRAFT and "GRANT" in modes_for(request.user, "MATERIAL_LINK"),
        "can_lscrr": "GRANT" in modes_for(request.user, "SED_TRX_LSCRR"),
        "can_edit": iw.is_editable and "GRANT" in modes_for(request.user, "IW_EDIT"),
        "can_request_void": iw.is_editable and iw.status != Iw.Status.DRAFT and not pending_void
                            and bool(void_modes & {"REQUEST", "APPROVE"}),
    })


@require_POST
@needs("IW_VOID", modes=("REQUEST", "APPROVE"))
def iw_void(request, pk):
    iw = get_object_or_404(Iw, pk=pk)
    form = VoidForm(request.POST)
    if not form.is_valid():
        messages.error(request, "A reason is mandatory for VOID.")
        return redirect("iw:detail", pk=pk)
    try:
        req = services.request_void(request.user, iw, form.cleaned_data["reason"])
        if req.status == req.Status.APPROVED:
            messages.success(request, f"{iw.iw_no} is VOID (you are the approver - recorded as self-approved).")
        else:
            messages.success(request, f"VOID of {iw.iw_no} requested. The CFA Manager must approve it.")
    except (services.IwError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return redirect("iw:detail", pk=pk)


@require_POST
@needs("IW_EDIT")
def iw_discard(request, pk):
    iw = get_object_or_404(Iw, pk=pk)
    try:
        services.discard_draft(request.user, iw)
        messages.success(request, "Draft discarded.")
    except PermissionDenied as exc:
        messages.error(request, str(exc))
        return redirect("iw:detail", pk=pk)
    return redirect("iw:list")


@require_POST
@needs("IW_EDIT")
def org_save(request):
    """IW form panel: add a source organisation / add its e-mails (JSON for org_add.js)."""
    from django.http import JsonResponse

    from inward.iw import orgs
    try:
        saved = orgs.save_org(request.user, request.POST.get("code", ""), request.POST.get("name", ""),
                              request.POST.get("org_type", ""), request.POST.get("emails", ""))
    except (services.IwError, PermissionDenied) as exc:
        return JsonResponse({"error": str(exc)})
    parts = [f"{saved.org} {'added - unverified until the CFA Manager confirms it' if saved.created else 'selected'}."]
    if saved.emails:
        parts.append(f"E-mail(s) added: {', '.join(saved.emails)} (unverified).")
    if saved.note:
        parts.append(saved.note)
    from inward.core.models import OrganisationEmail
    mra = ", ".join(OrganisationEmail.objects.filter(organisation=saved.org, kind="TO").values_list("email", flat=True))
    return JsonResponse({"value": str(saved.org), "message": " ".join(parts), "mra": mra})
