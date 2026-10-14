"""Vendor portal report ('Challan wise Source Despatches') importer (SoT 5.4)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from inward.domain.normalize import clean_code, clean_text, is_valid_sku, parse_date, to_decimal
from inward.importers.common import RowIssue, is_blank_row, locate_header, read_first_sheet

REQUIRED = ["Source", "Oracle Vendor ID", "Oracle Vendor Name", "PO No", "Vendor Challan No",
            "SKU Code", "Description", "Depot Code", "UOM", "NOP", "LTR", "KG"]
OPTIONAL = ["Vendor Challan Date", "Transporter", "Vehicle Number", "GRN No", "GRN Date", "Challan Id"]
_VENDOR_CODE = re.compile(r"\(([A-Z0-9]+)\)\s*$")


def _int_text(value) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return clean_text(value)


@dataclass
class VendorLine:
    row_no: int
    vendor_code: str          # e.g. V54, from 'CNA COATINGS INDIA PRIVATE LIMITED-COIMBATORE-(V54)'
    oracle_vendor_id: str
    vendor_name: str
    po_number: str
    challan_no: str
    challan_date: date | None
    depot_code: str
    sku: str
    description: str
    uom: str
    nop: Decimal
    ltr: Decimal
    kg: Decimal
    transporter: str
    vehicle: str

    @property
    def key(self) -> tuple[str, str]:
        """Unique vendor consignment key = Vendor Code + normalised Challan No (SoT 5.4)."""
        return self.vendor_code, clean_code(self.challan_no)


@dataclass
class VendorImport:
    filename: str
    sha256: str
    lines: list[VendorLine]
    issues: list[RowIssue]

    def for_challan(self, vendor_code: str, challan_no: str) -> list[VendorLine]:
        return [ln for ln in self.lines if ln.key == (vendor_code, clean_code(challan_no))]


def parse_vendor_portal(path: str | Path, depot_code: str = "035") -> VendorImport:
    sheet = read_first_sheet(path)
    header = locate_header(sheet, REQUIRED, OPTIONAL)
    lines: list[VendorLine] = []
    issues: list[RowIssue] = []
    for idx in range(header.row_index + 1, len(sheet.rows)):
        raw = sheet.rows[idx]
        if is_blank_row(raw):
            continue
        g = lambda name: header.get(raw, name)  # noqa: E731
        row_no = idx + 1
        m = _VENDOR_CODE.search(clean_text(g("Source")))
        sku = clean_code(g("SKU Code"))
        row_depot = clean_code(g("Depot Code"))
        if row_depot != depot_code:
            continue  # portal report may list other depots
        if not m:
            issues.append(RowIssue(row_no, "Vendor code '(Vnn)' not found in Source"))
            continue
        if not is_valid_sku(sku):
            issues.append(RowIssue(row_no, f"SKU Code '{sku}' is not a 16-character SKU"))
            continue
        lines.append(VendorLine(
            row_no=row_no, vendor_code=m.group(1), oracle_vendor_id=_int_text(g("Oracle Vendor ID")),
            vendor_name=clean_text(g("Oracle Vendor Name")), po_number=_int_text(g("PO No")),
            challan_no=clean_text(g("Vendor Challan No")), challan_date=parse_date(g("Vendor Challan Date")),
            depot_code=row_depot, sku=sku, description=clean_text(g("Description")), uom=clean_code(g("UOM")),
            nop=to_decimal(g("NOP")) or Decimal("0"), ltr=to_decimal(g("LTR")) or Decimal("0"),
            kg=to_decimal(g("KG")) or Decimal("0"), transporter=clean_text(g("Transporter")),
            vehicle=clean_code(g("Vehicle Number")),
        ))
    return VendorImport(sheet.filename, sheet.sha256, lines, issues)
