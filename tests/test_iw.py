"""Screen 02 - IW vehicle / consignment creation (SoT 4, 14.1, 18.2)."""
from datetime import datetime, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from inward.accounts import approvals
from inward.accounts.services import PermissionDenied
from inward.core.models import AuditEvent, LoadType, Organisation, Transporter
from inward.core.versioning import StaleEdit
from inward.domain.fy import IST
from inward.iw import services
from inward.iw.models import Iw
from tests.helpers import verified_client

pytestmark = pytest.mark.django_db


@pytest.fixture
def org(seeded):
    return Organisation.objects.create(code="390", name="Hindupur - Mfg", org_type="FACTORY")


@pytest.fixture
def clerk(make_user):
    return make_user("clerk_iw", "DEPOT_CLERK")


@pytest.fixture
def cfa(make_user):
    return make_user("cfa_iw", "CFA_MANAGER")


def _data(org, **kw):
    base = dict(arrival_at=datetime(2026, 9, 25, 9, 40, tzinfo=IST),
                load_type=LoadType.objects.get(code="DIRECT_FACTORY_OUTBOUND_TS"), source_org=org,
                vehicle_no="tn 60 e 8139", driver_name="Murugan", driver_mobile="+91 98765 43210",
                transporter_name="Ezlumalaiyan Logistics (786323)", lr_no="lr-4521")
    base.update(kw)
    return services.IwData(**base)


def test_create_issues_number_from_arrival_fy(org, clerk):
    iw = services.create(clerk, _data(org))
    assert (iw.iw_no, iw.fy_code, iw.status) == ("IW-2627-000001", "2627", Iw.Status.OPEN)
    assert (iw.vehicle_no, iw.vehicle_key, iw.driver_mobile, iw.lr_no) == ("TN 60 E 8139", "TN60E8139", "9876543210", "LR-4521")
    assert iw.transporter.name == "EZLUMALAIYAN LOGISTICS (786323)" and not iw.transporter.verified
    iw2 = services.create(clerk, _data(org, arrival_at=datetime(2026, 3, 31, 23, 10, tzinfo=IST), lr_no="lr-4400"))
    iw3 = services.create(clerk, _data(org, arrival_at=datetime(2026, 4, 1, 0, 5, tzinfo=IST), lr_no="lr-4401"))
    assert (iw2.iw_no, iw3.iw_no) == ("IW-2526-000001", "IW-2627-000002")
    assert AuditEvent.objects.filter(action="IW_CREATED").count() == 3


def test_draft_gets_no_number_until_created(org, clerk):
    draft = services.save_draft(clerk, _data(org, driver_name=""))
    assert draft.iw_no is None and draft.status == Iw.Status.DRAFT
    iw = services.create(clerk, _data(org), draft=draft)
    assert iw.pk == draft.pk and iw.iw_no == "IW-2627-000001"


@pytest.mark.parametrize("kw,message", [
    ({"arrival_at": "future"}, "future"),
    ({"driver_mobile": "12345"}, "10-digit"),
    ({"source_org": None}, "Source of load"),
    ({"vehicle_no": " - "}, "Vehicle number"),
])
def test_validation(org, clerk, kw, message):
    if kw.get("arrival_at") == "future":
        kw["arrival_at"] = timezone.now() + timedelta(hours=2)
    with pytest.raises(services.IwError, match=message):
        services.create(clerk, _data(org, **kw))
    assert not Iw.objects.exists()


def test_duplicate_iw_is_refused(org, clerk):
    """UAT: IW-2627-000009 repeated IW-2627-000008 (same vehicle, LR and arrival)."""
    first = services.create(clerk, _data(org))
    with pytest.raises(services.IwError, match="Duplicate entry: IW-2627-000001"):
        services.create(clerk, _data(org, arrival_at=datetime(2026, 9, 26, 9, 0, tzinfo=IST)))
    with pytest.raises(services.IwError, match="second trip"):
        services.create(clerk, _data(org, lr_no="LR-9999"))
    second = services.create(clerk, _data(org, lr_no="LR-9999", second_trip=True))
    with pytest.raises(services.IwError, match="Duplicate entry"):            # editing into a duplicate
        services.update(clerk, second.pk, second.version, _data(org, second_trip=True))
    Iw.objects.filter(pk=first.pk).update(status=Iw.Status.VOID)
    assert services.create(clerk, _data(org, second_trip=True)).iw_no       # a VOID IW does not count


