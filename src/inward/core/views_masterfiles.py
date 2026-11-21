"""Master files screen (21-Nov-2026; rules in inward.core.masterfiles)."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied as Forbidden
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from inward.accounts.services import PermissionDenied, modes_for
from inward.core import masterfiles as mf
from inward.core.models import MasterUpload
from inward.web.views import needs

MAX_BYTES = 20 * 1024 * 1024


def _can(user, perm: str) -> bool:
    return "GRANT" in modes_for(user, perm)


def _spec(request, key: str) -> mf.Spec:
    spec = mf.REGISTRY.get(key)
    if spec is None:
        raise Http404
    if not _can(request.user, spec.view_perm):
        raise Forbidden("You may not see this master.")
    return spec


@login_required
def home(request):
    specs = [s for s in mf.REGISTRY.values() if _can(request.user, s.view_perm)]
    if not specs:
        raise Forbidden("No master file for your role.")
    last = {}
    for up in MasterUpload.objects.select_related("uploaded_by", "decided_by")[:200]:
        last.setdefault(up.kind, up)
    return render(request, "core/masterfiles.html", {
        "specs": [{"s": s, "count": s.count() if s.count else None, "last": last.get(s.key)} for s in specs],
        "can_upload": _can(request.user, "MASTER_UPLOAD"),
        "recent": MasterUpload.objects.select_related("uploaded_by", "decided_by")[:15],
        "labels": {k: s.label for k, s in mf.REGISTRY.items()}})


@login_required
def download(request, key):
    spec = _spec(request, key)
    resp = HttpResponse(mf.workbook(spec), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{mf.filename(spec)}"'
    return resp


@require_POST
@needs("MASTER_UPLOAD")
def upload(request, key):
    spec = _spec(request, key)
    f = request.FILES.get("file")
    if f is None:
        messages.error(request, "Choose the file.")
        return redirect("core:masterfiles")
    if f.size > MAX_BYTES:
        messages.error(request, f"{f.name} is larger than 20 MB.")
        return redirect("core:masterfiles")
    try:
        up = mf.upload(request.user, spec.key, f.read(), f.name)
    except (mf.MasterFileError, PermissionDenied) as exc:
        messages.error(request, f"{spec.label}: {exc}")
        return redirect("core:masterfiles")
    return redirect("core:masterfile_preview", pk=up.pk)


@login_required
def preview(request, pk):
    up = get_object_or_404(MasterUpload.objects.select_related("uploaded_by", "decided_by"), pk=pk)
    spec = _spec(request, up.kind)
    p = up.plan
    return render(request, "core/masterfile_preview.html", {
        "up": up, "spec": spec, "p": p, "can_upload": _can(request.user, "MASTER_UPLOAD"),
        "to_apply": len(p.get("ops", [])), "result": up.result, "gone": len(p.get("left", [])) + len(p.get("removed", []))})


@require_POST
@needs("MASTER_UPLOAD")
def decide(request, pk):
    try:
        if request.POST.get("action") == "confirm":
            up = mf.confirm(request.user, pk)
            done = ", ".join(f"{v} {k}" for k, v in up.result.items() if v) or "nothing to change"
            messages.success(request, f"{mf.REGISTRY[up.kind].label}: applied - {done}.")
        else:
            mf.cancel(request.user, pk)
            messages.info(request, "Upload cancelled - nothing changed.")
    except (mf.MasterFileError, PermissionDenied) as exc:
        messages.error(request, str(exc))
    return redirect("core:masterfile_preview", pk=pk)
