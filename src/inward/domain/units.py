"""UOM families and expected measure (SoT 3.2).

SIT 'Volume' is always the pack size already expressed in litres or kilograms;
ML/G only mark small packs written as fractions (0.2, 0.5, 0.75).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class MeasureFamily(str, Enum):
    VOLUME = "LTR"
    WEIGHT = "KG"
    COUNT = "PCS"  # BULKPC fresh empty packing: pieces only


_FAMILY_BY_UOM = {
    "L": MeasureFamily.VOLUME,
    "ML": MeasureFamily.VOLUME,
    "K": MeasureFamily.WEIGHT,
    "KG": MeasureFamily.WEIGHT,
    "G": MeasureFamily.WEIGHT,
}
_FRACTION_UOMS = {"ML", "G"}


@dataclass(frozen=True)
class UomResult:
    family: MeasureFamily | None
    is_empty_packing: bool
    error: str | None = None


def classify_uom(uom: str, item_code: str, volume: Decimal | None) -> UomResult:
    uom = (uom or "").upper()
    if not uom:
        if "BULKPC" in (item_code or "").upper():
            return UomResult(MeasureFamily.COUNT, True)
        return UomResult(None, False, "Blank UOM on an item that is not BULKPC empty packing")
    family = _FAMILY_BY_UOM.get(uom)
    if family is None:
        return UomResult(None, False, f"Unknown UOM '{uom}'")
    if uom in _FRACTION_UOMS and volume is not None and volume >= 1:
        return UomResult(family, False, f"{uom} row with Volume {volume} >= 1 (expected a fraction such as 0.5)")
    return UomResult(family, False)


def expected_measure(volume: Decimal | None, quantity: Decimal, family: MeasureFamily | None) -> Decimal:
    """Volume x Quantity in L or KG; zero for count-only items."""
    if family in (None, MeasureFamily.COUNT) or volume is None:
        return Decimal("0")
    return volume * quantity
