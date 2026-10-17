"""Screen 04 part B - package counts, unloading charge, settlement and transporter ledger (SoT 6, 7.5)."""
from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from inward.accounts.approvals import decide_and_apply
from inward.accounts.models import ApprovalRequest
from inward.core.models import RateMaster, Transporter
from inward.iw import services, unloading
from inward.iw.models import Iw, TransporterLedgerEntry, UnloadingRecord
from inward.iw.services import IwError
from tests.helpers import verified_client
from tests.test_checklist import world  # noqa: F401  (fixture)
from tests.test_sed import unloading as unloading_iw  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db
S = UnloadingRecord.Status


@pytest.fixture
def ready(unloading_iw):  # noqa: F811
    t = Transporter.objects.create(name="SRI MURUGAN TRANSPORT", search_key="SRIMURUGANTRANSPORT")
    Iw.objects.filter(pk=unloading_iw["iw"].pk).update(transporter=t, gate_register_no="GR-0001")
    unloading_iw["iw"].refresh_from_db()
    unloading_iw["transporter"] = t
    return unloading_iw


def _rec(w, **kw):
    data = {"tonnage": "13.2", "status": "PAID", "payment_mode": "CASH"} | kw
    return unloading.record(w["clerk"], w["iw"].pk, unloading.UnloadingInput(**data))


def test_package_counts_from_checklist(ready):
    w = ready
    rows = {(r.warehouse, r.group): r for r in unloading.counts(w["iw"])}
    assert rows[("RETAIL", "CARTON")].count == 24 and rows[("RETAIL", "LOOSE")].count == 1   # 97 at 4
    assert rows[("RETAIL", "DRUM")].count == 49
    assert rows[("INDUSTRIAL", "EMPTY_PACKING")].unit == "pieces"
    assert ("OUTBOUND", "CARTON") in rows
    unloading.save_counts(w["clerk"], w["iw"].pk, {"RETAIL|DRUM": "48"})
    saved = {(r.warehouse, r.group): r for r in w["iw"].package_counts.all()}
    assert saved[("RETAIL", "DRUM")].count == 48 and saved[("RETAIL", "DRUM")].checklist_count == 49
    unloading.save_counts(w["clerk"], w["iw"].pk, {}, reload=True)
    assert w["iw"].package_counts.get(warehouse="RETAIL", group="DRUM").count == 49


def test_charge_uses_effective_rate_snapshot(ready):
    w = ready
    RateMaster.objects.create(rate_type="UNLOADING_PER_TON", amount=Decimal("350"), effective_from=date(2026, 10, 1))
    rec = _rec(w)   # arrival 25-Sep-2026: the Rs 300 rate still applies
    assert (rec.rate, rec.charge, rec.rate_effective_from) == (Decimal("300.00"), Decimal("3960.00"), date(2026, 4, 1))
    assert unloading.gate_ok(w["iw"]) and not TransporterLedgerEntry.objects.exists()
    with pytest.raises(IwError, match="choose Part paid"):
        _rec(w, paid_amount="1000")
    with pytest.raises(IwError, match="how it was paid"):
        _rec(w, payment_mode="")


def test_part_paid_and_outstanding_go_to_ledger(ready):
    w, t = ready, ready["transporter"]
    rec = _rec(w, status="PART_PAID", paid_amount="2000", payment_mode="UPI", payment_ref="UPI-1")
    assert rec.outstanding == Decimal("1960.00")
    entry = TransporterLedgerEntry.objects.get(iw=w["iw"])
    assert entry.amount == Decimal("1960.00") and entry.transporter == t
    _rec(w, status="OUTSTANDING")
    assert TransporterLedgerEntry.objects.get(iw=w["iw"]).amount == Decimal("3960.00")
    assert unloading.is_settled(w["iw"]) is False
    with pytest.raises(IwError, match="more than the outstanding"):
        unloading.add_receipt(w["clerk"], t.pk, amount="5000", payment_mode="CASH")
    unloading.add_receipt(w["clerk"], t.pk, amount="3960", payment_mode="CASH", reference="R-77")
    assert unloading.is_settled(w["iw"]) is True and unloading.ledger(t).balance == 0
    _rec(w, status="PAID")   # changed to paid: the ledger charge goes away
    assert not TransporterLedgerEntry.objects.filter(iw=w["iw"]).exists()


