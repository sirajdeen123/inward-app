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
    iw2 = services.create(clerk, _data(org, arrival_at=datetime(2026, 3, 31, 23, 10, tzinfo=IST)))
    iw3 = services.create(clerk, _data(org, arrival_at=datetime(2026, 4, 1, 0, 5, tzinfo=IST)))
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


def test_vendor_and_non_stock_need_no_source_org(org, clerk):
    for code in ("VENDOR", "OTHERS_NON_STOCK"):
        assert services.create(clerk, _data(org, source_org=None, load_type=LoadType.objects.get(code=code))).iw_no


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
    post = {"arrival_at": "2026-09-25T09:40", "load_type": lt.pk, "source_org": "390 : Hindupur - Mfg",
            "vehicle_no": "TN60E8139", "driver_mobile": "9876543210", "transporter": "PTC TRANSPORT",
            "lr_no": "L1", "action": "create"}
    resp = c.post(reverse("iw:new"), post, follow=True)
    assert "IW-2627-000001 created." in resp.content.decode()
    resp = c.post(reverse("iw:new"), post, follow=True)
    assert "already has open IW(s): IW-2627-000001" in resp.content.decode()
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
