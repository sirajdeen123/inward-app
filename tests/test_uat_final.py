"""UAT final round (18-Oct-2026): cash register, user-name case, organisations from the IW screen, Colour Bank /
Non-Stock duplicates and item detail, excess value, Ship date on the MRA, 12-hour times."""
from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from inward.accounts.services import PermissionDenied
from inward.cash import services as cash
from inward.cash.models import CashEntry
from inward.core.models import Organisation
from inward.iw import linking, mra, orgs, unloading
from inward.iw.models import Iw
from inward.iw.services import IwError
from tests.helpers import verified_client
from tests.test_checklist import world  # noqa: F401  (fixture)
from tests.test_mra import verified  # noqa: F401  (fixture)
from tests.test_sed import unloading as unloading_iw  # noqa: F401  (fixture)
from tests.test_unloading import ready  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db


def _pay(w, **kw):
    data = {"tonnage": "13.2", "status": "PAID", "payment_mode": "CASH"} | kw
    return unloading.record(w["clerk"], w["iw"].pk, unloading.UnloadingInput(**data))


def test_cash_flows_from_clerk_to_cashier_to_manager(ready, make_user):  # noqa: F811
    w = ready
    cashier, cfa = make_user("cashier1", "CASHIER"), make_user("cfa_c", "CFA_MANAGER")
    rec = _pay(w)                                                       # Rs 3960 paid in cash
    pending = CashEntry.objects.get(state="PENDING")
    assert pending.amount == Decimal("3960.00") and w["iw"].iw_no in pending.description
    assert cash.balance() == 0 and cash.position().to_receive == Decimal("3960.00")
    _pay(w, status="PART_PAID", paid_amount="3000", payment_mode="CASH")  # clerk corrects before handing over
    assert list(CashEntry.objects.filter(state="PENDING").values_list("amount", flat=True)) == [Decimal("3000.00")]
    cash.receive(cashier, CashEntry.objects.get(state="PENDING").pk)
    assert cash.balance() == Decimal("3000.00")
    rec.refresh_from_db()
    _pay(w, status="PART_PAID", paid_amount="2500", payment_mode="CASH")  # after hand-over: a correction entry
    adj = CashEntry.objects.get(state="PENDING")
    assert adj.kind == "ADJUST" and adj.amount == Decimal("-500.00")
    cash.receive(cashier, adj.pk)
    assert cash.balance() == Decimal("2500.00")
    with pytest.raises(cash.CashError, match="more than the cash in hand"):
        cash.add_expense(cashier, category="Tea / refreshments", amount="9999")
    cash.add_expense(cashier, category="Tea / refreshments", amount="120", paid_to="Tea stall")
    h = cash.record_handover(cashier, amount="2000")
    assert cash.balance() == Decimal("2380.00")                          # not until confirmed
    with pytest.raises(PermissionDenied):
        cash.confirm_handover(cashier, h.pk)                             # the other side confirms
    cash.confirm_handover(cfa, h.pk)
    assert cash.balance() == Decimal("380.00")
    st = cash.statement(date.today().year, date.today().month)
    assert st["closing"] == Decimal("380.00") and st["expenses"] == Decimal("120.00")
    assert cash.statement_xlsx(st)[:2] == b"PK"


def test_cashier_sees_only_the_cash_register(client, ready, make_user):  # noqa: F811
    cashier = make_user("cashier2", "CASHIER")
    _pay(ready)
    c = verified_client(client, cashier)
    resp = c.get(reverse("web:home"))
    assert resp.status_code == 302 and resp.url == reverse("cash:register")
    page = c.get(reverse("cash:register")).content.decode()
    assert "To receive from clerk" in page and ready["iw"].iw_no in page and "IW vehicles" not in page
    assert c.get(reverse("iw:detail", args=[ready["iw"].pk])).status_code == 403
    assert c.get(reverse("iw:list")).status_code == 403
    assert c.get(reverse("cash:statement")).status_code == 200


def test_opening_balance_by_manager_only(make_user, seeded):
    cashier, cfa = make_user("cashier3", "CASHIER"), make_user("cfa_o2", "CFA_MANAGER")
    with pytest.raises(PermissionDenied):
        cash.set_opening(cashier, "500")
    cash.set_opening(cfa, "500")
    with pytest.raises(cash.CashError, match="already set"):
        cash.set_opening(cfa, "100")
    assert cash.balance() == Decimal("500.00")


def test_user_names_ignore_capitals(client, make_user, seeded):
    u = make_user("RUBAN", "DEPOT_CLERK")
    u.refresh_from_db()
    assert u.username == "ruban"
    from inward.accounts.models import User
    nobody = User.objects.create_user("NoRole", password="x-Long-Password-123")
    resp = client.post(reverse("accounts:login"), {"username": "NoRole", "password": "x-Long-Password-123"})
    assert "has no role yet" in resp.content.decode() and nobody.username == "norole"


