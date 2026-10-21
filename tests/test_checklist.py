"""Unloading checklist - Retail / Industrial / Outbound / Other sheets (SoT 7.1)."""
import shutil
from datetime import datetime

import pytest
from django.urls import reverse

from inward.core import packaging
from inward.core.models import BusinessLineWarehouse, LoadType, Organisation
from inward.domain.fy import IST
from inward.iw import checklist, linking, outbound, services
from inward.iw.models import DespatchRow, Iw
from inward.iw.services import IwError
from inward.sit import services as sit
from tests.conftest import sample
from tests.helpers import sit_html, verified_client
from tests.test_packaging import _xlsx

pytestmark = pytest.mark.django_db
SHIP = ("'390:Hindupur - Mfg", "52497520", "13-JUN-2026")


@pytest.fixture
def world(seeded, tmp_path, settings, make_user):
    settings.DOCUMENT_ROOT = tmp_path / "docs"
    clerk = make_user("clerk_cl", "DEPOT_CLERK")
    sit.import_sit_file(sit_html(tmp_path / "s.xls", [
        (*SHIP, "F002980000020000", "WEATHERCOAT GLOW WHITE-WHITE-20 L", "RETAIL", "L", "20", "49"),
        (*SHIP, "F00XG90991003600", "EASY CLEAN . W1 BASE-.-3.6 L", "RETAIL", "L", "3.6", "97"),
        (*SHIP, "F00B470633002001", "QD A/C PUTTY-GREY-2 K", "AUTOMOTIVE", "K", "2", "70"),
        (*SHIP, "2010370991BULKPC", "20 LT MINT GREEN DRUM", "PROTECTON", "", "1", "25"),
    ]), source="MANUAL")
    up = packaging.create_upload(clerk, _xlsx(tmp_path / "p.xlsx", [
        ("F002980000020000", "WEATHERCOAT GLOW WHITE 20 L", "L", 20, "DRUM", 1),
        ("F00XG90991003600", "EASY CLEAN W1 BASE 3.6 L", "L", 3.6, "CARTON BOX", 4),
        ("F000000134001000", "LUXOL SKY BLUE 1 L", "L", 1, "CARTON BOX", 6),
        ("F000000160000500", "LUXOL PHIROZA 500 ML", "ML", 0.5, "CARTON BOX", 8),
        ("F000000160001000", "LUXOL PHIROZA 1 L", "L", 1, "CARTON BOX", 6)]), "p.xlsx")
    packaging.apply_upload(clerk, up.pk)
    iw = services.create(clerk, services.IwData(
        arrival_at=datetime(2026, 9, 25, 9, 40, tzinfo=IST), source_org=Organisation.objects.get(code="390"),
        load_type=LoadType.objects.get(code="DIRECT_FACTORY_OUTBOUND_TS"), vehicle_no="TN60E8139"))
    linking.link_ship(clerk, iw.pk, "52497520", gross_kg="11500")
    path = tmp_path / "d.xlsx"
    shutil.copy(sample("Outbound SIT sample.xlsx"), path)
    outbound.upload_report(clerk, iw.pk, path, "Outbound SIT sample.xlsx")
    ob = outbound.add_ship(clerk, iw.pk, ship_id="52611001", destination="094", doc_ltr="80", doc_kg="0")
    return {"clerk": clerk, "iw": iw, "ob": ob, "tmp": tmp_path}


def _tally(world):
    rows = list(DespatchRow.objects.filter(report__iw=world["iw"]).order_by("row_no")[:3])  # 12 + 8 + 60 L
    outbound.save_selection(world["clerk"], world["ob"].pk, {r.pk for r in rows}, set())


def test_blocked_until_outbound_tallied(world):
    b = checklist.build(world["iw"])
    assert any("outbound Ship" in x for x in b.blockers)
    with pytest.raises(IwError, match="tally"):
        checklist.generate(world["clerk"], world["iw"].pk)


