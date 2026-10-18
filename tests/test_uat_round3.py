"""UAT round 3 (17-Oct-2026): masters screen for the CFA Manager, Outbound Transhipment only, status colours,
next-step guide, unloading amount preview, 09:00 / 09:30 report schedule."""
from datetime import datetime

import pytest
from django.urls import reverse

from inward.core.models import LoadType, Organisation
from inward.domain.fy import IST
from inward.iw import ops, outbound, services
from inward.iw.models import Iw
from inward.iw.services import IwError
from tests.helpers import verified_client
from tests.test_checklist import world  # noqa: F401  (fixture)
from tests.test_outbound_vendor import env  # noqa: F401  (fixture)
from tests.test_sed import unloading as unloading_iw  # noqa: F401  (fixture)
from tests.test_unloading import ready  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db


def test_cfa_manager_sees_masters_but_clerk_does_not(make_user, seeded):
    cfa = make_user("cfa_m", "CFA_MANAGER")
    cfa.is_staff = True
    cfa.save()
    assert cfa.has_module_perms("core") and cfa.has_perm("core.change_vendor") and cfa.has_perm("accounts.add_user")
    assert not cfa.has_perm("core.delete_vendor") and not cfa.has_perm("accounts.change_rolepermission")
    clerk = make_user("clerk_m", "DEPOT_CLERK")
    clerk.is_staff = True
    clerk.save()
    assert not clerk.has_perm("core.change_vendor") and not clerk.has_module_perms("core")
    admin = make_user("admin_m", "ADMINISTRATOR")
    admin.is_staff = True
    admin.save()
    assert admin.has_perm("accounts.change_rolepermission")


def test_masters_screen_lists_the_masters(client, make_user, seeded, settings):
    cfa = make_user("cfa_s", "CFA_MANAGER")
    cfa.is_staff = True
    cfa.save()
    c = verified_client(client, cfa)
    page = c.get("/" + settings.INWARD["ADMIN_URL"]).content.decode()
    assert "Vendors" in page and "Transporters" in page and "Users" in page and "Load types" in page
    assert "have permission to view or edit anything" not in page


def test_outbound_transhipment_only(env):  # noqa: F811
    clerk = env["clerk"]
    lt = LoadType.objects.get(code="OUTBOUND_TS")
    assert lt.has_outbound and not lt.has_inbound_sit and lt.active
    data = services.IwData(arrival_at=datetime(2026, 9, 25, 9, 40, tzinfo=IST), load_type=lt, source_org=None,
                           vehicle_no="TN01AB1234")
    with pytest.raises(IwError):
        services.create(clerk, data)                                         # the source of the load is needed
    data.source_org = Organisation.objects.get(code="390")
    iw = services.create(clerk, data)
    Organisation.objects.create(code="094", name="Pondicherry", org_type="DEPOT")
    outbound.add_ship(clerk, iw.pk, ship_id="52611001", destination="094", doc_ltr="80", doc_kg="0",
                      source="MANUAL")
    iw = Iw.objects.get(pk=iw.pk)
    assert iw.status == Iw.Status.MATERIAL_LINKED and ops.actions(iw)[0].text == "Tally every outbound Ship"


def test_status_colour_and_next_step(client, ready):  # noqa: F811
    w = ready
    c = verified_client(client, w["clerk"])
    page = c.get(reverse("iw:detail", args=[w["iw"].pk])).content.decode()
    assert 'class="chip st st-UNLOADING_IN_PROGRESS"' in page and "Next step" in page and "Go &rarr;" in page
    assert 'class="chip st st-UNLOADING_IN_PROGRESS"' in c.get(reverse("iw:list")).content.decode()
    page = c.get(reverse("iw:unloading", args=[w["iw"].pk])).content.decode()
    assert "Next step" in page


def test_unloading_amount_shown_from_weights(client, ready):  # noqa: F811
    from inward.iw import unloading
    w = ready
    link = w["iw"].ship_links.get()
    ob = w["iw"].outbound_ships.get()
    unloading.save_weights(w["clerk"], w["iw"].pk, {f"SHIP:{link.pk}": "11,500", f"OUTBOUND:{ob.pk}": "1756"})
    c = verified_client(client, w["clerk"])
    page = c.get(reverse("iw:charges", args=[w["iw"].pk])).content.decode()
    assert "unloading <b>Rs 3960.00</b>" in page and 'id="u-amount"' in page and 'data-rate="300.00"' in page
    assert "charge.js" in page


