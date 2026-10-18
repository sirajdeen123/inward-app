"""IRR reconciliation engine (18-Oct-2026) - separate from the inward flow."""
import shutil
from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from inward.irr.models import IrrTransaction
from inward.irrecon import engine
from inward.irrecon.models import IrrMatch
from tests.conftest import sample
from tests.helpers import verified_client
from tests.test_erp_recon import _batch

pytestmark = pytest.mark.django_db
A, B = "F00XG90991003600", "F003860991001000"


def _row(b, typ, trx, sku, pcs, tt, src="", dst="", reason="", day=date(2026, 10, 20), lot="L1", ltr=None, ship=""):
    return IrrTransaction.objects.create(
        batch=b, type=typ, type_key=typ.upper(), trx_number=trx, trx_date=day, sku=sku, no_of_pack=Decimal(pcs),
        transaction_type=tt, source_subinv=src, dest_subinv=dst, reason_description=reason, lot_number=lot,
        volume_ltr=Decimal(ltr) if ltr is not None else None, shipping_id=ship)


def _result(row):
    return IrrMatch.objects.get(row=row)


def test_rules(seeded):
    b = _batch()
    i1 = _row(b, "Subinventory Transfer", "1000", A, 4, "Issue", "035OUT", "035RCK", "59 / Inter Locator Transfer")
    r1 = _row(b, "Subinventory Transfer", "1001", A, 4, "Receipt", "035RCK", "035OUT", "59 / Inter Locator Transfer")
    i2 = _row(b, "Subinventory Transfer", "2000", A, 2, "Issue", "035FLR", "035DMG", "68 / Damage - Local Carrier")
    r2 = _row(b, "Subinventory Transfer", "2001", A, 2, "Receipt", "035DMG", "035FLR", "68 / Damage - Local Carrier")
    lonely = _row(b, "Subinventory Transfer", "3000", A, 1, "Issue", "035FLR", "035RCK")
    p_i = _row(b, "Internal Order Pick", "4000", A, 6, "Issue", "035RCK", "035OUT")
    p_r = _row(b, "Internal Order Pick", "4001", A, 6, "Receipt", "035OUT", "035RCK")
    ship = _row(b, "Int Order Intr Ship", "52124799", A, 6, "Issue", "035OUT", "", "DEPOT TRANSFER", ship="52124799")
    c_i = _row(b, "Miscellaneous Issue", "5000", B, 1, "Issue", "035RCK", "", "33 / Depot-Mixing/Tinting", ltr="1")
    c_r = _row(b, "Miscellaneous Receipt", "5009", "F003860991CLRNTS", 6494, "Receipt", "035BO", "",
               "33 / Depot-Mixing/Tinting", ltr="1.000076")
    ls = _row(b, "Miscellaneous Issue", "6000", A, 1, "Issue", "035COMMON", "", "08 / Depot-Short Stock Receipt")
    rma = _row(b, "Rma Receipt", "7000", A, 3, "Receipt", "035DMG", "", "DAMAGE CUSTOMER RETURN")
    zero = _row(b, "Residual Qty Receipt", "8000", A, 0, "", "035BO", "035OUT")
    engine.run()
    assert _result(i1).status == _result(r1).status == "MATCHED" and _result(i1).group == "ST:1000"
    assert _result(i2).status == "OPEN" and "without an approved internal damage report" in _result(i2).note
    assert _result(lonely).status == "OPEN" and "No matching receipt" in _result(lonely).note
    assert {_result(x).status for x in (p_i, p_r, ship)} == {"MATCHED"}
    assert _result(c_i).status == _result(c_r).status == "MATCHED"
    assert _result(ls).status == "OPEN" and "not linked to an IW" in _result(ls).note
    assert _result(rma).status == _result(zero).status == "INFO"


def test_dmg_transfer_explained_by_internal_damage_and_marks_survive(seeded, make_user):
    from inward.damage.models import DamageReport, DamageTrx
    b = _batch()
    i = _row(b, "Subinventory Transfer", "2000", A, 2, "Issue", "035FLR", "035DMG", "68 / Damage - Local Carrier")
    _row(b, "Subinventory Transfer", "2001", A, 2, "Receipt", "035DMG", "035FLR", "68 / Damage - Local Carrier")
    lonely = _row(b, "Subinventory Transfer", "3000", A, 1, "Issue", "035FLR", "035RCK")
    sup = make_user("sup_e", "SYSTEM_SUPERVISOR")
    rep = DamageReport.objects.create(number="IDR-2627-000001", fy_code="2627", found_on=date(2026, 10, 20),
                                      status="TRANSFERRED", created_by=sup)
    DamageTrx.objects.create(report=rep, trx_no="2001", entered_by=sup)      # the receipt half was entered
    engine.run()
    assert _result(i).status == "EXPLAINED" and _result(i).link == "IDR-2627-000001"
    from inward.irrecon.models import IrrMark
    IrrMark.objects.create(row=lonely, reason="posted again next day", marked_by=sup)
    engine.run()
    assert _result(lonely).status == "EXPLAINED" and "posted again next day" in _result(lonely).note


