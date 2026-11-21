"""Master files Phase 2 (21-Nov-2026): organisations + MRA e-mails, vendors, senders, transporters, notification e-mails."""
import pytest
from django.urls import reverse

from inward.core import masterfiles as mf
from inward.core.models import (
    EmailStatus, NotificationRecipient, Organisation, OrganisationEmail, Sender, Transporter, Vendor,
)
from inward.iw import comms
from tests.helpers import verified_client
from tests.test_masterfiles import _edit

pytestmark = pytest.mark.django_db


@pytest.fixture
def people(seeded, make_user, settings, tmp_path):
    settings.DOCUMENT_ROOT = tmp_path / "docs"
    return {"cfa": make_user("cfa_mf2", "CFA_MANAGER"), "clerk": make_user("clerk_mf2", "DEPOT_CLERK")}


def _up(user, kind, change):
    return mf.upload(user, kind, _edit(mf.workbook(mf.REGISTRY[kind]), change), f"{kind}.xlsx")


def test_organisations_and_emails(people):
    cfa = people["cfa"]
    Organisation.objects.create(code="001", name="KOLKATA", org_type="DEPOT")
    o2 = Organisation.objects.create(code="279", name="RDC - WB-SREERAMPORE", org_type="RDC")
    OrganisationEmail.objects.create(organisation=o2, email="old@x.com", kind="TO", status=EmailStatus.VERIFIED)
    OrganisationEmail.objects.create(organisation=o2, email="cc@x.com", kind="CC", status=EmailStatus.VERIFIED)
    again = mf.upload(cfa, "ORGS", mf.workbook(mf.REGISTRY["ORGS"]), "same.xlsx")
    assert again.plan["unchanged"] == Organisation.objects.count() and not again.plan["ops"] and not again.plan["errors"]

    def change(rows):
        for r in rows:
            if r["Code"] == "001":
                r.update({"Code": 1, "MRA To e-mails": "kol@x.com; kol2@x.com"})          # Excel dropped the zeros
            if r["Code"] == "279":
                r.update({"MRA To e-mails": "new@x.com"})                                # old@x.com goes: no reason
        return rows + [{"Code": "777", "Name": "NEW FACTORY", "Type": "factory", "MRA To e-mails": "f@x.com"}]
    up = _up(cfa, "ORGS", change)
    assert [x["key"] for x in up.plan["new"]] == ["777"] and [x["key"] for x in up.plan["changed"]] == ["001"]
    assert "give the Change reason" in up.plan["errors"][0]["message"]

    def fixed(rows):
        for r in rows:
            if r["Code"] == "279":
                r.update({"MRA To e-mails": "new@x.com", "MRA Cc e-mails": "", "Change reason": "person left"})
        return rows
    up = _up(cfa, "ORGS", fixed)
    mf.confirm(cfa, up.pk)
    assert OrganisationEmail.objects.get(email="old@x.com").status == EmailStatus.RETIRED
    assert OrganisationEmail.objects.get(email="new@x.com").status == EmailStatus.VERIFIED   # the CFA Manager verifies
    assert sorted(OrganisationEmail.objects.filter(organisation_id="279").exclude(status=EmailStatus.RETIRED)
                  .values_list("email", flat=True)) == ["new@x.com"]

    # given to the clerk: what the clerk adds waits on Verify e-mails
    from inward.accounts.models import RolePermission
    RolePermission.objects.create(role_id="DEPOT_CLERK", permission_id="MASTER_UPLOAD", mode="GRANT")
    up = _up(people["clerk"], "ORGS", lambda rows: [dict(r, **({"MRA Cc e-mails": "c2@x.com"} if r["Code"] == "001"
                                                                else {})) for r in rows])
    mf.confirm(people["clerk"], up.pk)
    assert OrganisationEmail.objects.get(email="c2@x.com").status == EmailStatus.UNVERIFIED
    assert any(r["email"] == "c2@x.com" for r in comms.unverified_emails())


