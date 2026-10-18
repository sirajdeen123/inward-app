"""Screen 04 part A views - unloading, SED and physical verification (thin; rules in sed.py)."""
from __future__ import annotations

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from inward.accounts.services import PermissionDenied, modes_for
from inward.core import audit, documents
from inward.iw import checklist, sed
from inward.iw.models import Iw, SedLine, SedPhoto
from inward.iw.services import IwError
from inward.domain.normalize import clean_code
from inward.web.views import needs, upload_done

FIELDS = ("short_qty", "excess_qty", "damage_qty", "damage_kind", "damage_pct", "container_type", "container_count",
          "short_value", "damage_value", "short_remarks", "excess_remarks", "damage_remarks")


KIND_FIELDS = {
    "SHORT": ("short_qty", "short_value", "short_remarks"),
    "EXCESS": ("excess_qty", "excess_remarks"),
    "DAMAGE": ("damage_qty", "damage_kind", "damage_pct", "container_type", "container_count", "damage_value",
               "damage_remarks"),
}


def _kind_input(iw: Iw, post) -> tuple[sed.SedInput, SedLine | None]:
    """One of the three buttons (SHORT / EXCESS / DAMAGE): its fields are merged into the SKU's existing line."""
    kind = post.get("kind", "")
    if kind not in KIND_FIELDS:
        raise IwError("Choose Short, Excess or Damage.")
    if kind == "EXCESS":
        source_key, sku = post.get("came_with", ""), clean_code(post.get("sku", ""))
        if not source_key:
            raise IwError("Choose the Ship / challan the excess came with.")
    else:
        source_key, _, sku = post.get("pick", "").partition("|")
        if not sku:
            raise IwError("Select the SKU from the documents.")
    line = SedLine.objects.filter(iw=iw, source_key=source_key, sku=sku).first()
    values = {f: _fmt(getattr(line, f)) for f in FIELDS} if line else {f: "" for f in FIELDS}
    for f in KIND_FIELDS[kind]:
        values[f] = post.get(f, "")
    return sed.SedInput(source_key=source_key, sku=sku, **values), line


def _input(post) -> sed.SedInput:
    pick = post.get("pick", "")
    if pick == "EXTRA":
        source_key, sku = post.get("extra_source", ""), post.get("extra_sku", "")
    else:
        source_key, _, sku = pick.partition("|")
    return sed.SedInput(source_key=source_key, sku=sku, description=post.get("extra_desc", ""),
                        **{f: post.get(f, "") for f in FIELDS})


def _context(request, iw: Iw, **extra) -> dict:
    snap = iw.checklists.first()
    lines = list(iw.sed_lines.prefetch_related("photos"))
    return {"iw": iw, "snap": snap, "stale": checklist.is_stale(iw, snap) if snap else False,
            "lines": lines, "totals": sed.totals(iw), "options": sed.options(iw), "sources": sed.sources(iw),
            "blockers": sed.verification_blockers(iw) if iw.status == Iw.Status.UNLOADING_IN_PROGRESS else [],
            "can_value": "GRANT" in modes_for(request.user, "SED_VALUE"),
            "editable": iw.status == Iw.Status.UNLOADING_IN_PROGRESS,
            "declarations": sed.DECLARATIONS[sed.declaration_kind(iw)], "declaration_kind": sed.declaration_kind(iw),
            "kinds": SedLine.DamageKind.choices, "add": request.GET.get("add") or (
                (request.POST.get("kind") or "").lower() if request.method == "POST" else ""),
            "new_sku": request.GET.get("sku", ""),
            "can_add_sku": "GRANT" in modes_for(request.user, "SKU_PACKAGING_EDIT"),
            "rejected": sed.rejected_attempts(iw) if iw.status == Iw.Status.UNLOADING_IN_PROGRESS else [],
            "rejected_text": sed.REJECTED_TEXT, **extra}