def test_vendor_and_non_stock_need_no_source_org(org, clerk):
    for code in ("VENDOR", "OTHERS_NON_STOCK"):
        assert services.create(clerk, _data(org, source_org=None, load_type=LoadType.objects.get(code=code),
                                            vehicle_no=f"TN01{code[:2]}1", lr_no=code)).iw_no


def test_edit_rules(org, clerk):
    iw = services.create(clerk, _data(org))
    services.update(clerk, iw.pk, iw.version, _data(org, lr_no="LR-9"))
    with pytest.raises(StaleEdit):
        services.update(clerk, iw.pk, iw.version, _data(org, lr_no="LR-10"))  # old version
    fresh = Iw.objects.get(pk=iw.pk)
    with pytest.raises(services.IwError, match="another financial year"):
        services.update(clerk, iw.pk, fresh.version, _data(org, arrival_at=datetime(2026, 3, 30, 18, 0, tzinfo=IST)))
    assert Iw.objects.get(pk=iw.pk).lr_no == "LR-9"


def test_void_needs_cfa_approval_and_keeps_number(org, clerk, cfa):
    iw = services.create(clerk, _data(org))
    req = services.request_void(clerk, iw, "Created twice for the same vehicle")
    with pytest.raises(services.IwError, match="already waiting"):
        services.request_void(clerk, iw, "again")
    with pytest.raises(PermissionDenied):
        approvals.decide_and_apply(clerk, req.pk, True)  # nobody approves their own request
    approvals.decide_and_apply(cfa, req.pk, True, "ok")
    iw.refresh_from_db()
    assert iw.status == Iw.Status.VOID and iw.iw_no == "IW-2627-000001"
    assert services.create(clerk, _data(org)).iw_no == "IW-2627-000002"  # never reused
    with pytest.raises(services.IwError):
        services.update(clerk, iw.pk, iw.version, _data(org))


def test_rejected_void_changes_nothing(org, clerk, cfa):
    iw = services.create(clerk, _data(org))
    req = services.request_void(clerk, iw, "mistake")
    approvals.decide_and_apply(cfa, req.pk, False, "not a duplicate")
    iw.refresh_from_db()
    assert iw.status == Iw.Status.OPEN


def test_viewer_cannot_create(org, make_user):
    with pytest.raises(PermissionDenied):
        services.create(make_user("view_iw", "VIEWER"), _data(org))


def test_screens_create_edit_and_approve(client, org, clerk, cfa, seeded):
    c = verified_client(client, clerk)
    lt = LoadType.objects.get(code="DIRECT_FACTORY_OUTBOUND_TS")
    post = {"arrival_at_0": "2026-09-25", "arrival_at_1": "9", "arrival_at_2": "40", "arrival_at_3": "PM",
            "load_type": lt.pk, "source_org": "390 : Hindupur - Mfg", "vehicle_no": "tn60e8139",
            "driver_name": "ravi kumar", "driver_mobile": "9876543210", "transporter": "PTC TRANSPORT",
            "lr_no": "L1", "gate_register_no": "gr-114", "action": "create"}
    resp = c.post(reverse("iw:new"), post, follow=True)
    assert "IW-2627-000001 created." in resp.content.decode()
    made = Iw.objects.get(iw_no="IW-2627-000001")
    assert (made.vehicle_no, made.driver_name, made.gate_register_no) == ("TN60E8139", "RAVI KUMAR", "GR-114")
    local = timezone.localtime(made.arrival_at)
    assert (local.hour, local.minute) == (21, 40)                       # 9:40 PM
    page = c.get(reverse("iw:edit", args=[made.pk])).content.decode()
    assert 'value="PM" selected' in page and '<option value="9" selected>' in page
    resp = c.post(reverse("iw:new"), post, follow=True)          # the same vehicle and LR again: refused
    assert "Duplicate entry: IW-2627-000001" in resp.content.decode() and Iw.objects.count() == 1
    resp = c.post(reverse("iw:new"), post | {"lr_no": "L2"}, follow=True)   # same vehicle, same day, other LR
    assert "genuine second trip" in resp.content.decode() and Iw.objects.count() == 1
    resp = c.post(reverse("iw:new"), post | {"lr_no": "L2", "second_trip": "on"}, follow=True)
    assert "IW-2627-000002 created." in resp.content.decode()
    iw = Iw.objects.get(iw_no="IW-2627-000001")
    assert c.get(reverse("iw:list") + "?q=tn60e8139").status_code == 200
    c.post(reverse("iw:void", args=[iw.pk]), {"reason": "duplicate"})
    c.post(reverse("accounts:logout"))
    m = verified_client(client, cfa)
    page = m.get(reverse("accounts:approvals")).content.decode()
    assert "VOID IW" in page and "IW-2627-000001" in page
    req_pk = approvals.pending_for(cfa).first().pk
    m.post(reverse("accounts:decide", args=[req_pk]), {"decision": "approve"})
    iw.refresh_from_db()
    assert iw.status == Iw.Status.VOID


