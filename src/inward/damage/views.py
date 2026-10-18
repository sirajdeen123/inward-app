"""Internal damage screens (thin; rules in services.py)."""
from __future__ import annotations

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from inward.accounts.services import PermissionDenied, modes_for
from inward.damage import services as dmg
from inward.damage.models import DamageCategory, DamageLine, DamagePhoto, DamageReport
from inward.web.views import needs, upload_done

ERRORS = (dmg.DamageError, PermissionDenied)


@needs("DAMAGE_REPORT")
def report_list(request):
    status = request.GET.get("status", "")
    qs = DamageReport.objects.select_related("created_by").prefetch_related("lines")
    if status:
        qs = qs.filter(status=status)
    return render(request, "damage/list.html", {"reports": qs[:200], "status": status,
                                                "statuses": DamageReport.Status.choices})


@require_POST
@needs("DAMAGE_REPORT")
def report_new(request):
    try:
        r = dmg.create(request.user, request.POST.get("found_on", ""), request.POST.get("location", ""),
                       request.POST.get("remarks", ""))
        messages.success(request, f"{r} started - add the damaged SKUs and their photos.")
        return redirect("damage:detail", pk=r.pk)
    except ERRORS as exc:
        messages.error(request, str(exc))
        return redirect("damage:list")


@needs("DAMAGE_REPORT")
def detail(request, pk):
    r = get_object_or_404(DamageReport.objects.select_related("created_by", "request_mail", "reply"), pk=pk)
    return render(request, "damage/detail.html", {
        "r": r, "lines": r.lines.select_related("category").prefetch_related("photos"),
        "categories": DamageCategory.objects.filter(active=True),
        "blockers": dmg.submit_blockers(r) if r.editable else [], "trx": r.trx.select_related("entered_by"),
        "can_trx": "GRANT" in modes_for(request.user, "DAMAGE_TRX"), "recipients": dmg.recipients()[:2],
        "accepted": sorted(dmg.accepted_codes(r)),
        "mails": r.request_mail and [r.request_mail]})


def _do(request, pk, fn, ok):
    try:
        fn()
        messages.success(request, ok)
    except ERRORS as exc:
        messages.error(request, str(exc))
    return redirect("damage:detail", pk=pk)


@require_POST
@needs("DAMAGE_REPORT")
def line_add(request, pk):
    p = request.POST
    return _do(request, pk, lambda: dmg.add_line(request.user, pk, sku=p.get("sku") or p.get("sku_1", ""), category=p.get("category", ""),
                                                 quantity=p.get("quantity", ""), lot_no=p.get("lot_no", ""),
                                                 locator=p.get("locator", ""), remarks=p.get("remarks", "")),
               "Line added - now take its photo(s).")


@require_POST
@needs("DAMAGE_REPORT")
def line_remove(request, pk, line_pk):
    get_object_or_404(DamageLine, pk=line_pk, report_id=pk)
    return _do(request, pk, lambda: dmg.remove_line(request.user, line_pk), "Line removed.")


@require_POST
@needs("DAMAGE_REPORT")
def photo_add(request, pk, line_pk):
    get_object_or_404(DamageLine, pk=line_pk, report_id=pk)
    files = request.FILES.getlist("photo")
    if not files:
        messages.error(request, "Take or choose a photo first.")
    added = 0
    for f in files:
        try:
            dmg.add_photo(request.user, line_pk, f.read(), f.name or "photo.jpg")
            added += 1
        except ERRORS as exc:
            messages.error(request, str(exc))
    if added:
        messages.success(request, f"{added} photo(s) added.")
    return upload_done(request, reverse("damage:detail", args=[pk]))


@require_POST
@needs("DAMAGE_REPORT")
def photo_remove(request, pk, photo_pk):
    get_object_or_404(DamagePhoto, pk=photo_pk, line__report_id=pk)
    return _do(request, pk, lambda: dmg.remove_photo(request.user, photo_pk), "Photo removed (the file is kept).")


@needs("DAMAGE_REPORT")
def photo(request, pk, photo_pk, size):
    from inward.core import documents
    p = get_object_or_404(DamagePhoto.objects.select_related("original", "preview"), pk=photo_pk, line__report_id=pk)
    doc = p.preview if size == "preview" and p.preview else p.original
    resp = HttpResponse(documents.read(doc), content_type=doc.content_type)
    resp["Content-Disposition"] = f'inline; filename="{doc.original_filename}"'
    return resp


@needs("DAMAGE_REPORT")
def reply_file(request, pk):
    from inward.core import documents
    r = get_object_or_404(DamageReport.objects.select_related("reply"), pk=pk, reply__isnull=False)
    resp = HttpResponse(documents.read(r.reply), content_type="message/rfc822")
    resp["Content-Disposition"] = f'attachment; filename="{r.reply.original_filename}"'
    return resp


@require_POST
@needs("DAMAGE_REPORT")
def submit(request, pk):
    return _do(request, pk, lambda: dmg.submit(request.user, pk),
               "Approval mail queued to the Depot Manager and the Admin Officer. Their reply APPROVED / REJECTED "
               "is read automatically.")


@require_POST
@needs("DAMAGE_REPORT")
def cancel(request, pk):
    return _do(request, pk, lambda: dmg.cancel(request.user, pk, request.POST.get("reason", "")), "Report cancelled.")


@require_POST
@needs("DAMAGE_TRX")
def trx_add(request, pk):
    return _do(request, pk, lambda: dmg.add_trx(request.user, pk, request.POST.get("trx_no", "")),
               "TRX recorded - run the reconciliation after the next IRR import.")


@require_POST
@needs("DAMAGE_TRX")
def trx_remove(request, pk, trx_pk):
    return _do(request, pk, lambda: dmg.remove_trx(request.user, trx_pk), "TRX removed.")


@require_POST
@needs("DAMAGE_TRX")
def recon(request, pk):
    def run():
        r = dmg.run_recon(request.user, pk)
        if r.status != DamageReport.Status.RECONCILED:
            raise dmg.DamageError(f"Reconciliation saved: {r.recon.get('status')} - see the details below.")
    return _do(request, pk, run, "Reconciled with the IRR - the report is complete.")


@require_POST
@needs("DAMAGE_TRX")
def accept_reason(request, pk):
    return _do(request, pk, lambda: dmg.accept_reason(request.user, pk, request.POST.get("code", "")),
               "Reason code accepted for this report.")


@require_POST
@needs("DAMAGE_REPORT")
def category_add(request, pk):
    def add():
        cat = dmg.add_category(request.user, request.POST.get("name", ""), request.POST.get("photo") == "on",
                               request.POST.get("remarks") == "on")
        messages.success(request, f"Reason '{cat.name}' added - choose it for the line.")
    try:
        add()
    except ERRORS as exc:
        messages.error(request, str(exc))
    return redirect(f"{reverse('damage:detail', args=[pk])}#add-line")
