"""Screen 05 - receipts, LSCCR, SED TRX, IRR import and reconciliation (SoT 8.1 - 8.6, table 10)."""
import shutil
from datetime import date, datetime
from decimal import Decimal

import pytest
from django.urls import reverse

from inward.core.models import ImportBatch, LoadType, Vendor
from inward.domain.fy import IST
from inward.irr import services as irr
from inward.irr.models import IrrTransaction
from inward.iw import erp, recon, services, vendor
from inward.iw.models import Iw
from inward.iw.services import IwError
from tests.conftest import sample
from tests.helpers import verified_client
from tests.test_checklist import world  # noqa: F401  (fixture)
from tests.test_mra import verified  # noqa: F401  (fixture)
from tests.test_sed import EASYCLEAN, WEATHERCOAT
from tests.test_sed import unloading as unloading_iw  # noqa: F401  (fixture)
from tests.test_unloading import ready  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db
SHIP = "52497520"
PUTTY, BULK = "F00B470633002001", "2010370991BULKPC"
DOC = {WEATHERCOAT: (49, "980", None), EASYCLEAN: (97, "349.2", None), PUTTY: (70, None, "140"), BULK: (25, None, None)}


def _batch():
    return ImportBatch.objects.create(kind="IRR", source="MANUAL", filename="t.xlsx", sha256=str(ImportBatch.objects.count()))


def irr_rows(rows):
    """rows: (type, trx_number, sku, pieces, ltr, kg, transaction_type, dest_subinv)"""
    b = _batch()
    for typ, trx, sku, pcs, ltr, kg, ttype, dest in rows:
        no, _, ref = trx.partition(" / ")
        IrrTransaction.objects.create(
            batch=b, type=typ, type_key=typ.upper(), trx_number=trx, trx_date=date(2026, 9, 26),
            receipt_no=no if ref else "", trx_reference=ref, sku=sku, no_of_pack=Decimal(pcs),
            volume_ltr=Decimal(ltr) if ltr else None, weight_kg=Decimal(kg) if kg else None,
            transaction_type=ttype, dest_subinv=dest, shipping_id=ref if ref and typ.startswith("Int") else "")


def full_receipt(no="42637", ship=SHIP, less=None):
    less = less or {}
    rows = []
    for sku, (pcs, ltr, kg) in DOC.items():
        n = pcs - less.get(sku, 0)
        f = Decimal(n) / pcs
        rows.append(("Int Req Intr Rcpt", f"{no} / {ship}", sku, n, ltr and str(Decimal(ltr) * f),
                     kg and str(Decimal(kg) * f), "Receipt", ""))
    irr_rows(rows)


@pytest.fixture
def at_erp(verified):  # noqa: F811
    """IW with Short 3 x EASY CLEAN and Damage 1 x WEATHERCOAT, Screen 04 done."""
    Iw.objects.filter(pk=verified["iw"].pk).update(status=Iw.Status.MRA_ISSUED)
    verified["iw"] = Iw.objects.get(pk=verified["iw"].pk)
    verified["link"] = verified["iw"].ship_links.get()
    return verified


def _by_track(iw):
    return {r.track: r for r in recon.reconcile(iw)}