def test_sheets_groups_and_conversion(world):
    _tally(world)
    b = checklist.build(world["iw"])
    assert not b.blockers
    titles = [s["title"] for s in b.sheets]
    assert titles == ["RETAIL WAREHOUSE", "INDUSTRIAL WAREHOUSE", "OUTBOUND TRANSHIPMENT - Ship 52611001 to 094 : Pondicherry"]
    retail = {g["code"]: g for g in b.sheets[0]["groups"]}
    assert list(retail) == ["CARTON", "DRUM"]                    # cartons first
    carton = retail["CARTON"]["lines"][0]
    assert (carton.floor_sku, carton.full, carton.loose) == ("XG9 991", 24, 1)   # 97 at 4 = 24 + 1 loose
    assert retail["DRUM"]["lines"][0].full == 49
    industrial = {g["code"]: g for g in b.sheets[1]["groups"]}
    assert list(industrial) == ["NOT_SET", "EMPTY_PACKING"]       # putty has no packaging; BULKPC last
    assert industrial["NOT_SET"]["lines"][0].packaging_missing
    assert [ln.sku for ln in b.missing_packaging] == ["F00B470633002001"]
    out = b.sheets[2]
    assert out["keep_separate"] and out["pieces"] == 88 and out["ltr"] == 80


def test_generate_snapshot_status_and_stale(world):
    _tally(world)
    snap = checklist.generate(world["clerk"], world["iw"].pk)
    iw = Iw.objects.get(pk=world["iw"].pk)
    assert snap.revision == 1 and iw.status == Iw.Status.UNLOADING_IN_PROGRESS
    assert not checklist.is_stale(iw, snap)
    # packaging fixed afterwards: the generated revision does not change
    packaging.save_sku(world["clerk"], "F00B470633002001", group="CARTON", units="10", description="QD PUTTY 2 K")
    assert snap.content["sheets"][1]["groups"][0]["code"] == "NOT_SET"
    linking.add_colour_bank(world["clerk"], iw.pk, direction="IN", challan_no="CB-1")
    assert checklist.is_stale(iw, snap)
    snap2 = checklist.generate(world["clerk"], iw.pk)
    assert snap2.revision == 2 and snap2.content["sheets"][-1]["key"] == "OTHER"
    assert snap2.content["sheets"][1]["groups"][0]["code"] == "CARTON"   # putty now 7 cartons of 10


def test_unmapped_business_line_blocks(world):
    _tally(world)
    BusinessLineWarehouse.objects.filter(business_line="AUTOMOTIVE").delete()
    b = checklist.build(world["iw"])
    assert any("AUTOMOTIVE" in x for x in b.blockers)


def test_screens(client, world):
    _tally(world)
    c = verified_client(client, world["clerk"])
    page = c.get(reverse("iw:checklist", args=[world["iw"].pk])).content.decode()
    assert "Packaging not set (1)" in page and "Add packaging" in page
    c.post(reverse("iw:checklist_generate", args=[world["iw"].pk]))
    page = c.get(reverse("iw:checklist_print", args=[world["iw"].pk, 1])).content.decode()
    assert "RETAIL WAREHOUSE" in page and "Operated by: RAINBOW LOGISTICS (CFA)" in page and "BERGER PAINTS INDIA LTD" in page and "XG9 991" in page
    assert "KEEP SEPARATE" in page and "print.js" in page
    assert "24 Boxes + 1 loose" in page and "× 4 pcs" not in page                 # what the team counts


def test_same_sku_on_two_ships_is_one_row(world):
    """IW-2627-000003 (22-Oct-2026): the hamalis count a SKU once - one row, the Ship split in 'From'; packages are
    worked out per Ship and added (97 pcs = 24 ctn + 1, 3 pcs = 3 loose -> 24 ctn + 4 loose)."""
    sit.import_sit_file(sit_html(world["tmp"] / "s2.xls", [
        ("'390:Hindupur - Mfg", "52497521", "14-JUN-2026", "F00XG90991003600", "EASY CLEAN . W1 BASE-.-3.6 L",
         "RETAIL", "L", "3.6", "3")]), source="MANUAL")
    linking.link_ship(world["clerk"], world["iw"].pk, "52497521", gross_kg="20")
    _tally(world)
    b = checklist.build(world["iw"])
    lines = [ln for g in b.sheets[0]["groups"] for ln in g["lines"] if ln.sku == "F00XG90991003600"]
    assert len(lines) == 1
    ln = lines[0]
    assert (ln.pieces, ln.full, ln.loose) == (100, 24, 4)
    assert ln.as_dict()["parts"] == [{"source": "Ship 52497520", "pieces": "97"}, {"source": "Ship 52497521", "pieces": "3"}]
    assert b.sheets[0]["lines"] == 2                       # Weathercoat + one Easy Clean row
