"""Screen 03 part B - outbound tick-and-tally and vendor challans (SoT 5.3 revised 15-Oct-2026, 5.4)."""
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
# first rows of the real Despatch sample (all TO ORG 094 Pondicherry):
# row 7: 12 pcs 12 L · row 8: 16 pcs 8 L · row 9: 60 pcs 60 L · report total 36 rows


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


def _all_rows(iw):
    return list(DespatchRow.objects.filter(report__iw=iw).order_by("row_no"))


def test_report_first_then_ship_and_destination_from_report(env):
    clerk, iw = env["clerk"], _iw(env["clerk"])
    _report(env, iw)
    assert outbound.report_destinations(iw) == [("094", "Pondicherry", 36)]
    ship = outbound.add_ship(clerk, iw.pk, ship_id="52611001", destination="094", doc_ltr="80", doc_kg="0")
    assert Organisation.objects.get(code="094").name == "Pondicherry"   # name taken from the report
    with pytest.raises(IwError, match="LTR and / or KG"):
        outbound.add_ship(clerk, iw.pk, ship_id="2", destination="094", doc_ltr="0", doc_kg="")
    rows = outbound.available_rows(ship)
    assert len(rows) == 36 and outbound.covers(ship, outbound.Totals.of(rows))


def test_tick_until_tally_and_server_check(env):
    clerk, iw = env["clerk"], _iw(env["clerk"])
    _report(env, iw)
    a = outbound.add_ship(clerk, iw.pk, ship_id="52611001", destination="094", doc_ltr="80", doc_kg="0", doc_nop="88")
    r = _all_rows(iw)
    with pytest.raises(IwError, match="do not tally"):
        outbound.save_selection(clerk, a.pk, {r[0].pk, r[1].pk}, set())        # 20 L only
    outbound.save_selection(clerk, a.pk, {r[0].pk, r[1].pk, r[2].pk}, set())  # 12 + 8 + 60 L, 88 pcs
    a.refresh_from_db()
    assert a.status == TrackStatus.ROWS_ASSIGNED and a.tallied_at
    # Ship B on the same vehicle never sees Ship A's rows
    b = outbound.add_ship(clerk, iw.pk, ship_id="52611002", destination="094", doc_ltr="90", doc_kg="0")
    assert {x.pk for x in outbound.available_rows(b)}.isdisjoint({r[0].pk, r[1].pk, r[2].pk})
    with pytest.raises(IwError, match="no longer available"):
        outbound.save_selection(clerk, b.pk, {r[0].pk}, set())
    assert not outbound.state(iw).ready
    outbound.save_selection(clerk, b.pk, {r[3].pk, r[4].pk, r[5].pk}, set())  # 30 + 48 + 12 L
    assert outbound.state(iw).ready
    outbound.reopen(clerk, b.pk)
    assert not outbound.state(iw).ready


def test_report_with_less_than_document(env):
    clerk, iw = env["clerk"], _iw(env["clerk"])
    _report(env, iw)
    ship = outbound.add_ship(clerk, iw.pk, ship_id="1", destination="094", doc_ltr="999999", doc_kg="0")
    assert not outbound.covers(ship, outbound.Totals.of(outbound.available_rows(ship)))


def test_suggestion_finds_exact_combination(env):
    clerk, iw = env["clerk"], _iw(env["clerk"])
    _report(env, iw)
    ship = outbound.add_ship(clerk, iw.pk, ship_id="7", destination="094", doc_ltr="80", doc_kg="0", doc_nop="88")
    rows = outbound.available_rows(ship)
    picked = outbound.suggest(ship, rows)
    t = outbound.Totals.of([x for x in rows if x.pk in set(picked)])
    assert (t.ltr, t.kg, t.nop) == (Decimal("80"), Decimal("0"), Decimal("88"))
    outbound.save_selection(clerk, ship.pk, set(picked), set())


