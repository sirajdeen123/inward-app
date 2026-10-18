"""Daily IRR from Gmail (18-Oct-2026): fetch today's IRR mail -> import -> engine -> summary mail for yesterday."""
import shutil
from datetime import datetime, timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone

from inward.core.models import NotificationRecipient
from inward.domain.fy import IST
from inward.irr import autofetch
from inward.iw.models import MailMessage, ReportRun
from inward.mail.transport import FetchedMail
from tests.conftest import sample

pytestmark = pytest.mark.django_db


class Inbox:
    def __init__(self, received=None, files=()):
        self.received, self.files, self.calls = received, list(files), 0

    def fetch_latest(self, subject_contains, save_dir, extensions=(".xls", ".xlsx")):
        self.calls += 1
        if self.received is None:
            return None
        saved = []
        for f in self.files:
            target = save_dir / f"{self.received:%Y%m%d_%H%M%S}_{f.name}"
            shutil.copy(f, target)
            saved.append(target)
        return FetchedMail(message_id="<irr@x>", subject=f"{subject_contains} 035", received_at=self.received,
                           sender="oracle@bergerindia.com", attachments=saved)


@pytest.fixture
def env(seeded, make_user, settings, tmp_path):
    settings.DOCUMENT_ROOT = tmp_path / "docs"
    cache.delete("irr_fetch_last")
    make_user("cfa_irr", "CFA_MANAGER")
    for label, addr in (("System Supervisor", "sup@example.com"), ("CFA Manager", "cfa@example.com"),
                        ("Admin Officer", "ao@example.com")):
        NotificationRecipient.objects.update_or_create(purpose="IRR_EXCEPTIONS", role_label=label,
                                                       defaults={"kind": "TO", "email": addr, "mandatory": True})
    return tmp_path


def at(h, m, day=None):
    day = day or timezone.localdate()
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def test_waits_retries_and_gives_up_at_the_deadline(env):
    inbox = Inbox(received=None)
    assert autofetch.run(inbox, at(8, 0)) == "idle" and inbox.calls == 0           # before 08:30
    assert autofetch.run(inbox, at(8, 31)) == "waiting" and inbox.calls == 1
    assert autofetch.run(inbox, at(8, 35)) == "wait" and inbox.calls == 1           # every 10 minutes only
    old = Inbox(received=at(8, 40) - timedelta(days=1))                              # yesterday's mail only
    assert autofetch.run(old, at(8, 45)) == "waiting"
    cache.delete("irr_fetch_last")
    assert autofetch.run(inbox, at(11, 0)) == "missing"
    run = ReportRun.objects.get(kind="IRR_EXC")
    assert run.mail.to == ["sup@example.com", "cfa@example.com", "ao@example.com"]
    assert "No IRR received by 11:00 AM" in run.mail.html_body
    assert autofetch.run(inbox, at(11, 30)) == "idle"                                # once a day


@pytest.mark.samples
def test_imports_todays_irr_and_mails_the_summary(env):
    src = env / "BPIL_ISSUE_RECEIPT_REG.xlsx"
    shutil.copy(sample("IRR.xlsx"), src)
    inbox = Inbox(received=at(8, 33), files=[src])
    assert autofetch.run(inbox, at(8, 40)) == "imported"
    from inward.core.models import ImportBatch
    from inward.irrecon.models import IrrMatch
    b = ImportBatch.objects.get(kind="IRR")
    assert b.source == "GMAIL" and b.accepted_count == 18261 and IrrMatch.objects.count() == 18261
    m = MailMessage.objects.get(purpose="IRR_EXC")
    assert "IRR Reconciliation Summary" in m.subject and "received and imported" in m.html_body
    assert autofetch.run(inbox, at(9, 0)) == "idle" and MailMessage.objects.filter(purpose="IRR_EXC").count() == 1
