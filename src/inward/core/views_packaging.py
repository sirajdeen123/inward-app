"""SKU packaging master screens (thin; rules in inward.core.packaging)."""
from __future__ import annotations

import tempfile
from pathlib import Path

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from inward.accounts.services import PermissionDenied, modes_for
from inward.core import packaging
from inward.core.models import PackageType, PackagingChange, PackagingUpload, SkuPackaging
from inward.domain.normalize import clean_code
from inward.importers.common import ImportFailed
from inward.web.views import PreviewForm, needs


@needs("SEARCH_REPORTS")
def packaging_home(request):
    qs = SkuPackaging.objects.select_related("package_type")
    q = request.GET.get("q", "").strip()
    show = request.GET.get("show", "")
    if q:
        qs = qs.filter(Q(sku__icontains=clean_code(q)) | Q(floor_sku__icontains=q) | Q(description__icontains=q))
    if show == "not_set":
        qs = qs.filter(units_per_package__isnull=True)
    elif show == "verify":
        qs = qs.exclude(verify_note="")
    elif show == "clerk":
        qs = qs.filter(source=SkuPackaging.Source.CLERK)
    page = Paginator(qs.order_by("sku"), 50).get_page(request.GET.get("page"))
    return render(request, "core/packaging.html", {
        "page": page, "q": q, "show": show, "total": SkuPackaging.objects.count(),
        "not_set": SkuPackaging.objects.filter(units_per_package__isnull=True).count(),
        "verify": SkuPackaging.objects.exclude(verify_note="").count(),
        "pending": PackagingChange.objects.filter(review=PackagingChange.Review.PENDING).count(),
        "uploads": PackagingUpload.objects.select_related("uploaded_by")[:5],
        "can_edit": "GRANT" in modes_for(request.user, "SKU_PACKAGING_EDIT"),
        "can_review": "GRANT" in modes_for(request.user, "PACKAGING_REVIEW")})


@require_POST
@needs("SKU_PACKAGING_EDIT")
def packaging_upload(request):
    form = PreviewForm({"kind": "SIT"}, request.FILES)
    if not form.is_valid():
        messages.error(request, " ".join(str(e) for errs in form.errors.values() for e in errs))
        return redirect("core:packaging")
    upload = form.cleaned_data["file"]
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(upload.name).suffix.lower()) as tmp:
        for chunk in upload.chunks():
            tmp.write(chunk)
    try:
        up = packaging.create_upload(request.user, Path(tmp.name), upload.name)
    except ImportFailed as exc:
        messages.error(request, f"Not read: {exc}")
        return redirect("core:packaging")
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    return redirect("core:packaging_preview", pk=up.pk)


@needs("SKU_PACKAGING_EDIT")
def packaging_preview(request, pk):
    up = get_object_or_404(PackagingUpload, pk=pk)
    if request.method == "POST":
        try:
            if request.POST.get("action") == "apply":
                packaging.apply_upload(request.user, up.pk)
                messages.success(request, f"Packaging list applied: {up.counts.get('new', 0)} new, "
                                          f"{up.counts.get('changed', 0)} changed"
                                          f"{', drastic changes sent to the CFA Manager' if up.counts.get('drastic') else ''}.")
            else:
                packaging.cancel_upload(request.user, up.pk)
                messages.info(request, "Upload cancelled - nothing changed.")
        except packaging.PackagingError as exc:
            messages.error(request, str(exc))
        return redirect("core:packaging")
    return render(request, "core/packaging_preview.html", {"up": up,
                                                           "bulk_limit": packaging._bulk_limit()})


@needs("SEARCH_REPORTS")
def packaging_sku(request, sku):
    sku = clean_code(sku)
    p = SkuPackaging.objects.select_related("package_type").filter(sku=sku).first()
    can_edit = "GRANT" in modes_for(request.user, "SKU_PACKAGING_EDIT")
    error = None
    if request.method == "POST":
        if not can_edit:
            raise PermissionDenied("Not allowed")
        target = request.POST.get("sku_override", "") if sku == "NEW" else sku
        try:
            ch = packaging.save_sku(request.user, target, group=request.POST.get("group", ""),
                                    units=request.POST.get("units", ""),
                                    description=request.POST.get("description", ""),
                                    pack_size=request.POST.get("pack_size") or None, uom=request.POST.get("uom", ""),
                                    reason=request.POST.get("reason", ""), verified=bool(request.POST.get("verified")))
            note = " - flagged to the CFA Manager for review" if ch.drastic else ""
            messages.success(request, f"Packaging of {ch.sku} saved ({ch.action.lower()}){note}.")
            return redirect(request.POST.get("next") or "core:packaging")
        except packaging.PackagingError as exc:
            error = str(exc)
    return render(request, "core/packaging_sku.html", {
        "sku": sku, "p": p, "error": error, "post": request.POST, "can_edit": can_edit,
        "groups": PackageType.objects.order_by("sort_priority"),
        "history": PackagingChange.objects.filter(sku=sku).select_related("changed_by", "reviewed_by")[:30],
        "next": request.GET.get("next", ""), "prefill_desc": request.GET.get("desc", ""),
        "prefill_size": request.GET.get("size", ""), "prefill_uom": request.GET.get("uom", "")})


@needs("PACKAGING_REVIEW")
def packaging_review(request):
    if request.method == "POST":
        try:
            ch = packaging.review(request.user, int(request.POST["change"]), request.POST.get("decision") == "accept")
            messages.success(request, f"Change to {ch.sku} {ch.review.lower()}.")
        except packaging.PackagingError as exc:
            messages.error(request, str(exc))
        return redirect("core:packaging_review")
    pending = PackagingChange.objects.filter(review=PackagingChange.Review.PENDING).select_related("changed_by")
    return render(request, "core/packaging_review.html", {"pending": pending})
