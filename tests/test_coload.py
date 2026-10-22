"""Co-loaded truck (22-Oct-2026, live IW-2627-000008/9 voided): two consignments - own source and LR each - on one
truck: two IWs, ONE unloading checklist, ONE unloading charge, each IW keeps its own MRA and package counts."""
from datetime import datetime
from decimal import Decimal

import pytest
from django.urls import reverse

from inward.core import packaging
from inward.core.models import LoadType, Organisation
from inward.domain.fy import IST
from inward.iw import checklist, coload, linking, mra, services, unloading
from inward.iw.models import Iw
from inward.iw.services import IwError
from inward.sit import services as sit
from tests.helpers import sit_html, verified_client
from tests.test_packaging import _xlsx

pytestmark = pytest.mark.django_db
A = ("'105:Salem", "60000001", "13-OCT-2026")
B = ("'036:Calicut", "60000002", "14-OCT-2026")


def _data(org, lr, **kw):
    return services.IwData(arrival_at=datetime(2026, 10, 22, 11, 50, tzinfo=IST),
                           load_type=LoadType.objects.get(code="INTER_DEPOT"), source_org=org, vehicle_no="TN39CE7382",
                           driver_name="Selvam", driver_mobile="9500555081", transporter_name="Sri Krishna Roadlines",
                           lr_no=lr, gate_register_no="1439", **kw)


@pytest.fixture
def truck(seeded, tmp_path, settings, make_user):
    settings.DOCUMENT_ROOT = tmp_path / "docs"
    clerk = make_user("clerk_co", "DEPOT_CLERK")
    a = Organisation.objects.create(code="105", name="Salem", org_type="DEPOT")
    b = Organisation.objects.create(code="036", name="Calicut", org_type="DEPOT")
    sit.import_sit_file(sit_html(tmp_path / "s.xls", [
        (*A, "F00XG90991003600", "EASY CLEAN . W1 BASE-.-3.6 L", "RETAIL", "L", "3.6", "97"),
        (*A, "F002980000020000", "WEATHERCOAT GLOW WHITE-WHITE-20 L", "RETAIL", "L", "20", "10"),
        (*B, "F00XG90991003600", "EASY CLEAN . W1 BASE-.-3.6 L", "RETAIL", "L", "3.6", "3")]), source="MANUAL")
    up = packaging.create_upload(clerk, _xlsx(tmp_path / "p.xlsx", [
        ("F002980000020000", "WEATHERCOAT GLOW WHITE 20 L", "L", 20, "DRUM", 1),
        ("F00XG90991003600", "EASY CLEAN W1 BASE 3.6 L", "L", 3.6, "CARTON BOX", 4)]), "p.xlsx")
    packaging.apply_upload(clerk, up.pk)
    iw1 = services.create(clerk, _data(a, "10907"))
    return {"clerk": clerk, "a": a, "b": b, "iw1": iw1}


def test_second_consignment_on_the_same_truck(truck):
    clerk, b, iw1 = truck["clerk"], truck["b"], truck["iw1"]
    with pytest.raises(IwError, match="Same truck - another consignment"):
        services.create(clerk, _data(b, "10908"))                      # asked, not silently accepted
    with pytest.raises(IwError, match="Duplicate entry"):
        services.create(clerk, _data(b, "10907", coload=True))         # same LR is still a duplicate
    iw2 = services.create(clerk, _data(b, "10908", coload=True))
    assert iw2.coload_lead == iw1 and [m.pk for m in coload.members(iw2)] == [iw1.pk, iw2.pk]
    assert coload.members(iw1) == coload.members(iw2) and coload.charge_iw(iw2) == iw1
    other = services.create(clerk, services.IwData(**{**_data(truck["a"], "1").__dict__, "vehicle_no": "KA01AB1"}))
    assert coload.members(other) == [other] and not coload.is_group(other)   # a normal IW is untouched
    with pytest.raises(IwError, match="has no other IW"):
        services.create(clerk, services.IwData(**{**_data(b, "2").__dict__, "vehicle_no": "KA99ZZ9", "coload": True}))


