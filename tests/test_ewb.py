"""Outbound transhipment - E-way bill Transporter ID update mail to the source (decision 6-Nov-2026)."""
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from inward.accounts.approvals import decide_and_apply
from inward.core.models import DepotOperator, EmailStatus, NotificationRecipient, Organisation, OrganisationEmail
from inward.iw import closure, ewb, mra, ops, outbound
from inward.iw.models import EwbEvent, Iw, MailMessage
from inward.iw.services import IwError
from tests.helpers import verified_client
from tests.test_outbound_vendor import _iw
from tests.test_outbound_vendor import env  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db
TID = "33AADPZ7939E1ZC"


@pytest.fixture
def w(env, make_user):  # noqa: F811
    DepotOperator.objects.update(transporter_id=TID, transporter_name="RAINBOW LOGISTICS")
    Organisation.objects.create(code="094", name="Pondicherry", org_type="DEPOT")
    Organisation.objects.create(code="095", name="Salem", org_type="DEPOT")
    OrganisationEmail.objects.create(organisation_id="390", email="hindupur@example.com", kind="TO",
                                     status=EmailStatus.VERIFIED)
    NotificationRecipient.objects.filter(purpose="EWB_TRANSPORTER", role_label="CFA Manager").update(email="cfa@example.com")
    clerk = env["clerk"]
    iw = _iw(clerk)
    a = outbound.add_ship(clerk, iw.pk, ship_id="52611001", destination="094", doc_ltr="72", doc_kg="20",
                          source="MANUAL", ewb_no="1234 5678 9012")
    b = outbound.add_ship(clerk, iw.pk, ship_id="52611002", destination="095", doc_ltr="10", doc_kg="0",
                          source="MANUAL")
    return {**env, "iw": iw, "a": a, "b": b, "cfa": make_user("cfa_ewb", "CFA_MANAGER")}


def _iw_now(w):
    return Iw.objects.select_related("load_type", "source_org").get(pk=w["iw"].pk)


def test_mail_needs_every_eway_bill_and_lists_them_all(w):
    clerk = w["clerk"]
    iw = _iw_now(w)
    assert w["a"].ewb_no == "123456789012"
    with pytest.raises(IwError, match="12 digits"):
        outbound.add_ship(clerk, iw.pk, ship_id="52611003", destination="094", doc_ltr="1", doc_kg="0",
                          source="MANUAL", ewb_no="12345")
    st = ewb.state(iw)
    assert st.needed and st.missing_ewb == ["52611002"] and not st.mra_ok
    assert "E-way bill No of outbound Ship 52611002" in ewb.mra_blocker(iw)
    assert any("E-way bill" in x for x in mra.blockers(iw))                      # hard stop at the MRA
    assert ops.actions(iw)[0].text.startswith("Enter the E-way bill No")        # first action from the start
    with pytest.raises(IwError, match="E-way bill No of every outbound Ship"):
        ewb.send(clerk, iw.pk)
    with pytest.raises(IwError, match="12 digits"):
        ewb.set_ewb(clerk, w["b"].pk, "98765")
    ewb.set_ewb(clerk, w["b"].pk, "987654321098")
    with pytest.raises(IwError, match="Send the update request first"):
        ewb.send(clerk, iw.pk, reminder=True)
    m = ewb.send(clerk, iw.pk)
    assert m.purpose == MailMessage.Purpose.EWB_TRANSPORTER and m.to == ["hindupur@example.com"]
    assert "cfa@example.com" in m.cc and "Request to update Transporter ID in E-way Bill" in m.subject
    for text in (TID, "RAINBOW LOGISTICS", "123456789012", "987654321098", "52611001", "52611002"):
        assert text in m.html_body                                              # one mail with every Ship
    iw = _iw_now(w)
    assert ewb.mra_blocker(iw) is None and not any("E-way bill" in x for x in mra.blockers(iw))
    with pytest.raises(IwError, match="reminder is possible"):
        ewb.send(clerk, iw.pk, reminder=True)
    EwbEvent.objects.update(at=timezone.now() - timedelta(hours=3))
    r = ewb.send(clerk, iw.pk, reminder=True)
    assert r.subject.startswith("REMINDER:") and "Reminder" in r.html_body

    # closure needs the source's update; a changed Ship needs a new mail
    gate = next(g for g in closure.gates(iw, []) if "E-way bill" in g.label)
    assert not gate.ok
    ewb.confirm(clerk, iw.pk, "reply mail 11:20 AM")
    iw = _iw_now(w)
    assert next(g for g in closure.gates(iw, []) if "E-way bill" in g.label).ok
    ewb.set_ewb(clerk, w["b"].pk, "987654321099")
    iw = _iw_now(w)
    assert "again - the outbound Ships changed" in ewb.mra_blocker(iw)
    ewb.send(clerk, iw.pk)
    assert not _iw_now(w).ewb_confirmed_at                                      # confirm the new request again


