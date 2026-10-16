"""OneDrive mirror of the document store (SoT 9), through rclone.

Every stored document is copied to OneDrive in a readable structure - Inward Bundles / FY2627 / IW-2627-000123 /
<folder> / <date> <file> - so any IW can be browsed and downloaded from OneDrive with or without the application.
Documents not tied to an IW (SIT / IRR imports) go under _Imports. The VPS copy stays authoritative: a weekly check
compares each OneDrive file's QuickXorHash with the local file and raises a critical exception for any changed or
missing copy (a missing one is uploaded again). Off until INWARD_ONEDRIVE_REMOTE is set and rclone is connected.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from inward.core import documents
from inward.core.models import Document, DocumentLink, ExceptionEvent

log = logging.getLogger("inward.mirror")
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f#%]')
SKIP = {"DAMAGE_PHOTO_PREVIEW"}
FOLDERS = {"MRA_PDF": "1 MRA", "SED_AUDIT_MEMO": "1 MRA", "DAMAGE_PHOTO": "2 Damage photos", "SIGNED_MRA": "3 Signed MRA and LR",
           "LR_SCAN": "3 Signed MRA and LR", "MAIL_SENT_EML": "4 E-mails", "SED_APPROVAL_REPLY": "4 E-mails",
           "SED_APPROVAL_EVIDENCE": "4 E-mails", "IRR_RECON_REPORT": "5 Reconciliation",
           "DESPATCH_REPORT": "6 Source reports", "DESTINATION_SIT_REPORT": "6 Source reports",
           "VENDOR_PORTAL_REPORT": "6 Source reports"}


def enabled() -> bool:
    return bool(settings.INWARD.get("ONEDRIVE_REMOTE"))


def _rclone(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["rclone", "--config", settings.INWARD["RCLONE_CONFIG"], *args], capture_output=True,
                          text=True, timeout=timeout)


def path_for(doc: Document) -> str:
    from inward.iw.models import Iw
    link = DocumentLink.objects.filter(document=doc, entity_type="iw").first()
    when = timezone.localtime(doc.created_at)
    name = f"{when:%Y%m%d-%H%M} {_UNSAFE.sub('_', doc.original_filename)}"
    iw = Iw.objects.filter(pk=int(link.entity_id)).first() if link and link.entity_id.isdigit() else None
    if iw and iw.iw_no:
        return f"FY{iw.fy_code}/{iw.iw_no}/{FOLDERS.get(doc.doc_type, '7 Other')}/{name}"
    return f"_Imports/{doc.doc_type}/{when:%Y-%m}/{name}"


def mirror_pending(limit: int = 50, older_than_s: int = 60) -> dict:
    """Upload documents not yet on OneDrive (oldest first)."""
    if not enabled():
        return {"mirror": "off"}
    root = settings.INWARD["ONEDRIVE_REMOTE"].rstrip("/")
    done = failed = 0
    cutoff = timezone.now() - timedelta(seconds=older_than_s)
    for doc in (Document.objects.filter(cloud_mirrored_at__isnull=True, created_at__lt=cutoff)
                .exclude(doc_type__in=SKIP).order_by("created_at")[:limit]):
        target = doc.cloud_path or path_for(doc)
        r = _rclone("copyto", str(documents._path(doc.storage_key)), f"{root}/{target}")
        if r.returncode == 0:
            Document.objects.filter(pk=doc.pk).update(cloud_mirrored_at=timezone.now(), cloud_path=target)
            done += 1
        else:
            failed += 1
            log.warning("onedrive copy %s: %s", doc.pk, r.stderr.strip()[:300])
            if "couldn't find" in r.stderr or "didn't find section" in r.stderr:
                break  # not connected yet
    return {"mirrored": done, "failed": failed}


def _local_quickxor(path) -> str:
    r = _rclone("hashsum", "QuickXorHash", str(path))
    return r.stdout.split()[0].lower() if r.returncode == 0 and r.stdout.strip() else ""


def integrity_check() -> dict:
    """Weekly: OneDrive copies compared with the local files (QuickXorHash)."""
    if not enabled():
        return {"check": "off"}
    root = settings.INWARD["ONEDRIVE_REMOTE"].rstrip("/")
    r = _rclone("lsjson", "-R", "--files-only", "--hash", "--hash-type", "QuickXorHash", root, timeout=1800)
    if r.returncode != 0:
        return {"check": f"failed: {r.stderr.strip()[:200]}"}
    remote = {e["Path"]: (e.get("Hashes") or {}).get("quickxor", "").lower() for e in json.loads(r.stdout or "[]")}
    changed = missing = 0
    for doc in Document.objects.exclude(cloud_mirrored_at__isnull=True).exclude(cloud_path=""):
        if doc.cloud_path not in remote:
            missing += 1
            Document.objects.filter(pk=doc.pk).update(cloud_mirrored_at=None)  # uploaded again
            ExceptionEvent.objects.create(code="ONEDRIVE_MISSING", severity="WARN", entity_type="document",
                                          entity_id=str(doc.pk), message=f"Missing on OneDrive: {doc.cloud_path}")
            continue
        local = _local_quickxor(documents._path(doc.storage_key))
        if local and remote[doc.cloud_path] and local != remote[doc.cloud_path]:
            changed += 1
            ExceptionEvent.objects.create(code="ONEDRIVE_CHANGED", severity="CRITICAL", entity_type="document",
                                          entity_id=str(doc.pk),
                                          message=f"OneDrive copy differs from the stored document: {doc.cloud_path}")
    return {"checked": len(remote), "changed": changed, "missing": missing}


def status() -> dict:
    total = Document.objects.exclude(doc_type__in=SKIP).count()
    pending = Document.objects.exclude(doc_type__in=SKIP).filter(cloud_mirrored_at__isnull=True).count()
    return {"enabled": enabled(), "total": total, "pending": pending}
