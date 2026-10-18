"""Internal damage (18-Oct-2026): report with photos -> approval by reply mail (Depot Manager / Admin Officer) ->
DMG transfer TRX -> IRR reconciliation, unexpected reason codes flagged for the supervisor."""
from datetime import date, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from inward.core.models import NotificationRecipient, PackageType, SkuPackaging
from inward.damage import services as dmg
from inward.damage.models import DamageReport
from inward.iw import comms
from inward.iw.models import MailMessage
from tests.helpers import verified_client
from tests.test_comms import FakeGmail, _reply
from tests.test_erp_recon import irr_rows
from tests.test_sed import _jpeg

pytestmark = pytest.mark.django_db
SKU = "F00XG90991003600"
DM, AO = "dm.coimbatore@example.com", "ao.coimbatore@example.com"


@pytest.fixture
def env(seeded, make_user, settings, tmp_path):
    settings.DOCUMENT_ROOT = tmp_path / "docs"
    settings.INWARD = {**settings.INWARD, "MODE": "LIVE", "GMAIL_ADDRESS": "inward@example.com"}
    SkuPackaging.objects.update_or_create(sku=SKU, defaults=dict(
        floor_sku="XG9 991", description="WEATHERCOAT 3.6 L", measure_family="LTR", pack_size="3.6",
        package_type=PackageType.objects.get(code="CARTON"), units_per_package=4))
    for label, addr in (("Depot Manager", DM), ("Admin Officer", AO), ("CFA Manager", "cfa@example.com")):
        NotificationRecipient.objects.filter(purpose="DAMAGE_APPROVAL", role_label=label).update(email=addr)
    return {"sup": make_user("sup_d", "SYSTEM_SUPERVISOR"), "clerk": make_user("clerk_d", "DEPOT_CLERK"),
            "cfa": make_user("cfa_d", "CFA_MANAGER")}


def _report(env, qty="3", category="HANDLING"):
    r = dmg.create(env["sup"], date.today(), "WH1 R13", "found while stacking")
    line = dmg.add_line(env["sup"], r.pk, sku=SKU.lower(), category=category, quantity=qty, lot_no="330/1",
                        locator="wh1.r13.c4.1", remarks="carton torn")
    return r, line


def _sent(report):
    gmail = FakeGmail()
    comms.process_outbox(gmail)
    comms.process_outbox(gmail)
    report.refresh_from_db()
    assert report.request_mail.status == MailMessage.Status.SENT
    return gmail


def _subject(report, revision=None):
    stamp = report.request_mail.stamp if revision is None else report.request_mail.stamp.replace(
        f" R{report.revision} ", f" R{revision} ")
    return f"Re: Internal damage approval {report.number} [{stamp}]"


def test_full_flow_to_reconciled(env):
    r, line = _report(env)
    assert r.number == f"IDR-{r.fy_code}-000001" and line.floor_sku == "XG9 991" and line.locator == "WH1.R13.C4.1"
    with pytest.raises(dmg.DamageError, match="needs at least one photo"):
        dmg.submit(env["sup"], r.pk)
    dmg.add_photo(env["sup"], line.pk, _jpeg(), "dmg.jpg")
    mail = dmg.submit(env["sup"], r.pk)
    assert mail.purpose == "IDR_APPR" and mail.to == [DM, AO] and mail.cc == ["cfa@example.com"]
    assert mail.attachments.count() == 1 and "APPROVED" in mail.html_body
    _sent(r)
    with pytest.raises(dmg.DamageError, match="no longer be changed"):
        dmg.add_line(env["sup"], r.pk, sku=SKU, category="EXPIRY", quantity="1")
    assert comms.handle_reply(_reply(_subject(r), "APPROVED", sender="x@example.com", mid="<a@x>")).startswith("ignored")
    assert comms.handle_reply(_reply(_subject(r), "APPROVED", sender=AO, mid="<b@x>")) == "approved"   # Admin Officer
    r.refresh_from_db()
    assert r.status == "APPROVED" and r.reply_from == AO and r.reply.doc_type == "INTERNAL_DAMAGE_REPLY"
    dmg.add_trx(env["sup"], r.pk, "3562905913")
    with pytest.raises(dmg.DamageError, match="already entered"):
        dmg.add_trx(env["sup"], r.pk, "3562905913")
    assert dmg.run_recon(env["sup"], r.pk).recon["status"] == "NOT_FOUND"
    irr_rows([("Subinventory Transfer", "3562905913", SKU, 3, "10.8", None, "Issue", "035DMG"),
              ("Subinventory Transfer", "3562905914", SKU, 3, "10.8", None, "Receipt", "035RCK")])
    from inward.irr.models import IrrTransaction
    IrrTransaction.objects.filter(trx_number__in=["3562905913", "3562905914"]).update(
        reason_description="68 / Damage - Local Carrier", source_subinv="035RCK")
    r = dmg.run_recon(env["sup"], r.pk)
    assert r.status == "RECONCILED" and r.recon["lines"][0]["status"] == "FULL"


