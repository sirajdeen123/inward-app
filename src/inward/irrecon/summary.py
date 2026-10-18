"""Counts of the engine's results for a date range - the screen and the daily summary mail use the same figures."""
from __future__ import annotations

from datetime import date

from decimal import Decimal

from django.db.models import Count, Q, Sum

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


def by_reason(d1: date | None = None, d2: date | None = None) -> list[dict]:
    """One line per IRR Reason Description (the Type when the IRR has no reason): rows, pieces issued / received,
    reconciled and open - the way the depot reads the IRR (decision 18-Oct-2026)."""
    qs = IrrMatch.objects.all()
    if d1:
        qs = qs.filter(row__trx_date__gte=d1)
    if d2:
        qs = qs.filter(row__trx_date__lte=d2)
    issue = Q(row__transaction_type__iexact="Issue")
    receipt = Q(row__transaction_type__iexact="Receipt")
    rows = (qs.values("row__reason_description", "row__type")
            .annotate(n=Count("pk"), open=Count("pk", filter=Q(status="OPEN")),
                      pcs_out=Sum("row__no_of_pack", filter=issue), pcs_in=Sum("row__no_of_pack", filter=receipt)))
    merged: dict[str, dict] = {}
    for r in rows:
        reason = (r["row__reason_description"] or "").strip() or r["row__type"]
        m = merged.setdefault(reason, {"reason": reason, "types": set(), "rows": 0, "open": 0,
                                       "pcs_out": Decimal(0), "pcs_in": Decimal(0)})
        m["types"].add(r["row__type"])
        m["rows"] += r["n"]
        m["open"] += r["open"]
        m["pcs_out"] += r["pcs_out"] or 0
        m["pcs_in"] += r["pcs_in"] or 0
    out = []
    for m in merged.values():
        m["types"] = ", ".join(sorted(m["types"]))
        m["done"] = m["rows"] - m["open"]
        m["pct"] = round(100 * m["done"] / m["rows"], 1) if m["rows"] else 0
        out.append(m)
    return sorted(out, key=lambda m: (-m["open"], -m["rows"], m["reason"]))
