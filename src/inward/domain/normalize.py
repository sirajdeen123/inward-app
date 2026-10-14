"""Text, code, number and date normalisation shared by all importers (SoT 15)."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_SPACES = re.compile(r"\s+")
_SKU = re.compile(r"^[A-Z0-9]{16}$")
_ORG = re.compile(r"^\s*([0-9A-Z]+)\s*:\s*(.*)$")
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}
_ORACLE_DATE = re.compile(r"^(\d{1,2})-([A-Z]{3})-(\d{2}|\d{4})$")

MEASURE_PLACES = Decimal("0.001")
MONEY_PLACES = Decimal("0.01")


def clean_text(value) -> str:
    """Strip Oracle/Excel artefacts: leading apostrophes, NBSP, outer and repeated spaces."""
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ")
    text = _SPACES.sub(" ", text).strip()
    while text.startswith("'"):
        text = text[1:].strip()
    return text


def clean_code(value) -> str:
    """Upper-case code with all whitespace removed (search / key form)."""
    return _SPACES.sub("", clean_text(value)).upper()


def search_key(value) -> str:
    """Normalised search variant: uppercase, no spaces or punctuation."""
    return re.sub(r"[^0-9A-Z]", "", clean_text(value).upper())


def parse_org(value) -> tuple[str, str]:
    """\"'279:RDC - WB-Sreerampore\" -> ("279", "RDC - WB-Sreerampore")."""
    text = clean_text(value)
    m = _ORG.match(text)
    if not m:
        return clean_code(text), ""
    return m.group(1), m.group(2).strip()


def is_valid_sku(sku: str) -> bool:
    return bool(_SKU.match(sku))


def floor_sku(sku: str) -> str:
    """Floor SKU = characters 4-6 + space + characters 8-10 (1-based). Display/search only (SoT 7.1)."""
    if len(sku) < 10:
        return ""
    return f"{sku[3:6]} {sku[7:10]}"


def to_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    try:
        return Decimal(clean_text(value).replace(",", ""))
    except InvalidOperation:
        return None


def round_measure(value: Decimal) -> Decimal:
    return value.quantize(MEASURE_PLACES, rounding=ROUND_HALF_UP)


def round_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def parse_date(value) -> date | None:
    """Excel dates and Oracle text dates (18-FEB-26, 27-JUN-2026)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    m = _ORACLE_DATE.match(clean_text(value).upper())
    if not m:
        return None
    day, mon, year = int(m.group(1)), _MONTHS.get(m.group(2)), int(m.group(3))
    if mon is None:
        return None
    if year < 100:
        year += 2000
    return date(year, mon, day)