def test_rejected_then_sent_again(env):
    r, line = _report(env, category="EXPIRY")                     # expiry: photo optional
    dmg.submit(env["sup"], r.pk)
    _sent(r)
    assert comms.handle_reply(_reply(_subject(r), "REJECTED recount the tins", sender=DM, mid="<r1@x>")) == "rejected"
    r.refresh_from_db()
    assert r.status == "REJECTED" and r.decision_text == "recount the tins"
    dmg.add_line(env["sup"], r.pk, sku=SKU, category="EXPIRY", quantity="2")
    dmg.submit(env["sup"], r.pk)
    _sent(r)
    assert r.revision == 2
    old = _reply(_subject(r, revision=1), "APPROVED", sender=DM, mid="<r2@x>")
    assert comms.handle_reply(old).startswith("ignored: reply to R1")
    assert comms.handle_reply(_reply(_subject(r), "Approved.", sender=DM, mid="<r3@x>")) == "approved"


def test_other_reason_code_is_flagged_for_the_supervisor(env):
    r, line = _report(env, qty="2")
    dmg.add_photo(env["sup"], line.pk, _jpeg(), "d.jpg")
    dmg.submit(env["sup"], r.pk)
    _sent(r)
    comms.handle_reply(_reply(_subject(r), "APPROVED", sender=DM, mid="<f@x>"))
    dmg.add_trx(env["sup"], r.pk, "3570000001")
    irr_rows([("Subinventory Transfer", "3570000001", SKU, 2, "7.2", None, "Issue", "035DMG")])
    from inward.irr.models import IrrTransaction
    IrrTransaction.objects.filter(trx_number="3570000001").update(reason_description="54 / Damage - Factory Carrier")
    r = dmg.run_recon(env["sup"], r.pk)
    assert r.status == "TRANSFERRED" and r.recon["reason_flags"][0]["code"] == "54"
    with pytest.raises(Exception):
        dmg.accept_reason(env["clerk"], r.pk, "54")               # the System Supervisor / CFA Manager decides
    r = dmg.accept_reason(env["sup"], r.pk, "54")
    assert r.status == "RECONCILED" and r.accepted_reasons == ["54"]


def test_wrong_quantity_and_not_to_dmg(env):
    r, line = _report(env, qty="5", category="LEAKAGE")
    dmg.add_photo(env["sup"], line.pk, _jpeg(), "d.jpg")
    dmg.submit(env["sup"], r.pk)
    _sent(r)
    comms.handle_reply(_reply(_subject(r), "APPROVED", sender=DM, mid="<q@x>"))
    dmg.add_trx(env["sup"], r.pk, "3580000001")
    irr_rows([("Subinventory Transfer", "3580000001", SKU, 4, "14.4", None, "Issue", "035FLR")])
    from inward.irr.models import IrrTransaction
    IrrTransaction.objects.filter(trx_number="3580000001").update(reason_description="68 / Damage - Local Carrier")
    res = dmg.run_recon(env["sup"], r.pk).recon
    assert res["status"] == "REVIEW" and any("not to the DMG" in n for n in res["notes"])


def test_reminder_once_after_24_hours(env):
    r, line = _report(env, category="EXPIRY")
    dmg.submit(env["sup"], r.pk)
    _sent(r)
    MailMessage.objects.filter(pk=r.request_mail_id).update(sent_at=timezone.now() - timedelta(hours=25))
    assert dmg.send_reminders() == 1 and dmg.send_reminders() == 0
    assert MailMessage.objects.filter(purpose="IDR_REMIND").count() == 1


def test_screens(client, env):
    c = verified_client(client, env["clerk"])
    resp = c.post(reverse("damage:new"), {"found_on": date.today().isoformat(), "location": "wh1"}, follow=True)
    r = DamageReport.objects.get()
    assert r.number in resp.content.decode() and "Add a damaged SKU" in resp.content.decode()
    c.post(reverse("damage:line_add", args=[r.pk]), {"sku_1": SKU, "category": "EXPIRY", "quantity": "2"})
    page = c.get(reverse("damage:detail", args=[r.pk])).content.decode()
    assert "XG9 991" in page and "data-sku-lookup" in page and DM in page
    assert c.post(reverse("damage:trx_add", args=[r.pk]), {"trx_no": "1"}).status_code == 403   # clerk: no TRX
    assert "Internal damage" in c.get(reverse("damage:list")).content.decode()
