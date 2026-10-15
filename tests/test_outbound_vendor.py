"""Screen 03 part B - outbound Despatch row assignment and vendor challans (SoT 5.3, 5.4)."""
import shutil
from datetime import datetime
from decimal import Decimal

import pytest
from django.urls import reverse

from inward.core.models import LoadType, Organisation, Vendor
from inward.domain.fy import IST
from inward.iw import outbound, services, vendor
from inward.iw.models import DespatchRow, Iw, TrackStatus
from inward.iw.services import IwError
from tests.conftest import sample
from tests.helpers import verified_client

pytestmark = pytest.mark.django_db


@pytest.fixture
def env(seeded, make_user, settings, tmp_path):
    settings.DOCUMENT_ROOT = tmp_path / "docs"
    Organisation.objects.create(code="390", name="Hindupur - Mfg", org_type="FACTORY")
    return {"clerk": make_user("clerk_ob", "DEPOT_CLERK"), "tmp": tmp_path}


def _iw(clerk, code="DIRECT_FACTORY_OUTBOUND_TS", vehicle="TN60E8139"):
    return services.create(clerk, services.IwData(
        arrival_at=datetime(2026, 9, 25, 9, 40, tzinfo=IST), load_type=LoadType.objects.get(code=code),
        source_org=Organisation.objects.get(code="390") if code != "VENDOR" else None, vehicle_no=vehicle))


def _report(env, iw):
    path = env["tmp"] / "despatch.xlsx"
    shutil.copy(sample("Outbound SIT sample.xlsx"), path)
    return outbound.upload_report(env["clerk"], iw.pk, path, "Outbound SIT sample.xlsx")


def test_outbound_ships_and_balance(env):
    clerk, iw = env["clerk"], _iw(env["clerk"])
    with pytest.raises(IwError, match="number of packages"):
        outbound.add_ship(clerk, iw.pk, ship_id="52611001", destination="094 : Pondicherry", doc_nop="0")
    # Ship A = first 3 rows (12 + 16 + 60 NOP; 12 + 8 + 60 LTR), Ship B = next 3 (30 + 12 + 24; 30 + 48 + 12)
    a = outbound.add_ship(clerk, iw.pk, ship_id="52611001", destination="094 : Pondicherry", doc_nop="88", doc_ltr="80")
    b = outbound.add_ship(clerk, iw.pk, ship_id="52611002", destination="094", doc_nop="66", doc_ltr="90")
    assert Organisation.objects.get(code="094").name == "Pondicherry"
    _report(env, iw)
    rows = list(outbound.visible_rows(iw).order_by("row_no"))
    assert len(rows) == 36
    choice = {r.pk: str(a.pk) for r in rows[:3]} | {r.pk: str(b.pk) for r in rows[3:6]}
    outbound.assign(clerk, iw.pk, choice, set())
    st = outbound.state(iw)
    assert all(bal.balanced for bal in st.balances) and st.undecided == 30 and not st.ready
    outbound.assign(clerk, iw.pk, {r.pk: "X" for r in rows[6:]}, set())
    st = outbound.state(iw)
    assert st.ready
    a.refresh_from_db()
    assert a.status == TrackStatus.ROWS_ASSIGNED
    outbound.assign(clerk, iw.pk, {rows[0].pk: "X"}, set())   # unbalances Ship A
    assert not outbound.state(iw).ready


def test_wrong_destination_and_assign_all(env):
    clerk, iw = env["clerk"], _iw(env["clerk"])
    other = outbound.add_ship(clerk, iw.pk, ship_id="1", destination="092 : Chennai-2", doc_nop="5")
    pondy = outbound.add_ship(clerk, iw.pk, ship_id="2", destination="094 : Pondicherry", doc_nop="1")
    _report(env, iw)
    row = outbound.visible_rows(iw).first()
    with pytest.raises(IwError, match="goes to 094, but Ship 1 goes to 092"):
        outbound.assign(clerk, iw.pk, {row.pk: str(other.pk)}, set())
    assert outbound.assign_all_for_destination(clerk, iw.pk, "094") == 36
    assert DespatchRow.objects.filter(outbound_ship=pondy).count() == 36