def test_mra_release_by_the_cfa_manager(w):
    clerk, cfa = w["clerk"], w["cfa"]
    iw = _iw_now(w)
    with pytest.raises(IwError, match="Choose the reason"):
        ewb.request_release(clerk, iw, "whatever")
    with pytest.raises(IwError, match="Write the reason"):
        ewb.request_release(clerk, iw, "Other")
    req = ewb.request_release(clerk, iw, "Source has no e-mail on file", "Hindupur gave no address")
    assert req.status == req.Status.PENDING and ewb.mra_blocker(iw)
    with pytest.raises(IwError, match="already waiting"):
        ewb.request_release(clerk, iw, "Other", "again please")
    decide_and_apply(cfa, req.pk, True)
    iw = _iw_now(w)
    assert ewb.state(iw).release and ewb.mra_blocker(iw) is None                # the MRA may be issued
    assert not next(g for g in closure.gates(iw, []) if "E-way bill" in g.label).ok
    outbound.add_ship(clerk, iw.pk, ship_id="52611009", destination="094", doc_ltr="5", doc_kg="0", source="MANUAL",
                      ewb_no="111122223333")
    assert ewb.mra_blocker(_iw_now(w))                                         # a new Ship cancels the release
    ewb.request_release(cfa, _iw_now(w), "E-way bill not applicable (e.g. under the value limit)")  # own: direct
    assert ewb.mra_blocker(_iw_now(w)) is None


def test_screens(client, w):
    c = verified_client(client, w["clerk"])
    iw = w["iw"]
    page = c.get(reverse("iw:detail", args=[iw.pk])).content.decode()
    assert "send the Transporter ID update mail to the source (mandatory)" in page and "52611002" in page
    page = c.get(reverse("iw:outbound", args=[iw.pk])).content.decode()
    assert "E-way bill No (12 digits)" in page and "123456789012" in page and "input-missing" in page
    page = c.get(reverse("iw:ewb", args=[iw.pk])).content.decode()
    assert TID in page and "RAINBOW LOGISTICS" in page and "Enter every E-way bill No above" in page
    c.post(reverse("iw:ewb_set", args=[w["b"].pk]), {"ewb_no": "987654321098", "back": reverse("iw:ewb", args=[iw.pk])})
    page = c.get(reverse("iw:ewb", args=[iw.pk])).content.decode()
    assert "Preview of the mail" in page and "hindupur@example.com" in page and "Send the update request" in page
    resp = c.post(reverse("iw:ewb_send", args=[iw.pk]), follow=True).content.decode()
    assert "Transporter ID update request queued to hindupur@example.com" in resp and "Request sent" in resp
    resp = c.post(reverse("iw:ewb_confirm", args=[iw.pk]), {"note": "phone 12:10"}, follow=True).content.decode()
    assert "the source updated the Transporter ID" in resp
    assert "E-way bill: the source updated" in c.get(reverse("iw:detail", args=[iw.pk])).content.decode()