def test_cfa_manager_void_is_applied_at_once(client, world, make_user):  # noqa: F811
    from inward.accounts.models import ApprovalRequest
    from inward.core.models import AuditEvent
    cfa = make_user("cfa_v", "CFA_MANAGER")
    iw = Iw.objects.get(pk=world["iw"].pk)
    req = services.request_void(cfa, iw, "Created twice")
    assert req.status == ApprovalRequest.Status.APPROVED and req.pk is None      # no request left waiting
    assert Iw.objects.get(pk=iw.pk).status == Iw.Status.VOID and not ApprovalRequest.objects.exists()
    assert AuditEvent.objects.filter(action="APPROVER_DIRECT_ACTION", iw_id=iw.pk).exists()


def test_clerk_void_still_needs_the_cfa_manager(client, world, make_user):  # noqa: F811
    from inward.accounts.models import ApprovalRequest
    iw = Iw.objects.get(pk=world["iw"].pk)
    req = services.request_void(world["clerk"], iw, "Wrong vehicle")
    assert req.status == ApprovalRequest.Status.PENDING and Iw.objects.get(pk=iw.pk).status != Iw.Status.VOID
    from inward.accounts.approvals import decide_and_apply
    with pytest.raises(Exception, match="own request"):
        decide_and_apply(world["clerk"], req.pk, True)


def test_stuck_own_request_can_be_applied_from_the_screen(client, world, make_user):  # noqa: F811
    """UAT IW-2627-000005: the CFA Manager's own VOID request waited for an approval nobody could give."""
    from inward.accounts.services import request_approval
    cfa = make_user("cfa_o", "CFA_MANAGER")
    iw = Iw.objects.get(pk=world["iw"].pk)
    req = request_approval(cfa, "IW_VOID", "iw", iw.pk, "Created twice", payload={"iw_no": iw.iw_no})  # old way
    c = verified_client(client, cfa)
    page = c.get(reverse("accounts:approvals")).content.decode()
    assert "Your own requests" in page and "Apply now (you are the approver)" in page
    c.post(reverse("accounts:decide", args=[req.pk]), {"decision": "self_apply"})
    assert Iw.objects.get(pk=iw.pk).status == Iw.Status.VOID
    req.refresh_from_db()
    assert req.status == "CANCELLED" and "directly by the approver" in req.decision_note


def test_sku_search_by_code_floor_and_words(client, world):  # noqa: F811
    from inward.core.models import PackageType, SkuPackaging
    carton = PackageType.objects.get(code="CARTON")
    SkuPackaging.objects.update_or_create(sku="F00XG90991003600", defaults=dict(
        floor_sku="XG9 991", description="WEATHERCOAT ANTI DIRT BASE 3.6 L", measure_family="LTR",
        pack_size="3.6", package_type=carton, units_per_package=4))
    c = verified_client(client, world["clerk"])
    url = reverse("iw:sku_search")
    for q in ("xg9 991", "XG9991", "xg99", "F00XG9", "anti dirt 3.6"):
        items = c.get(url, {"q": q}).json()["items"]
        assert any(i["sku"] == "F00XG90991003600" for i in items), q
    first = c.get(url, {"q": "F00XG90991003600"}).json()["items"][0]
    assert first == {"sku": "F00XG90991003600", "floor": "XG9 991", "description": "WEATHERCOAT ANTI DIRT BASE 3.6 L",
                     "uom": "L", "pack": "3.6"}
    assert c.get(url, {"q": "x"}).json()["items"] == []


def test_manual_rows_offer_the_sku_lookup(client, env):  # noqa: F811
    from tests.test_outbound_vendor import _iw
    clerk = env["clerk"]
    iw = _iw(clerk)
    Organisation.objects.create(code="094", name="Pondicherry", org_type="DEPOT")
    ship = outbound.add_ship(clerk, iw.pk, ship_id="52611001", destination="094", doc_ltr="80", doc_kg="0",
                             source="MANUAL")
    c = verified_client(client, clerk)
    page = c.get(reverse("iw:outbound_manual", args=[ship.pk])).content.decode()
    assert "data-sku-lookup" in page and "data-autoqty" in page and "sku_lookup.js" in page
    vendor_iw = _iw(clerk, code="VENDOR", vehicle="TN01VV1")
    page = c.get(reverse("iw:vendor", args=[vendor_iw.pk])).content.decode()
    assert "data-sku-lookup" in page and "sku_lookup.js" in page
