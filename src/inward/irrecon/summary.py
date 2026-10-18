"""Counts of the engine's results for a date range - the screen and the daily summary mail use the same figures."""
from __future__ import annotations

from datetime import date

from django.db.models import Count

from inward.irrecon.models import IrrMatch

FAMILIES = [("SUBINV", "Subinventory transfers"), ("PICK", "Internal order picks"), ("SHIP", "Outbound shipments"),
            ("CONVERT", "Conversions (tinting, repacking, restencilling)"), ("LSCCR", "LSCCR postings"),
            ("INBOUND", "Receipts (stock / PO)"), ("RETURN", "Customer returns"), ("RESIDUAL", "Residual rows"),
            ("MISC", "Other miscellaneous"), ("OTHER", "Unknown types")]
STATUSES = IrrMatch.Status.choices


def summary(d1: date | None = None, d2: date | None = None) -> dict:
    qs = IrrMatch.objects.all()
    if d1:
        qs = qs.filter(row__trx_date__gte=d1)
    if d2:
        qs = qs.filter(row__trx_date__lte=d2)
    counts = {(c["family"], c["status"]): c["n"] for c in qs.values("family", "status").annotate(n=Count("pk"))}
    table = []
    for code, label in FAMILIES:
        row = {s: counts.get((code, s), 0) for s, _ in STATUSES}
        total = sum(row.values())
        if total:
            done = total - row["OPEN"]
            table.append({"code": code, "label": label, "counts": row, "open": row["OPEN"], "total": total,
                          "pct": round(100 * done / total, 1)})
    total = sum(t["total"] for t in table)
    open_n = sum(t["open"] for t in table)
    return {"from": d1, "to": d2, "table": table, "total": total, "open": open_n,
            "pct": round(100 * (total - open_n) / total, 1) if total else 0,
            "by_status": {s: sum(t["counts"][s] for t in table) for s, _ in STATUSES}}
