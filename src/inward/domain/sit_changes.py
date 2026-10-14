"""SIT change detection between consecutive accepted imports (SoT 3.3).

Rows never vanish blindly: a vanished row means Oracle processed it, and a Ship never grows.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum


class SitEvent(str, Enum):
    NEW_SHIP = "NEW_SHIP"
    SKU_REDUCED = "SKU_REDUCED"
    SKU_REMOVED = "SKU_REMOVED"
    SHIP_REMOVED_PROCESSED = "SHIP_REMOVED_PROCESSED"        # linked + receipt entered: normal
    SHIP_REMOVED_NO_RECEIPT = "SHIP_REMOVED_NO_RECEIPT"      # linked, no receipt yet: exception
    SHIP_REMOVED_NEVER_LINKED = "SHIP_REMOVED_NEVER_LINKED"  # CRITICAL
    SHIP_OUT_OF_WINDOW = "SHIP_OUT_OF_WINDOW"
    QTY_INCREASED = "QTY_INCREASED"                          # CRITICAL anomaly
    SKU_ADDED = "SKU_ADDED"                                  # CRITICAL anomaly


CRITICAL = {SitEvent.SHIP_REMOVED_NEVER_LINKED, SitEvent.QTY_INCREASED, SitEvent.SKU_ADDED}


@dataclass(frozen=True)
class ShipState:
    """Aggregated pending quantity per SKU for one Ship in one import."""
    ship_id: str
    shipment_date: date | None
    quantities: dict[str, Decimal]


@dataclass(frozen=True)
class ChangeEvent:
    ship_id: str
    event: SitEvent
    sku: str | None = None
    before: Decimal | None = None
    after: Decimal | None = None

    @property
    def is_critical(self) -> bool:
        return self.event in CRITICAL


def detect_changes(
    previous: dict[str, ShipState],
    current: dict[str, ShipState],
    new_window: tuple[date, date],
    linked_ships: set[str],
    ships_with_receipt: set[str],
    excluded_ships: set[str] = frozenset(),
) -> list[ChangeEvent]:
    """Classify every difference. `excluded_ships` = Ships marked Pre-go-live."""
    events: list[ChangeEvent] = []
    win_from, win_to = new_window
    for ship_id, cur in current.items():
        prev = previous.get(ship_id)
        if prev is None:
            events.append(ChangeEvent(ship_id, SitEvent.NEW_SHIP))
            continue
        for sku, qty in cur.quantities.items():
            before = prev.quantities.get(sku)
            if before is None:
                events.append(ChangeEvent(ship_id, SitEvent.SKU_ADDED, sku, None, qty))
            elif qty > before:
                events.append(ChangeEvent(ship_id, SitEvent.QTY_INCREASED, sku, before, qty))
            elif qty < before:
                events.append(ChangeEvent(ship_id, SitEvent.SKU_REDUCED, sku, before, qty))
        for sku, before in prev.quantities.items():
            if sku not in cur.quantities:
                events.append(ChangeEvent(ship_id, SitEvent.SKU_REMOVED, sku, before, Decimal("0")))
    for ship_id, prev in previous.items():
        if ship_id in current or ship_id in excluded_ships:
            continue
        d = prev.shipment_date
        if d is not None and not (win_from <= d <= win_to):
            events.append(ChangeEvent(ship_id, SitEvent.SHIP_OUT_OF_WINDOW))
        elif ship_id not in linked_ships:
            events.append(ChangeEvent(ship_id, SitEvent.SHIP_REMOVED_NEVER_LINKED))
        elif ship_id in ships_with_receipt:
            events.append(ChangeEvent(ship_id, SitEvent.SHIP_REMOVED_PROCESSED))
        else:
            events.append(ChangeEvent(ship_id, SitEvent.SHIP_REMOVED_NO_RECEIPT))
    return events


def ship_states_from_lines(lines, shipment_dates: dict[str, date | None]) -> dict[str, ShipState]:
    """Build ShipStates from SitImport.lines (keyed (ship_id, sku))."""
    grouped: dict[str, dict[str, Decimal]] = {}
    for (ship_id, sku), line in lines.items():
        grouped.setdefault(ship_id, {})[sku] = line.quantity
    return {s: ShipState(s, shipment_dates.get(s), q) for s, q in grouped.items()}
