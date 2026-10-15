"""Screen 03 part A - inbound Ships, split vehicle, LSCRR, Colour Bank, Non-Stock (SoT 5)."""
from datetime import datetime
from decimal import Decimal

import pytest
from django.urls import reverse

from inward.core.models import LoadType, Organisation, Sender
from inward.domain.fy import IST
from inward.iw import linking, services
from inward.iw.models import Iw, ShipLink
from inward.iw.services import IwError
from inward.sit import services as sit
from inward.sit.models import SitShip
from tests.helpers import sit_html, verified_client

pytestmark = pytest.mark.django_db
SHIP = ("'390:Hindupur - Mfg", "52497520", "13-JUN-2026")
OTHER = ("'036:Calicut", "52464774", "09-JUN-2026")


@pytest.fixture
def world(seeded, tmp_path, settings, make_user):
    settings.DOCUMENT_ROOT = tmp_path / "docs"
    sit.import_sit_file(sit_html(tmp_path / "s1.xls", [
        (*SHIP, "F002980000020000", "WEATHERCOAT GLOW WHITE-WHITE-20 L", "RETAIL", "L", "20", "49"),
        (*SHIP, "F00B470633002001", "QD A/C PUTTY-GREY-2 K", "AUTOMOTIVE", "K", "2", "70"),
        (*SHIP, "2010370991BULKPC", "20 LT MINT GREEN DRUM", "PROTECTON", "", "1", "25"),
        (*OTHER, "F00AQR0000020000", "PLATINA WEATHERCOAT WC300 WHITE 20 L", "RETAIL", "L", "20", "50"),
    ]), source="MANUAL")
    clerk = make_user("clerk_ln", "DEPOT_CLERK")
    return {"clerk": clerk, "tmp": tmp_path}


def _iw(clerk, code="DIRECT_FACTORY", vehicle="TN60E8139"):
    return services.create(clerk, services.IwData(
        arrival_at=datetime(2026, 9, 25, 9, 40, tzinfo=IST), load_type=LoadType.objects.get(code=code),
        source_org=Organisation.objects.get(code="390") if code != "VENDOR" else None, vehicle_no=vehicle))


def test_link_freezes_baseline_and_sets_status(world):
    iw = _iw(world["clerk"])
    link = linking.link_ship(world["clerk"], iw.pk, "52497520", lscrr_no="lscrr-77", lscrr_applied=True)
    lines = {ln.sku: ln for ln in link.lines.all()}
    assert lines["F002980000020000"].quantity == 49 and lines["F002980000020000"].measure == 980
    assert lines["F00B470633002001"].measure == 140 and lines["F00B470633002001"].measure_family == "KG"
    assert lines["2010370991BULKPC"].is_empty_packing and lines["2010370991BULKPC"].measure == 0
    assert (link.lscrr_no, link.lscrr_applied) == ("LSCRR-77", True)
    iw.refresh_from_db()
    assert iw.status == Iw.Status.MATERIAL_LINKED
    assert linking.ship_link_state()[0] == {"52497520"}
    linking.unlink_ship(world["clerk"], link.pk)
    iw.refresh_from_db()
    assert iw.status == Iw.Status.OPEN


def test_second_vehicle_needs_split_and_cannot_exceed_sit(world):
    first, second = _iw(world["clerk"]), _iw(world["clerk"], vehicle="TN52AC1171")
    linking.link_ship(world["clerk"], first.pk, "52497520", split=True, split_reason="two trucks",
                      allocations={"F002980000020000": "30", "F00B470633002001": "70"})
    with pytest.raises(IwError, match="already linked to IW-2627-000001"):
        linking.link_ship(world["clerk"], second.pk, "52497520")
    with pytest.raises(IwError, match="more than the 19"):
        linking.link_ship(world["clerk"], second.pk, "52497520", split=True, split_reason="two trucks",
                          allocations={"F002980000020000": "20"})
    link = linking.link_ship(world["clerk"], second.pk, "52497520", split=True, split_reason="two trucks",
                             allocations={"F002980000020000": "19", "2010370991BULKPC": "25"})
    assert {ln.sku: ln.quantity for ln in link.lines.all()} == {"F002980000020000": 19, "2010370991BULKPC": 25}
    cand = linking.ship_candidate(_iw(world["clerk"], vehicle="KA01AB1234"), "52497520")
    assert "already allocated" in " ".join(cand.blockers)


