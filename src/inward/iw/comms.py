"""Screen 04 part D - mandatory communications, SED approval and the Screen 04 gates (SoT 7.6 - 7.8, 14.2).

On "Send MRA" the app queues: the MRA e-mail to the source (always, PDF attached), and for any SED the internal SED
notification (Depot Manager + Admin Officer + CFA Manager) and the SED approval request (To Depot Manager, CC
Admin Officer). Only the worker talks to Gmail; a mail counts only when it is found in Gmail 'Sent Mail'.
When G04-DMG, G04-ULC, G04-MAIL and G04-SEDMAIL all pass the IW moves to MRA_ISSUED. A mandatory mail pending
longer than the configured hours may be released by the CFA Manager (reason mandatory; the mail stays queued).
The Depot Manager approves by replying APPROVED or REJECTED + reason on the first line; the approval is tracked
and shown, never blocking.
"""
from __future__ import annotations

import email
import email.policy
import logging
import re
import smtplib
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from inward.accounts.models import User
from inward.accounts.services import require
from inward.core import audit, branding, documents
from inward.core.models import (
    Depot, EmailStatus, FeatureFlag, NotificationRecipient, Organisation, OrganisationEmail, Sender, Vendor,
)
from inward.iw import mra as mra_service
from inward.iw import sed, unloading
from inward.iw.models import InboundMail, Iw, MailMessage, MailRelease, MraRevision, SedApproval, SedPhoto
from inward.iw.services import IwError

log = logging.getLogger("inward.mail")
M = MailMessage.Status
P = MailMessage.Purpose
A = SedApproval.Status
TAG_RE = re.compile(r"SED-APPR (IW-\d{4}-\d{6}) R(\d+)")
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
INBOUND_TS = {"INBOUND_TS", "INTER_DEPOT_INBOUND_TS"}
MAX_ATTEMPTS = 3


def _flag(key: str, default):
    f = FeatureFlag.objects.filter(key=key).first()
    v = (f.value or {}).get("value") if f else None
    return default if v is None else v


def _clean_emails(text: str) -> list[str]:
    out = []
    for part in re.split(r"[,;\s]+", text or ""):
        part = part.strip().lower()
        if part:
            if not EMAIL_RE.match(part):
                raise IwError(f"'{part}' is not a valid e-mail address.")
            out.append(part)
    return out

# ---------------------------------------------------------------- recipients


@dataclass
class Target:
    """Who the MRA goes to: the source organisation, the vendor or the Non-Stock sender(s)."""
    kind: str      # ORG / VENDOR / SENDER
    key: str       # org code / vendor code / sender pk
    name: str
    to: list[str]
    cc: list[str]
    unverified: list[str]


def mra_targets(iw: Iw) -> list[Target]:
    out = []
    live = OrganisationEmail.objects.exclude(status=EmailStatus.RETIRED)
    if iw.load_type.is_non_stock:
        for ref in iw.non_stock_refs.select_related("sender"):
            s = ref.sender
            if all(t.key != str(s.pk) for t in out):
                out.append(Target("SENDER", str(s.pk), s.name, [s.email] if s.email else [], [],
                                  [s.email] if s.email and s.status == EmailStatus.UNVERIFIED else []))
    elif iw.load_type.is_vendor:
        for c in iw.vendor_consignments.select_related("vendor"):
            v = c.vendor
            if all(t.key != v.code for t in out):
                out.append(Target("VENDOR", v.code, f"{v.code} {v.name}", [v.email] if v.email else [], [],
                                  [v.email] if v.email and v.email_status == EmailStatus.UNVERIFIED else []))
    elif iw.source_org_id:
        rows = list(live.filter(organisation_id=iw.source_org_id))
        out.append(Target("ORG", iw.source_org_id, str(iw.source_org), [r.email for r in rows if r.kind == "TO"],
                          [r.email for r in rows if r.kind == "CC"],
                          [r.email for r in rows if r.status == EmailStatus.UNVERIFIED]))
    return out