def test_direct_factory_full_receipt_and_lsccr_trx(at_erp):
    w, clerk = at_erp, at_erp["clerk"]
    ref = f"SHIP:{w['link'].pk}"
    erp.add_receipt(clerk, w["iw"].pk, ref, "NORMAL", "42637")          # no LSCCR needed for the receipt
    assert Iw.objects.get(pk=w["iw"].pk).status == Iw.Status.ERP_IN_PROGRESS
    res = _by_track(w["iw"])
    assert res[f"Ship {SHIP}"].status == "NOT_FOUND"
    assert res[f"SED Ship {SHIP} short"].status == "PENDING"
    full_receipt()                       # always the full document quantity, also with a short on the LSCCR
    assert _by_track(w["iw"])[f"Ship {SHIP}"].status == "FULL"
    memos = {m.kind: m for m in erp.memos(w["iw"])}
    assert memos["SHORT"].direct and not memos["SHORT"].lsccr_no
    with pytest.raises(IwError, match="LSCCR No first"):
        erp.add_sed_trx(clerk, w["iw"].pk, memos["SHORT"].key, "3560000101")
    erp.set_lscrr(clerk, w["link"].pk, "lsc617983")
    memos = {m.kind: m for m in erp.memos(w["iw"])}
    assert memos["SHORT"].lsccr_no == "LSC617983"
    erp.add_sed_trx(clerk, w["iw"].pk, memos["SHORT"].key, "3560000101")
    erp.add_sed_trx(clerk, w["iw"].pk, memos["DAMAGE"].key, "3562905914")   # the Receipt half of the pair
    irr_rows([("Miscellaneous Issue", "3560000101", EASYCLEAN, 3, "10.8", None, "Issue", ""),
              ("Subinventory Transfer", "3562905913", WEATHERCOAT, 1, "20", None, "Issue", "035DMG"),
              ("Subinventory Transfer", "3562905914", WEATHERCOAT, 1, "20", None, "Receipt", "035FLR")])
    res = _by_track(w["iw"])
    assert res[f"SED Ship {SHIP} short"].status == "FULL" and res[f"SED Ship {SHIP} damage"].status == "FULL"
    rr = recon.run(clerk, w["iw"].pk)
    assert rr.all_ok and Iw.objects.get(pk=w["iw"].pk).status == Iw.Status.RECON_COMPLETE


def test_partial_and_over_receipt(at_erp):
    w, clerk = at_erp, at_erp["clerk"]
    erp.add_receipt(clerk, w["iw"].pk, f"SHIP:{w['link'].pk}", "NORMAL", "42637")
    full_receipt(less={EASYCLEAN: 10})
    assert _by_track(w["iw"])[f"Ship {SHIP}"].status == "PARTIAL"
    erp.add_receipt(clerk, w["iw"].pk, f"SHIP:{w['link'].pk}", "NORMAL", "42700")   # one Ship, many receipts
    irr_rows([("Int Req Intr Rcpt", f"42700 / {SHIP}", EASYCLEAN, 12, "43.2", None, "Receipt", "")])
    r = _by_track(w["iw"])[f"Ship {SHIP}"]
    assert r.status == "REVIEW" and any("IRR 99" in n for n in r.notes)


def test_lsccr_request_set_rows(at_erp):
    """As IW-2627-000001 in UAT: full receipt, then the request set's Misc Issue / Misc Receipt (reason 08)."""
    w, clerk = at_erp, at_erp["clerk"]
    from inward.iw.models import SedLine
    SedLine.objects.filter(iw=w["iw"], sku=EASYCLEAN).update(short_qty=0, excess_qty=2, short_value=None)
    erp.set_lscrr(clerk, w["link"].pk, "LSC617983")
    erp.add_receipt(clerk, w["iw"].pk, f"SHIP:{w['link'].pk}", "NORMAL", "42637")
    with pytest.raises(IwError, match="Stock Receipt numbers"):
        erp.add_receipt(clerk, w["iw"].pk, f"SHIP:{w['link'].pk}", "EXCESS", "3570001")
    full_receipt()
    memos = {m.kind: m for m in erp.memos(w["iw"])}
    erp.add_sed_trx(clerk, w["iw"].pk, memos["EXCESS"].key, "3672576526")
    erp.add_sed_trx(clerk, w["iw"].pk, memos["DAMAGE"].key, "3672576518")
    irr_rows([("Miscellaneous Receipt", "3672576526", EASYCLEAN, 2, "7.2", None, "Receipt", ""),
              ("Miscellaneous Issue", "3672576518", WEATHERCOAT, 1, "20", None, "Issue", "")])
    r = _by_track(w["iw"])
    assert r[f"Ship {SHIP}"].status == "FULL" and f"Ship {SHIP} excess" not in r
    assert r[f"SED Ship {SHIP} excess"].status == "FULL"
    assert r[f"SED Ship {SHIP} damage"].status == "FULL"          # LSCCR damage as a Miscellaneous Issue
    assert any("not the LSCCR request set" in n for n in r[f"SED Ship {SHIP} damage"].notes)
    IrrTransaction.objects.filter(trx_number__in=["3672576526", "3672576518"]).update(
        reason_description="08 / Depot-Short Stock Receipt")
    r = _by_track(w["iw"])
    assert not r[f"SED Ship {SHIP} damage"].notes and not r[f"SED Ship {SHIP} excess"].notes


