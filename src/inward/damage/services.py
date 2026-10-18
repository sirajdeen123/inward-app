"""Internal damage: report -> approval by reply mail -> DMG transfer TRX -> IRR reconciliation (18-Oct-2026).

Approval is by mail only: the request goes to the Depot Manager and the Admin Officer (CFA Manager in copy) with
the list and the photos; the first line of a reply from either registered address, "APPROVED" or "REJECTED
<reason>", decides it (sender authentication checked, the reply .eml kept as evidence). After the approval the
Oracle Subinventory Transfer into DMG is entered as TRX No(s) and reconciled against the IRR. A reason code that is
not in the accepted list (feature flag damage_reason_codes, default 63 and 68) is flagged for the supervisor, who
accepts it for that report or corrects Oracle.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from inward.accounts.models import User
from inward.accounts.services import PermissionDenied, require
from inward.core import audit, documents
from inward.core.models import FeatureFlag, NotificationRecipient, SkuPackaging
from inward.damage.models import DamageCounter, DamageLine, DamagePhoto, DamageReport, DamageTrx
from inward.domain.fy import fy_code
from inward.domain.normalize import clean_code, clean_text, floor_sku, is_valid_sku

R = DamageReport.Status
TAG_RE = re.compile(r"IDR-APPR (IDR-\d{4}-\d{6}) R(\d+)")
ZERO = Decimal(0)


class DamageError(ValueError):
    pass


def _flag(key: str, default):
    f = FeatureFlag.objects.filter(key=key).first()
    v = (f.value or {}).get("value") if f else None
    return default if v is None else v


def accepted_codes(report: DamageReport | None = None) -> set[str]:
    codes = {str(c).strip() for c in (_flag("damage_reason_codes", ["63", "68"]) or [])}
    return codes | set(report.accepted_reasons if report else [])

# ---------------------------------------------------------------- report and lines


def _next_number(day: date) -> tuple[str, str]:
    fy = fy_code(day)
    counter, _ = DamageCounter.objects.select_for_update().get_or_create(fy_code=fy)
    counter.last_number += 1
    counter.save(update_fields=["last_number"])
    return f"IDR-{fy}-{counter.last_number:06d}", fy


def _date(value) -> date:
    if isinstance(value, date):
        d = value
    else:
        try:
            d = date.fromisoformat(str(value).strip()) if str(value or "").strip() else timezone.localdate()
        except ValueError as exc:
            raise DamageError("Date not valid.") from exc
    if d > timezone.localdate():
        raise DamageError("The date cannot be in the future.")
    return d


@transaction.atomic
def create(user: User, found_on="", location: str = "", remarks: str = "") -> DamageReport:
    require(user, "DAMAGE_REPORT")
    day = _date(found_on)
    number, fy = _next_number(day)
    r = DamageReport.objects.create(number=number, fy_code=fy, found_on=day, location=clean_text(location)[:120],
                                    remarks=clean_text(remarks)[:500], created_by=user)
    audit.record("DAMAGE_REPORT_CREATED", "damage_report", r.pk, user=user,
                 after={"number": number, "found_on": str(day), "location": r.location})
    return r


def _editable(report: DamageReport) -> None:
    if not report.editable:
        raise DamageError(f"{report} is {report.get_status_display().lower()} - it can no longer be changed.")


def _qty(value) -> Decimal:
    try:
        q = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise DamageError("Quantity: enter the number of pieces.") from exc
    if q <= 0 or q != q.to_integral_value():
        raise DamageError("Quantity must be whole pieces, more than zero.")
    return q


@transaction.atomic
def add_line(user: User, report_pk: int, *, sku: str, category: str, quantity, lot_no: str = "", locator: str = "",
             remarks: str = "") -> DamageLine:
    require(user, "DAMAGE_REPORT")
    report = DamageReport.objects.select_for_update().get(pk=report_pk)
    _editable(report)
    code = clean_code(sku)
    if not is_valid_sku(code):
        raise DamageError(f"'{sku}' - pick the full 16-character SKU from the list.")
    if category not in DamageLine.Category.values:
        raise DamageError("Choose the category: handling damage, expiry, leakage or other.")
    if category == DamageLine.Category.OTHER and not clean_text(remarks):
        raise DamageError("Write what the damage is (category Other).")
    pk = SkuPackaging.objects.filter(sku=code).first()
    line = DamageLine.objects.create(report=report, sku=code, floor_sku=floor_sku(code),
                                     description=pk.description if pk else "", category=category,
                                     quantity=_qty(quantity), lot_no=clean_text(lot_no).upper()[:40],
                                     locator=clean_text(locator).upper()[:40], remarks=clean_text(remarks)[:300],
                                     created_by=user)
    audit.record("DAMAGE_LINE_ADDED", "damage_report", report.pk, user=user,
                 after={"sku": code, "category": category, "qty": str(line.quantity), "lot": line.lot_no})
    return line


@transaction.atomic
def remove_line(user: User, line_pk: int) -> None:
    require(user, "DAMAGE_REPORT")
    line = DamageLine.objects.select_related("report").get(pk=line_pk)
    _editable(line.report)
    audit.record("DAMAGE_LINE_REMOVED", "damage_report", line.report_id, user=user,
                 before={"sku": line.sku, "qty": str(line.quantity), "photos": line.photos.count()})
    line.delete()


@transaction.atomic
def add_photo(user: User, line_pk: int, data: bytes, filename: str) -> DamagePhoto:
    from PIL import Image, UnidentifiedImageError

    from inward.iw.sed import PHOTO_MAX_BYTES, _preview
    require(user, "DAMAGE_REPORT")
    line = DamageLine.objects.select_related("report").get(pk=line_pk)
    _editable(line.report)
    if len(data) > PHOTO_MAX_BYTES:
        raise DamageError("Photo is larger than 25 MB.")
    try:
        with Image.open(__import__("io").BytesIO(data)) as img:
            img.verify()
            fmt = (img.format or "").upper()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise DamageError(f"'{filename}' is not a photo.") from exc
    ctype = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}.get(fmt)
    if ctype is None:
        raise DamageError("Use a JPEG or PNG photo.")
    links = [("damage_report", line.report_id, "damage_photo")]
    original = documents.store(data, filename=filename, content_type=ctype, doc_type="INTERNAL_DAMAGE_PHOTO",
                               user=user, links=links)
    preview = documents.store(_preview(data), filename=f"preview-{filename.rsplit('.', 1)[0]}.jpg",
                              content_type="image/jpeg", doc_type="INTERNAL_DAMAGE_PREVIEW", user=user)
    photo = DamagePhoto.objects.create(line=line, original=original, preview=preview, uploaded_by=user)
    audit.record("DAMAGE_PHOTO_ADDED", "damage_report", line.report_id, user=user,
                 after={"sku": line.sku, "document": str(original.pk), "sha256": original.sha256})
    return photo


@transaction.atomic
def remove_photo(user: User, photo_pk: int) -> None:
    require(user, "DAMAGE_REPORT")
    photo = DamagePhoto.objects.select_related("line__report").get(pk=photo_pk)
    _editable(photo.line.report)
    audit.record("DAMAGE_PHOTO_REMOVED", "damage_report", photo.line.report_id, user=user,
                 before={"document": str(photo.original_id)})
    photo.delete()   # the stored file stays (evidence)

# ---------------------------------------------------------------- approval by mail


def recipients() -> tuple[list[str], list[str], list[str]]:
    from inward.iw.comms import internal
    return internal("DAMAGE_APPROVAL")


def approver_addresses() -> set[str]:
    addrs = {r.email.lower() for r in NotificationRecipient.objects.filter(
        purpose="DAMAGE_APPROVAL", kind="TO", active=True, role_label__in=["Depot Manager", "Admin Officer"]).exclude(email="")}
    if settings.INWARD["MODE"] == "TEST":
        addrs |= {a.lower() for a in settings.INWARD["TEST_MAIL_TO"]}
    return addrs


def submit_blockers(report: DamageReport) -> list[str]:
    out = []
    lines = list(report.lines.prefetch_related("photos"))
    if not lines:
        out.append("Add at least one damaged SKU.")
    for ln in lines:
        if ln.category in DamageLine.PHOTO_NEEDED and not ln.photos.all():
            out.append(f"{ln.floor_sku} ({ln.get_category_display().lower()}) needs at least one photo.")
    to, _, missing = recipients()
    if not to:
        out.append("No approver e-mail: the CFA Manager fills the Depot Manager / Admin Officer under "
                   "Notification e-mails (Internal damage approval).")
    return out


def _attachments(report: DamageReport) -> list:
    return [p.preview or p.original for ln in report.lines.all() for p in ln.photos.select_related("preview", "original")]


@transaction.atomic
def submit(user: User, report_pk: int):
    from inward.iw import comms
    from inward.iw.models import MailMessage
    require(user, "DAMAGE_REPORT")
    report = DamageReport.objects.select_for_update().get(pk=report_pk)
    _editable(report)
    problems = submit_blockers(report)
    if problems:
        raise DamageError(" ".join(problems))
    to, cc, _ = recipients()
    report.revision += 1
    lines = list(report.lines.prefetch_related("photos"))
    mail = comms._queue(None, MailMessage.Purpose.DAMAGE_APPROVAL, None, report.tag, to, cc,
                        f"Internal damage approval {report.number} R{report.revision} - {len(lines)} SKU(s) - "
                        f"Depot {settings.INWARD['DEPOT_CODE']}",
                        "damage/mail/approval.html", {"r": report, "lines": lines, "reminder": False},
                        _attachments(report), user)
    report.status, report.request_mail, report.reminded_at = R.SUBMITTED, mail, None
    report.decided_at, report.decision_text, report.reply_from = None, "", ""
    report.save()
    audit.record("DAMAGE_APPROVAL_REQUESTED", "damage_report", report.pk, user=user,
                 after={"revision": report.revision, "to": to, "cc": cc, "stamp": mail.stamp,
                        "lines": [(ln.sku, ln.category, str(ln.quantity)) for ln in lines]})
    return mail


def apply_reply(raw: bytes, msg, sender: str, received, first_line: str, authenticated: bool) -> tuple[str, DamageReport | None]:
    """Called by the mail worker for a reply whose subject carries an IDR-APPR tag."""
    tag = TAG_RE.search(str(msg.get("Subject") or ""))
    report = DamageReport.objects.filter(number=tag.group(1)).first() if tag else None
    if report is None:
        return "no matching internal damage report", None
    if int(tag.group(2)) != report.revision:
        return f"ignored: reply to R{tag.group(2)}, the report is now at R{report.revision}", report
    if sender not in approver_addresses():
        return f"ignored: {sender} is not the registered Depot Manager / Admin Officer address", report
    if not authenticated:
        return "ignored: sender authentication (SPF/DKIM) did not pass", report
    word = first_line.split(None, 1)[0].strip(":.,-").upper() if first_line else ""
    if word not in ("APPROVED", "REJECTED"):
        return f"ignored: first line is not APPROVED / REJECTED ({first_line[:60]!r})", report
    if report.status != R.SUBMITTED:
        return f"ignored: {report} is {report.get_status_display().lower()}", report
    with transaction.atomic():
        report = DamageReport.objects.select_for_update().get(pk=report.pk)
        doc = documents.store(raw, filename=f"{report.tag} reply.eml", content_type="message/rfc822",
                              doc_type="INTERNAL_DAMAGE_REPLY", links=[("damage_report", report.pk, "approval_reply")])
        report.status = R.APPROVED if word == "APPROVED" else R.REJECTED
        report.decided_at = received or timezone.now()
        report.decision_text = (first_line[:500] if word == "APPROVED"
                                else (first_line.split(None, 1)[1:] or ["(no reason given)"])[0][:500])
        report.reply_from, report.reply = sender, doc
        report.save()
        audit.record("DAMAGE_" + report.status, "damage_report", report.pk,
                     after={"revision": report.revision, "from": sender, "text": report.decision_text,
                            "reply_sha256": doc.sha256})
    return report.status.lower(), report


def send_reminders(now=None) -> int:
    from inward.iw import comms
    from inward.iw.models import MailMessage
    now = now or timezone.now()
    hours = float(_flag("damage_approval_reminder_hours", 24))
    n = 0
    for report in DamageReport.objects.filter(status=R.SUBMITTED, reminded_at__isnull=True,
                                              request_mail__status=MailMessage.Status.SENT,
                                              request_mail__sent_at__lte=now - timedelta(hours=hours)):
        with transaction.atomic():
            req = report.request_mail
            comms._queue(None, MailMessage.Purpose.DAMAGE_REMINDER, None, f"{report.tag} REMINDER", list(req.to),
                         list(req.cc), f"REMINDER: {req.subject}", "damage/mail/approval.html",
                         {"r": report, "lines": list(report.lines.all()), "reminder": True},
                         list(req.attachments.all()), req.created_by)
            report.reminded_at = now
            report.save(update_fields=["reminded_at"])
            n += 1
    return n


def waiting_replies() -> bool:
    return DamageReport.objects.filter(status=R.SUBMITTED).exists()


@transaction.atomic
def cancel(user: User, report_pk: int, reason: str) -> DamageReport:
    require(user, "DAMAGE_REPORT")
    report = DamageReport.objects.select_for_update().get(pk=report_pk)
    if report.status not in (R.DRAFT, R.REJECTED, R.SUBMITTED):
        raise DamageError("Only a draft, rejected or still-unanswered report can be cancelled.")
    if not clean_text(reason):
        raise DamageError("A reason is mandatory.")
    before = report.status
    report.status, report.cancel_reason = R.CANCELLED, clean_text(reason)[:200]
    report.save(update_fields=["status", "cancel_reason", "updated_at"])
    audit.record("DAMAGE_REPORT_CANCELLED", "damage_report", report.pk, user=user, before={"status": before},
                 after={"reason": report.cancel_reason})
    return report

# ---------------------------------------------------------------- Oracle TRX and IRR reconciliation


@transaction.atomic
def add_trx(user: User, report_pk: int, trx_no: str) -> DamageTrx:
    from inward.iw.models import SedTrx
    require(user, "DAMAGE_TRX")
    report = DamageReport.objects.select_for_update().get(pk=report_pk)
    if report.status not in (R.APPROVED, R.TRANSFERRED):
        raise DamageError("The TRX No is entered after the approval (and before reconciliation is complete).")
    no = clean_text(trx_no).upper().replace(" ", "")
    if not re.match(r"^[A-Z0-9][A-Z0-9/\-]{0,39}$", no):
        raise DamageError("TRX No: enter the number as shown in Oracle.")
    clash = DamageTrx.objects.filter(trx_no=no).select_related("report").first()
    if clash:
        raise DamageError(f"TRX {no} is already entered on {clash.report}.")
    sed = SedTrx.objects.filter(trx_no=no).select_related("iw").first()
    if sed:
        raise DamageError(f"TRX {no} is an SED TRX of {sed.iw} - not an internal damage transfer.")
    t = DamageTrx.objects.create(report=report, trx_no=no, entered_by=user)
    if report.status == R.APPROVED:
        report.status = R.TRANSFERRED
        report.save(update_fields=["status", "updated_at"])
    audit.record("DAMAGE_TRX_ENTERED", "damage_report", report.pk, user=user, after={"trx_no": no})
    return t


@transaction.atomic
def remove_trx(user: User, trx_pk: int) -> None:
    require(user, "DAMAGE_TRX")
    t = DamageTrx.objects.select_related("report").get(pk=trx_pk)
    if t.report.status == R.RECONCILED:
        raise DamageError("The report is reconciled - the TRX can no longer be removed.")
    audit.record("DAMAGE_TRX_REMOVED", "damage_report", t.report_id, user=user, before={"trx_no": t.trx_no})
    report = t.report
    t.delete()
    if not report.trx.exists() and report.status == R.TRANSFERRED:
        report.status = R.APPROVED
        report.save(update_fields=["status", "updated_at"])


def _reason_code(text: str) -> str:
    return (text or "").split("/", 1)[0].strip()


def _issue_rows(no: str, notes: list[str]):
    """The Issue side of a Subinventory Transfer for this TRX (either number of the pair may be entered)."""
    from inward.irr.models import IrrTransaction
    found = list(IrrTransaction.objects.filter(trx_number=no))
    if not found:
        notes.append(f"TRX {no} is not in the imported IRR yet.")
        return []
    if any(r.type_key != "SUBINVENTORY TRANSFER" for r in found):
        notes.append(f"TRX {no} is {found[0].type} - expected a Subinventory Transfer into DMG.")
        return None
    issue = [r for r in found if r.transaction_type.upper() == "ISSUE"]
    if not issue and no.isdigit():
        for d in (-1, -2, -3, 1, 2, 3):
            cand = [r for r in IrrTransaction.objects.filter(trx_number=str(int(no) + d), type_key="SUBINVENTORY TRANSFER")
                    if r.transaction_type.upper() == "ISSUE"]
            if cand and {c.sku for c in cand} == {r.sku for r in found}:
                issue = cand
                break
    if not issue:
        notes.append(f"TRX {no}: the Issue half of the transfer was not found.")
        return None
    return issue


def reconcile(report: DamageReport) -> dict:
    """Per SKU: report pieces vs the IRR transfers into DMG. FULL / PARTIAL / REVIEW / NOT_FOUND / PENDING."""
    notes, flags = [], []
    expected = defaultdict(lambda: ZERO)
    names = {}
    for ln in report.lines.all():
        expected[ln.sku] += ln.quantity
        names[ln.sku] = (ln.floor_sku, ln.description)
    actual, places, bad = defaultdict(lambda: ZERO), defaultdict(list), False
    trx = [t.trx_no for t in report.trx.all()]
    ok_codes = accepted_codes(report)
    found_any = False
    for no in trx:
        rows = _issue_rows(no, notes)
        if rows is None:
            bad = True
            continue
        if rows:
            found_any = True
        for r in rows:
            if not r.dest_subinv.endswith("DMG"):
                bad = True
                notes.append(f"TRX {no}: {r.sku} goes to {r.dest_subinv or '-'}, not to the DMG sub-inventory.")
            code = _reason_code(r.reason_description)
            if code not in ok_codes and code not in [f["code"] for f in flags]:
                flags.append({"code": code, "text": r.reason_description, "trx": no})
            actual[r.sku] += r.no_of_pack
            places[r.sku].append(f"{r.source_subinv} {r.source_locator.strip(chr(39) + ' ')} -> {r.dest_subinv} ({no})")
    lines = []
    for sku in sorted(set(expected) | set(actual)):
        e, a = expected.get(sku, ZERO), actual.get(sku, ZERO)
        st = "FULL" if e == a else "PARTIAL" if a < e else "REVIEW"
        if not e:
            notes.append(f"{sku} is in the IRR transfer but not on the report.")
        lines.append({"sku": sku, "floor": names.get(sku, (floor_sku(sku), ""))[0],
                      "description": names.get(sku, ("", ""))[1], "expected": format(e.normalize(), "f"),
                      "actual": format(a.normalize(), "f"), "status": st, "from": places.get(sku, [])})
    if not trx:
        status = "PENDING"
    elif not found_any and not bad:
        status = "NOT_FOUND"
    elif bad or any(ln["status"] == "REVIEW" for ln in lines):
        status = "REVIEW"
    elif any(ln["status"] == "PARTIAL" for ln in lines):
        status = "PARTIAL"
    else:
        status = "FULL"
    return {"status": status, "lines": lines, "notes": notes, "reason_flags": flags,
            "at": timezone.now().isoformat(), "trx": trx}


@transaction.atomic
def run_recon(user: User | None, report_pk: int) -> DamageReport:
    if user is not None:
        require(user, "DAMAGE_TRX")
    report = DamageReport.objects.select_for_update().get(pk=report_pk)
    if report.status not in (R.TRANSFERRED, R.RECONCILED):
        raise DamageError("Enter the Oracle TRX No first.")
    res = reconcile(report)
    report.recon = res
    done = res["status"] == "FULL" and not res["reason_flags"]
    report.status = R.RECONCILED if done else R.TRANSFERRED
    report.save(update_fields=["recon", "status", "updated_at"])
    audit.record("DAMAGE_RECONCILED" if done else "DAMAGE_RECON_RUN", "damage_report", report.pk, user=user,
                 after={"status": res["status"], "flags": [f["code"] for f in res["reason_flags"]]})
    return report


@transaction.atomic
def accept_reason(user: User, report_pk: int, code: str) -> DamageReport:
    """The supervisor decides on a reason code outside the accepted list: accepted for this report (audited)."""
    require(user, "DAMAGE_TRX")
    report = DamageReport.objects.select_for_update().get(pk=report_pk)
    code = clean_text(code)
    if not code:
        raise DamageError("No reason code.")
    if code not in report.accepted_reasons:
        report.accepted_reasons = [*report.accepted_reasons, code]
        report.save(update_fields=["accepted_reasons", "updated_at"])
    audit.record("DAMAGE_REASON_ACCEPTED", "damage_report", report.pk, user=user, after={"code": code})
    return run_recon(user, report.pk)


def reconcile_open(user=None) -> int:
    """After every IRR import: reports waiting for the IRR are reconciled again."""
    n = 0
    for pk in DamageReport.objects.filter(status=R.TRANSFERRED).values_list("pk", flat=True):
        try:
            run_recon(None, pk)
            n += 1
        except DamageError:
            pass
    return n
