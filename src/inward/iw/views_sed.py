"""Screen 04 part A views - unloading, SED and physical verification (thin; rules in sed.py)."""
from __future__ import annotations

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from inward.accounts.services import PermissionDenied, modes_for
from inward.core import documents
from inward.iw import checklist, sed
from inward.iw.models import Iw, SedLine, SedPhoto
from inward.iw.services import IwError
from inward.web.views import needs

FIELDS = ("short_qty", "excess_qty", "damage_qty", "damage_kind", "damage_pct", "container_type", "container_count",
          "short_value", "damage_value", "short_remarks", "excess_remarks", "damage_remarks")


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
            "kinds": SedLine.DamageKind.choices, **extra}


@needs("PHYSICAL_SED_QTY")
def unloading_page(request, pk):
    iw = get_object_or_404(Iw.objects.select_related("load_type", "verified_by"), pk=pk)
    error, post = None, None
    if request.method == "POST":
        try:
            line = sed.save_line(request.user, iw.pk, _input(request.POST))
            messages.success(request, f"SED saved for {line.floor_sku} ({line.source_label})."
                             + (" Now add the damage photo(s)." if line.damage_qty else ""))
            return redirect("iw:unloading", pk=iw.pk)
        except (IwError, PermissionDenied) as exc:
            error, post = str(exc), request.POST
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
    return redirect("iw:unloading", pk=line.iw_id)


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
        sed.confirm_verification(request.user, pk)
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