def test_receipt_of_another_ship_and_wrong_trx_type(at_erp):
    w, clerk = at_erp, at_erp["clerk"]
    erp.add_receipt(clerk, w["iw"].pk, f"SHIP:{w['link'].pk}", "NORMAL", "42637")
    full_receipt(ship="51882836")
    r = _by_track(w["iw"])[f"Ship {SHIP}"]
    assert r.status == "REVIEW" and any("belongs to Ship 51882836" in n for n in r.notes)
    erp.set_lscrr(clerk, w["link"].pk, "LSC1")
    memo = {m.kind: m for m in erp.memos(w["iw"])}["SHORT"]
    erp.add_sed_trx(clerk, w["iw"].pk, memo.key, "3560000101")
    irr_rows([("Miscellaneous Receipt", "3560000101", EASYCLEAN, 3, "10.8", None, "Receipt", "")])
    assert _by_track(w["iw"])[f"SED Ship {SHIP} short"].status == "REVIEW"
    with pytest.raises(IwError, match="already entered"):
        erp.add_sed_trx(clerk, w["iw"].pk, memo.key, "3560000101")


def test_lsccr_change_after_trx_needs_supervisor(at_erp, make_user):
    w, clerk = at_erp, at_erp["clerk"]
    with pytest.raises(IwError, match="LSCCR number"):
        erp.set_lscrr(clerk, w["link"].pk, " ")
    erp.set_lscrr(clerk, w["link"].pk, "LS-1")
    erp.set_lscrr(clerk, w["link"].pk, "LS-2")                         # no TRX yet: the clerk may correct it
    erp.add_sed_trx(clerk, w["iw"].pk, erp.memos(w["iw"])[0].key, "3560000101")
    with pytest.raises(IwError, match="System Supervisor"):
        erp.set_lscrr(clerk, w["link"].pk, "LS-3")
    sup = make_user("sup_ls", "SYSTEM_SUPERVISOR")
    with pytest.raises(IwError, match="reason"):
        erp.set_lscrr(sup, w["link"].pk, "LS-3")
    assert erp.set_lscrr(sup, w["link"].pk, "LS-3", "wrong request set number").lscrr_no == "LS-3"


def test_vendor_po_receipt(seeded, make_user, settings, tmp_path):
    settings.DOCUMENT_ROOT = tmp_path
    clerk = make_user("clerk_po", "DEPOT_CLERK")
    iw = services.create(clerk, services.IwData(arrival_at=datetime(2026, 9, 25, 9, 0, tzinfo=IST),
                                                load_type=LoadType.objects.get(code="VENDOR"), source_org=None,
                                                vehicle_no="TN01AB1"))
    Vendor.objects.create(code="V54", name="SANJAY ENTERPRISES")
    cons = vendor.add_manual(clerk, iw.pk, vendor_code="V54", vendor_name="", po_no="PO-1", challan_no="SE/26-27/084",
                             lines=[("F003990991050002", "PRIMER 50 K", "K", "300", "15000")], warehouse="INDUSTRIAL")
    Iw.objects.filter(pk=iw.pk).update(status=Iw.Status.MRA_ISSUED)
    with pytest.raises(IwError, match="PO Receipt"):
        erp.add_receipt(clerk, iw.pk, f"VENDOR:{cons.pk}", "NORMAL", "42663")
    erp.add_receipt(clerk, iw.pk, f"VENDOR:{cons.pk}", "PO", "42663")
    irr_rows([("Po Receipt", "42663 / SE/26-27/O84", "F003990991050002", 300, None, "15000", "Receipt", "")])
    r = recon.reconcile(Iw.objects.get(pk=iw.pk))[0]
    assert r.status == "FULL" and any(n.startswith("Info: challan text") for n in r.notes)


@pytest.mark.samples
def test_irr_import_and_overlap(seeded, make_user, settings, tmp_path):
    settings.DOCUMENT_ROOT = tmp_path / "docs"
    clerk = make_user("clerk_irr", "DEPOT_CLERK")
    path = tmp_path / "irr.xlsx"
    shutil.copy(sample("IRR.xlsx"), path)
    b = irr.import_irr(clerk, path, "IRR.xlsx")
    assert b.accepted_count == IrrTransaction.objects.count() == 18261 and b.status == "ACCEPTED"
    with pytest.raises(irr.AlreadyImported):
        irr.import_irr(clerk, path, "IRR again.xlsx")
    IrrTransaction.objects.filter(trx_number="42637 / 51882836").delete()  # history missing some rows
    path.write_bytes(path.read_bytes() + b"\0")                             # same content, new file hash
    b2 = irr.import_irr(clerk, path, "IRR overlap.xlsx")
    assert b2.summary["new_groups"] == 1 and b2.summary["identical"] > 1000
    assert IrrTransaction.objects.count() == 18261