def origin_cc(iw: Iw) -> list[str]:
    """Inbound transhipment: the Origin Org (factory on the SIT) is copied (SoT 23)."""
    if iw.load_type_id not in INBOUND_TS:
        return []
    origins = {l.ship.from_org_code for l in iw.ship_links.select_related("ship")} - {iw.source_org_id}
    return sorted(set(OrganisationEmail.objects.exclude(status=EmailStatus.RETIRED)
                      .filter(organisation_id__in=origins).values_list("email", flat=True)))


def internal(purpose: str) -> tuple[list[str], list[str], list[str]]:
    """(to, cc, missing role labels) from the Notification Master."""
    to, cc, missing = [], [], []
    for r in NotificationRecipient.objects.filter(purpose=purpose, active=True).order_by("id"):
        if not r.email:
            if r.mandatory:
                missing.append(r.role_label)
            continue
        (to if r.kind == "TO" else cc).append(r.email.lower())
    return to, cc, missing


@transaction.atomic
def capture_email(user: User, iw_pk: int, kind: str, key: str, address: str) -> None:
    """A missing MRA address entered on the spot: used at once, marked Unverified for the CFA Manager."""
    require(user, "MRA_ISSUE")
    addr = _clean_emails(address)
    if len(addr) != 1:
        raise IwError("Enter one e-mail address.")
    addr = addr[0]
    if kind == "ORG":
        org = Organisation.objects.get(pk=key)
        OrganisationEmail.objects.get_or_create(organisation=org, email=addr, kind="TO",
                                                defaults={"status": EmailStatus.UNVERIFIED, "entered_by": user})
    elif kind == "VENDOR":
        Vendor.objects.filter(pk=key).update(email=addr, email_status=EmailStatus.UNVERIFIED)
    elif kind == "SENDER":
        Sender.objects.filter(pk=key).update(email=addr, status=EmailStatus.UNVERIFIED)
    else:
        raise IwError("Unknown recipient.")
    audit.record("EMAIL_CAPTURED", kind.lower(), key, user=user, iw_id=iw_pk, after={"email": addr, "status": "UNVERIFIED"})


def unverified_emails() -> list[dict]:
    out = [{"kind": "ORG", "pk": e.pk, "owner": str(e.organisation), "email": e.email,
            "by": e.entered_by.username if e.entered_by else ""}
           for e in OrganisationEmail.objects.filter(status=EmailStatus.UNVERIFIED).select_related("organisation",
                                                                                                  "entered_by")]
    out += [{"kind": "VENDOR", "pk": v.pk, "owner": str(v), "email": v.email, "by": ""}
            for v in Vendor.objects.filter(email_status=EmailStatus.UNVERIFIED).exclude(email="")]
    out += [{"kind": "SENDER", "pk": s.pk, "owner": s.name, "email": s.email, "by": ""}
            for s in Sender.objects.filter(status=EmailStatus.UNVERIFIED).exclude(email="")]
    return out


@transaction.atomic
def verify_email(user: User, kind: str, pk: str, corrected: str = "") -> None:
    require(user, "EMAIL_VERIFY")
    new = _clean_emails(corrected)[0] if corrected.strip() else None
    if kind == "ORG":
        e = OrganisationEmail.objects.select_for_update().get(pk=pk)
        before = e.email
        if new and new != e.email:
            e.email = new
        e.status, e.verified_by = EmailStatus.VERIFIED, user
        e.save()
    elif kind == "VENDOR":
        v = Vendor.objects.select_for_update().get(pk=pk)
        before = v.email
        v.email, v.email_status = new or v.email, EmailStatus.VERIFIED
        v.save(update_fields=["email", "email_status"])
    else:
        s = Sender.objects.select_for_update().get(pk=pk)
        before = s.email
        s.email, s.status = new or s.email, EmailStatus.VERIFIED
        s.save(update_fields=["email", "status"])
    audit.record("EMAIL_VERIFIED", kind.lower(), pk, user=user, before={"email": before}, after={"email": new or before})