def test_vendors_senders_transporters(people):
    cfa = people["cfa"]
    Vendor.objects.create(code="V54", name="ASIAN PACK", email="v@x.com", email_status=EmailStatus.VERIFIED)
    Sender.objects.create(name="HO STORES", email="ho@x.com")
    t = Transporter.objects.create(name="SRI MURUGAN TRANSPROT", search_key="SRIMURUGANTRANSPROT")
    Transporter.objects.create(name="VRL LOGISTICS", search_key="VRLLOGISTICS", verified=True)

    up = _up(cfa, "VENDORS", lambda rows: [dict(r, **{"MRA e-mail": "w@x.com"}) for r in rows])
    assert "give the Change reason" in up.plan["errors"][0]["message"]
    up = _up(cfa, "VENDORS", lambda rows: [dict(r, **{"MRA e-mail": "w@x.com", "Change reason": "new contact"})
                                           for r in rows] + [{"Code": "v60", "Name": "NEW VENDOR"}])
    mf.confirm(cfa, up.pk)
    assert Vendor.objects.get(pk="V54").email == "w@x.com" and Vendor.objects.filter(pk="V60").exists()

    up = _up(cfa, "SENDERS", lambda rows: rows + [{"Name": "Admin Office", "E-mail": "ad@x.com"}])
    mf.confirm(cfa, up.pk)
    assert Sender.objects.get(name="ADMIN OFFICE").status == EmailStatus.VERIFIED

    def trans(rows):
        for r in rows:
            if r["Name"] == "SRI MURUGAN TRANSPROT":
                r.update({"Name": "Sri Murugan Transport", "Verified": "Yes"})
        return rows + [{"Name": "vrl logistics"}]                                     # the same as an existing one
    up = _up(cfa, "TRANSPORTERS", trans)
    assert len(up.plan["changed"]) == 1 and "cannot be merged" in up.plan["errors"][0]["message"]
    mf.confirm(cfa, up.pk)
    t.refresh_from_db()
    assert t.name == "SRI MURUGAN TRANSPORT" and t.search_key == "SRIMURUGANTRANSPORT" and t.verified


def test_notification_emails(people):
    cfa = people["cfa"]
    NotificationRecipient.objects.filter(role_label="Admin Officer").update(email="ao@x.com")
    spec = mf.REGISTRY["NOTIFICATIONS"]
    assert mf.upload(cfa, "NOTIFICATIONS", mf.workbook(spec), "n.xlsx").plan["ops"] == []

    def change(rows):
        for r in rows:
            if r["Role"] == "Admin Officer":
                r["E-mail"] = "ao2@x.com"
            if r["Role"].startswith("MRA default Cc"):
                r["E-mail"] = "a@x.com, b@x.com"
        return rows + [{"Role": "Somebody", "E-mail": "s@x.com"}]
    up = _up(cfa, "NOTIFICATIONS", change)
    assert len(up.plan["changed"]) == 2 and "not a role" in up.plan["errors"][0]["message"]
    mf.confirm(cfa, up.pk)
    assert set(NotificationRecipient.objects.filter(role_label="Admin Officer").values_list("email", flat=True)) == {"ao2@x.com"}
    assert comms.default_mra_cc() == ["a@x.com", "b@x.com"]


def test_page_lists_by_module(client, people):
    c = verified_client(client, people["clerk"])
    page = c.get(reverse("core:masterfiles")).content.decode()
    assert "Stock inward" in page and "Transporters" in page and "Notification e-mails" not in page   # clerk: no
    page = verified_client(client, people["cfa"]).get(reverse("core:masterfiles")).content.decode()
    assert "Notification e-mails" in page and "Material returns" in page
    for key in ("ORGS", "VENDORS", "SENDERS", "TRANSPORTERS", "NOTIFICATIONS"):
        assert client.get(reverse("core:masterfile_download", args=[key])).status_code == 200