@needs("PHYSICAL_SED_QTY")
def unloading_page(request, pk):
    iw = get_object_or_404(Iw.objects.select_related("load_type", "verified_by"), pk=pk)
    error, post = None, None
    if request.method == "POST":
        try:
            if request.POST.get("kind"):
                data, existing = _kind_input(iw, request.POST)
                line = sed.save_line(request.user, iw.pk, data, line_pk=existing.pk if existing else None,
                                     version=existing.version if existing else None)
            else:
                line = sed.save_line(request.user, iw.pk, _input(request.POST))
            messages.success(request, f"{request.POST.get('kind', 'SED').title()} saved for {line.floor_sku} "
                                      f"({line.source_label})." + (" Now take the damage photo(s) on the line."
                                                                     if line.damage_qty and not line.photos.exists() else ""))
            return redirect("iw:unloading", pk=iw.pk)
        except (IwError, PermissionDenied) as exc:
            error, post = str(exc), request.POST
            kind = request.POST.get("kind", "")
            sku = clean_code(request.POST.get("sku", "")) if kind == "EXCESS" else request.POST.get("pick", "").partition("|")[2]
            audit.record("SED_SAVE_REJECTED", "iw", iw.pk, user=request.user, iw_id=iw.pk,
                         after={"kind": kind, "sku": sku, "error": error,
                                "entered": {f: request.POST.get(f, "") for f in FIELDS if request.POST.get(f)}})
    return render(request, "iw/unloading.html", _context(request, iw, error=error, post=post))


@needs("PHYSICAL_SED_QTY")
def sed_edit(request, line_pk):
    line = get_object_or_404(SedLine.objects.select_related("iw"), pk=line_pk)
    error = None
    if request.method == "POST":
        try:
            sed.save_line(request.user, line.iw_id, _input(request.POST), line_pk=line.pk,
                          version=request.POST.get("version"))
            messages.success(request, f"SED for {line.floor_sku} updated.")
            return redirect("iw:unloading", pk=line.iw_id)
        except (IwError, PermissionDenied) as exc:
            error = str(exc)
    post = request.POST if request.method == "POST" else {f: _fmt(getattr(line, f)) for f in FIELDS}
    return render(request, "iw/sed_edit.html", {"iw": line.iw, "line": line, "post": post, "error": error,
                                                "kinds": SedLine.DamageKind.choices,
                                                "can_value": "GRANT" in modes_for(request.user, "SED_VALUE")})


def _fmt(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "normalize"):
        return f"{value.normalize():f}" if value else ""
    return str(value)


@require_POST
@needs("PHYSICAL_SED_QTY")
def sed_delete(request, line_pk):
    line = get_object_or_404(SedLine, pk=line_pk)
    try:
        sed.delete_line(request.user, line.pk)
        messages.success(request, f"SED line {line.floor_sku} removed.")
    except IwError as exc:
        messages.error(request, str(exc))
    return redirect("iw:unloading", pk=line.iw_id)


@require_POST
@needs("PHYSICAL_SED_QTY")
def sed_photo_add(request, line_pk):
    line = get_object_or_404(SedLine, pk=line_pk)
    files = request.FILES.getlist("photo")
    if not files:
        messages.error(request, "Take or choose a photo first.")
    added = 0
    for f in files:
        try:
            sed.add_photo(request.user, line.pk, f.read(), f.name or "photo.jpg")
            added += 1
        except IwError as exc:
            messages.error(request, str(exc))
    if added:
        messages.success(request, f"{added} photo(s) added to {line.floor_sku}.")
    return upload_done(request, reverse("iw:unloading", args=[line.iw_id]))


@require_POST
@needs("PHYSICAL_SED_QTY")
def sed_photo_remove(request, photo_pk):
    photo = get_object_or_404(SedPhoto.objects.select_related("line"), pk=photo_pk)
    try:
        sed.remove_photo(request.user, photo.pk)
        messages.success(request, "Photo removed from the SED line (the stored file is kept as evidence).")
    except IwError as exc:
        messages.error(request, str(exc))
    return redirect("iw:unloading", pk=photo.line.iw_id)


