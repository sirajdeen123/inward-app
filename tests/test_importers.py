"""Phase 0 exit criterion: all four importers pass on the real sample files."""
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal

import pytest

from inward.domain.irr_merge import GroupAction, plan_merge
from inward.domain.units import MeasureFamily
from tests.conftest import sample

pytestmark = pytest.mark.samples
D = Decimal


def test_sit_structure(sit_import):
    s = sit_import
    assert (s.window_from, s.window_to, s.system_date) == (date(2026, 2, 18), date(2027, 1, 18), date(2026, 6, 27))
    assert len(s.rows) == 366
    assert Counter(r.uom for r in s.rows) == {"L": 337, "K": 25, "": 4}
    assert all(r.is_empty_packing for r in s.rows if r.uom == "")
    assert s.issues == []
    assert {r.to_org_code for r in s.rows} == {"035"}


def test_sit_ship_52497520_aggregates(sit_import):
    lines = {sku: ln for (ship, sku), ln in sit_import.lines.items() if ship == "52497520"}
    assert len(lines) == 8
    assert sum(ln.quantity for ln in lines.values()) == 342
    assert sum(ln.measure for ln in lines.values()) == D("4483.6")
    assert lines["F002980000020000"].quantity == 49  # three SIT rows 8 + 3 + 20 + ... summed
    assert lines["F002980000020000"].floor_sku == "298 000"


def test_despatch_report():
    from inward.importers.despatch import parse_despatch
    d = parse_despatch(sample("Outbound SIT sample.xlsx"))
    assert (d.window_from, d.window_to) == (date(2026, 9, 1), date(2026, 9, 22))
    assert len(d.rows) == 36 and d.issues == []
    first = d.rows[0]
    assert (first.to_org_code, first.sku, first.nop, first.ltr) == ("094", "F000000134001000", 12, 12)
    assert first.description == "LUXOL HI GLOSS COLS-SKY BLUE-1 L"
    assert len(d.rows_for("094")) == 36


def test_vendor_portal():
    from inward.importers.vendor_portal import parse_vendor_portal
    v = parse_vendor_portal(sample("Vendor SIT sample.xlsx"))
    assert v.issues == []
    cna = v.for_challan("V54", "641406/876/2627")
    assert len(cna) == 1
    ln = cna[0]
    assert (ln.oracle_vendor_id, ln.po_number, ln.sku, ln.uom, ln.nop) == ("45188", "376841", "F007500991040002", "K", 300)
    # a challan may carry several SKU lines, but always belongs to one vendor PO
    po_by_challan = defaultdict(set)
    for line in v.lines:
        po_by_challan[line.key].add(line.po_number)
    assert len(v.lines) == 13 and len(po_by_challan) == 8
    assert all(len(pos) == 1 for pos in po_by_challan.values())


def test_irr_structure(irr_import):
    i = irr_import
    assert (i.report_from, i.report_to) == (date(2026, 5, 1), date(2026, 9, 12))
    assert i.run_at is not None and i.run_at.date() == date(2026, 9, 12)
    assert i.last_day_possibly_incomplete
    assert len(i.rows) == 18261 and i.issues == []
    dup_rows = sum(n - 1 for grp in i.groups.values() for n in grp.values() if n > 1)
    assert dup_rows > 0, "legitimately identical rows must be kept"
    assert sum(sum(g.values()) for g in i.groups.values()) == 18261
    # a row carries LTR or KG, never both
    assert not any(r.volume_ltr and r.weight_kg for r in i.rows)


def test_irr_receipt_43200_matches_sit(irr_import, sit_import):
    irr = defaultdict(lambda: [D(0), D(0)])
    for r in irr_import.rows:
        if r.receipt_no == "43200" and r.trx_reference == "52497520":
            irr[r.sku][0] += r.no_of_pack
            irr[r.sku][1] += r.volume_ltr or 0
    sit = {sku: ln for (ship, sku), ln in sit_import.lines.items() if ship == "52497520"}
    assert set(irr) == set(sit)
    for sku, ln in sit.items():
        assert irr[sku][0] == ln.quantity
        assert irr[sku][1] == ln.measure
        assert ln.family is MeasureFamily.VOLUME


def test_irr_po_receipt_parts(irr_import):
    po = next(r for r in irr_import.rows if r.trx_number == "42679 / CBE/0181/26-27")
    assert (po.receipt_no, po.trx_reference, po.type) == ("42679", "CBE/0181/26-27", "Po Receipt")


def test_irr_reimport_and_overlap_add_nothing_twice(irr_import):
    store = {k: Counter(v) for k, v in irr_import.groups.items()}
    again = plan_merge(store, irr_import.groups)
    assert again.rows_to_add == [] and again.count(GroupAction.IDENTICAL) == len(store)
    # a later file whose last day now has one more row for an existing group
    key = next(iter(irr_import.groups))
    extra_row = next(iter(irr_import.groups[key]))
    bigger = {key: irr_import.groups[key] + Counter({extra_row: 1})}
    plan = plan_merge(store, bigger)
    assert plan.actions[key] is GroupAction.EXTENDED and plan.rows_to_add == [extra_row]
