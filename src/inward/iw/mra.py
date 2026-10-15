"""Screen 04 part C - MRA and SED memos (SoT 7.3, 7.4).

The MRA is built from the IW header, linked references, confirmed package counts (per warehouse), the SED
summary and the unloading position. It never lists the consignment's full SKU detail and never prints the
unloading rate. One PDF holds the MRA twice (driver copy, depot copy) followed by the SED memos for the System
Supervisor. Revisions R01, R02 ... are frozen; a new one is issued only when the content changed (with a reason).

Memo rules (SoT 7.3, table 10): Direct Factory Ship with LSCRR applied - Short and Damage are LSCRR-covered (no
separate TRX), Excess needs a separate Excess Receipt; otherwise Short -> issue TRX, Excess -> receipt TRX,
Damage -> subinventory transfer to DMG. Outbound, Colour Bank and Non-Stock have no memos.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from inward.accounts.models import User
from inward.accounts.services import require
from inward.core import audit, documents, pdf
from inward.core.models import Depot, DepotOperator
from inward.domain.normalize import clean_text
from inward.iw import sed, unloading
from inward.iw.models import Iw, MraRevision, SedLine, UnloadingRecord
from inward.iw.services import IwError

MEMO_KINDS = [("SHORT", "SHORT MEMO"), ("EXCESS", "EXCESS MEMO"), ("DAMAGE", "DAMAGE MEMO")]


def _s(v) -> str:
    if v is None:
        return ""
    return format(v.normalize(), "f") if isinstance(v, Decimal) else str(v)


def _money(v) -> str:
    return f"{(v or Decimal(0)):.2f}"


def blockers(iw: Iw) -> list[str]:
    out = []
    if iw.status != Iw.Status.PHYSICALLY_VERIFIED:
        out.append("Confirm physical verification first (Unloading & SED).")
    if not iw.package_counts.exists():
        out.append("Save the package counts (Packages & unloading charge).")
    if not unloading.gate_ok(iw):
        out.append("Record the unloading charge and its payment position (an Exempt request must be approved).")
    t = sed.totals(iw)
    if t.values_missing:
        out.append(f"Enter the financial value of {t.values_missing} Short / Damage line(s).")
    if _operator(iw) is None:
        out.append("No operating CFA is set in the Depot master for the arrival date.")
    return out


def _operator(iw: Iw):
    day = timezone.localtime(iw.arrival_at).date()
    return (DepotOperator.objects.filter(effective_from__lte=day).filter(Q(effective_to__isnull=True)
                                                                        | Q(effective_to__gte=day))
            .select_related("depot").order_by("-effective_from").first())


def _memo_rule(line: SedLine, kind: str) -> tuple[str, str]:
    """(Oracle action, note) for one SED quantity."""
    link = line.ship_link
    direct = link is not None and line.iw.load_type.is_direct_factory
    if direct and link.lscrr_applied:
        if kind == "EXCESS":
            return "Excess Receipt", f"LSCRR {link.lscrr_no or '-'} applied - enter the separate Excess Receipt No"
        return "LSCRR-covered", f"LSCRR {link.lscrr_no or '-'} - no separate TRX expected"
    note = "LSCRR not yet declared for this Ship - confirm before ERP entry" if direct and link.lscrr_applied is None else ""
    return {"SHORT": "Issue TRX", "EXCESS": "Receipt TRX", "DAMAGE": "Subinventory transfer to DMG"}[kind], note


def build(iw: Iw) -> dict:
    op = _operator(iw)
    depot = Depot.objects.filter(code=op.depot_id).first() if op else None
    refs = []
    for l in iw.ship_links.select_related("ship").order_by("linked_at"):
        refs.append({"kind": "Ship", "ref": l.ship_id, "detail": f"{l.ship.from_org_code} {l.ship.from_org_name}".strip()
                     + (f" · LSCRR {l.lscrr_no}" if l.lscrr_no else "") + (" · split vehicle" if l.is_split else "")})
    for c in iw.vendor_consignments.select_related("vendor"):
        refs.append({"kind": "Vendor", "ref": f"{c.vendor_id} · PO {c.po_no} · challan {c.challan_no}",
                     "detail": c.vendor.name})
    for s in iw.outbound_ships.select_related("destination").order_by("linked_at"):
        refs.append({"kind": "Outbound Ship", "ref": s.ship_id, "detail": f"to {s.destination}"})
    for cb in iw.colour_bank_items.all():
        refs.append({"kind": "Colour Bank", "ref": cb.challan_no,
                     "detail": f"{cb.get_direction_display()} · {cb.description} {cb.serial_no}".strip()})
    for ns in iw.non_stock_refs.select_related("sender"):
        refs.append({"kind": "Non-Stock", "ref": ns.document_no, "detail": ns.sender.name})

    counts = {}
    for pc in unloading.counts(iw):
        counts.setdefault(unloading.WAREHOUSE_LABEL[pc.warehouse], []).append(
            {"label": pc.label, "unit": pc.unit, "count": _s(pc.count)})

    lines = list(iw.sed_lines.select_related("ship_link", "iw__load_type").order_by("source_key", "floor_sku"))
    t = sed.totals(iw)
    sed_rows = [{"source": ln.source_label, "sku": ln.sku, "floor_sku": ln.floor_sku, "description": ln.description,
                 "extra": ln.is_extra, "short": _s(ln.short_qty), "excess": _s(ln.excess_qty),
                 "damage": _s(ln.damage_qty), "damage_kind": ln.get_damage_kind_display() if ln.damage_qty else "",
                 "short_value": _money(ln.short_value) if ln.short_qty else "",
                 "damage_value": _money(ln.damage_value) if ln.damage_qty else "",
                 "remarks": " / ".join(x for x in (ln.short_remarks, ln.excess_remarks, ln.damage_remarks) if x),
                 "photos": ln.photos.count()} for ln in lines]

    memos = []
    for kind, title in MEMO_KINDS:
        field = {"SHORT": "short_qty", "EXCESS": "excess_qty", "DAMAGE": "damage_qty"}[kind]
        rows = []
        for ln in lines:
            qty = getattr(ln, field)
            if not qty or ln.source == SedLine.Source.OUTBOUND:
                continue
            action, note = _memo_rule(ln, kind)
            value = ln.short_value if kind == "SHORT" else ln.damage_value if kind == "DAMAGE" else None
            rows.append({"source": ln.source_label, "sku": ln.sku, "floor_sku": ln.floor_sku,
                         "description": ln.description, "qty": _s(qty), "value": _money(value) if value is not None else "",
                         "action": action, "note": note,
                         "remarks": {"SHORT": ln.short_remarks, "EXCESS": ln.excess_remarks,
                                     "DAMAGE": ln.damage_remarks}[kind]})
        if rows:
            memos.append({"kind": kind, "title": title, "rows": rows,
                          "all_lscrr": all(r["action"] == "LSCRR-covered" for r in rows)})

    rec = UnloadingRecord.objects.filter(iw=iw).first()
    unl = {"charge": _money(rec.charge), "status": rec.get_status_display(), "paid": _money(rec.paid_amount),
           "outstanding": _money(rec.outstanding), "mode": rec.get_payment_mode_display() if rec.payment_mode else "",
           "ref": rec.payment_ref, "tonnage": _s(rec.tonnage)} if rec else None
    arrival = timezone.localtime(iw.arrival_at)
    return {
        "depot": {"code": op.depot_id if op else "", "name": depot.name if depot else "",
                  "address": depot.address if depot else "", "cfa": op.cfa_name if op else "",
                  "seal": f"For {op.cfa_name}, CFA - {depot.name if depot else ''} Depot {op.depot_id}" if op else ""},
        "iw": {"no": iw.iw_no, "arrival": arrival.strftime("%d-%b-%Y %H:%M"), "load_type": str(iw.load_type),
               "source_org": str(iw.source_org or ""), "vehicle": iw.vehicle_no, "transporter": str(iw.transporter or ""),
               "lr": iw.lr_no, "driver": iw.driver_name, "driver_mobile": iw.driver_mobile,
               "verified": timezone.localtime(iw.verified_at).strftime("%d-%b-%Y %H:%M") if iw.verified_at else ""},
        "references": refs, "package_counts": counts, "sed": sed_rows,
        "sed_totals": {"lines": t.lines, "short": _s(t.short), "excess": _s(t.excess), "damage": _s(t.damage),
                       "short_value": _money(t.short_value), "damage_value": _money(t.damage_value),
                       "claimable": _money(t.claimable)},
        "unloading": unl, "memos": memos,
    }


def _hash(content: dict) -> str:
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def render(content: dict, revision: int, issued_at, issued_by: str, reason: str = "") -> str:
    return pdf.render_html("iw/mra_pdf.html", {"c": content, "rev": f"R{revision:02d}", "issued_by": issued_by,
                                              "issued_at": timezone.localtime(issued_at).strftime("%d-%b-%Y %H:%M"),
                                              "reason": reason, "copies": ["DRIVER COPY", "DEPOT COPY"]})


def is_current(iw: Iw, mra: MraRevision | None) -> bool:
    return bool(mra) and _hash(build(iw)) == mra.content_hash


@transaction.atomic
def issue(user: User, iw_pk: int, reason: str = "") -> tuple[MraRevision, bool]:
    """Returns (revision, created). An unchanged MRA is not re-issued; R02+ needs a reason."""
    require(user, "MRA_ISSUE")
    iw = Iw.objects.select_for_update(of=("self",)).select_related("load_type", "source_org", "transporter").get(pk=iw_pk)
    problems = blockers(iw)
    if problems:
        raise IwError(" ".join(problems))
    content = build(iw)
    digest = _hash(content)
    last = iw.mras.first()
    if last and last.content_hash == digest:
        return last, False
    if last and not clean_text(reason):
        raise IwError(f"The MRA changed since {last.label}. Enter what changed - it is printed on {_next(iw)}.")
    rev = (iw.mras.aggregate(m=Max("revision"))["m"] or 0) + 1
    now = timezone.now()
    html = render(content, rev, now, user.full_name or user.username, clean_text(reason))
    data = pdf.html_to_pdf(html)
    doc = None
    if data is not None:
        doc = documents.store(data, filename=f"MRA {iw.iw_no} R{rev:02d}.pdf", content_type="application/pdf",
                              doc_type="MRA_PDF", user=user, links=[("iw", iw.pk, "mra")])
    mra = MraRevision.objects.create(iw=iw, revision=rev, content=content, content_hash=digest, pdf=doc,
                                     reason=clean_text(reason), issued_by=user)
    MraRevision.objects.filter(pk=mra.pk).update(issued_at=now)
    mra.issued_at = now
    audit.record("MRA_GENERATED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"revision": mra.label, "reason": mra.reason, "pdf": str(doc.pk) if doc else None,
                        "sha256": doc.sha256 if doc else None, "claimable": content["sed_totals"]["claimable"],
                        "memos": [m["kind"] for m in content["memos"]]})
    return mra, True


def _next(iw: Iw) -> str:
    return f"R{(iw.mras.aggregate(m=Max('revision'))['m'] or 0) + 1:02d}"


def record_print(user: User, mra: MraRevision, what: str) -> None:
    audit.record("MRA_PRINTED", "iw", mra.iw_id, user=user, iw_id=mra.iw_id, after={"revision": mra.label, "as": what})
