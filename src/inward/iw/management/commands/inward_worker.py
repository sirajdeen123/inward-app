"""Background worker (SoT 14.2): sends queued mail, confirms it in Gmail 'Sent Mail', reads SED approval
replies and queues reminders. Web requests never talk to Gmail; only this process does.

    python manage.py inward_worker            # run forever (systemd: inward-worker@uat / @live)
    python manage.py inward_worker --once     # one round
"""
from __future__ import annotations

import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from inward.iw import comms

log = logging.getLogger("inward.worker")


class Command(BaseCommand):
    help = "Mail queue, Sent Mail confirmation, SED approval replies and reminders."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--interval", type=int, default=30, help="seconds between rounds")

    def handle(self, *args, once=False, interval=30, **opts):
        from inward.mail.gmail import from_settings
        rounds = 0
        while True:
            close_old_connections()
            try:  # the inbox (SED approval replies) is read every 10th round, about every 5 minutes
                stats = comms.run_once(from_settings(), read_inbox=once or rounds % 10 == 0)
                rounds += 1
                if any(v for v in stats.values()):
                    log.info("worker round: %s", stats)
                if once:
                    self.stdout.write(str(stats))
            except Exception as exc:  # noqa: BLE001 - never die; the next round retries
                log.exception("worker round failed: %s", exc)
                if once:
                    raise
            if once:
                return
            time.sleep(interval)
