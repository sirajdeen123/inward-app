"""Screen 04 part A - SED capture, damage photos, physical verification (SoT 7.2, decisions 16-Oct-2026)."""
import io
from decimal import Decimal

import pytest
from django.urls import reverse
from PIL import Image

from inward.accounts.services import PermissionDenied
from inward.iw import checklist, linking, sed
from inward.iw.models import Iw
from inward.iw.services import IwError
from tests.helpers import verified_client
from tests.test_checklist import _tally, world  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db
WEATHERCOAT = "F002980000020000"   # 49 pcs, drum
EASYCLEAN = "F00XG90991003600"     # 97 pcs, carton of 4


def _jpeg(color="red") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (1600, 1200), color).save(out, "JPEG")
    return out.getvalue()


@pytest.fixture
def unloading(world):  # noqa: F811
    _tally(world)
    checklist.generate(world["clerk"], world["iw"].pk)
    iw = Iw.objects.get(pk=world["iw"].pk)
    world["iw"] = iw
    world["ship_key"] = f"SHIP:{iw.ship_links.get().pk}"
    return world


def _in(w, sku, **kw):
    return sed.SedInput(source_key=w["ship_key"], sku=sku, **kw)


def test_needs_checklist_first(world):  # noqa: F811
    with pytest.raises(IwError, match="checklist first"):
        sed.save_line(world["clerk"], world["iw"].pk, sed.SedInput(source_key="SHIP:1", sku=WEATHERCOAT, short_qty="1"))


def test_quantity_rules(unloading):
    w, clerk = unloading, unloading["clerk"]
    with pytest.raises(IwError, match="Enter a Short"):
        sed.save_line(clerk, w["iw"].pk, _in(w, EASYCLEAN))
    with pytest.raises(IwError, match="both short and excess"):
        sed.save_line(clerk, w["iw"].pk, _in(w, EASYCLEAN, short_qty="1", excess_qty="2"))
    with pytest.raises(IwError, match="more than the document"):
        sed.save_line(clerk, w["iw"].pk, _in(w, EASYCLEAN, short_qty="90", damage_qty="8", damage_kind="MATERIAL",
                                               damage_remarks="wet"))
    with pytest.raises(IwError, match="whole pieces"):
        sed.save_line(clerk, w["iw"].pk, _in(w, EASYCLEAN, short_qty="1.5"))
    with pytest.raises(IwError, match="remarks are mandatory"):
        sed.save_line(clerk, w["iw"].pk, _in(w, EASYCLEAN, damage_qty="2", damage_kind="MATERIAL"))
    line = sed.save_line(clerk, w["iw"].pk, _in(w, EASYCLEAN, short_qty="3", short_value="1,250"))
    assert line.short_remarks == "Short received - 3 pcs" and line.short_value == Decimal("1250.00")
    assert line.floor_sku == "XG9 991" and line.doc_pieces == 97
    with pytest.raises(IwError, match="already has an SED line"):
        sed.save_line(clerk, w["iw"].pk, _in(w, EASYCLEAN, excess_qty="1"))


def test_extra_sku_is_excess_only(unloading):
    w, clerk = unloading, unloading["clerk"]
    with pytest.raises(IwError, match="only be Excess"):
        sed.save_line(clerk, w["iw"].pk, _in(w, "F000000134001000", short_qty="2"))
    line = sed.save_line(clerk, w["iw"].pk, _in(w, "f000000134001000", excess_qty="6"))
    assert line.is_extra and line.description == "LUXOL SKY BLUE 1 L" and line.excess_remarks.startswith("Wrong item")


def test_container_damage_uses_rate_snapshot(unloading):
    w, clerk = unloading, unloading["clerk"]
    with pytest.raises(IwError, match="carton or drum"):
        sed.save_line(clerk, w["iw"].pk, _in(w, WEATHERCOAT, damage_qty="2", damage_kind="CONTAINER",
                                               damage_remarks="dented"))
    line = sed.save_line(clerk, w["iw"].pk, _in(w, WEATHERCOAT, damage_qty="2", damage_kind="CONTAINER",
                                                container_type="drum", container_count="2", damage_remarks="dented"))
    assert line.container_rate == Decimal("150.00") and line.damage_value == Decimal("300.00")


