"""Phase 0 spike: prove the Gmail mail engine (SoT 14.2, 21.2).

  python manage.py mail_spike check                 # sign in to SMTP and IMAP only
  python manage.py mail_spike find-sit [--parse]    # read-only: latest SIT mail (+ parse the attachment)
  python manage.py mail_spike send-test --to you@example.com
"""
import smtplib
import time
import uuid
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from inward.mail.gmail import SMTP_HOST, SMTP_PORT, from_settings, new_message_id
from inward.mail.transport import OutgoingMessage

SPIKE_DIR = Path(settings.BASE_DIR) / "var" / "spike"


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        sub.add_parser("check")
        f = sub.add_parser("find-sit")
        f.add_argument("--parse", action="store_true")
        s = sub.add_parser("send-test")
        s.add_argument("--to", required=True)
        s.add_argument("--wait-minutes", type=float, default=5)

    def handle(self, *args, action, **o):
        try:
            gmail = from_settings()
        except ValueError as exc:
            raise CommandError(f"{exc} - set it in .env") from exc
        getattr(self, "action_" + action.replace("-", "_"))(gmail, **o)

    def action_check(self, gmail, **o):
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(gmail.address, gmail._password)
        self.stdout.write(self.style.SUCCESS("SMTP sign-in OK"))
        imap = gmail._imap()
        sent = gmail._sent_folder(imap)
        imap.logout()
        self.stdout.write(self.style.SUCCESS(f"IMAP sign-in OK · sent folder {sent}"))

    def action_find_sit(self, gmail, parse=False, **o):
        target = SPIKE_DIR / "sit"
        mail = gmail.fetch_latest(settings.INWARD["SIT_SUBJECT"], target)
        if mail is None:
            raise CommandError(f"No inbox mail with subject containing '{settings.INWARD['SIT_SUBJECT']}'")
        self.stdout.write(f"Subject    : {mail.subject}\nReceived   : {mail.received_at}\nFrom       : {mail.sender}\n"
                          f"Message-ID : {mail.message_id}\nAttachments: {[p.name for p in mail.attachments]}")
        if parse:
            from inward.importers.sit import parse_sit
            for p in mail.attachments:
                sit = parse_sit(p)
                self.stdout.write(self.style.SUCCESS(
                    f"Parsed {p.name}: window {sit.window_from}..{sit.window_to}, rows {len(sit.rows)}, "
                    f"ships {len(sit.ship_ids)}, issues {len(sit.issues)}"))
        else:
            for p in mail.attachments:
                p.unlink(missing_ok=True)

    def action_send_test(self, gmail, to, wait_minutes, **o):
        stamp = f"SPIKE {datetime.now():%Y%m%d-%H%M%S} #{uuid.uuid4().hex[:6].upper()}"
        msg = OutgoingMessage(
            stamp=stamp, message_id=new_message_id(settings.INWARD["MESSAGE_ID_DOMAIN"]), to=[to],
            subject="Stock Inward app - Gmail test",
            html_body="<p>Test message from the Stock Inward application Phase 0 spike. No action needed.</p>"
                      "<p>RAINBOW LOGISTICS - CFA, Coimbatore Depot 035</p>")
        gmail.send(msg)
        self.stdout.write(f"Accepted by Gmail SMTP with stamp [{stamp}] -> QUEUED")
        deadline = time.time() + wait_minutes * 60
        while time.time() < deadline:
            ev = gmail.confirm_sent(msg)
            if ev:
                path = SPIKE_DIR / "sent" / (stamp.replace(" ", "_").replace("#", "") + ".eml")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(ev.eml)
                self.stdout.write(self.style.SUCCESS(
                    f"SENT confirmed in Gmail Sent Mail at {ev.sent_at}\nMessage-ID: {ev.message_id}\n"
                    f"To: {ev.to}\n.eml saved: {path}"))
                return
            self.stdout.write("  not in Sent Mail yet ...")
            time.sleep(10)
        raise CommandError("NOT confirmed within the wait window -> would become FAILED_UNCONFIRMED and retry")
