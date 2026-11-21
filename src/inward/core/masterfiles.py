"""Master files - download, edit in Excel, upload back (decision 21-Nov-2026).

Every master registers a `Spec`: the columns of its file (the editable ones first, then grey reference columns the
upload ignores), how to export the values in use now, how to compare an uploaded file with them (the preview:
new / changed / left / removed / missing / errors) and how to apply the confirmed operations. Nothing changes before
the confirmation; rows missing from the file are kept, never deleted silently; every applied row is audited.
Download: the master's view permission. Upload: MASTER_UPLOAD (CFA Manager; assignable on Roles & permissions).
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Callable

from django.db import transaction
from django.utils import timezone

from inward.accounts.models import User
from inward.accounts.services import require
from inward.core import audit, documents
from inward.core.models import MasterUpload


class MasterFileError(Exception):
    pass


@dataclass
class Spec:
    key: str
    label: str
    module: str
    columns: list[str]                       # editable columns, in file order
    required: list[str]                      # columns the uploaded file must have
    ref_columns: list[str]                   # reference only (grey, ignored on upload)
    view_perm: str
    export: Callable[[], list[dict]]
    plan: Callable[[list[dict]], dict]
    apply: Callable[[User, list[dict]], dict]
    help: list[str] = field(default_factory=list)
    count: Callable[[], int] | None = None


REGISTRY: dict[str, Spec] = {}


def register(spec: Spec) -> None:
    REGISTRY[spec.key] = spec


def empty_plan() -> dict:
    return {"new": [], "changed": [], "left": [], "removed": [], "missing": [], "errors": [], "unchanged": 0, "ops": []}


def text(v) -> str:
    """Excel cell -> text: 19.0 -> '19', None -> ''."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return " ".join(str(v).split())

# ---------------------------------------------------------------- Excel


def workbook(spec: Spec) -> bytes:
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = spec.label[:31]
    head = spec.columns + spec.ref_columns
    ws.append(head)
    for row in spec.export():
        ws.append([row.get(c, "") for c in head])
    edit_fill, ref_fill = PatternFill("solid", fgColor="E4E4EC"), PatternFill("solid", fgColor="F2F2F2")
    for i, h in enumerate(head, 1):
        c = ws.cell(row=1, column=i)
        c.font = Font(bold=True, color="000000" if h in spec.columns else "777777")
        c.fill = edit_fill if h in spec.columns else ref_fill
        if h in spec.ref_columns:
            c.comment = Comment("Reference only - not read on upload", "Stock Inward")
        ws.column_dimensions[get_column_letter(i)].width = max(14, min(42, len(h) + 6))
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions
    how = wb.create_sheet("How to")
    for line in [f"{spec.label} - download, edit, upload back", ""] + spec.help + [
            "", "Columns with a grey heading are for reference only - the upload ignores them.",
            "Rows missing from the file are kept as they are - nothing is deleted silently.",
            "After the upload you see a preview of every change; nothing changes until you confirm."]:
        how.append([line])
    how.column_dimensions["A"].width = 120
    how["A1"].font = Font(bold=True, size=13)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def read_rows(spec: Spec, data: bytes) -> list[dict]:
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - not an .xlsx file
        raise MasterFileError("Not an Excel (.xlsx) file - download the master here, edit it and upload it back.") from exc
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    want = {c.lower(): c for c in spec.columns}
    for h, raw in enumerate(rows[:10]):
        names = [text(v).lower() for v in raw]
        if all(r.lower() in names for r in spec.required):
            cols = {want[n]: i for i, n in enumerate(names) if n in want}
            break
    else:
        raise MasterFileError(f"Columns not found: {', '.join(spec.required)} - use the file downloaded from this page.")
    out = []
    for no, raw in enumerate(rows[h + 1:], start=h + 2):
        rec = {c: text(raw[i]) if i < len(raw) else "" for c, i in cols.items()}
        if any(rec.values()):
            rec["_row"] = no
            out.append(rec)
    if not out:
        raise MasterFileError("The file has no rows.")
    return out

# ---------------------------------------------------------------- upload, preview, apply


def upload(user: User, kind: str, data: bytes, filename: str) -> MasterUpload:
    require(user, "MASTER_UPLOAD")
    spec = REGISTRY[kind]
    rows = read_rows(spec, data)
    plan = spec.plan(rows)
    doc = documents.store(data, filename=filename, content_type="application/vnd.openxmlformats-officedocument."
                          "spreadsheetml.sheet", doc_type="MASTER_FILE", user=user)
    return MasterUpload.objects.create(kind=kind, filename=filename[:260], document=doc, plan=plan, uploaded_by=user)


@transaction.atomic
def confirm(user: User, pk: int) -> MasterUpload:
    require(user, "MASTER_UPLOAD")
    up = MasterUpload.objects.select_for_update().get(pk=pk)
    if up.status != MasterUpload.Status.PREVIEW:
        raise MasterFileError("This upload is already " + up.get_status_display().lower() + ".")
    spec = REGISTRY[up.kind]
    plan = spec.plan(read_rows(spec, documents.read(up.document)))     # again: the master may have changed meanwhile
    result = spec.apply(user, plan["ops"])
    up.plan, up.result, up.status = plan, result, MasterUpload.Status.APPLIED
    up.decided_by, up.decided_at = user, timezone.now()
    up.save(update_fields=["plan", "result", "status", "decided_by", "decided_at"])
    audit.record("MASTER_FILE_APPLIED", "master_upload", up.pk, user=user,
                 after={"master": spec.label, "file": up.filename, **{k: len(plan[k]) for k in
                        ("new", "changed", "left", "removed", "missing", "errors")}, "result": result})
    return up


def cancel(user: User, pk: int) -> MasterUpload:
    require(user, "MASTER_UPLOAD")
    up = MasterUpload.objects.get(pk=pk)
    if up.status == MasterUpload.Status.PREVIEW:
        up.status, up.decided_by, up.decided_at = MasterUpload.Status.CANCELLED, user, timezone.now()
        up.save(update_fields=["status", "decided_by", "decided_at"])
    return up


def filename(spec: Spec) -> str:
    from django.conf import settings
    return f"{spec.label} Depot {settings.INWARD['DEPOT_CODE']} {timezone.localdate():%Y-%m-%d}.xlsx"