def test_screens(client, at_erp):
    w = at_erp
    c = verified_client(client, w["clerk"])
    page = c.get(reverse("iw:erp", args=[w["iw"].pk])).content.decode()
    assert f"Ship {SHIP}" in page and "LSCCR No needed before the SED TRX Nos" in page and "LSCCR request set" in page
    c.post(reverse("iw:lscrr_set", args=[w["iw"].pk, w["link"].pk]), {"lscrr_no": "LSC617983"})
    assert "LSCCR LSC617983" in c.get(reverse("iw:erp", args=[w["iw"].pk])).content.decode()
    resp = c.post(reverse("iw:receipt_add", args=[w["iw"].pk]),
                  {"ref": f"SHIP:{w['link'].pk}", "kind": "NORMAL", "receipt_no": "42637"}, follow=True)
    assert "IRR verification pending" in resp.content.decode()
    resp = c.post(reverse("iw:recon_run", args=[w["iw"].pk]), follow=True)
    assert "not fully matched" in resp.content.decode() and "NOT_FOUND" in resp.content.decode()
    assert "IRR import" in c.get(reverse("irr:home")).content.decode()


def _incoming(rows, text):
    from collections import Counter
    from dataclasses import replace
    out = {}
    for t in rows:
        r = replace(irr._row(t), trx_number=text)
        out.setdefault(r.group_key, Counter())[r] += 1
    return out


def test_receipt_text_edited_in_erp_is_not_counted_twice(seeded, make_user):
    """28-Oct-2026, IW-2627-000006: the challan text of PO receipt 44370 was edited in ERP after the first IRR
    download; the next IRR brought the same receipt under the new text and the IW showed double the pieces."""
    from inward.irrecon.models import IrrMark
    clerk = make_user("clerk_ed", "DEPOT_CLERK")
    old, new = "44370 / 2026-SE/26-27/453", "44370 / SE/26-27/453"
    irr_rows([("Po Receipt", old, "F003990991050002", 200, None, "10000", "Receipt", ""),
              ("Po Receipt", old, "F007500991040002", 375, None, "15000", "Receipt", "")])
    stored = list(IrrTransaction.objects.filter(trx_number=old))
    # first IRR with the edited text: the stored rows take it, nothing is added again
    out = irr._edited_receipts(clerk, _incoming(stored, new))
    assert [e["action"] for e in out] == ["renamed"]
    assert IrrTransaction.objects.filter(trx_number=new, trx_reference="SE/26-27/453").count() == 2
    assert not IrrTransaction.objects.filter(trx_number=old).exists()

    # a double already stored (as on live): the old-text rows go, a user's mark moves to the twin
    irr_rows([("Po Receipt", old, "F003990991050002", 200, None, "10000", "Receipt", ""),
              ("Po Receipt", old, "F007500991040002", 375, None, "15000", "Receipt", "")])
    stale = IrrTransaction.objects.get(trx_number=old, sku="F003990991050002")
    IrrMark.objects.create(row=stale, reason="checked", marked_by=clerk)
    out = irr._edited_receipts(clerk, _incoming(IrrTransaction.objects.filter(trx_number=new), new))
    assert [e["action"] for e in out] == ["double removed"]
    assert IrrTransaction.objects.filter(receipt_no="44370").count() == 2
    assert IrrMark.objects.get().row.trx_number == new

    # old rows that are not the same receipt content are kept for review
    irr_rows([("Po Receipt", old, "F003990991050002", 150, None, "7500", "Receipt", "")])
    out = irr._edited_receipts(clerk, _incoming(IrrTransaction.objects.filter(trx_number=new), new))
    assert [e["action"] for e in out] == ["differs - kept for review"]
    assert IrrTransaction.objects.filter(trx_number=old).count() == 1
