"""Screen 05 part A - ERP receipt and SED TRX entry (SoT 8.1, 8.6).

Each inbound Ship takes one or more Stock Receipt Nos (always booked for the full document quantity); a vendor
challan takes PO Receipt Nos. SED memos (Ship / vendor reference x Short / Excess / Damage) take one or more Oracle
TRX Nos. Direct Factory short / excess / damage is posted in Oracle by the LSCCR request set, never manually
(decision 17-Oct-2026): the memo shows the Ship's LSCCR No, the clerk checks the request set has run and enters the
TRX Nos it created; the reconciliation treats them like any other SED TRX. Entering a number only records it - the
IRR proves it. Changing the LSCCR No after a TRX is entered for that Ship needs the System Supervisor and a reason.
(The LSCCR No is stored in the ShipLink.lscrr_* fields.)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction

from inward.accounts.models import User
from inward.accounts.services import require, role_codes
from inward.core import audit
from inward.domain.normalize import clean_text
from inward.iw.models import ErpReceipt, Iw, SedLine, SedTrx, ShipLink, VendorConsignment
from inward.iw.services import IwError

ERP_STAGES = {Iw.Status.MRA_ISSUED, Iw.Status.ERP_IN_PROGRESS, Iw.Status.RECON_COMPLETE,
              Iw.Status.DOCUMENTS_COMPLETE, Iw.Status.REOPENED}
NUMBER = re.compile(r"^[A-Z0-9][A-Z0-9/\-]{0,39}$")
R = ErpReceipt.Kind


def _number(value: str, what: str) -> str:
    n = clean_text(value).upper().replace(" ", "")
    if not NUMBER.match(n):
        raise IwError(f"{what}: enter the number as shown in Oracle (e.g. 42637).")
    return n


def _require_erp(iw: Iw) -> None:
    if iw.status not in ERP_STAGES:
        raise IwError("ERP entries start after the MRA is issued (Screen 04 complete or released).")


def _to_erp(iw: Iw) -> None:
    if iw.status == Iw.Status.MRA_ISSUED:
        iw.status = Iw.Status.ERP_IN_PROGRESS
        iw.version += 1
        iw.save(update_fields=["status", "version", "updated_at"])

# ---------------------------------------------------------------- receipts


@transaction.atomic
def add_receipt(user: User, iw_pk: int, ref_key: str, kind: str, receipt_no: str) -> ErpReceipt:
    require(user, "RECEIPT_ENTRY")
    iw = Iw.objects.select_for_update(of=("self",)).select_related("load_type").get(pk=iw_pk)
    _require_erp(iw)
    ref, _, pk = (ref_key or "").partition(":")
    no = _number(receipt_no, "Receipt No")
    values = {}
    if ref == "SHIP":
        link = ShipLink.objects.filter(iw=iw, pk=pk).first()
        if link is None:
            raise IwError("That Ship is not on this IW.")
        if kind != R.NORMAL:
            raise IwError("A Ship takes Stock Receipt numbers; short / excess / damage go through the SED memo TRX Nos.")
        values["ship_link"] = link
        label = f"Ship {link.ship_id}"
    elif ref == "VENDOR":
        cons = VendorConsignment.objects.filter(iw=iw, pk=pk).first()
        if cons is None:
            raise IwError("That vendor challan is not on this IW.")
        if kind != R.PO:
            raise IwError("A vendor challan takes PO Receipt numbers.")
        values["vendor_consignment"] = cons
        label = f"Vendor {cons.vendor_id} challan {cons.challan_no}"
    else:
        raise IwError("Choose the Ship or vendor challan.")
    clash = ErpReceipt.objects.filter(kind=kind, receipt_no=no).select_related("iw").first()
    if clash:
        raise IwError(f"{ErpReceipt.Kind(kind).label} {no} is already entered on {clash.iw}.")
    rec = ErpReceipt.objects.create(iw=iw, kind=kind, receipt_no=no, entered_by=user, **values)
    _to_erp(iw)
    audit.record("ERP_RECEIPT_ENTERED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"kind": kind, "receipt_no": no, "reference": label})
    return rec


@transaction.atomic
def remove_receipt(user: User, receipt_pk: int) -> None:
    require(user, "RECEIPT_ENTRY")
    rec = ErpReceipt.objects.select_related("iw").get(pk=receipt_pk)
    _require_erp(rec.iw)
    audit.record("ERP_RECEIPT_REMOVED", "iw", rec.iw_id, user=user, iw_id=rec.iw_id,
                 before={"kind": rec.kind, "receipt_no": rec.receipt_no})
    rec.delete()


@transaction.atomic
def set_lscrr(user: User, link_pk: int, lscrr_no: str, reason: str = "") -> ShipLink:
    """Record the LSCCR No of a Direct Factory Ship - the request set that posts its short / excess / damage."""
    require(user, "SED_TRX_LSCRR")
    link = ShipLink.objects.select_for_update().select_related("iw__load_type").get(pk=link_pk)
    if not link.iw.load_type.is_direct_factory:
        raise IwError("LSCCR applies only to Direct Factory loads.")
    if link.iw.status in (Iw.Status.CLOSED, Iw.Status.VOID):
        raise IwError("This IW is closed.")
    number = clean_text(lscrr_no).upper()
    if not number:
        raise IwError("Enter the LSCCR number.")
    changing = bool(link.lscrr_no) and link.lscrr_no != number
    if changing and SedTrx.objects.filter(iw_id=link.iw_id, source_key=f"SHIP:{link.pk}").exists():
        if "SYSTEM_SUPERVISOR" not in role_codes(user):
            raise IwError("The LSCCR No can be changed after a TRX is entered only by the System Supervisor.")
        if not clean_text(reason):
            raise IwError("A reason is mandatory to change the LSCCR No after a TRX is entered.")
    before = {"lsccr_no": link.lscrr_no}
    link.lscrr_applied, link.lscrr_no = True, number
    link.save(update_fields=["lscrr_applied", "lscrr_no"])
    audit.record("LSCCR_SET", "iw", link.iw_id, user=user, iw_id=link.iw_id, before=before,
                 after={"ship": link.ship_id, "lsccr_no": number, "reason": clean_text(reason)})
    return link

# ---------------------------------------------------------------- SED memos and TRX


@dataclass
class Memo:
    source_key: str
    source_label: str
    kind: str
    lines: list = field(default_factory=list)  # (SedLine, qty)
    direct: bool = False   # Direct Factory Ship: posted by the LSCCR request set
    lsccr_no: str = ""
    trx: list = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.source_key}|{self.kind}"

    @property
    def qty(self) -> Decimal:
        return sum((q for _, q in self.lines), Decimal(0))


def memos(iw: Iw) -> list[Memo]:
    """The SED memos of the IW (current SED lines; outbound has none), with their entered TRX numbers."""
    out: dict[str, Memo] = {}
    field_of = {"SHORT": "short_qty", "EXCESS": "excess_qty", "DAMAGE": "damage_qty"}
    for ln in iw.sed_lines.exclude(source=SedLine.Source.OUTBOUND).select_related("ship_link").order_by("source_key", "floor_sku"):
        direct = bool(ln.ship_link and iw.load_type.is_direct_factory)
        for kind, f in field_of.items():
            qty = getattr(ln, f)
            if qty:
                m = out.setdefault(f"{ln.source_key}|{kind}", Memo(ln.source_key, ln.source_label, kind))
                m.lines.append((ln, qty))
                m.direct, m.lsccr_no = direct, ln.ship_link.lscrr_no if direct else ""
    for t in iw.sed_trx.all():
        m = out.get(f"{t.source_key}|{t.kind}")
        if m:
            m.trx.append(t)
    order = {"SHORT": 0, "EXCESS": 1, "DAMAGE": 2}
    return sorted(out.values(), key=lambda m: (m.source_key, order[m.kind]))


@transaction.atomic
def add_sed_trx(user: User, iw_pk: int, memo_key: str, trx_no: str) -> SedTrx:
    require(user, "SED_TRX_LSCRR")
    iw = Iw.objects.select_for_update(of=("self",)).select_related("load_type").get(pk=iw_pk)
    _require_erp(iw)
    memo = next((m for m in memos(iw) if m.key == memo_key), None)
    if memo is None:
        raise IwError("That SED memo is not on this IW.")
    if memo.direct and not memo.lsccr_no:
        raise IwError(f"{memo.source_label}: record the LSCCR No first - Direct Factory short / excess / damage is "
                      "posted by the LSCCR request set.")
    no = _number(trx_no, "TRX No")
    clash = SedTrx.objects.filter(trx_no=no).select_related("iw").first()
    if clash:
        raise IwError(f"TRX {no} is already entered on {clash.iw} ({clash.source_label} {clash.kind.lower()}).")
    t = SedTrx.objects.create(iw=iw, source_key=memo.source_key, source_label=memo.source_label, kind=memo.kind,
                              trx_no=no, entered_by=user)
    _to_erp(iw)
    audit.record("SED_TRX_ENTERED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"memo": f"{memo.source_label} {memo.kind}", "trx_no": no})
    return t


@transaction.atomic
def remove_sed_trx(user: User, trx_pk: int) -> None:
    require(user, "SED_TRX_LSCRR")
    t = SedTrx.objects.select_related("iw").get(pk=trx_pk)
    _require_erp(t.iw)
    audit.record("SED_TRX_REMOVED", "iw", t.iw_id, user=user, iw_id=t.iw_id,
                 before={"memo": f"{t.source_label} {t.kind}", "trx_no": t.trx_no})
    t.delete()