def test_values_need_sed_value_permission(unloading, make_user):
    w = unloading
    sup = make_user("sup_sed", "SYSTEM_SUPERVISOR")
    with pytest.raises(PermissionDenied):
        sed.save_line(sup, w["iw"].pk, _in(w, EASYCLEAN, short_qty="1", short_value="10"))
    line = sed.save_line(sup, w["iw"].pk, _in(w, EASYCLEAN, short_qty="1"))
    assert line.short_value is None
    sed.save_line(w["clerk"], w["iw"].pk, _in(w, EASYCLEAN, short_qty="1", short_value="10"), line_pk=line.pk)
    line = sed.save_line(sup, w["iw"].pk, _in(w, EASYCLEAN, short_qty="2"), line_pk=line.pk)
    assert line.short_value == Decimal("10.00")     # supervisor's quantity edit keeps the clerk's value
    assert sed.totals(w["iw"]).values_missing == 0


def test_photos_and_verification(unloading):
    w, clerk = unloading, unloading["clerk"]
    line = sed.save_line(clerk, w["iw"].pk, _in(w, EASYCLEAN, damage_qty="4", damage_kind="MATERIAL",
                                                damage_remarks="cartons wet, 4 tins leaking", damage_value="900"))
    assert any("needs at least one photo" in b for b in sed.verification_blockers(w["iw"]))
    with pytest.raises(IwError, match="not a photo"):
        sed.add_photo(clerk, line.pk, b"not an image", "x.jpg")
    photo = sed.add_photo(clerk, line.pk, _jpeg(), "IMG_001.jpg")
    assert photo.original.content_type == "image/jpeg" and photo.preview.size_bytes < photo.original.size_bytes
    iw = sed.confirm_verification(clerk, w["iw"].pk, sed.CONFIRM_ALL)
    assert iw.status == Iw.Status.PHYSICALLY_VERIFIED and iw.verified_by == clerk
    with pytest.raises(IwError, match="Reopen it"):
        sed.save_line(clerk, iw.pk, _in(w, WEATHERCOAT, short_qty="1"))
    with pytest.raises(IwError, match="reopen it"):
        linking.add_colour_bank(clerk, iw.pk, direction="IN", challan_no="CB-9")
    with pytest.raises(IwError, match="reason"):
        sed.reopen_verification(clerk, iw.pk, " ")
    assert sed.reopen_verification(clerk, iw.pk, "one more drum found damaged").status == Iw.Status.UNLOADING_IN_PROGRESS


def test_stale_checklist_blocks_verification(unloading):
    w = unloading
    linking.add_colour_bank(w["clerk"], w["iw"].pk, direction="IN", challan_no="CB-1")
    with pytest.raises(IwError, match="regenerate"):
        sed.confirm_verification(w["clerk"], w["iw"].pk, sed.CONFIRM_ALL)


