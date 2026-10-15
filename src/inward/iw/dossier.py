"""Complete IW Dossier (SoT 9): every stored document of the IW with a clickable index (index.html) listing each
file's type, date and SHA-256, plus the audit trail as CSV. Built on demand from the document store."""
from __future__ import annotations

import csv
import io
import re
import tempfile
import zipfile

from django.template.loader import render_to_string
from django.utils import timezone

from inward.core import documents
from inward.core.models import AuditEvent, DocumentLink
from inward.iw.models import Iw

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
FOLDERS = {"MRA_PDF": "1 MRA", "DAMAGE_PHOTO": "2 Damage photos", "DAMAGE_PHOTO_PREVIEW": None,
           "SIGNED_MRA": "3 Signed MRA", "LR_SCAN": "3 LR", "MAIL_SENT_EML": "4 E-mails",
           "SED_APPROVAL_REPLY": "4 E-mails", "SED_APPROVAL_EVIDENCE": "4 E-mails", "IRR_RECON_REPORT": "5 Reconciliation",
           "DESPATCH_REPORT": "6 Source reports", "DESTINATION_SIT_REPORT": "6 Source reports",
           "VENDOR_PORTAL_REPORT": "6 Source reports"}


def build_zip(iw: Iw) -> tempfile.SpooledTemporaryFile:
    docs, seen = [], set()
    for link in (DocumentLink.objects.filter(entity_type="iw", entity_id=str(iw.pk)).select_related("document")
                 .order_by("document__created_at")):
        d = link.document
        folder = FOLDERS.get(d.doc_type, "7 Other")
        if folder is None or d.pk in seen:
            continue
        seen.add(d.pk)
        name = f"{folder}/{timezone.localtime(d.created_at):%Y%m%d-%H%M} {_UNSAFE.sub('_', d.original_filename)}"
        docs.append((name, d, link.role))
    out = tempfile.SpooledTemporaryFile(max_size=50 * 1024 * 1024)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, d, _ in docs:
            z.writestr(name, documents.read(d))
        trail = io.StringIO()
        w = csv.writer(trail)
        w.writerow(["time", "event", "user", "before", "after"])
        for e in AuditEvent.objects.filter(iw_id=iw.pk).select_related("user").order_by("id"):
            w.writerow([timezone.localtime(e.occurred_at).isoformat(timespec="seconds"), e.action,
                        e.user.username if e.user else "system", e.before or "", e.after or ""])
        z.writestr("audit_trail.csv", trail.getvalue())
        z.writestr("index.html", render_to_string("iw/dossier_index.html", {
            "iw": iw, "docs": docs, "generated": timezone.localtime(timezone.now())}))
    out.seek(0)
    return out
