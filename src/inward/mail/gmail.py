"""Gmail transport (SoT 14.2): SMTP to send, IMAP to confirm in Sent Mail and to read the inbox.

Uses the dedicated RAINBOW LOGISTICS Gmail account with 2-Step Verification and an App Password.
Only the worker process uses this; web requests never talk to Gmail directly.
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import mimetypes
import re
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid, parsedate_to_datetime
from pathlib import Path

from inward.mail.transport import FetchedMail, OutgoingMessage, SentEvidence

SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 587
IMAP_HOST = "imap.gmail.com"
STAMP_HEADER = "X-Inward-Stamp"
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def new_message_id(domain: str) -> str:
    return make_msgid(idstring="inward", domain=domain)


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class GmailTransport:
    def __init__(self, address: str, app_password: str, timeout: int = 30, test_redirect: list[str] | None = None):
        """test_redirect: in TEST mode every message goes only to these addresses, marked [TEST]."""
        if not address or not app_password:
            raise ValueError("Gmail address and App Password must be configured")
        self.address = address
        self._password = app_password.replace(" ", "")
        self.timeout = timeout
        self.test_redirect = test_redirect

    # ------------------------------------------------------------ sending
    def build(self, message: OutgoingMessage, sent_at: datetime | None = None) -> EmailMessage:
        if not message.to:
            raise ValueError("At least one recipient is required")
        msg = EmailMessage()
        msg["From"] = self.address
        html = message.html_body
        if self.test_redirect is not None:  # TEST mode: never reaches real recipients
            msg["To"] = ", ".join(self.test_redirect)
            msg["X-Inward-Original-To"] = ", ".join(message.to)
            if message.cc:
                msg["X-Inward-Original-Cc"] = ", ".join(message.cc)
            msg["Subject"] = "[TEST] " + message.stamped_subject
            html = (f"<p><b>TEST MODE</b> - would have gone to: {', '.join(message.to)}"
                    f"{'; cc ' + ', '.join(message.cc) if message.cc else ''}</p><hr>" + html)
        else:
            msg["To"] = ", ".join(message.to)
            if message.cc:
                msg["Cc"] = ", ".join(message.cc)
            msg["Subject"] = message.stamped_subject
        msg["Message-ID"] = message.message_id
        msg["Date"] = format_datetime(sent_at or datetime.now().astimezone())
        msg[STAMP_HEADER] = message.stamp
        msg.set_content("This message is best viewed in an e-mail program that shows HTML.")
        msg.add_alternative(html, subtype="html")
        if message.inline_images:
            html_part = msg.get_payload()[-1]
            for cid, data in message.inline_images.items():
                html_part.add_related(data, maintype="image", subtype="png", cid=f"<{cid}>")
        for path in message.attachments:
            path = Path(path)
            ctype, _ = mimetypes.guess_type(path.name)
            maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
            msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
        return msg

    def send(self, message: OutgoingMessage) -> None:
        msg = self.build(message)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=self.timeout) as smtp:
            smtp.starttls()
            smtp.login(self.address, self._password)
            smtp.send_message(msg)

    # ------------------------------------------------------------ IMAP helpers
    def _imap(self) -> imaplib.IMAP4_SSL:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, timeout=self.timeout)
        imap.login(self.address, self._password)
        return imap

    @staticmethod
    def _sent_folder(imap: imaplib.IMAP4_SSL) -> str:
        """Gmail marks its sent folder with the \\Sent special-use flag; the name depends on language."""
        typ, folders = imap.list()
        for raw in folders or []:
            line = raw.decode(errors="replace")
            if "\\Sent" in line:
                return line.rsplit(' "/" ', 1)[-1].strip()
        return '"[Gmail]/Sent Mail"'

    @staticmethod
    def _search(imap: imaplib.IMAP4_SSL, *criteria: str) -> list[bytes]:
        typ, data = imap.uid("SEARCH", None, *criteria)
        return data[0].split() if typ == "OK" and data and data[0] else []

    # ------------------------------------------------------------ confirmation
    def confirm_sent(self, message: OutgoingMessage) -> SentEvidence | None:
        imap = self._imap()
        try:
            imap.select(self._sent_folder(imap), readonly=True)
            uids = self._search(imap, "HEADER", "Message-ID", _quote(message.message_id))
            if not uids:
                uids = self._search(imap, "SUBJECT", _quote(f"[{message.stamp}]"))
            if not uids:
                return None
            typ, data = imap.uid("FETCH", uids[-1], "(BODY.PEEK[])")
            raw = data[0][1]
        finally:
            imap.logout()
        parsed = email.message_from_bytes(raw, policy=email.policy.default)
        return SentEvidence(stamp=message.stamp, message_id=parsed.get("Message-ID", message.message_id),
                            sent_at=parsedate_to_datetime(parsed["Date"]), to=parsed.get("To", ""),
                            cc=parsed.get("Cc", ""), subject=parsed.get("Subject", ""), eml=raw)

    # ------------------------------------------------------------ inbox (SIT mail)
    def fetch_latest(self, subject_contains: str, save_dir: Path,
                     extensions: tuple[str, ...] = (".xls", ".xlsx")) -> FetchedMail | None:
        imap = self._imap()
        try:
            imap.select("INBOX", readonly=True)
            uids = self._search(imap, "SUBJECT", _quote(subject_contains))
            if not uids:
                return None
            typ, data = imap.uid("FETCH", max(uids, key=int), "(BODY.PEEK[])")  # newest arrival
            raw = data[0][1]
        finally:
            imap.logout()
        parsed = email.message_from_bytes(raw, policy=email.policy.default)
        received = parsedate_to_datetime(parsed["Date"])
        save_dir.mkdir(parents=True, exist_ok=True)
        saved = []
        for part in parsed.iter_attachments():
            name = part.get_filename() or ""
            if name.lower().endswith(extensions):
                target = save_dir / f"{received:%Y%m%d_%H%M%S}_{_UNSAFE.sub('_', name)}"
                target.write_bytes(part.get_payload(decode=True))
                saved.append(target)
        return FetchedMail(message_id=parsed.get("Message-ID", ""), subject=parsed.get("Subject", ""),
                           received_at=received, sender=parsed.get("From", ""), attachments=saved)

    # ------------------------------------------------------------ inbox (SED approval replies)
    def fetch_raw(self, subject_contains: str, since_days: int = 30) -> list[bytes]:
        since = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")
        imap = self._imap()
        try:
            imap.select("INBOX", readonly=True)
            out = []
            for uid in self._search(imap, "SINCE", since, "SUBJECT", _quote(subject_contains)):
                typ, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                if typ == "OK" and data and data[0]:
                    out.append(data[0][1])
            return out
        finally:
            imap.logout()


def from_settings() -> GmailTransport:
    from django.conf import settings

    cfg = settings.INWARD
    redirect = None
    if cfg["MODE"] == "TEST":
        if not cfg["TEST_MAIL_TO"]:
            raise ValueError("TEST mode needs INWARD_TEST_MAIL_TO (where all test e-mails are redirected)")
        redirect = cfg["TEST_MAIL_TO"]
    return GmailTransport(cfg["GMAIL_ADDRESS"], cfg["GMAIL_APP_PASSWORD"], test_redirect=redirect)
