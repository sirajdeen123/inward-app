"""IRR (ISSUE/RECEIPT Register) importer (SoT 8.2).

Legitimately identical rows exist (same TRX, lot, locator, quantity), so rows are never
deduplicated by content; they are grouped by (Type, TRX Number, TRX Date) as a multiset.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from inward.domain.normalize import clean_code, clean_text, parse_date, to_decimal
from inward.importers.common import ImportFailed, RowIssue, is_blank_row, locate_header, read_first_sheet

REQUIRED = ["Type", "TRX Number", "TRX Date", "Source SUB INV", "Lot Number", "Source Locator",
            "Destination SUB INV", "Destination Locator", "Reason Code", "Reason Description",
            "Shipping ID Date", "Shipping ID Number", "Item Description", "Item Code",
            "No of Pack", "Volume LTR", "Weight KG", "Transaction type"]
_WINDOW = re.compile(r"From:\s*(\S+)\s+To:\s*(\S+)", re.I)
RECEIPT_TYPES = {"INT REQ INTR RCPT", "PO RECEIPT"}
GroupKey = tuple[str, str, date]


def _num_text(value) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return clean_text(value)


@dataclass(frozen=True)
class IrrRow:
    type: str
    trx_number: str
    trx_date: date
    source_subinv: str
    lot_number: str
    source_locator: str
    dest_subinv: str
    dest_locator: str
    reason_code: str
    reason_description: str
    shipping_id_date: date | None
    shipping_id: str
    item_description: str
    sku: str
    no_of_pack: Decimal
    volume_ltr: Decimal | None
    weight_kg: Decimal | None
    transaction_type: str

    @property
    def group_key(self) -> GroupKey:
        return self.type.upper(), self.trx_number, self.trx_date

    @property
    def receipt_no(self) -> str | None:
        """'42637 / 51882836' -> '42637' for stock and PO receipts."""
        if self.type.upper() in RECEIPT_TYPES and "/" in self.trx_number:
            return self.trx_number.split("/", 1)[0].strip()
        return None

    @property
    def trx_reference(self) -> str | None:
        """Ship ID for stock receipts, vendor challan text for PO receipts."""
        if self.type.upper() in RECEIPT_TYPES and "/" in self.trx_number:
            return self.trx_number.split("/", 1)[1].strip()
        return None

    @property
    def is_zero(self) -> bool:
        return self.no_of_pack == 0 and not self.volume_ltr and not self.weight_kg


@dataclass
class IrrImport:
    filename: str
    sha256: str
    report_from: date | None
    report_to: date | None
    run_at: datetime | None
    rows: list[IrrRow]
    issues: list[RowIssue]
    groups: dict[GroupKey, Counter] = field(default_factory=dict)

    @property
    def last_day_possibly_incomplete(self) -> bool:
        return bool(self.run_at and self.report_to and self.report_to == self.run_at.date())


def parse_irr(path: str | Path) -> IrrImport:
    sheet = read_first_sheet(path)
    header = locate_header(sheet, REQUIRED)
    report_from = report_to = None
    run_date: date | None = None
    run_time: time | None = None
    for row in sheet.rows[: header.row_index]:
        for j, cell in enumerate(row):
            text = clean_text(cell)
            if m := _WINDOW.search(text):
                report_from, report_to = parse_date(m.group(1)), parse_date(m.group(2))
            nxt = next((c for c in row[j + 1:] if c not in (None, "")), None)
            if text.upper() == "DATE:" and nxt is not None:
                run_date = parse_date(nxt)
            if text.upper() == "TIME:" and isinstance(nxt, time):
                run_time = nxt
    if report_from is None or report_to is None:
        raise ImportFailed("IRR report window ('From: ... To: ...') not found")
    run_at = datetime.combine(run_date, run_time or time(0)) if run_date else None

    rows: list[IrrRow] = []
    issues: list[RowIssue] = []
    for idx in range(header.row_index + 1, len(sheet.rows)):
        raw = sheet.rows[idx]
        if is_blank_row(raw):
            continue
        g = lambda name: header.get(raw, name)  # noqa: E731
        trx_date = parse_date(g("TRX Date"))
        trx_number = _num_text(g("TRX Number"))
        rtype = clean_text(g("Type"))
        if not rtype or not trx_number or trx_date is None:
            issues.append(RowIssue(idx + 1, "Type, TRX Number or TRX Date missing"))
            continue
        rows.append(IrrRow(
            type=rtype, trx_number=trx_number, trx_date=trx_date,
            source_subinv=clean_code(g("Source SUB INV")), lot_number=clean_text(g("Lot Number")),
            source_locator=clean_code(g("Source Locator")), dest_subinv=clean_code(g("Destination SUB INV")),
            dest_locator=clean_code(g("Destination Locator")), reason_code=clean_text(g("Reason Code")),
            reason_description=clean_text(g("Reason Description")),
            shipping_id_date=parse_date(g("Shipping ID Date")), shipping_id=_num_text(g("Shipping ID Number")),
            item_description=clean_text(g("Item Description")), sku=clean_code(g("Item Code")),
            no_of_pack=to_decimal(g("No of Pack")) or Decimal("0"),
            volume_ltr=to_decimal(g("Volume LTR")), weight_kg=to_decimal(g("Weight KG")),
            transaction_type=clean_text(g("Transaction type")),
        ))
    result = IrrImport(sheet.filename, sheet.sha256, report_from, report_to, run_at, rows, issues)
    for r in rows:
        result.groups.setdefault(r.group_key, Counter())[r] += 1
    return result