@needs("SEARCH_REPORTS")
def sed_photo(request, photo_pk, size):
    photo = get_object_or_404(SedPhoto.objects.select_related("original", "preview"), pk=photo_pk)
    doc = photo.preview if size == "preview" and photo.preview else photo.original
    resp = HttpResponse(documents.read(doc), content_type=doc.content_type)
    resp["Content-Disposition"] = f'inline; filename="{doc.original_filename}"'
    resp["Cache-Control"] = "private, max-age=86400"
    return resp


@require_POST
@needs("PHYSICAL_SED_QTY")
def verification_confirm(request, pk):
    try:
        sed.confirm_verification(request.user, pk, set(request.POST.getlist("confirm")))
        messages.success(request, "Physical verification confirmed. Next: package counts, unloading charge, then the MRA.")
    except IwError as exc:
        messages.error(request, str(exc))
    return redirect("iw:unloading", pk=pk)


@require_POST
@needs("PHYSICAL_SED_QTY")
def verification_reopen(request, pk):
    try:
        sed.reopen_verification(request.user, pk, request.POST.get("reason", ""))
        messages.success(request, "Physical verification reopened - SED and material can be changed again.")
    except IwError as exc:
        messages.error(request, str(exc))
    return redirect("iw:unloading", pk=pk)


@needs("SEARCH_REPORTS")
def sku_search(request):
    """SKU suggestions from the packaging master for every SKU box (Excess, manual Ship rows, vendor challan rows):
    the start of the full SKU, the Floor SKU with or without its space (XG9 991 / XG9991 / XG99), or words of the
    description. Exact / code matches come first."""
    from django.db.models import Case, IntegerField, Q, Value, When
    from django.http import JsonResponse

    from inward.core.models import SkuPackaging
    q = " ".join((request.GET.get("q") or "").upper().split())
    if len(q) < 2:
        return JsonResponse({"items": []})
    key = q.replace(" ", "")
    code = Q(sku__startswith=key)
    floor = Q()
    if key.isalnum() and len(key) <= 6:   # a Floor SKU typed with or without the space
        floor = Q(floor_sku__startswith=key[:3] + (" " + key[3:] if len(key) > 3 else ""))
    words = Q()
    for w in q.split():
        words &= Q(description__icontains=w)
    cond = code | floor | (words if len(q) >= 3 else Q(pk__in=[]))
    qs = (SkuPackaging.objects.filter(cond, active=True)
          .annotate(rank=Case(When(sku=key, then=Value(0)), When(code, then=Value(1)),
                              When(floor, then=Value(2)) if floor else When(pk__in=[], then=Value(2)),
                              default=Value(3), output_field=IntegerField()))
          .order_by("rank", "floor_sku", "sku")[:25])
    return JsonResponse({"items": [{"sku": p.sku, "floor": p.floor_sku, "description": p.description,
                                    "uom": {"LTR": "L", "KG": "K"}.get(p.measure_family, p.uom),
                                    "pack": format(p.pack_size.normalize(), "f") if p.pack_size else ""}
                                   for p in qs]})


@require_POST
@needs("SKU_PACKAGING_EDIT")
def sku_quick_add(request, pk):
    from django.urls import reverse

    from inward.core import packaging
    try:
        p = packaging.quick_add(request.user, request.POST.get("sku", ""), request.POST.get("description", ""))
        messages.success(request, f"SKU {p.sku} added to the packaging list (packaging not set yet).")
        return redirect(f"{reverse('iw:unloading', args=[pk])}?add=excess&sku={p.sku}#add")
    except packaging.PackagingError as exc:
        messages.error(request, str(exc))
    return redirect(f"{reverse('iw:unloading', args=[pk])}?add=excess#add")