# ---------------------------------------------------------------- queueing


def _branding(iw: Iw | None) -> dict:
    depot = Depot.objects.filter(code=settings.INWARD["DEPOT_CODE"]).first()
    content = (iw.mras.first().content if iw and iw.mras.exists() else {}) or {}
    if not content.get("depot") and depot:
        from inward.web.context import depot_info
        info = depot_info()
        content = {"depot": {"company": depot.company_name, "code": depot.code, "name": depot.name, "cfa": info["cfa"],
                             "seal": branding.seal_lines(depot.company_name, info["cfa"], depot.code, depot.name)}}
    d = content.get("depot") or {}
    return {"company": d.get("company") or (depot.company_name if depot else ""),
            "depot_code": d.get("code") or settings.INWARD["DEPOT_CODE"], "depot_name": d.get("name", ""),
            "cfa": d.get("cfa", ""), "seal": d.get("seal") or {}}


def _queue(iw: Iw | None, purpose: str, mra: MraRevision | None, tag: str, to: list[str], cc: list[str], subject: str,
           template: str, context: dict, attachments, user: User) -> MailMessage:
    from inward.mail.gmail import new_message_id
    m = MailMessage.objects.create(iw=iw, mra=mra, purpose=purpose, to=to, cc=[c for c in cc if c not in to],
                                   subject=subject, html_body="", created_by=user)
    m.stamp = f"{tag} #C-{m.pk:06d}"
    m.message_id = new_message_id(settings.INWARD["MESSAGE_ID_DOMAIN"])
    m.html_body = render_to_string(template, {"iw": iw, "mra": mra, "b": _branding(iw), "stamp": m.stamp, **context})
    m.save(update_fields=["stamp", "message_id", "html_body"])
    m.attachments.set(attachments)
    return m


def _photos(iw: Iw) -> list:
    return [p.preview or p.original for p in SedPhoto.objects.filter(line__iw=iw).select_related("preview", "original")]


def send_blockers(iw: Iw) -> list[str]:
    out = []
    last = iw.mras.first()
    if iw.status != Iw.Status.PHYSICALLY_VERIFIED:
        out.append("The MRA can be sent after physical verification and before it is already issued.")
    if not unloading.gate_ok(iw):
        out.append("Record the unloading charge and its payment position first (mandatory before the MRA).")
    if last is None:
        out.append("Issue the MRA first.")
    elif not mra_service.is_current(iw, last):
        out.append(f"The details changed after {last.label} - issue a new revision first.")
    elif last.pdf_id is None:
        out.append("The MRA PDF is made on the server - send it from the server (uat / live).")
    elif last.mails.filter(purpose=P.MRA).exclude(status=M.FAILED).exists():
        out.append(f"{last.label} has already been sent or queued.")
    targets = mra_targets(iw)
    if not targets:
        out.append("No MRA recipient: set the source organisation on the IW.")
    for t in targets:
        if not t.to:
            out.append(f"No e-mail for {t.name} - enter it below.")
    if sed.totals(iw).any:
        for purpose, label in (("SED_IMMEDIATE", "SED notification"), ("SED_APPROVAL", "SED approval request")):
            missing = internal(purpose)[2]
            if missing:
                out.append(f"{label}: set the e-mail of {', '.join(missing)} in the Notification Master.")
    return out


