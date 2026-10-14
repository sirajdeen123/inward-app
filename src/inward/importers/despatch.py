"""Berger Despatch Detail Report importer for outbound transhipment (SoT 5.3).

The report has no Ship ID column; rows are assigned to outbound Ship IDs by the clerk.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from inward.domain.normalize import clean_code, clean_text, is_valid_sku, parse_date, parse_org, to_decimal
from inward.importers.common import ImportFailed, RowIssue, is_blank_row, locate_header, read_first_sheet

REQUIRED = ["TO ORG", "BRAND", "SHADE", "PACK", "DESCRIPTION", "UOM", "NOP", "VOL LTR", "VOL KG"]
OPTIONAL = ["B LINE", "VOL"]
_WINDOW = re.compile(r"FROM\s*::\s*(\S+)\s+TO\s+(\S+)", re.I)


@dataclass
class DespatchRow:
    row_no: int
    to_org_code: str
    to_org_name: str
    sku: str
    description: str
    business_line: str
    uom: str
    nop: Decimal
    pack_size: Decimal | None
    ltr: Decimal
    kg: Decimal

    @property
    def fingerprint(self) -> str:
        """Content fingerprint used to warn about re-use of an identical row in another IW."""
        return f"{self.to_org_code}|{self.sku}|{self.nop}|{self.ltr}|{self.kg}"


@dataclass
class DespatchImport:
    filename: str
    sha256: str
    window_from: date | None
    window_to: date | None
    rows: list[DespatchRow]
    issues: list[RowIssue]

    def rows_for(self, org_code: str) -> list[DespatchRow]:
        return [r for r in self.rows if r.to_org_code == org_code]


def parse_despatch(path: str | Path) -> DespatchImport:
    sheet = read_first_sheet(path)
    header = locate_header(sheet, REQUIRED, OPTIONAL)
    window_from = window_to = None
    for row in sheet.rows[: header.row_index]:
        for cell in row:
            if m := _WINDOW.search(clean_text(cell).replace("'", "")):
                window_from, window_to = parse_date(m.group(1)), parse_date(m.group(2))
    rows: list[DespatchRow] = []
    issues: list[RowIssue] = []
    for idx in range(header.row_index + 1, len(sheet.rows)):
        raw = sheet.rows[idx]
        if is_blank_row(raw):
            continue
        g = lambda name: header.get(raw, name)  # noqa: E731
        row_no = idx + 1
        to_code, to_name = parse_org(g("TO ORG"))
        sku = clean_code(g("BRAND")) + clean_code(g("SHADE")) + clean_code(g("PACK"))
        nop = to_decimal(g("NOP"))
        if not is_valid_sku(sku):
            issues.append(RowIssue(row_no, f"BRAND+SHADE+PACK '{sku}' is not a 16-character SKU"))
            continue
        if nop is None:
            issues.append(RowIssue(row_no, "NOP missing"))
            continue
        rows.append(DespatchRow(
            row_no=row_no, to_org_code=to_code, to_org_name=to_name, sku=sku,
            description=clean_text(g("DESCRIPTION")), business_line=clean_text(g("B LINE")),
            uom=clean_code(g("UOM")), nop=nop, pack_size=to_decimal(g("VOL")),
            ltr=to_decimal(g("VOL LTR")) or Decimal("0"), kg=to_decimal(g("VOL KG")) or Decimal("0"),
        ))
    if not rows and not issues:
        raise ImportFailed("Despatch Detail Report contains no data rows")
    return DespatchImport(sheet.filename, sheet.sha256, window_from, window_to, rows, issues)