def test_one_checklist_one_charge_own_mra(truck):
    clerk, b, iw1 = truck["clerk"], truck["b"], truck["iw1"]
    iw2 = services.create(clerk, _data(b, "10908", coload=True))
    linking.link_ship(clerk, iw1.pk, A[1], gross_kg="1200")
    linking.link_ship(clerk, iw2.pk, B[1], gross_kg="300")
    checklist.generate(clerk, iw2.pk)                                  # generated from either IW ...
    iw1.refresh_from_db()
    iw2.refresh_from_db()
    assert iw1.status == iw2.status == Iw.Status.UNLOADING_IN_PROGRESS   # ... it is on both
    s1, s2 = iw1.checklists.first(), iw2.checklists.first()
    assert s1.content["sheets"] == s2.content["sheets"] and s1.content["truck"] == [str(iw1), str(iw2)]
    retail = s1.content["sheets"][0]
    assert retail["title"] == "RETAIL WAREHOUSE" and retail["lines"] == 2      # Easy Clean once for the truck
    ec = next(ln for g in retail["groups"] for ln in g["lines"] if ln["sku"] == "F00XG90991003600")
    assert ec["pieces"] == "100" and (ec["full"], ec["loose"]) == (24, "4")
    assert ec["parts"] == [{"source": f"{iw1} · Ship {A[1]}", "pieces": "97"}, {"source": f"{iw2} · Ship {B[1]}", "pieces": "3"}]
    assert not checklist.is_stale(iw1, s1) and not checklist.is_stale(iw2, s2)
    # package counts stay per IW (each source's MRA shows its own packages)
    own1 = {p.group: p.count for p in unloading.from_checklist(iw1)}
    own2 = {p.group: p.count for p in unloading.from_checklist(iw2)}
    assert own1 == {"CARTON": 24, "LOOSE": 1, "DRUM": 10} and own2 == {"LOOSE": 3}
    # the unloading charge: once, on the lead IW, for the whole truck
    assert unloading.suggested_tonnage(iw1) == (Decimal("1.5"), Decimal("1500"))
    with pytest.raises(IwError, match="recorded once, on"):
        unloading.record(clerk, iw2.pk, unloading.UnloadingInput(tonnage="1.5", status="OUTSTANDING"))
    assert not unloading.gate_ok(iw2)
    unloading.record(clerk, iw1.pk, unloading.UnloadingInput(tonnage="1.5", status="OUTSTANDING"))
    assert unloading.gate_ok(iw2) and unloading.gate_ok(iw1)
    c2 = mra.build(iw2)
    assert c2["unloading"] is None and c2["unloading_with"]["iw"] == str(iw1)
    assert mra.build(iw1)["unloading"]["charge"] and mra.build(iw1)["unloading_with"] is None


def test_screens(client, truck):
    clerk, b, iw1 = truck["clerk"], truck["b"], truck["iw1"]
    c = verified_client(client, clerk)
    resp = c.post(reverse("iw:new"), {**{k: v for k, v in {
        "arrival_at": "2026-10-22T11:50", "load_type": LoadType.objects.get(code="INTER_DEPOT").pk,
        "source_org": "036 : Calicut", "vehicle_no": "TN39CE7382", "driver_name": "Selvam",
        "driver_mobile": "9500555081", "transporter": "Sri Krishna Roadlines", "lr_no": "10908",
        "gate_register_no": "1439"}.items()}, "action": "create"}, follow=True)
    assert "Same truck - another consignment" in resp.content.decode()
    iw2 = services.create(clerk, _data(b, "10908", coload=True))
    page = c.get(reverse("iw:detail", args=[iw2.pk])).content.decode()
    assert "Co-loaded truck TN39CE7382" in page and str(iw1) in page
    assert "recorded once, on" in c.get(reverse("iw:charges", args=[iw2.pk])).content.decode()