@pytest.mark.parametrize("setup,message", [
    ("vendor_load", "does not carry inbound SIT"),
    ("pre_go_live", "Pre-go-live"),
    ("unknown", "not in the imported SIT"),
])
def test_blockers(world, setup, message):
    iw = _iw(world["clerk"], code="VENDOR" if setup == "vendor_load" else "DIRECT_FACTORY")
    ship = "99999999" if setup == "unknown" else "52497520"
    if setup == "pre_go_live":
        SitShip.objects.filter(pk=ship).update(pre_go_live=True)
    with pytest.raises(IwError, match=message):
        linking.link_ship(world["clerk"], iw.pk, ship)


def test_draft_and_void_rules(world, make_user):
    draft = services.save_draft(world["clerk"], services.IwData(
        arrival_at=datetime(2026, 9, 25, 9, 40, tzinfo=IST), load_type=LoadType.objects.get(code="RDC"),
        source_org=None, vehicle_no="X1"))
    with pytest.raises(IwError, match="Create the IW"):
        linking.link_ship(world["clerk"], draft.pk, "52497520")
    iw = _iw(world["clerk"])
    linking.link_ship(world["clerk"], iw.pk, "52497520")
    Iw.objects.filter(pk=iw.pk).update(status=Iw.Status.VOID)
    assert linking.ship_link_state()[0] == set()  # a VOID IW releases its Ships
    assert linking.link_ship(world["clerk"], _iw(world["clerk"], vehicle="NEW1").pk, "52497520")


def test_sit_removal_of_linked_ship_is_not_critical(world):
    iw = _iw(world["clerk"])
    linking.link_ship(world["clerk"], iw.pk, "52497520")
    imp = sit.import_sit_file(sit_html(world["tmp"] / "s2.xls", [
        (*OTHER, "F00AQR0000020000", "PLATINA WEATHERCOAT WC300 WHITE 20 L", "RETAIL", "L", "20", "50"),
    ], system_date="27-JUN-2026"), source="MANUAL")
    ev = imp.events.get(ship_id="52497520")
    assert ev.event == "SHIP_REMOVED_NO_RECEIPT" and not ev.critical


def test_colour_bank_and_non_stock(world):
    iw = _iw(world["clerk"], code="OTHERS_NON_STOCK")
    with pytest.raises(IwError, match="Challan number is mandatory"):
        linking.add_colour_bank(world["clerk"], iw.pk, direction="IN", challan_no=" ")
    linking.add_colour_bank(world["clerk"], iw.pk, direction="IN", challan_no="cb/12", serial_no="sn-1")
    ref = linking.add_non_stock(world["clerk"], iw.pk, sender_name="Berger Marketing", sender_email="mkt@example.com",
                                document_no="dc-55", lines=[("Diwali gift box", "Nos", "40"), ("", "", "")])
    assert list(ref.lines.values_list("description", "quantity")) == [("Diwali gift box", Decimal("40"))]
    assert Sender.objects.get(name="Berger Marketing").status == "UNVERIFIED"
    iw.refresh_from_db()
    assert iw.status == Iw.Status.MATERIAL_LINKED
    with pytest.raises(IwError, match="at least one item"):
        linking.add_non_stock(world["clerk"], iw.pk, sender_name="X", sender_email="", document_no="1",
                              lines=[("Box", "Nos", "0")])


def test_screens(client, world):
    c = verified_client(client, world["clerk"])
    iw = _iw(world["clerk"])
    page = c.get(reverse("iw:link_ship", args=[iw.pk]) + "?ship=52497520").content.decode()
    assert "298 000" in page and "Link Ship 52497520" in page
    resp = c.post(reverse("iw:link_ship", args=[iw.pk]), {"ship_id": "52497520", "confirm": "1",
                                                          "lscrr_no": "L-1", "lscrr_applied": "no"}, follow=True)
    html = resp.content.decode()
    assert "Ship 52497520 linked" in html and "980 LTR" in html and "LSCRR L-1" in html
    link = ShipLink.objects.get(iw=iw)
    c.post(reverse("iw:lscrr", args=[link.pk]), {"lscrr_no": "L-2", "lscrr_applied": "yes"})
    link.refresh_from_db()
    assert (link.lscrr_no, link.lscrr_applied) == ("L-2", True)
    resp = c.post(reverse("iw:add_non_stock", args=[iw.pk]), {"sender_name": "Mkt", "document_no": "D1",
                                                              "desc_0": "Calendar", "unit_0": "Nos", "qty_0": "5"},
                  follow=True)
    assert "no e-mail - needed for MRA" in resp.content.decode()