def test_screens(client, unloading):
    w = unloading
    c = verified_client(client, w["clerk"])
    url = reverse("iw:unloading", args=[w["iw"].pk])
    page = c.get(url).content.decode()
    assert "SHORT" in page and "EXCESS" in page and "DAMAGE" in page and "Confirm physical verification" in page
    assert "XG9 991" in c.get(url + "?add=damage").content.decode()
    assert "XG9 991" in c.get(url + "?add=damage").content.decode()
    resp = c.post(url, {"pick": f"{w['ship_key']}|{EASYCLEAN}", "damage_qty": "2", "damage_kind": "MATERIAL",
                        "damage_remarks": "leaking", "damage_rate": "225"}, follow=True)
    assert "Now take the damage photo" in resp.content.decode() and "photo needed" in resp.content.decode()
    line = w["iw"].sed_lines.get()
    resp = c.post(reverse("iw:sed_photo_add", args=[line.pk]),
                  {"photo": _upload()}, follow=True)
    assert "1 photo(s) added" in resp.content.decode()
    photo = line.photos.get()
    img = c.get(reverse("iw:sed_photo", args=[photo.pk, "preview"]))
    assert img["Content-Type"] == "image/jpeg"
    page = c.get(reverse("iw:sed_edit", args=[line.pk])).content.decode()
    assert 'value="225"' in page and line.damage_value == Decimal("450.00")   # 2 pcs x Rs 225 (22-Oct-2026)
    resp = c.post(reverse("iw:verification_confirm", args=[w["iw"].pk]), {"confirm": ["sed_complete"]}, follow=True)
    assert "Tick both declarations" in resp.content.decode()
    resp = c.post(reverse("iw:verification_confirm", args=[w["iw"].pk]),
                  {"confirm": ["sed_complete", "sed_photos"]}, follow=True)
    assert "Physical verification confirmed" in resp.content.decode() and "Reopen physical verification" in resp.content.decode()


def _upload():
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile("IMG_002.jpg", _jpeg("blue"), content_type="image/jpeg")


def test_nil_sed_needs_both_declarations(unloading):
    w = unloading
    with pytest.raises(IwError, match="NO short and NO excess"):
        sed.confirm_verification(w["clerk"], w["iw"].pk, {"nil_damage"})
    with pytest.raises(IwError, match="Tick both"):
        sed.confirm_verification(w["clerk"], w["iw"].pk, {"sed_complete", "sed_photos"})   # SED ticks for a NIL IW
    iw = sed.confirm_verification(w["clerk"], w["iw"].pk, {"nil_short_excess", "nil_damage"})
    assert iw.sed_declaration["kind"] == "NIL" and iw.sed_declaration["lines"] == 0 and len(iw.sed_declaration["statements"]) == 2
    assert sed.reopen_verification(w["clerk"], iw.pk, "found a dented drum").sed_declaration == {}


def test_sed_declaration_records_photos(unloading):
    w = unloading
    line = sed.save_line(w["clerk"], w["iw"].pk, _in(w, EASYCLEAN, damage_qty="1", damage_kind="MATERIAL",
                                                     damage_remarks="leak", damage_value="10"))
    sed.add_photo(w["clerk"], line.pk, _jpeg(), "a.jpg")
    with pytest.raises(IwError, match="Tick both"):
        sed.confirm_verification(w["clerk"], w["iw"].pk, {"nil_short_excess", "nil_damage"})
    iw = sed.confirm_verification(w["clerk"], w["iw"].pk, {"sed_complete", "sed_photos"})
    assert iw.sed_declaration["kind"] == "SED" and iw.sed_declaration["photos"] == 1


def test_three_buttons_merge_into_one_line(client, unloading):
    w = unloading
    c = verified_client(client, w["clerk"])
    url = reverse("iw:unloading", args=[w["iw"].pk])
    assert "SHORT" in c.get(url + "?add=short").content.decode()
    pick = f"{w['ship_key']}|{EASYCLEAN}"
    resp = c.post(url, {"kind": "SHORT", "pick": pick, "short_qty": "3", "short_rate": "50"}, follow=True)
    assert "Short saved for XG9 991" in resp.content.decode()
    c.post(url, {"kind": "DAMAGE", "pick": pick, "damage_qty": "2", "damage_kind": "MATERIAL",
                 "damage_remarks": "leaking", "damage_rate": "100"})
    line = w["iw"].sed_lines.get()
    assert (line.short_qty, line.damage_qty, line.short_value, line.damage_value) == (3, 2, 150, 200)   # rate x pcs
    resp = c.post(url, {"kind": "EXCESS", "came_with": w["ship_key"], "sku": EASYCLEAN.lower(), "excess_qty": "1"},
                  follow=True)
    assert "both short and excess" in resp.content.decode()