def test_identical_row_on_another_iw_needs_acknowledgement(env):
    clerk = env["clerk"]
    first, second = _iw(clerk), _iw(clerk, vehicle="KA01AB1234")
    for iw in (first, second):
        outbound.add_ship(clerk, iw.pk, ship_id="9", destination="094 : Pondicherry", doc_nop="12", doc_ltr="12")
        _report(env, iw)
    r1 = outbound.visible_rows(first).order_by("row_no").first()
    outbound.assign(clerk, first.pk, {r1.pk: str(first.outbound_ships.get().pk)}, set())
    r2 = outbound.visible_rows(second).order_by("row_no").first()
    with pytest.raises(IwError, match="identical to a row already assigned on IW-2627-000001"):
        outbound.assign(clerk, second.pk, {r2.pk: str(second.outbound_ships.get().pk)}, set())
    outbound.assign(clerk, second.pk, {r2.pk: str(second.outbound_ships.get().pk)}, {r2.pk})
    r2.refresh_from_db()
    assert r2.reuse_acknowledged and r2.outbound_ship_id


def test_outbound_only_for_outbound_load_types(env):
    iw = _iw(env["clerk"], code="DIRECT_FACTORY")
    with pytest.raises(IwError, match="no outbound"):
        outbound.add_ship(env["clerk"], iw.pk, ship_id="1", destination="094 : Pondicherry", doc_nop="1")


def test_vendor_from_portal_and_manual(env):
    clerk, iw = env["clerk"], _iw(env["clerk"], code="VENDOR")
    path = env["tmp"] / "portal.xlsx"
    shutil.copy(sample("Vendor SIT sample.xlsx"), path)
    cons = vendor.add_from_portal(clerk, iw.pk, path, "portal.xlsx", "v54", "641406/876/2627")
    assert (cons.vendor_id, cons.po_no) == ("V54", "376841")
    line = cons.lines.get()
    assert (line.sku, line.nop, line.measure_family, line.floor_sku) == ("F007500991040002", 300, "KG", "750 991")
    assert Vendor.objects.get(code="V54").name == "CNA COATINGS INDIA PRIVATE LIMITED"
    with pytest.raises(IwError, match="already on this IW"):
        vendor.add_from_portal(clerk, iw.pk, path, "portal.xlsx", "V54", "641406/876/2627")
    other = _iw(clerk, code="VENDOR", vehicle="TN52AC1171")
    with pytest.raises(IwError, match="already on IW-2627-000001"):
        vendor.add_manual(clerk, other.pk, vendor_code="V54", vendor_name="", po_no="1", challan_no="641406 / 876 / 2627",
                          lines=[("F007500991040002", "Putty", "K", "10", "400")])
    with pytest.raises(IwError, match="is new"):
        vendor.add_manual(clerk, other.pk, vendor_code="V99", vendor_name="", po_no="1", challan_no="C1",
                          lines=[("F007500991040002", "Putty", "K", "10", "400")])
    manual = vendor.add_manual(clerk, other.pk, vendor_code="V99", vendor_name="New Vendor", po_no="77",
                               challan_no="C1", lines=[("F007500991040002", "Putty", "K", "10", "400"), ("", "", "", "", "")])
    assert manual.lines.get().kg == Decimal("400")
    with pytest.raises(IwError, match="not a vendor load"):
        vendor.add_manual(clerk, _iw(clerk, code="RDC", vehicle="X9").pk, vendor_code="V99", vendor_name="", po_no="1",
                          challan_no="C2", lines=[("F007500991040002", "Putty", "K", "1", "40")])


def test_screens(client, env):
    c = verified_client(client, env["clerk"])
    iw = _iw(env["clerk"])
    c.post(reverse("iw:outbound_add_ship", args=[iw.pk]),
           {"ship_id": "52611001", "destination": "094 : Pondicherry", "doc_nop": "88", "doc_ltr": "80"})
    with open(sample("Outbound SIT sample.xlsx"), "rb") as fh:
        resp = c.post(reverse("iw:outbound_upload", args=[iw.pk]), {"file": fh}, follow=True)
    assert "36 rows" in resp.content.decode()
    page = c.get(reverse("iw:outbound", args=[iw.pk])).content.decode()
    assert "Assign all 094 rows to Ship 52611001" in page and "Not balanced" in page
    detail = c.get(reverse("iw:detail", args=[iw.pk])).content.decode()
    assert "Outbound not ready" in detail
    assert Iw.objects.get(pk=iw.pk).status == Iw.Status.MATERIAL_LINKED
