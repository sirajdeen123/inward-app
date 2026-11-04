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
SHORT = {"SUBINV": "Sub-inventory transfer", "PICK": "Pick", "SHIP": "Shipment", "CONVERT": "Conversion",
         "LSCCR": "LSCCR", "INBOUND": "Receipt", "RETURN": "Customer return", "RESIDUAL": "Residual",
         "MISC": "Miscellaneous", "OTHER": "Other"}


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


def open_dates() -> list[dict]:
    """The IRR dates that still have open exceptions, oldest first, with what kind (4-Nov-2026): the clerk works the
    backlog date by date."""
    label = SHORT
    days: dict[date, dict] = {}
    for d, fam, n in (IrrMatch.objects.filter(status="OPEN").values_list("row__trx_date", "family")
                      .annotate(n=Count("pk")).order_by("row__trx_date")):
        x = days.setdefault(d, {"date": d, "open": 0, "families": []})
        x["open"] += n
        x["families"].append((label.get(fam, fam), n))
    totals = dict(IrrMatch.objects.filter(row__trx_date__in=list(days)).values_list("row__trx_date")
                  .annotate(n=Count("pk")))
    for d, x in days.items():
        x["total"] = totals.get(d, 0)
        x["families"].sort(key=lambda f: -f[1])
    return list(days.values())


def calendar(months: int = 3) -> list[dict]:
    """Month grids of the last few months with IRR rows: each day all reconciled / open (count) / no IRR rows."""
    import calendar as cal
    last = IrrMatch.objects.order_by("-row__trx_date").values_list("row__trx_date", flat=True).first()
    if not last:
        return []
    opn = {x["date"]: x["open"] for x in open_dates()}
    out = []
    y, m = last.year, last.month
    for _ in range(months):
        start, end = date(y, m, 1), date(y, m, cal.monthrange(y, m)[1])
        have = set(IrrMatch.objects.filter(row__trx_date__range=(start, end)).values_list("row__trx_date", flat=True)
                   .distinct())
        cells = [None] * start.weekday()
        for day in range(1, end.day + 1):
            d = date(y, m, day)
            cells.append({"date": d, "open": opn.get(d, 0), "state": "bad" if opn.get(d) else "ok" if d in have else "none"})
        if have or any(c and c["open"] for c in cells):
            out.append({"title": start.strftime("%B %Y"), "cells": cells})
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return list(reversed(out))