def test_sit_import_fills_organisations(tmp_path, settings, seeded):
    from inward.sit.services import import_sit_file
    from tests.helpers import sit_html
    settings.DOCUMENT_ROOT = tmp_path / "docs"
    import_sit_file(sit_html(tmp_path / "s.xls", [("'279:RDC - WB-Sreerampore", "52252922", "20-MAY-2026",
                                                   "F00Z00021G001000", "FANDECK", "RETAIL", "L", "1", "50")]),
                    source="MANUAL")
    org = Organisation.objects.get(code="279")
    assert (org.name, org.org_type) == ("RDC - WB-Sreerampore", "RDC")
    assert Transporter.objects.count() == 0



def test_source_email_is_required(org, clerk, settings):
    """Pre-go-live 19-Oct-2026: no IW for a source without an MRA 'To' address; the clerk types it on the IW."""
    from inward.core.models import OrganisationEmail
    settings.INWARD = {**settings.INWARD, "REQUIRE_SOURCE_EMAIL": True}
    with pytest.raises(services.IwError, match="no MRA e-mail yet"):
        services.create(clerk, _data(org))
    with pytest.raises(services.IwError, match="not a valid e-mail"):
        services.create(clerk, _data(org, source_email="depot390-at-berger"))
    iw = services.create(clerk, _data(org, source_email="Depot390@BergerIndia.com"))
    e = OrganisationEmail.objects.get(organisation=org)
    assert iw.iw_no and (e.email, e.kind, e.status, e.entered_by) == ("depot390@bergerindia.com", "TO", "UNVERIFIED", clerk)
    assert services.create(clerk, _data(org, lr_no="LR-2", vehicle_no="TN01X1")).iw_no      # known now: not asked
    assert services.create(clerk, _data(org, source_org=None, lr_no="LR-3", vehicle_no="TN01X2",
                                        load_type=LoadType.objects.get(code="VENDOR"))).iw_no   # vendor: no source


def test_source_email_only_when_none_on_file(org, clerk, settings):
    """21-Oct-2026: an e-mail on file switches the IW box / org panel add off; typed text is not added."""
    from inward.core.models import OrganisationEmail
    from inward.iw import orgs
    settings.INWARD = {**settings.INWARD, "REQUIRE_SOURCE_EMAIL": True}
    OrganisationEmail.objects.create(organisation=org, email="on.file@example.com", kind="TO", status="VERIFIED")
    services.create(clerk, _data(org, source_email="other@example.com"))
    assert list(OrganisationEmail.objects.filter(organisation=org).values_list("email", flat=True)) == ["on.file@example.com"]
    with pytest.raises(services.IwError, match="already has the MRA e-mail on.file@example.com"):
        orgs.save_org(clerk, org.code, "", "", "new@example.com")
    assert orgs.save_org(clerk, "777", "NEW DEPOT", "DEPOT", "n@example.com").emails == ["n@example.com"]


def test_import_orgs_command(seeded, tmp_path):
    import openpyxl
    from django.core.management import call_command
    from inward.core.models import Organisation, OrganisationEmail
    Organisation.objects.create(code="001", name="KEEP ME", org_type="OTHER")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Source Name", "To Email"])
    for r in (["001:Kolkata", ""], ["279:RDC - WB-Sreerampore", ""], ["390:Hindupur - Mfg-CESA Reporting", ""], ["390:Hindupur - Mfg", "h@example.com"],
              ["BPM: Item Master Organization", None]):
        ws.append(r)
    path = tmp_path / "orgs.xlsx"
    wb.save(path)
    before = Organisation.objects.count()
    call_command("import_orgs", str(path))                       # dry run
    assert Organisation.objects.count() == before
    call_command("import_orgs", str(path), apply=True)
    assert Organisation.objects.get(code="001").name == "KEEP ME"   # existing left as it is
    assert Organisation.objects.get(code="279").org_type == "RDC" and Organisation.objects.get(code="390").org_type == "FACTORY"
    assert Organisation.objects.get(code="BPM").name == "Item Master Organization"
    assert Organisation.objects.get(code="390").name == "Hindupur - Mfg"   # not the 'CESA Reporting' twin
    assert OrganisationEmail.objects.get(organisation_id="390").status == "VERIFIED"
