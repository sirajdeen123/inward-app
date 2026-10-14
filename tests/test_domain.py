from collections import Counter
from datetime import date, datetime
from decimal import Decimal

import pytest

from inward.domain.fy import IST, fy_code, format_iw_number
from inward.domain.irr_merge import GroupAction, plan_merge
from inward.domain.normalize import clean_text, floor_sku, parse_date, parse_org, search_key
from inward.domain.sit_changes import SitEvent, ShipState, detect_changes
from inward.domain.units import MeasureFamily, classify_uom, expected_measure

D = Decimal


def test_floor_sku_rule():
    assert floor_sku("F007130991001000") == "713 991"
    assert floor_sku("F00XG90991003600") == "XG9 991"
    assert floor_sku("F002980000020000") == "298 000"
    assert floor_sku("2010370991BULKPC") == "037 991"


def test_text_normalisation():
    assert clean_text("'0134") == "0134"
    assert clean_text("LUXOL HI GLOSS COLS-SKY\xa0 BLUE-1 L") == "LUXOL HI GLOSS COLS-SKY BLUE-1 L"
    assert parse_org("'279:RDC - WB-Sreerampore") == ("279", "RDC - WB-Sreerampore")
    assert search_key("TN 60 E-8139") == "TN60E8139"
    assert parse_date("18-FEB-26") == date(2026, 2, 18)
    assert parse_date("27-JUN-2026") == date(2026, 6, 27)


@pytest.mark.parametrize("uom,code,vol,family,empty,error", [
    ("L", "F002980000020000", D("20"), MeasureFamily.VOLUME, False, False),
    ("ML", "F000000160000500", D("0.5"), MeasureFamily.VOLUME, False, False),
    ("ML", "F000000160000500", D("500"), MeasureFamily.VOLUME, False, True),
    ("K", "F007500991040002", D("40"), MeasureFamily.WEIGHT, False, False),
    ("G", "F007500991000200", D("0.2"), MeasureFamily.WEIGHT, False, False),
    ("", "2010370991BULKPC", D("1"), MeasureFamily.COUNT, True, False),
    ("", "F002980000020000", D("20"), None, False, True),
    ("XX", "F002980000020000", D("20"), None, False, True),
])
def test_uom_classification(uom, code, vol, family, empty, error):
    r = classify_uom(uom, code, vol)
    assert r.family == family
    assert r.is_empty_packing == empty
    assert (r.error is not None) == error


def test_expected_measure_uses_fraction_directly():
    assert expected_measure(D("0.5"), D("96"), MeasureFamily.VOLUME) == D("48.0")
    assert expected_measure(D("20"), D("50"), MeasureFamily.VOLUME) == D("1000")
    assert expected_measure(D("1"), D("25"), MeasureFamily.COUNT) == D("0")


def test_fy_from_arrival_ist():
    assert fy_code(datetime(2027, 3, 31, 23, 10, tzinfo=IST)) == "2627"
    assert fy_code(datetime(2027, 4, 1, 0, 5, tzinfo=IST)) == "2728"
    assert fy_code(date(2026, 9, 25)) == "2627"
    assert format_iw_number("2627", 1) == "IW-2627-000001"
    with pytest.raises(ValueError):
        fy_code(datetime(2026, 9, 25, 10, 0))


def test_sit_change_detection_all_cases():
    prev = {
        "A": ShipState("A", date(2026, 6, 1), {"S1": D(10), "S2": D(5)}),
        "B": ShipState("B", date(2026, 6, 1), {"S1": D(3)}),   # removed, linked, receipt
        "C": ShipState("C", date(2026, 6, 1), {"S1": D(3)}),   # removed, linked, no receipt
        "D": ShipState("D", date(2026, 6, 1), {"S1": D(3)}),   # removed, never linked
        "E": ShipState("E", date(2025, 1, 1), {"S1": D(3)}),   # out of window
        "F": ShipState("F", date(2026, 6, 1), {"S1": D(3)}),   # pre-go-live
        "G": ShipState("G", date(2026, 6, 1), {"S1": D(3)}),
    }
    cur = {
        "A": ShipState("A", date(2026, 6, 1), {"S1": D(4)}),              # reduced + S2 removed
        "G": ShipState("G", date(2026, 6, 1), {"S1": D(9), "S9": D(1)}),  # increased + added
        "N": ShipState("N", date(2026, 7, 1), {"S1": D(1)}),
    }
    events = detect_changes(prev, cur, (date(2026, 2, 18), date(2027, 1, 18)),
                            linked_ships={"A", "B", "C"}, ships_with_receipt={"B"}, excluded_ships={"F"})
    got = {(e.ship_id, e.event) for e in events}
    assert got == {
        ("A", SitEvent.SKU_REDUCED), ("A", SitEvent.SKU_REMOVED),
        ("G", SitEvent.QTY_INCREASED), ("G", SitEvent.SKU_ADDED),
        ("N", SitEvent.NEW_SHIP),
        ("B", SitEvent.SHIP_REMOVED_PROCESSED), ("C", SitEvent.SHIP_REMOVED_NO_RECEIPT),
        ("D", SitEvent.SHIP_REMOVED_NEVER_LINKED), ("E", SitEvent.SHIP_OUT_OF_WINDOW),
    }
    assert {e.ship_id for e in events if e.is_critical} == {"D", "G"}


def test_irr_merge_multiset_rules():
    a, b, c = "row-a", "row-b", "row-c"
    existing = {"k1": Counter({a: 2}), "k2": Counter({a: 1}), "k3": Counter({a: 1, b: 1}), "k4": Counter({a: 1, b: 1})}
    incoming = {"k1": Counter({a: 2}), "k2": Counter({a: 1, b: 1}), "k3": Counter({a: 1}),
                "k4": Counter({a: 1, c: 1}), "k5": Counter({c: 3})}
    plan = plan_merge(existing, incoming)
    assert plan.actions == {"k1": GroupAction.IDENTICAL, "k2": GroupAction.EXTENDED, "k3": GroupAction.SUBSET,
                            "k4": GroupAction.REVIEW, "k5": GroupAction.NEW}
    assert Counter(plan.rows_to_add) == Counter({b: 1, c: 3})