@transaction.atomic
def send_mra(user: User, iw_pk: int, extra_cc: str = "") -> list[MailMessage]:
    require(user, "MRA_ISSUE")
    iw = Iw.objects.select_for_update(of=("self",)).select_related("load_type", "source_org", "transporter").get(pk=iw_pk)
    problems = send_blockers(iw)
    if problems:
        raise IwError(" ".join(problems))
    rev = iw.mras.first()
    extra = _clean_emails(extra_cc)
    to, cc = [], []
    for t in mra_targets(iw):
        to += [e for e in t.to if e not in to]
        cc += [e for e in t.cc if e not in cc]
    cc += [e for e in origin_cc(iw) + extra if e not in cc and e not in to]
    c = rev.content
    base = f"{iw.iw_no} MRA-{rev.label}"
    mails = [_queue(iw, P.MRA, rev, base, to, cc,
                    f"MRA {iw.iw_no} {rev.label} - {iw.vehicle_no} - Depot {c['depot']['code']} {c['depot']['name']}",
                    "iw/mail/mra.html", {"c": c}, [rev.pdf], user)]
    if sed.totals(iw).any:
        photos = _photos(iw)
        n_to, n_cc, _ = internal("SED_IMMEDIATE")
        mails.append(_queue(iw, P.SED_NOTIFY, rev, f"{iw.iw_no} SED-{rev.label}", n_to, n_cc,
                            f"SED {iw.iw_no} {rev.label} - claimable Rs {c['sed_totals']['claimable']} - {iw.vehicle_no}",
                            "iw/mail/sed_notify.html", {"c": c}, [rev.pdf, *photos], user))
        a_to, a_cc, _ = internal("SED_APPROVAL")
        req = _queue(iw, P.SED_APPROVAL, rev, f"SED-APPR {iw.iw_no} {rev.label}", a_to, a_cc,
                     f"SED Approval Request {iw.iw_no} {rev.label} - {iw.vehicle_no}",
                     "iw/mail/sed_approval.html", {"c": c, "reminder": False}, [rev.pdf, *photos], user)
        mails.append(req)
        SedApproval.objects.create(iw=iw, mra=rev, request_mail=req)
    audit.record("MRA_MAILS_QUEUED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"revision": rev.label, "mails": [{"purpose": m.purpose, "stamp": m.stamp, "to": m.to, "cc": m.cc}
                                                         for m in mails]})
    return mails

# ---------------------------------------------------------------- gates and completion


@dataclass
class Gate:
    code: str
    label: str
    ok: bool
    detail: str = ""


def _mail_state(rev: MraRevision | None, purpose: str) -> MailMessage | None:
    return rev.mails.filter(purpose=purpose).order_by("-id").first() if rev else None


def gates(iw: Iw) -> list[Gate]:
    rev = iw.mras.first()
    dmg = [b for b in sed.verification_blockers(iw) if "photo" in b]
    out = [Gate("G04-DMG", "Damage photos and remarks", not dmg, " ".join(dmg)),
           Gate("G04-ULC", "Unloading amount and payment position", unloading.gate_ok(iw))]
    m = _mail_state(rev, P.MRA)
    out.append(Gate("G04-MAIL", "MRA e-mail SENT", bool(m and m.status == M.SENT),
                    m.get_status_display() if m else "not sent"))
    if sed.totals(iw).any:
        n = _mail_state(rev, P.SED_NOTIFY)
        out.append(Gate("G04-SEDMAIL", "Internal SED notification SENT", bool(n and n.status == M.SENT),
                        n.get_status_display() if n else "not sent"))
    return out


