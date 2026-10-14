"""Database-controlled IW numbers: never 'last number + 1' in the UI (SoT 4, 14.1).

UPDATE ... RETURNING takes a row lock held until the surrounding transaction ends, so concurrent
creators queue on it; a rollback returns the number, so failures leave no gaps.
"""
from __future__ import annotations

from datetime import datetime

from django.db import connection, transaction

from inward.domain.fy import format_iw_number, fy_code


def next_iw_number(arrival: datetime) -> str:
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("next_iw_number must run inside the IW-creation transaction")
    fy = fy_code(arrival)
    with connection.cursor() as cur:
        cur.execute("INSERT INTO iw_counter (fy_code, last_number) VALUES (%s, 0) ON CONFLICT DO NOTHING", [fy])
        cur.execute("UPDATE iw_counter SET last_number = last_number + 1 WHERE fy_code = %s RETURNING last_number",
                    [fy])
        number = cur.fetchone()[0]
    return format_iw_number(fy, number)