def test_identical_row_on_another_iw_needs_acknowledgement(env):
    clerk = env["clerk"]
    first, second = _iw(clerk), _iw(clerk, vehicle="KA01AB1234")
    for iw in (first, second):
        _report(env, iw)
        outbound.add_ship(clerk, iw.pk, ship_id="9", destination="094", doc_ltr="12", doc_kg="0")
    s1, s2 = first.outbound_ships.get(), second.outbound_ships.get()
    outbound.save_selection(clerk, s1.pk, {_all_rows(first)[0].pk}, set())
    row2 = _all_rows(second)[0]
    assert [x.reused_on for x in outbound.available_rows(s2) if x.pk == row2.pk] == ["IW-2627-000001"]
    with pytest.raises(IwError, match="need the 'I checked' tick"):
        outbound.save_selection(clerk, s2.pk, {row2.pk}, set())
    outbound.save_selection(clerk, s2.pk, {row2.pk}, {row2.pk})


def test_outbound_only_for_outbound_load_types(env):
    iw = _iw(env["clerk"], code="DIRECT_FACTORY")
    with pytest.raises(IwError, match="no outbound"):
        outbound.add_ship(env["clerk"], iw.pk, ship_id="1", destination="094 : Pondicherry", doc_ltr="1")


def test_vendor_from_portal_and_manual(env):
    clerk, iw = env["clerk"], _iw(env["clerk"], code="VENDOR")
    path = env["tmp"] / "portal.xlsx"
    shutil.copy(sample("Vendor SIT sample.xlsx"), path)
    with pytest.raises(IwError, match="Select the warehouse"):
        vendor.add_from_portal(clerk, iw.pk, path, "portal.xlsx", "v54", "641406/876/2627")
    cons = vendor.add_from_portal(clerk, iw.pk, path, "portal.xlsx", "v54", "641406/876/2627", "industrial")
    assert (cons.vendor_id, cons.po_no, cons.warehouse) == ("V54", "376841", "INDUSTRIAL")
    line = cons.lines.get()
    assert (line.sku, line.nop, line.measure_family, line.floor_sku) == ("F007500991040002", 300, "KG", "750 991")
    assert Vendor.objects.get(code="V54").name == "CNA COATINGS INDIA PRIVATE LIMITED"
    with pytest.raises(IwError, match="already on this IW"):
        vendor.add_from_portal(clerk, iw.pk, path, "portal.xlsx", "V54", "641406/876/2627", "RETAIL")
    other = _iw(clerk, code="VENDOR", vehicle="TN52AC1171")
    with pytest.raises(IwError, match="already on IW-2627-000001"):
        vendor.add_manual(clerk, other.pk, vendor_code="V54", vendor_name="", po_no="1", challan_no="641406 / 876 / 2627",
                          lines=[("F007500991040002", "Putty", "K", "10", "400")], warehouse="RETAIL")
    manual = vendor.add_manual(clerk, other.pk, vendor_code="V99", vendor_name="New Vendor", po_no="77",
                               challan_no="C1", lines=[("F007500991040002", "Putty", "K", "10", "400")],
                               warehouse="RETAIL")
    assert manual.lines.get().kg == Decimal("400") and manual.warehouse == "RETAIL"


def test_screens(client, env):
    c = verified_client(client, env["clerk"])
    iw = _iw(env["clerk"])
    with open(sample("Outbound SIT sample.xlsx"), "rb") as fh:
        resp = c.post(reverse("iw:outbound_upload", args=[iw.pk]), {"file": fh}, follow=True)
    assert "36 rows" in resp.content.decode() and "094 : Pondicherry" in resp.content.decode()
    resp = c.post(reverse("iw:outbound_add_ship", args=[iw.pk]),
                  {"ship_id": "52611001", "destination": "094 : Pondicherry", "doc_ltr": "80", "doc_kg": "0"},
                  follow=True)
    page = resp.content.decode()
    assert "Tick the SKUs" in page and "tally.js" in page
    ship = iw.outbound_ships.get()
    page = c.get(reverse("iw:outbound_rows", args=[ship.pk]) + "?suggest=1").content.decode()
    assert "Suggested rows are ticked" in page
    r = _all_rows(iw)
    resp = c.post(reverse("iw:outbound_rows", args=[ship.pk]), {"row": [r[0].pk]}, follow=True)
    assert "do not tally" in resp.content.decode()
    resp = c.post(reverse("iw:outbound_rows", args=[ship.pk]), {"row": [r[0].pk, r[1].pk, r[2].pk]}, follow=True)
    assert "tally with the document" in resp.content.decode()
    assert "Outbound ready" in c.get(reverse("iw:detail", args=[iw.pk])).content.decode()
    assert Iw.objects.get(pk=iw.pk).status == Iw.Status.MATERIAL_LINKED