def try_complete(iw: Iw, user: User | None = None) -> bool:
    """Move to MRA_ISSUED when every Screen 04 gate passes (called after a mail is confirmed SENT)."""
    iw = Iw.objects.select_for_update().get(pk=iw.pk)
    if iw.status != Iw.Status.PHYSICALLY_VERIFIED or not all(g.ok for g in gates(iw)):
        return False
    iw.status = Iw.Status.MRA_ISSUED
    iw.version += 1
    iw.save(update_fields=["status", "version", "updated_at"])
    audit.record("SCREEN04_COMPLETED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"revision": iw.mras.first().label, "gates": [g.code for g in gates(iw)]})
    return True


def release_blockers(iw: Iw) -> list[str]:
    out = []
    if iw.status != Iw.Status.PHYSICALLY_VERIFIED:
        out.append("Only an IW waiting for its mails can be released.")
        return out
    gs = {g.code: g for g in gates(iw)}
    for code in ("G04-DMG", "G04-ULC"):
        if not gs[code].ok:
            out.append(f"{gs[code].label} is not complete - it cannot be released.")
    rev = iw.mras.first()
    pending = [m for m in (rev.mails.filter(purpose__in=[P.MRA, P.SED_NOTIFY]) if rev else []) if m.status != M.SENT]
    if not rev or not rev.mails.filter(purpose=P.MRA).exists():
        out.append("The MRA has not been sent yet.")
    elif not pending:
        out.append("All mandatory mails are already SENT.")
    else:
        hours = float(_flag("mail_pending_release_after_hours", 2))
        oldest = min(m.created_at for m in pending)
        if timezone.now() - oldest < timedelta(hours=hours):
            out.append(f"A mail must be pending for {hours:g} hours before release "
                       f"(queued {timezone.localtime(oldest):%d-%b %H:%M}).")
    return out


@transaction.atomic
def release(user: User, iw_pk: int, reason: str) -> Iw:
    require(user, "MAIL_PENDING_RELEASE")
    iw = Iw.objects.select_for_update().get(pk=iw_pk)
    if not (reason or "").strip():
        raise IwError("A reason is mandatory for Mail-Pending Release.")
    problems = release_blockers(iw)
    if problems:
        raise IwError(" ".join(problems))
    rev = iw.mras.first()
    pending = [m.stamp for m in rev.mails.filter(purpose__in=[P.MRA, P.SED_NOTIFY]).exclude(status=M.SENT)]
    MailRelease.objects.create(iw=iw, reason=reason.strip(), pending=pending, released_by=user)
    iw.status = Iw.Status.MRA_ISSUED
    iw.version += 1
    iw.save(update_fields=["status", "version", "updated_at"])
    audit.record("MAIL_PENDING_RELEASED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"reason": reason.strip(), "pending": pending})
    return iw

# ---------------------------------------------------------------- SED approval: status, manual evidence


def approval(iw: Iw) -> SedApproval | None:
    return iw.sed_approvals.select_related("mra", "request_mail").first()


@transaction.atomic
def upload_evidence(user: User, approval_pk: int, data: bytes, filename: str) -> SedApproval:
    """Depot Manager approved from another address / channel: the clerk uploads the .eml or PDF."""
    require(user, "MRA_ISSUE")
    ap = SedApproval.objects.select_for_update().get(pk=approval_pk)
    if ap.status not in (A.REQUESTED, A.REJECTED, A.MANUAL_PENDING):
        raise IwError("This SED approval is already on file.")
    name = filename.lower()
    if not (name.endswith(".eml") or name.endswith(".pdf")):
        raise IwError("Upload the approval e-mail as .eml or PDF.")
    if len(data) > 20 * 1024 * 1024:
        raise IwError("The file is larger than 20 MB.")
    doc = documents.store(data, filename=filename, content_type="message/rfc822" if name.endswith(".eml")
                          else "application/pdf", doc_type="SED_APPROVAL_EVIDENCE", user=user,
                          links=[("iw", ap.iw_id, "sed_approval_evidence")])
    ap.status, ap.evidence, ap.evidence_by = A.MANUAL_PENDING, doc, user
    ap.save(update_fields=["status", "evidence", "evidence_by"])
    audit.record("SED_APPROVAL_EVIDENCE_UPLOADED", "iw", ap.iw_id, user=user, iw_id=ap.iw_id,
                 after={"revision": ap.mra.label, "document": str(doc.pk)})
    return ap


@transaction.atomic
def decide_evidence(user: User, approval_pk: int, accept: bool) -> SedApproval:
    require(user, "SED_EVIDENCE_ACCEPT")
    ap = SedApproval.objects.select_for_update().get(pk=approval_pk)
    if ap.status != A.MANUAL_PENDING:
        raise IwError("No manual evidence is waiting for a check.")
    ap.status = A.MANUAL_ACCEPTED if accept else A.REQUESTED
    ap.accepted_by = user if accept else None
    ap.decided_at = timezone.now() if accept else None
    ap.save(update_fields=["status", "accepted_by", "decided_at"])
    audit.record("SED_APPROVAL_EVIDENCE_" + ("ACCEPTED" if accept else "REJECTED"), "iw", ap.iw_id, user=user,
                 iw_id=ap.iw_id, after={"revision": ap.mra.label})
    if accept:
        _audit_memo(ap.iw, "manual approval evidence accepted", user)
    return ap


def _audit_memo(iw: Iw, why: str, user=None) -> None:
    """Refresh the SED Audit Memo; never let a PDF problem stop the mail / approval flow."""
    from inward.iw import audit_memo
    try:
        audit_memo.regenerate(iw, user, why)
    except Exception as exc:  # noqa: BLE001
        log.warning("SED audit memo for %s: %s", iw.pk, exc)

# ---------------------------------------------------------------- worker: outbox


def _outgoing(m: MailMessage, tmp: Path):
    from inward.mail.transport import OutgoingMessage
    paths = []
    for doc in m.attachments.all():
        p = tmp / doc.original_filename
        p.write_bytes(documents.read(doc))
        paths.append(p)
    logo = branding.logo_data_uri()
    inline = {}
    if logo:
        import base64
        inline["logo"] = base64.b64decode(logo.split(",", 1)[1])
    return OutgoingMessage(stamp=m.stamp, message_id=m.message_id, to=list(m.to), cc=list(m.cc), subject=m.subject,
                           html_body=m.html_body, attachments=paths, inline_images=inline)


PERMANENT = (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
             smtplib.SMTPDataError, ValueError)


def _mark_sent(m: MailMessage, ev) -> None:
    doc = documents.store(ev.eml, filename=f"{m.stamp.replace('#', '').replace(' ', '_')}.eml",
                          content_type="message/rfc822", doc_type="MAIL_SENT_EML",
                          links=[("iw", m.iw_id, f"mail_{m.purpose.lower()}")] if m.iw_id else [])
    m.status, m.sent_at, m.eml, m.last_error = M.SENT, ev.sent_at, doc, ""
    m.save(update_fields=["status", "sent_at", "eml", "last_error"])
    audit.record("MAIL_SENT", "iw", m.iw_id, iw_id=m.iw_id,
                 after={"purpose": m.purpose, "stamp": m.stamp, "message_id": ev.message_id, "to": ev.to, "cc": ev.cc,
                        "eml_sha256": doc.sha256,
                        "attachments": [{"name": d.original_filename, "sha256": d.sha256} for d in m.attachments.all()]})
    if m.iw_id:
        with transaction.atomic():
            try_complete(m.iw)
        if m.purpose == P.SED_APPROVAL:
            _audit_memo(m.iw, "SED approval request sent")


def process_outbox(transport, now=None) -> dict:
    now = now or timezone.now()
    stats = {"sent": 0, "queued": 0, "failed": 0}
    timeout = timedelta(minutes=settings.INWARD.get("MAIL_CONFIRM_TIMEOUT_MIN", 30))
    for m in MailMessage.objects.filter(status__in=[M.QUEUED, M.FAILED_UNCONFIRMED]).order_by("id"):
        try:
            ev = transport.confirm_sent(_shell(m))
        except Exception as exc:  # noqa: BLE001 - Gmail unreachable: try again next round
            log.warning("confirm %s: %s", m.stamp, exc)
            continue
        if ev:
            _mark_sent(m, ev)
            stats["sent"] += 1
        elif m.status == M.QUEUED and m.queued_at and now - m.queued_at > timeout:
            m.status = M.FAILED_UNCONFIRMED
            m.last_error = f"Not found in Sent Mail within {timeout.seconds // 60} minutes"
            m.save(update_fields=["status", "last_error"])
            audit.record("MAIL_UNCONFIRMED", "iw", m.iw_id, iw_id=m.iw_id, after={"stamp": m.stamp, "attempts": m.attempts})
    for m in MailMessage.objects.filter(status__in=[M.PENDING, M.FAILED_UNCONFIRMED], attempts__lt=MAX_ATTEMPTS).order_by("id"):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                transport.send(_outgoing(m, Path(tmp)))
            except PERMANENT as exc:
                m.status, m.last_error = M.FAILED, f"{type(exc).__name__}: {exc}"[:2000]
                m.save(update_fields=["status", "last_error"])
                audit.record("MAIL_FAILED", "iw", m.iw_id, iw_id=m.iw_id, after={"stamp": m.stamp, "error": m.last_error})
                stats["failed"] += 1
                continue
            except Exception as exc:  # noqa: BLE001 - outage: stays in the queue (SoT 14.2)
                m.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                m.save(update_fields=["last_error"])
                log.warning("send %s: %s", m.stamp, exc)
                continue
        m.status, m.queued_at, m.attempts, m.last_error = M.QUEUED, timezone.now(), m.attempts + 1, ""
        m.save(update_fields=["status", "queued_at", "attempts", "last_error"])
        stats["queued"] += 1
    return stats


def _shell(m: MailMessage):
    from inward.mail.transport import OutgoingMessage
    return OutgoingMessage(stamp=m.stamp, message_id=m.message_id, to=list(m.to), cc=list(m.cc), subject=m.subject,
                           html_body="")


@transaction.atomic
def retry(user: User, mail_pk: int) -> MailMessage:
    """Put a FAILED mail (e.g. after fixing the address or the Gmail password) back in the queue."""
    require(user, "MRA_ISSUE")
    m = MailMessage.objects.select_for_update().get(pk=mail_pk)
    if m.status not in (M.FAILED, M.FAILED_UNCONFIRMED):
        raise IwError("Only a failed mail can be retried.")
    m.status, m.attempts = M.PENDING, 0
    m.save(update_fields=["status", "attempts"])
    audit.record("MAIL_RETRY", "iw", m.iw_id, user=user, iw_id=m.iw_id, after={"stamp": m.stamp})
    return m

# ---------------------------------------------------------------- worker: approval replies and reminders


def _first_line(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    text = part.get_content() if part else ""
    if part is not None and part.get_content_type() == "text/html":
        text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(">"):
            return line
    return ""


def _authenticated(msg) -> bool:
    results = " ".join(str(h) for h in (msg.get_all("Authentication-Results") or [])).lower()
    if "dmarc=fail" in results:
        return False
    return "spf=pass" in results or "dkim=pass" in results


def _manager_addresses() -> set[str]:
    addrs = {r.email.lower() for r in NotificationRecipient.objects.filter(purpose="SED_APPROVAL", kind="TO",
                                                                           active=True).exclude(email="")}
    if settings.INWARD["MODE"] == "TEST":  # test replies come from the test mailbox
        addrs |= {a.lower() for a in settings.INWARD["TEST_MAIL_TO"]}
    return addrs


def handle_reply(raw: bytes) -> str:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    mid = (msg.get("Message-ID") or "").strip()
    subject = str(msg.get("Subject") or "")
    if not mid or InboundMail.objects.filter(message_id=mid).exists():
        return "skip"
    sender = parseaddr(str(msg.get("From") or ""))[1].lower()
    if msg.get("X-Inward-Stamp"):
        return "skip"  # one of our own requests / reminders seen in the inbox
    received = parsedate_to_datetime(msg["Date"]) if msg.get("Date") else None
    tag = TAG_RE.search(subject)
    ap = None
    if tag:
        ap = (SedApproval.objects.select_related("iw", "mra")
              .filter(iw__iw_no=tag.group(1), mra__revision=int(tag.group(2))).first())
    if ap is None:
        outcome = "no matching SED approval request"
    elif sender not in _manager_addresses():
        outcome = f"ignored: {sender} is not the registered Depot Manager address"
    elif not _authenticated(msg):
        outcome = "ignored: sender authentication (SPF/DKIM) did not pass"
    else:
        first = _first_line(msg)
        word = first.split(None, 1)[0].strip(":.,-").upper() if first else ""
        if word not in ("APPROVED", "REJECTED"):
            outcome = f"ignored: first line is not APPROVED / REJECTED ({first[:60]!r})"
        elif ap.status in (A.APPROVED, A.MANUAL_ACCEPTED):
            outcome = "already approved"
        else:
            doc = documents.store(raw, filename=f"SED-APPR {ap.iw.iw_no} {ap.mra.label} reply.eml",
                                  content_type="message/rfc822", doc_type="SED_APPROVAL_REPLY",
                                  links=[("iw", ap.iw_id, "sed_approval_reply")])
            ap.status = A.APPROVED if word == "APPROVED" else A.REJECTED
            ap.decided_at = received or timezone.now()
            ap.decision_text = first[:500] if word == "APPROVED" else (first.split(None, 1)[1:] or ["(no reason given)"])[0][:500]
            ap.reply_from, ap.evidence = sender, doc
            ap.save(update_fields=["status", "decided_at", "decision_text", "reply_from", "evidence"])
            audit.record("SED_APPROVAL_" + ap.status, "iw", ap.iw_id, iw_id=ap.iw_id,
                         after={"revision": ap.mra.label, "from": sender, "text": ap.decision_text,
                                "closed_iw": ap.iw.status == Iw.Status.CLOSED})
            outcome = ap.status.lower()
            _audit_memo(ap.iw, f"Depot Manager reply: {outcome}")
    InboundMail.objects.create(message_id=mid[:250], subject=subject[:250], sender=sender[:256], received_at=received,
                               outcome=outcome[:200], approval=ap if ap and outcome in ("approved", "rejected") else None)
    return outcome


def poll_replies(transport) -> list[str]:
    return [handle_reply(raw) for raw in transport.fetch_raw("SED-APPR")]


def send_reminders(now=None) -> int:
    now = now or timezone.now()
    hours = float(_flag("sed_approval_reminder_hours", 24))
    n = 0
    for ap in SedApproval.objects.filter(status=A.REQUESTED, reminded_at__isnull=True,
                                         request_mail__status=M.SENT,
                                         request_mail__sent_at__lte=now - timedelta(hours=hours)).select_related("iw", "mra"):
        with transaction.atomic():
            req = ap.request_mail
            _queue(ap.iw, P.SED_REMINDER, ap.mra, f"SED-APPR {ap.iw.iw_no} {ap.mra.label} REMINDER", list(req.to),
                   list(req.cc), f"REMINDER: {req.subject}", "iw/mail/sed_approval.html",
                   {"c": ap.mra.content, "reminder": True}, list(req.attachments.all()), req.created_by)
            ap.reminded_at = now
            ap.save(update_fields=["reminded_at"])
            n += 1
    return n


def run_once(transport, read_inbox: bool = True) -> dict:
    from inward.iw import reports
    stats = {"reports": len(reports.due_reports())}
    if MailMessage.objects.exclude(status__in=[M.SENT, M.FAILED]).exists():
        stats.update(process_outbox(transport))  # Gmail is contacted only when something waits
    stats["reminders"] = send_reminders()
    if not read_inbox or not SedApproval.objects.filter(status__in=[A.REQUESTED, A.REJECTED]).exists():
        return stats
    try:
        stats["replies"] = poll_replies(transport)
    except Exception as exc:  # noqa: BLE001
        log.warning("reply poll: %s", exc)
    return stats

