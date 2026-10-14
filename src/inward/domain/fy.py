"""Indian financial year of the vehicle arrival date and IW number format (SoT 4)."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def fy_code(arrival: datetime | date) -> str:
    """1 Apr - 31 Mar, evaluated on the IST calendar date of arrival. 2026-09-25 -> '2627'."""
    if isinstance(arrival, datetime):
        if arrival.tzinfo is None:
            raise ValueError("arrival datetime must be timezone-aware")
        arrival = arrival.astimezone(IST).date()
    start = arrival.year if arrival.month >= 4 else arrival.year - 1
    return f"{start % 100:02d}{(start + 1) % 100:02d}"


def format_iw_number(fy: str, number: int) -> str:
    return f"IW-{fy}-{number:06d}"
