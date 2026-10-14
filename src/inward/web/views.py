from __future__ import annotations

import shutil
import tempfile
from collections import Counter
from functools import wraps
from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import render

from inward.accounts.services import modes_for, role_codes
from inward.core.models import ImportBatch
from inward.importers.common import ImportFailed

PREVIEW_ROWS = 300


def needs(permission: str, modes=("GRANT",)):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(request, *args, **kwargs):
            if not modes_for(request.user, permission) & set(modes):
                return HttpResponseForbidden("You do not have permission for this screen.")
            return view(request, *args, **kwargs)
        return wrapped
    return decorator


def healthz(request):
    """Unauthenticated liveness probe for the reverse proxy / uptime monitor."""
    return HttpResponse("ok", content_type="text/plain")


@login_required
def home(request):
    return render(request, "web/home.html", {"roles": role_codes(request.user)})


class PreviewForm(forms.Form):
    KINDS = [("SIT", "SIT"), ("IRR", "IRR"), ("DESPATCH", "Despatch Detail (outbound)"), ("VENDOR", "Vendor portal")]
    kind = forms.ChoiceField(choices=KINDS, widget=forms.RadioSelect, initial="SIT")
    file = forms.FileField(help_text="Excel file (.xls / .xlsx), up to 50 MB")

    def clean_file(self):
        f = self.cleaned_data["file"]
        suffix = Path(f.name).suffix.lower()
        if suffix not in (".xls", ".xlsx"):
            raise forms.ValidationError("Only .xls or .xlsx files are accepted")
        head = f.read(2048)
        f.seek(0)
        probe = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
        is_excel = head.startswith((b"PK\x03\x04", b"\xd0\xcf\x11\xe0"))  # .xlsx (zip) / .xls (OLE2)
        is_oracle_html = probe.startswith((b"<html", b"<!doctype html")) or b"<table" in head.lower()
        if not (is_excel or is_oracle_html):
            raise forms.ValidationError("The file content is not an Excel workbook or an Oracle HTML report")
        return f


def _parse(kind: str, path: Path) -> dict:
    """Run the production importer and shape a preview; nothing is saved."""
    if kind == "SIT":
        from inward.importers.sit import parse_sit
        r = parse_sit(path)
        return {"summary": [("Window", f"{r.window_from} to {r.window_to}"), ("Rows", len(r.rows)),
                            ("Ship IDs", len(r.ship_ids)),
                            ("UOM", dict(Counter(x.uom or "(blank)" for x in r.rows))),
                            ("Business lines", dict(Counter(x.business_line or "(blank)" for x in r.rows)))],
                "columns": ["Ship ID", "SKU", "Floor SKU", "Description", "BL", "UOM", "Pcs", "Measure", "Empty pkg"],
                "rows": [[ln.ship_id, ln.sku, ln.floor_sku, ln.description, ln.business_line, ln.uom, ln.quantity,
                          ln.measure, "yes" if ln.is_empty_packing else ""] for ln in r.lines.values()],
                "issues": r.issues}
    if kind == "IRR":
        from inward.importers.irr import parse_irr
        r = parse_irr(path)
        return {"summary": [("Window", f"{r.report_from} to {r.report_to}"), ("Run at", r.run_at),
                            ("Rows", len(r.rows)), ("TRX groups", len(r.groups)),
                            ("Last day possibly incomplete", "yes" if r.last_day_possibly_incomplete else "no"),
                            ("Types", dict(Counter(x.type for x in r.rows)))],
                "columns": ["Type", "TRX Number", "TRX Date", "SKU", "Packs", "LTR", "KG"],
                "rows": [[x.type, x.trx_number, x.trx_date, x.sku, x.no_of_pack, x.volume_ltr or "", x.weight_kg or ""]
                         for x in r.rows],
                "issues": r.issues}
    if kind == "DESPATCH":
        from inward.importers.despatch import parse_despatch
        r = parse_despatch(path)
        return {"summary": [("Window", f"{r.window_from} to {r.window_to}"), ("Rows", len(r.rows))],
                "columns": ["To Org", "SKU", "Description", "B. Line", "NOP", "LTR", "KG"],
                "rows": [[x.to_org_code, x.sku, x.description, x.business_line, x.nop, x.ltr, x.kg] for x in r.rows],
                "issues": r.issues}
    from inward.importers.vendor_portal import parse_vendor_portal
    r = parse_vendor_portal(path, settings.INWARD["DEPOT_CODE"])
    return {"summary": [("Lines", len(r.lines)), ("Challans", len({x.key for x in r.lines}))],
            "columns": ["Vendor", "PO", "Challan", "SKU", "UOM", "NOP", "LTR", "KG"],
            "rows": [[x.vendor_code, x.po_number, x.challan_no, x.sku, x.uom, x.nop, x.ltr, x.kg] for x in r.lines],
            "issues": r.issues}


@needs("IMPORT_RECONCILE")
def import_preview(request):
    form = PreviewForm(request.POST or None, request.FILES or None)
    result = error = None
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["file"]
        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(upload.name).suffix.lower()) as tmp:
            for chunk in upload.chunks():
                tmp.write(chunk)
        path = Path(tmp.name)
        try:
            result = _parse(form.cleaned_data["kind"], path)
            result["total"] = len(result["rows"])
            result["rows"] = result["rows"][:PREVIEW_ROWS]
        except ImportFailed as exc:
            error = f"Import rejected: {exc}"
        finally:
            path.unlink(missing_ok=True)
    return render(request, "web/import_preview.html",
                  {"form": form, "result": result, "error": error, "preview_rows": PREVIEW_ROWS})


@needs("SYSTEM_ADMIN", modes=("GRANT", "VIEW"))
def health(request):
    checks = []
    try:
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        checks.append(("Database", not pending, "connected; schema up to date" if not pending
                       else f"{len(pending)} migration(s) not applied"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("Database", False, str(exc)))
    root = Path(settings.DOCUMENT_ROOT)
    try:
        root.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(root)
        pct = 100 * usage.used / usage.total
        checks.append(("Document store", pct < 80, f"{root} · disk {pct:.0f}% used"))
    except OSError as exc:
        checks.append(("Document store", False, str(exc)))
    gmail = settings.INWARD["GMAIL_ADDRESS"]
    checks.append(("Gmail account", bool(gmail and settings.INWARD["GMAIL_APP_PASSWORD"]),
                   gmail or "not configured (INWARD_GMAIL_ADDRESS / INWARD_GMAIL_APP_PASSWORD)"))
    latest = ImportBatch.objects.order_by("-uploaded_at")[:5]
    return render(request, "web/health.html", {"checks": checks, "latest": latest})
