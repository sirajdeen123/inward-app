"""Screen 09 - reports on screen and by scheduled e-mail (SoT 13, table 15)."""
from datetime import datetime, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from inward.core.models import ExceptionEvent, NotificationRecipient
from inward.domain.fy import IST
from inward.iw import closure, reports
from inward.iw.models import MailMessage, ReportRun
from tests.helpers import verified_client
from tests.test_checklist import world  # noqa: F401  (fixture)
from tests.test_closure import PDF, reconciled  # noqa: F401  (fixture)
from tests.test_erp_recon import at_erp  # noqa: F401  (fixture)
from tests.test_mra import verified  # noqa: F401  (fixture)
from tests.test_sed import unloading as unloading_iw  # noqa: F401  (fixture)
from tests.test_unloading import ready  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db


def _recipients():
    for purpose in ("DAILY_ESCALATION", "MONTHLY_MIS", "SED_REPORT", "IRR_RECON_REPORT", "DAILY_UNLOADING"):
        for i, r in enumerate(NotificationRecipient.objects.filter(purpose=purpose)):
            r.email = f"{purpose.lower()}{i}@example.com"
            r.save()


def test_report_content(at_erp):
    w = at_erp
    d = reports.daily_escalation()
    assert [o.iw for o in d["erp_pending"]] == [w["iw"]] and d["open_sed"]
    u = reports.daily_unloading()
    assert u["records"] and u["charged"] > 0
    today = timezone.localdate()
    s = reports.sed_report(today - timedelta(days=1), today)
    assert len(s["lines"]) == 2 and s["totals"]["value"] == 3900
    mis = reports.monthly_mis(2026, 9)                           # the test IW arrived on 25-Sep-2026
    assert mis["iws"] == 1 and mis["by_type"][0]["n"] == 1
    assert reports.sed_xlsx(today - timedelta(days=1), today)[:2] == b"PK"


def test_schedule_sends_once_after_report_time(at_erp):
    _recipients()
    ExceptionEvent.objects.create(code="SIT_X", severity="CRITICAL", entity_type="ship", entity_id="1", message="m")
    assert reports.due_reports(datetime(2026, 10, 16, 8, 59, tzinfo=IST)) == []
    runs = reports.due_reports(datetime(2026, 10, 16, 9, 0, tzinfo=IST))          # 09:00: yesterday's collection
    assert [(r.kind, r.period) for r in runs] == [("DAILY_UNL", "2026-10-15")] and runs[0].mail
    m = ReportRun.objects.get(kind="DAILY_UNL").mail
    assert m.to == ["daily_unloading0@example.com"] and m.iw is None and "Daily unloading collection" in m.html_body
    runs = reports.due_reports(datetime(2026, 10, 16, 9, 30, tzinfo=IST))         # 09:30: incomplete IWs
    assert [(r.kind, r.period) for r in runs] == [("DAILY_ESC", "2026-10-16")]
    assert "Daily status of incomplete IWs" in runs[0].mail.subject
    assert ExceptionEvent.objects.get().escalation_count == 1
    assert reports.due_reports(datetime(2026, 10, 16, 19, 30, tzinfo=IST)) == []  # once per day
    first = datetime(2026, 11, 1, 9, 45, tzinfo=IST)
    kinds = {r.kind for r in reports.due_reports(first)}
    assert {"MONTHLY_MIS", "SED_REPORT"} <= kinds
    assert ReportRun.objects.get(kind="MONTHLY_MIS").period == "2026-10"


def test_missing_recipients_are_skipped_not_failed(at_erp):
    run = reports.queue_report("DAILY_ESC", "2026-10-16", {"r": reports.daily_escalation()})
    assert run.mail is None and "no recipient" in run.skipped


def test_irr_report_mailed_at_closure(reconciled):
    _recipients()
    w = reconciled
    closure.add_scan(w["clerk"], w["iw"].pk, "signed_mra", PDF, "mra.pdf", "application/pdf")
    closure.add_scan(w["clerk"], w["iw"].pk, "lr_scan", PDF, "lr.pdf", "application/pdf")
    snap = closure.close(w["clerk"], w["iw"].pk)
    if snap.report is None:                                       # no PDF engine on a development PC
        assert not MailMessage.objects.filter(purpose="IRR_RECON").exists()
    else:
        assert MailMessage.objects.get(purpose="IRR_RECON").attachments.get() == snap.report


def test_screens(client, at_erp, make_user):
    w = at_erp
    c = verified_client(client, w["clerk"])
    for kind, text in (("daily", "Daily status of incomplete IWs"), ("unloading", "Daily unloading collection"),
                       ("sed", "SED report"), ("mis", "Monthly MIS")):
        assert text in c.get(reverse("iw:reports"), {"r": kind}).content.decode()
    resp = c.get(reverse("iw:reports"), {"r": "sed", "xlsx": "1"})
    assert resp["Content-Type"].startswith("application/vnd.openxmlformats")
    resp = c.post(reverse("iw:report_send"), {"r": "daily"}, follow=True)
    assert "Only the CFA Manager" in resp.content.decode()
    _recipients()
    cfa = verified_client(client, make_user("cfa_rep", "CFA_MANAGER"))
    resp = cfa.post(reverse("iw:report_send"), {"r": "daily"}, follow=True)
    assert "Report queued" in resp.content.decode()