def test_screen_and_marking(client, seeded, make_user):
    b = _batch()
    lonely = _row(b, "Subinventory Transfer", "3000", A, 1, "Issue", "035FLR", "035RCK")
    engine.run()
    sup = make_user("sup_s", "SYSTEM_SUPERVISOR")
    c = verified_client(client, sup)
    page = c.get(reverse("irrecon:home")).content.decode()
    assert "IRR reconciliation engine" in page and "No matching receipt half" in page
    c.post(reverse("irrecon:mark"), {"row": [lonely.pk], "reason": "checked with Oracle - reversed"})
    assert _result(lonely).status == "EXPLAINED"
    clerk = make_user("clerk_s", "DEPOT_CLERK")
    c2 = verified_client(client, clerk)
    assert c2.post(reverse("irrecon:run")).status_code == 403       # supervisor / CFA Manager only
    assert c2.get(reverse("irrecon:home") + "?xlsx=1")["Content-Type"].startswith("application/vnd.openxml")


@pytest.mark.samples
def test_engine_on_the_sample_irr(seeded, make_user, settings, tmp_path, capsys):
    from inward.irr import services as irr
    settings.DOCUMENT_ROOT = tmp_path / "docs"
    path = tmp_path / "irr.xlsx"
    shutil.copy(sample("IRR.xlsx"), path)
    irr.import_irr(make_user("clerk_x", "DEPOT_CLERK"), path, "IRR.xlsx")     # runs the engine
    total = IrrMatch.objects.count()
    open_n = IrrMatch.objects.filter(status="OPEN").count()
    with capsys.disabled():
        from django.db.models import Count
        print("\nENGINE ON SAMPLE:", total, "rows,", open_n, "open")
        for x in IrrMatch.objects.values("family", "status").annotate(n=Count("pk")).order_by("family", "status"):
            print("  ", x)
        for m in IrrMatch.objects.filter(status="OPEN").values("family", "note")[:0]:
            print(m)
    assert total == 18261 and open_n < total * 0.05


def test_period_and_summary_mail(client, seeded, make_user):
    from datetime import timedelta

    from django.utils import timezone

    from inward.iw import reports
    b = _batch()
    today = timezone.localdate()
    _row(b, "Subinventory Transfer", "3000", A, 1, "Issue", "035FLR", "035RCK", day=today - timedelta(days=1))
    _row(b, "Rma Receipt", "7000", A, 3, "Receipt", "035RCK", "", "RETURN", day=today - timedelta(days=40))
    engine.run()
    from inward.irrecon.summary import summary
    y = today - timedelta(days=1)
    assert summary(y, y)["total"] == 1 and summary(y, y)["open"] == 1 and summary()["total"] == 2
    sup = make_user("sup_p", "SYSTEM_SUPERVISOR")
    c = verified_client(client, sup)
    page = c.get(reverse("irrecon:home"), {"from": y.isoformat(), "to": y.isoformat(), "status": ""}).content.decode()
    assert "Show period" in page and "Yesterday" in page and "3000" in page and "7000" not in page
    r = reports.irr_summary(today)
    assert r["yesterday"]["open"] == 1 and r["y_open_count"] == 1 and r["older_open"] == 0
    assert [x["reason"] for x in r["reasons"]] == ["Subinventory Transfer"]          # only yesterday's rows
    assert r["reasons"][0]["pcs_out"] == 1 and r["reasons"][0]["open"] == 1
    from django.template.loader import render_to_string
    html = render_to_string("irrecon/mail_summary.html", {"r": r, "b": {}, "stamp": "x"})
    assert "IRR Reconciliation Summary" in html and "No matching receipt half" in html
    assert "Reason Description" in html and "RETURN" not in html                     # the 40-day-old return
    page = c.get(reverse("irrecon:home"), {"from": y.isoformat(), "to": y.isoformat()}).content.decode()
    assert "By IRR Reason Description" in page