def test_excess_search_and_quick_add(client, unloading):
    w = unloading
    c = verified_client(client, w["clerk"])
    items = c.get(reverse("iw:sku_search"), {"q": "luxol"}).json()["items"]
    assert {"F000000134001000", "F000000160001000"} <= {i["sku"] for i in items}
    assert c.get(reverse("iw:sku_search"), {"q": "000134"}).json()["items"][0]["floor"] == "000 134"
    resp = c.post(reverse("iw:sku_quick_add", args=[w["iw"].pk]),
                  {"sku": "f00zz10991001000", "description": "test wrong item 1 l"}, follow=True)
    assert "SKU F00ZZ10991001000 added" in resp.content.decode()
    from inward.core.models import SkuPackaging
    p = SkuPackaging.objects.get(sku="F00ZZ10991001000")
    assert p.description == "TEST WRONG ITEM 1 L" and p.units_per_package is None
    url = reverse("iw:unloading", args=[w["iw"].pk])
    c.post(url, {"kind": "EXCESS", "came_with": w["ship_key"], "sku": "f00zz10991001000", "excess_qty": "4"})
    line = w["iw"].sed_lines.get(sku="F00ZZ10991001000")
    assert line.is_extra and line.excess_qty == 4 and line.description == "TEST WRONG ITEM 1 L"


def test_refused_damage_blocks_silent_nil(client, unloading):
    """UAT IW-2627-000002: two damage saves were refused, then NIL was confirmed seconds later."""
    w = unloading
    c = verified_client(client, w["clerk"])
    url = reverse("iw:unloading", args=[w["iw"].pk])
    pick = f"{w['ship_key']}|{EASYCLEAN}"
    page = c.post(url + "?add=damage", {"kind": "DAMAGE", "pick": pick, "damage_qty": "2", "damage_kind": "CONTAINER",
                                        "damage_remarks": "carton torn"}).content.decode()
    assert "NOT SAVED" in page and "choose the damaged container" in page and 'action="?add=damage#add"' in page
    rejected = sed.rejected_attempts(w["iw"])
    assert len(rejected) == 1 and rejected[0]["kind"] == "DAMAGE" and rejected[0]["sku"] == "XG9 991"
    assert "Entries that were NOT saved" in c.get(url).content.decode()
    with pytest.raises(IwError, match="was NOT saved"):
        sed.confirm_verification(w["clerk"], w["iw"].pk, {"nil_short_excess", "nil_damage"})
    iw = sed.confirm_verification(w["clerk"], w["iw"].pk, {"nil_short_excess", "nil_damage", "rejected_checked"})
    assert sed.REJECTED_TEXT in iw.sed_declaration["statements"] and len(iw.sed_declaration["rejected_attempts"]) == 1


def test_refused_entry_saved_later_is_cleared(client, unloading):
    w = unloading
    c = verified_client(client, w["clerk"])
    url = reverse("iw:unloading", args=[w["iw"].pk])
    pick = f"{w['ship_key']}|{EASYCLEAN}"
    c.post(url, {"kind": "DAMAGE", "pick": pick, "damage_qty": "2", "damage_kind": "MATERIAL"})   # no remarks
    assert sed.rejected_attempts(w["iw"])
    c.post(url, {"kind": "DAMAGE", "pick": pick, "damage_qty": "2", "damage_kind": "MATERIAL",
                 "damage_remarks": "leaking", "damage_rate": "45"})
    assert sed.rejected_attempts(w["iw"]) == []



def test_rate_times_pieces(unloading):
    """IW-2627-000003 (22-Oct-2026): 2 damaged pieces at Rs 100 must claim Rs 200, not Rs 100."""
    w = unloading
    line = sed.save_line(w["clerk"], w["iw"].pk, _in(w, EASYCLEAN, damage_qty="2", damage_kind="MATERIAL",
                                                     damage_remarks="dented", damage_rate="100", short_qty="1",
                                                     short_rate="40.5"))
    assert (line.damage_rate, line.damage_value, line.short_value) == (100, Decimal("200.00"), Decimal("40.50"))
    assert sed.totals(w["iw"]).claimable == Decimal("240.50")
