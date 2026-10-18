"""IRR import screen (thin; rules in services.py)."""
from __future__ import annotations

import tempfile
from pathlib import Path

from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from inward.accounts.services import PermissionDenied
from inward.core.models import ImportBatch
from inward.importers.common import ImportFailed
from inward.irr import services
from inward.irr.models import IrrTransaction
from inward.web.views import PreviewForm, needs


@needs("IMPORT_RECONCILE")
def irr_home(request):
    batches = ImportBatch.objects.filter(kind="IRR").select_related("uploaded_by").order_by("-uploaded_at")[:30]
    span = IrrTransaction.objects.order_by("trx_date").values_list("trx_date", flat=True)
    from inward.accounts.services import role_codes
    from inward.irr import autofetch
    from inward.iw.models import ReportRun
    return render(request, "irr/home.html", {"batches": batches, "rows": IrrTransaction.objects.count(),
                                             "first": span.first(), "last": span.last(), "auto": autofetch.settings_now(),
                                             "auto_runs": ReportRun.objects.filter(kind=autofetch.KIND).order_by("-period")[:7],
                                             "can_set": bool({"CFA_MANAGER", "ADMINISTRATOR"} & set(role_codes(request.user)))})


@require_POST
@needs("IMPORT_RECONCILE")
def irr_fetch_now(request):
    from inward.irr import autofetch
    from inward.mail.gmail import from_settings
    try:
        out = autofetch.import_from_mail(from_settings(), request.user)
        (messages.success if out.status == "imported" else messages.warning)(request, out.message)
    except (ImportFailed, PermissionDenied) as exc:
        messages.error(request, str(exc))
    except Exception as exc:  # noqa: BLE001 - mailbox not reachable etc.
        messages.error(request, f"Gmail could not be read: {exc}")
    return redirect("irr:home")


@require_POST
@needs("IMPORT_RECONCILE")
def irr_auto_settings(request):
    import re

    from inward.accounts.services import role_codes
    from inward.core import audit
    from inward.core.models import FeatureFlag
    if not {"CFA_MANAGER", "ADMINISTRATOR"} & set(role_codes(request.user)):
        messages.error(request, "Only the CFA Manager or Administrator changes the IRR mail settings.")
        return redirect("irr:home")
    values = {"irr_mail_subject": request.POST.get("subject", "").strip(),
              "irr_fetch_time": request.POST.get("fetch_time", "").strip(),
              "irr_fetch_deadline": request.POST.get("deadline", "").strip()}
    if len(values["irr_mail_subject"]) < 4 or not all(re.match(r"^\d{1,2}:\d{2}$", values[k])
                                                     for k in ("irr_fetch_time", "irr_fetch_deadline")):
        messages.error(request, "Enter the subject text (at least 4 characters) and times as HH:MM.")
        return redirect("irr:home")
    for key, value in values.items():
        FeatureFlag.objects.filter(key=key).update(value={"value": value})
    audit.record("IRR_AUTO_SETTINGS", "feature_flag", "irr_mail", user=request.user, after=values)
    messages.success(request, "IRR mail settings saved.")
    return redirect("irr:home")


@require_POST
@needs("IMPORT_RECONCILE")
def irr_upload(request):
    form = PreviewForm({"kind": "IRR"}, request.FILES)
    if not form.is_valid():
        messages.error(request, " ".join(str(e) for errs in form.errors.values() for e in errs))
        return redirect("irr:home")
    upload = form.cleaned_data["file"]
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(upload.name).suffix.lower()) as tmp:
        for chunk in upload.chunks():
            tmp.write(chunk)
    path = Path(tmp.name)
    try:
        b = services.import_irr(request.user, path, upload.name)
        s = b.summary
        text = (f"IRR {upload.name}: {b.accepted_count} new row(s) added; {s['new_groups']} new and {s['extended']} "
                f"extended transaction(s), {s['identical'] + s['subset']} already in the history.")
        if s["review_count"]:
            messages.warning(request, text + f" {s['review_count']} transaction(s) differ from the history and were "
                                             "NOT applied - see the import below.")
        else:
            messages.success(request, text)
        if s.get("last_day_possibly_incomplete"):
            messages.info(request, "The report was run on its last day; that day may be incomplete - import the "
                                   "next report too.")
    except services.AlreadyImported as exc:
        messages.info(request, str(exc))
    except (ImportFailed, PermissionDenied) as exc:
        messages.error(request, str(exc))
    finally:
        path.unlink(missing_ok=True)
    return redirect("irr:home")
