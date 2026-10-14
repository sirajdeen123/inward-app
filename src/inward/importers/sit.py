"""SIT (BPIL Depot Stock In Transit Report) importer (SoT 3)."""
from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from inward.domain.normalize import (
    clean_code, clean_text, floor_sku, is_valid_sku, parse_date, parse_org, to_decimal,
)
from inward.domain.units import MeasureFamily, classify_uom, expected_measure
from inward.importers.common import ImportFailed, RowIssue, is_blank_row, locate_header, read_first_sheet

REQUIRED = ["From Org", "To Org", "Delivery Id", "Shipment Date", "Item Type", "Item Code",
            "Item Desc", "Uom", "Volume", "Quantity Issued"]
OPTIONAL = ["Shipment Period", "Item BL"]
_WINDOW = re.compile(r"From Date\s*::\s*(\S+)\s+To Date\s*::\s*(\S+)", re.I)
_SYSDATE = re.compile(r"System Date\s*::\s*(\S+)", re.I)


@dataclass
class SitRow:
    row_no: int
    ship_id: str
    from_org_code: str
    from_org_name: str
    to_org_code: str
    shipment_date: date | None
    item_type: str
    sku: str
    description: str
    business_line: str
    uom: str
    family: MeasureFamily | None
    is_empty_packing: bool
    volume: Decimal | None
    quantity: Decimal
    measure: Decimal


@dataclass
class SitLine:
    """Aggregate of one SKU within one Ship (several rows of the same SKU are normal)."""
    ship_id: str
    sku: str
    description: str
    uom: str
    family: MeasureFamily | None
    is_empty_packing: bool
    pack_size: Decimal | None
    business_line: str = ""  # decides Retail / Industrial warehouse (SoT 7.1)
    quantity: Decimal = Decimal("0")
    measure: Decimal = Decimal("0")

    @property
    def floor_sku(self) -> str:
        return floor_sku(self.sku)


@dataclass
class SitImport:
    filename: str
    sha256: str
    window_from: date | None
    window_to: date | None
    system_date: date | None
    rows: list[SitRow]
    issues: list[RowIssue]
    lines: "OrderedDict[tuple[str, str], SitLine]" = field(default_factory=OrderedDict)

    @property
    def ship_ids(self) -> set[str]:
        return {k[0] for k in self.lines}

    @property
    def blocked_ships(self) -> set[str]:
        """Ships with a row-level exception cannot be linked until resolved (SoT 3.2)."""
        return {i.ref for i in self.issues if i.ref}


def parse_sit(path: str | Path) -> SitImport:
    sheet = read_first_sheet(path)
    header = locate_header(sheet, REQUIRED, OPTIONAL)
    window_from = window_to = system_date = None
    for row in sheet.rows[: header.row_index]:
        for cell in row:
            text = clean_text(cell)
            if m := _WINDOW.search(text):
                window_from, window_to = parse_date(m.group(1)), parse_date(m.group(2))
            if m := _SYSDATE.search(text):
                system_date = parse_date(m.group(1))
    if window_from is None or window_to is None:
        raise ImportFailed("SIT report window ('From Date :: ... To Date :: ...') not found")

    rows: list[SitRow] = []
    issues: list[RowIssue] = []
    for idx in range(header.row_index + 1, len(sheet.rows)):
        raw = sheet.rows[idx]
        if is_blank_row(raw):
            continue
        row_no = idx + 1
        g = lambda name: header.get(raw, name)  # noqa: E731
        ship_raw = g("Delivery Id")
        ship_id = clean_code(int(ship_raw) if isinstance(ship_raw, float) and ship_raw.is_integer() else ship_raw)
        sku = clean_code(g("Item Code"))
        from_code, from_name = parse_org(g("From Org"))
        to_code, _ = parse_org(g("To Org"))
        uom = clean_code(g("Uom"))
        volume = to_decimal(g("Volume"))
        qty = to_decimal(g("Quantity Issued"))
        if not ship_id:
            issues.append(RowIssue(row_no, "Missing Delivery Id"))
            continue
        if not is_valid_sku(sku):
            issues.append(RowIssue(row_no, f"Item Code '{sku}' is not a 16-character SKU", ship_id))
            continue
        if qty is None:
            issues.append(RowIssue(row_no, "Quantity Issued missing", ship_id))
            continue
        uom_result = classify_uom(uom, sku, volume)
        if uom_result.error:
            issues.append(RowIssue(row_no, uom_result.error, ship_id))
        rows.append(SitRow(
            row_no=row_no, ship_id=ship_id, from_org_code=from_code, from_org_name=from_name,
            to_org_code=to_code, shipment_date=parse_date(g("Shipment Date")),
            item_type=clean_code(g("Item Type")), sku=sku, description=clean_text(g("Item Desc")),
            business_line=clean_text(g("Item BL")), uom=uom, family=uom_result.family,
            is_empty_packing=uom_result.is_empty_packing, volume=volume, quantity=qty,
            measure=expected_measure(volume, qty, uom_result.family),
        ))
    result = SitImport(sheet.filename, sheet.sha256, window_from, window_to, system_date, rows, issues)
    for r in rows:
        key = (r.ship_id, r.sku)
        line = result.lines.get(key)
        if line is None:
            line = result.lines[key] = SitLine(r.ship_id, r.sku, r.description, r.uom, r.family,
                                               r.is_empty_packing, r.volume, r.business_line)
        line.quantity += r.quantity
        line.measure += r.measure
    return result