def test_outstanding_needs_transporter(unloading_iw):  # noqa: F811
    with pytest.raises(IwError, match="transporter"):
        _rec(unloading_iw, status="OUTSTANDING")


def test_exempt_needs_cfa_approval(ready, make_user):
    w = ready
    cfa = make_user("cfa_ul", "CFA_MANAGER")
    with pytest.raises(IwError, match="reason"):
        _rec(w, status="EXEMPT")
    rec = _rec(w, status="EXEMPT", exempt_reason="Company vehicle - no unloading charge")
    assert rec.status == S.EXEMPT_REQUESTED and not unloading.gate_ok(w["iw"])
    decide_and_apply(cfa, rec.exempt_request.pk, True, "ok")
    rec.refresh_from_db()
    assert rec.status == S.EXEMPT and rec.outstanding == 0 and unloading.gate_ok(w["iw"])
    # the CFA Manager may record Exempt directly
    rec = unloading.record(cfa, w["iw"].pk, unloading.UnloadingInput(tonnage="10", status="EXEMPT", exempt_reason="Nil"))
    assert rec.status == S.EXEMPT


def test_changing_to_paid_cancels_pending_exempt(ready):
    w = ready
    req = _rec(w, status="EXEMPT", exempt_reason="Nil").exempt_request
    _rec(w, status="PAID")
    req.refresh_from_db()
    assert req.status == ApprovalRequest.Status.CANCELLED


def test_locked_after_mra_and_void_clears_ledger(ready, make_user):
    w = ready
    _rec(w, status="OUTSTANDING")
    req = services.request_void(w["clerk"], w["iw"], "Wrong vehicle")
    decide_and_apply(make_user("cfa_v", "CFA_MANAGER"), req.pk, True)
    assert not TransporterLedgerEntry.objects.filter(iw=w["iw"]).exists()
    with pytest.raises(IwError, match="MRA is issued"):
        _rec(w)


def test_screens(client, ready):
    w = ready
    c = verified_client(client, w["clerk"])
    url = reverse("iw:charges", args=[w["iw"].pk])
    page = c.get(url).content.decode()
    assert "Not saved - proposal from the checklist" in page and "Rs 300.00 per ton" in page
    resp = c.post(url, {"action": "counts", "pc_RETAIL|DRUM": "49"}, follow=True)
    assert "Package counts saved" in resp.content.decode()
    resp = c.post(url, {"action": "unloading", "tonnage": "13.2", "status": "OUTSTANDING"}, follow=True)
    assert "Rs 3960.00" in resp.content.decode() and "In transporter ledger" in resp.content.decode()
    page = c.get(reverse("iw:ledger")).content.decode()
    assert "SRI MURUGAN TRANSPORT" in page
    resp = c.post(reverse("iw:ledger_detail", args=[w["transporter"].pk]),
                  {"amount": "960", "payment_mode": "CASH", "entry_date": ""}, follow=True)
    page = resp.content.decode()
    assert "Receipt of Rs 960.00" in page and "Rs 3000.00 due" in page


def test_gross_weights_suggest_tonnage(client, ready):
    w = ready
    link = w["iw"].ship_links.get()
    ob = w["iw"].outbound_ships.get()
    unloading.save_weights(w["clerk"], w["iw"].pk, {f"SHIP:{link.pk}": "11,500", f"OUTBOUND:{ob.pk}": "1756"})
    assert unloading.suggested_tonnage(w["iw"]) == (Decimal("13.2"), Decimal("13256.000"))   # cut to one decimal
    with pytest.raises(IwError, match="kg"):
        unloading.save_weights(w["clerk"], w["iw"].pk, {f"SHIP:{link.pk}": "abc"})
    c = verified_client(client, w["clerk"])
    page = c.get(reverse("iw:charges", args=[w["iw"].pk])).content.decode()
    assert 'value="13.2"' in page and "13256 kg = 13.2 t" in page
    rec = _rec(w, tonnage="13.5")
    assert rec.tonnage_from_weights == Decimal("13.2") and rec.tonnage == Decimal("13.5")
    assert "Edited" in c.get(reverse("iw:charges", args=[w["iw"].pk])).content.decode()