def test_clerk_adds_organisation_manager_verifies(make_user, seeded):
    from inward.iw import comms
    clerk, cfa = make_user("clerk_org", "DEPOT_CLERK"), make_user("cfa_org", "CFA_MANAGER")
    saved = orgs.save_org(clerk, "777", "Trichy depot", "DEPOT", "trichy@bergerindia.com")
    assert saved.created and not saved.org.verified and saved.emails == ["trichy@bergerindia.com"]
    rows = {r["kind"]: r for r in comms.unverified_emails()}
    assert rows["ORGNEW"]["owner"] == "777 : TRICHY DEPOT" and rows["ORG"]["email"] == "trichy@bergerindia.com"
    comms.verify_email(cfa, "ORGNEW", "777", "Trichy - Depot")
    org = Organisation.objects.get(code="777")
    assert org.verified and org.name == "TRICHY - DEPOT"
    again = orgs.save_org(clerk, "777", "Changed name", "DEPOT", "")
    assert "kept" in again.note and Organisation.objects.get(code="777").name == "TRICHY - DEPOT"


def test_org_panel_endpoint(client, make_user, seeded):
    clerk = make_user("clerk_org2", "DEPOT_CLERK")
    c = verified_client(client, clerk)
    assert "+ Add / update organisation" in c.get(reverse("iw:new")).content.decode()
    d = c.post(reverse("iw:org_save"), {"code": "778", "name": "Salem", "org_type": "DEPOT", "emails": ""}).json()
    assert d["value"] == "778 : SALEM" and "unverified" in d["message"]
    assert "error" in c.post(reverse("iw:org_save"), {"code": "", "name": "x"}).json()


def test_colour_bank_and_non_stock_are_not_entered_twice(world, make_user):  # noqa: F811
    from tests.test_linking import _iw
    clerk = world["clerk"]
    a, b = _iw(clerk, vehicle="TN01CB0001"), _iw(clerk, vehicle="TN52AC1171")
    linking.add_colour_bank(clerk, a.pk, direction="IN", challan_no="cb-1", serial_no="S1")
    linking.add_colour_bank(clerk, a.pk, direction="IN", challan_no="cb-1", serial_no="S2")   # 2nd machine
    with pytest.raises(IwError, match="already on this IW"):
        linking.add_colour_bank(clerk, a.pk, direction="IN", challan_no="CB-1", serial_no="s1")
    with pytest.raises(IwError, match=f"already on {a.iw_no}"):
        linking.add_colour_bank(clerk, b.pk, direction="IN", challan_no="CB-1")
    linking.add_non_stock(clerk, a.pk, sender_name="Marketing", sender_email="", document_no="d-9",
                          lines=[("Calendar", "Nos", "5")])
    with pytest.raises(IwError, match=f"already on {a.iw_no}"):
        linking.add_non_stock(clerk, b.pk, sender_name="marketing", sender_email="", document_no="D-9",
                              lines=[("Calendar", "Nos", "5")])
    items = mra.other_items(Iw.objects.get(pk=a.pk))
    assert [m["serial"] for m in items["colour_bank"]] == ["S1", "S2"]
    assert items["non_stock"] == [{"document": "D-9", "sender": "Marketing", "description": "Calendar", "unit": "Nos",
                                   "qty": "5"}]


def test_mra_shows_ship_date_and_excess_value(verified):  # noqa: F811
    w = verified
    from inward.iw.models import SedLine
    from inward.sit.models import SitShip
    SitShip.objects.filter(pk=w["iw"].ship_links.get().ship_id).update(shipment_date=date(2026, 9, 11))
    SedLine.objects.filter(iw=w["iw"]).update(excess_qty=0)
    line = SedLine.objects.filter(iw=w["iw"]).first()
    SedLine.objects.filter(pk=line.pk).update(excess_qty=2, short_qty=0, short_value=None, excess_value=Decimal("80"))
    c = mra.build(Iw.objects.get(pk=w["iw"].pk))
    assert c["references"][0]["ref"].endswith(" / 11-Sep-2026")
    assert c["sed_totals"]["excess_value"] == "80.00"
    ex = next(m for m in c["memos"] if m["kind"] == "EXCESS")
    assert ex["rows"][0]["value"] == "80.00"
    from inward.iw.sed import totals
    t = totals(Iw.objects.get(pk=w["iw"].pk))
    assert t.excess_value == Decimal("80") and t.claimable == t.short_value + t.damage_value   # not claimable


def test_times_are_12_hour(client, world):  # noqa: F811
    iw = world["iw"]
    c = verified_client(client, world["clerk"])
    page = c.get(reverse("iw:detail", args=[iw.pk])).content.decode()
    assert " AM" in page or " PM" in page
