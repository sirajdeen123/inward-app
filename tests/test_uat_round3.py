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
