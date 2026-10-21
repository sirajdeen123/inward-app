"""Screen 04 part C - MRA and SED memos (SoT 7.3, 7.4).

The MRA is built from the IW header, linked references, confirmed package counts (per warehouse), the SED
summary and the unloading position. It never lists the consignment's full SKU detail and never prints the
unloading rate. One PDF holds the MRA twice (driver copy, depot copy) followed by the SED memos for the System
Supervisor. Revisions R01, R02 ... are frozen; a new one is issued only when the content changed (with a reason).

Memo rules (SoT 7.3, table 10): Short -> issue TRX, Excess -> receipt TRX, Damage -> subinventory transfer to DMG.
Direct Factory Ship: all three are posted by the LSCCR request set, never manually (decision 17-Oct-2026); the
TRX Nos it created are entered on Screen 05. Outbound, Colour Bank and Non-Stock have no memos.
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
from inward.core import audit, branding, documents, pdf
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
    if iw.status == Iw.Status.PHYSICALLY_VERIFIED and not iw.sed_declaration:
        out.append("The NIL / SED declaration is missing - reopen physical verification and confirm it again.")
    if not iw.gate_register_no:
        out.append("Enter the security gate register No on the IW (Edit IW).")
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
    if link is not None and line.iw.load_type.is_direct_factory:
        return "By LSCCR request set", (f"LSCCR {link.lscrr_no}" if link.lscrr_no else "LSCCR No to be recorded") + \
            " - do not post manually; enter the TRX No it created"
    return {"SHORT": "Issue TRX", "EXCESS": "Receipt TRX", "DAMAGE": "Subinventory transfer to DMG"}[kind], ""


def other_items(iw: Iw) -> dict:
    """Colour Bank machines and Non-Stock items received, item by item (UAT 18-Oct-2026) - these have no SKU, no SIT
    and no IRR, so the MRA (and the detailed reconciliation report) list every item with its quantity."""
    def q(v):
        return format(v.normalize(), "f") if isinstance(v, Decimal) else str(v)
    colour = [{"challan": cb.challan_no, "direction": cb.get_direction_display(), "description": cb.description or "-",
               "serial": cb.serial_no or "-", "asset": cb.asset_no or "-", "qty": q(cb.quantity)}
              for cb in iw.colour_bank_items.order_by("challan_no", "pk")]
    non_stock = [{"document": ns.document_no, "sender": ns.sender.name, "description": ln.description,
                  "unit": ln.unit, "qty": q(ln.quantity)}
                 for ns in iw.non_stock_refs.select_related("sender").order_by("document_no", "pk")
                 for ln in ns.lines.order_by("pk")]
    return {"colour_bank": colour, "non_stock": non_stock}


def build(iw: Iw) -> dict:
    op = _operator(iw)
    depot = Depot.objects.filter(code=op.depot_id).first() if op else None
    refs = []
    for l in iw.ship_links.select_related("ship").order_by("linked_at"):
        shipped = f" / {l.ship.shipment_date:%d-%b-%Y}" if l.ship.shipment_date else ""
        refs.append({"kind": "Ship", "ref": f"{l.ship_id}{shipped}", "detail": f"{l.ship.from_org_code} {l.ship.from_org_name}".strip()
                     + (f" · LSCCR {l.lscrr_no}" if l.lscrr_no else "") + (" · split vehicle" if l.is_split else "")})
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

    # one consolidated total per package type: the Retail / Industrial split is only for the unloading team
    # (checklist), not for the driver or the source (UAT feedback 17-Oct-2026)
    packages: dict[tuple[str, str], dict] = {}
    for pc in sorted(unloading.counts(iw), key=lambda p: (p.sort_order, p.label)):
        row = packages.setdefault((pc.label, pc.unit), {"label": pc.label, "unit": pc.unit, "n": Decimal(0)})
        row["n"] += pc.count
    package_rows = [{"label": r["label"], "unit": r["unit"], "count": _s(r["n"])} for r in packages.values()]

    lines = list(iw.sed_lines.select_related("ship_link", "iw__load_type").order_by("source_key", "floor_sku"))
    t = sed.totals(iw)
    sed_rows = [{"source": ln.source_label, "sku": ln.sku, "floor_sku": ln.floor_sku, "description": ln.description,
                 "extra": ln.is_extra, "short": _s(ln.short_qty), "excess": _s(ln.excess_qty),
                 "damage": _s(ln.damage_qty), "damage_kind": ln.get_damage_kind_display() if ln.damage_qty else "",
                 "short_value": _money(ln.short_value) if ln.short_qty else "",
                 "damage_value": _money(ln.damage_value) if ln.damage_qty else "",
                 "excess_value": _money(ln.excess_value) if ln.excess_qty and ln.excess_value is not None else "",
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
            value = {"SHORT": ln.short_value, "DAMAGE": ln.damage_value, "EXCESS": ln.excess_value}[kind]
            rows.append({"source": ln.source_label, "sku": ln.sku, "floor_sku": ln.floor_sku,
                         "description": ln.description, "qty": _s(qty), "value": _money(value) if value is not None else "",
                         "action": action, "note": note,
                         "remarks": {"SHORT": ln.short_remarks, "EXCESS": ln.excess_remarks,
                                     "DAMAGE": ln.damage_remarks}[kind]})
        if rows:
            memos.append({"kind": kind, "title": title, "rows": rows})

    rec = UnloadingRecord.objects.filter(iw=iw).first()
    unl = {"charge": _money(rec.charge), "status": rec.get_status_display(), "paid": _money(rec.paid_amount),
           "outstanding": _money(rec.outstanding), "mode": rec.get_payment_mode_display() if rec.payment_mode else "",
           "ref": rec.payment_ref, "tonnage": _s(rec.tonnage)} if rec else None
    arrival = timezone.localtime(iw.arrival_at)
    return {
        "depot": {"code": op.depot_id if op else "", "name": depot.name if depot else "",
                  "address": depot.address if depot else "", "cfa": op.cfa_name if op else "",
                  "company": depot.company_name if depot else "",
                  "seal": branding.seal_lines(depot.company_name if depot else "", op.cfa_name if op else "",
                                              op.depot_id if op else "", depot.name if depot else "")},
        "iw": {"no": iw.iw_no, "arrival": arrival.strftime("%d-%b-%Y %I:%M %p"), "load_type": str(iw.load_type),
               "source_org": str(iw.source_org or ""), "vehicle": iw.vehicle_no, "transporter": str(iw.transporter or ""),
               "lr": iw.lr_no, "driver": iw.driver_name, "driver_mobile": iw.driver_mobile,
               "gate_register": iw.gate_register_no,
               "verified": timezone.localtime(iw.verified_at).strftime("%d-%b-%Y %I:%M %p") if iw.verified_at else ""},
        "references": refs, "packages": package_rows, "sed": sed_rows, "items": other_items(iw),
        "sed_totals": {"lines": t.lines, "short": _s(t.short), "excess": _s(t.excess), "damage": _s(t.damage),
                       "short_value": _money(t.short_value), "damage_value": _money(t.damage_value),
                       "excess_value": _money(t.excess_value),
                       "claimable": _money(t.claimable)},
        "unloading": unl, "memos": memos,
        "declaration": {k: v for k, v in (iw.sed_declaration or {}).items() if k != "by_id"},
    }


def _hash(content: dict) -> str:
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def render(content: dict, revision: int, issued_at, issued_by: str, reason: str = "") -> str:
    return pdf.render_html("iw/mra_pdf.html", {"c": content, "rev": f"R{revision:02d}", "issued_by": issued_by,
                                              "logo": branding.logo_data_uri(),
                                              "issued_at": timezone.localtime(issued_at).strftime("%d-%b-%Y %I:%M %p"),
                                              "reason": reason, "copies": ["DRIVER COPY", "DEPOT COPY"]})


def is_current(iw: Iw, mra: MraRevision | None) -> bool:
    return bool(mra) and _hash(build(iw)) == mra.content_hash


@transaction.atomic
def issue(user: User, iw_pk: int, reason: str = "", reconfirm: bool = False) -> tuple[MraRevision, bool]:
    """Returns (revision, created). An unchanged MRA is not re-issued; R02+ needs a reason. The clerk re-confirms the
    SED declaration made at verification (second check, 16-Oct-2026)."""
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
    if not reconfirm:
        d = iw.sed_declaration or {}
        raise IwError("Re-confirm the SED declaration before issuing the MRA: "
                      + ("NIL SED - no short, excess or damage." if d.get("kind") == "NIL"
                         else f"{d.get('lines', 0)} SED line(s) with {d.get('photos', 0)} damage photo(s)."))
    if last and not clean_text(reason):
        raise IwError(f"The MRA changed since {last.label}. Enter what changed - it is printed on {_next(iw)}.")
    rev = (iw.mras.aggregate(m=Max("revision"))["m"] or 0) + 1
    now = timezone.now()
    html = render(content, rev, now, user.full_name or user.username, clean_text(reason))
    data = pdf.html_to_pdf(html, fit=True)
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
