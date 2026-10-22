"""Screen 04 part B - document package counts, unloading charge and settlement (SoT 6, 7.5).

Package counts per warehouse are pre-filled from the latest unloading checklist (full packages per group, loose
pieces, empty packing / unconverted pieces, Colour Bank and Non-Stock) and confirmed or corrected by the clerk
from the documents; the MRA prints them. Unloading charge = tonnage (as entered, e.g. 13.2) x the Unloading Rate
Master rate effective on the arrival date, both snapshotted. Settlement: Paid, Part paid, Outstanding (posted to
the transporter ledger) or Exempt / Nil (clerk requests, CFA Manager approves). The ledger runs independently of
IW closure; receipts are entered against the transporter and settle the oldest outstanding IWs first.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from inward.accounts.models import ApprovalRequest, User
from inward.accounts.services import PermissionDenied, modes_for, request_approval, require
from inward.core import audit
from inward.core.models import Transporter
from inward.domain.normalize import clean_text
from inward.iw.models import Iw, PackageCount, TransporterLedgerEntry, UnloadingRecord
from inward.iw.sed import rate_on
from inward.iw.services import IwError

PIECE_GROUPS = {"EMPTY_PACKING", "NOT_SET", "SHOTS"}
WAREHOUSE_ORDER = ["RETAIL", "INDUSTRIAL", "OUTBOUND", "OTHER"]
WAREHOUSE_LABEL = {"RETAIL": "Retail warehouse", "INDUSTRIAL": "Industrial warehouse",
                   "OUTBOUND": "Outbound transhipment", "OTHER": "Other items"}
S = UnloadingRecord.Status


def _dec(value, name: str, places: str = "0.001", required: bool = True) -> Decimal | None:
    text = str(value or "").strip().replace(",", "")
    if not text:
        if required:
            raise IwError(f"{name} is mandatory.")
        return None
    try:
        d = Decimal(text)
    except InvalidOperation as exc:
        raise IwError(f"{name}: '{value}' is not a number.") from exc
    if d < 0:
        raise IwError(f"{name} cannot be negative.")
    return d.quantize(Decimal(places))


def _require_before_mra(iw: Iw) -> None:
    if iw.status in (Iw.Status.DRAFT, Iw.Status.OPEN):
        raise IwError("Link the material first.")
    if not iw.is_editable:
        raise IwError("The MRA is issued - package counts and unloading can no longer be changed.")

# ---------------------------------------------------------------- package counts


def from_checklist(iw: Iw) -> list[PackageCount]:
    """Package counts computed from the latest checklist revision (not saved)."""
    snap = iw.checklists.first()
    if snap is None:
        raise IwError("Generate the unloading checklist first - package counts are taken from it.")
    content = snap.content.get("own") or snap.content     # co-loaded truck: this IW's own packages for its MRA
    acc: dict[tuple[str, str], PackageCount] = {}

    def add(wh, group, label, unit, n, order):
        pc = acc.setdefault((wh, group), PackageCount(iw=iw, warehouse=wh, group=group, label=label, unit=unit,
                                                      count=Decimal(0), sort_order=order))
        pc.count += Decimal(str(n))

    for sheet in content["sheets"]:
        if sheet["key"] == "OTHER":
            cb = sheet["other"]["colour_bank"]
            if cb:
                add("OTHER", "COLOUR_BANK", "Colour Bank machines", "units", sum(Decimal(str(c["quantity"])) for c in cb), 1)
            for ns in sheet["other"]["non_stock"]:
                for x in ns["lines"]:
                    add("OTHER", f"NS:{x['unit'][:16]}", f"Non-Stock ({x['unit']})", "units", x["quantity"], 2)
            continue
        for g in sheet["groups"]:
            if g["code"] in PIECE_GROUPS:
                add(sheet["key"], g["code"], g["name"], "pieces", g["pieces"], 80 if g["code"] != "NOT_SET" else 75)
                continue
            if g["full"]:
                add(sheet["key"], g["code"], g["name"], "packages", g["full"], len(acc) + 1)
            if Decimal(g["loose"] or "0"):
                add(sheet["key"], "LOOSE", "Loose tins / units", "pieces", g["loose"], 60)
    out = list(acc.values())
    for pc in out:
        pc.checklist_count = pc.count
    return sorted(out, key=lambda p: (WAREHOUSE_ORDER.index(p.warehouse), p.sort_order, p.label))


def counts(iw: Iw) -> list[PackageCount]:
    """Saved counts, or the checklist proposal when nothing is saved yet."""
    saved = list(iw.package_counts.all())
    if saved:
        return sorted(saved, key=lambda p: (WAREHOUSE_ORDER.index(p.warehouse), p.sort_order, p.label))
    try:
        return from_checklist(iw)
    except IwError:
        return []


@transaction.atomic
def save_counts(user: User, iw_pk: int, values: dict[str, str], reload: bool = False) -> list[PackageCount]:
    """values: '<warehouse>|<group>' -> count entered by the clerk (blank keeps the checklist figure)."""
    require(user, "PHYSICAL_SED_QTY")
    iw = Iw.objects.select_for_update().get(pk=iw_pk)
    _require_before_mra(iw)
    proposal = from_checklist(iw)
    before = {f"{p.warehouse}|{p.group}": str(p.count) for p in iw.package_counts.all()}
    iw.package_counts.all().delete()
    for pc in proposal:
        key = f"{pc.warehouse}|{pc.group}"
        if not reload and str(values.get(key, "")).strip():
            pc.count = _dec(values[key], f"{WAREHOUSE_LABEL[pc.warehouse]} - {pc.label}")
        pc.pk = None
    PackageCount.objects.bulk_create(proposal)
    audit.record("PACKAGE_COUNTS_SAVED", "iw", iw.pk, user=user, iw_id=iw.pk, before=before or None,
                 after={f"{p.warehouse}|{p.group}": str(p.count) for p in proposal},
                 changed_from_checklist=[f"{p.warehouse}|{p.group}" for p in proposal if p.count != p.checklist_count])
    return proposal

# ---------------------------------------------------------------- gross weights -> tonnage (UAT feedback 17-Oct-2026)


@dataclass
class Weight:
    kind: str     # SHIP / OUTBOUND / VENDOR
    pk: int
    label: str
    kg: Decimal | None


def weights(iw: Iw) -> list[Weight]:
    out = [Weight("SHIP", l.pk, f"Ship {l.ship_id}", l.gross_kg) for l in iw.ship_links.order_by("linked_at")]
    out += [Weight("OUTBOUND", s.pk, f"Outbound Ship {s.ship_id}", s.gross_kg) for s in iw.outbound_ships.order_by("linked_at")]
    out += [Weight("VENDOR", c.pk, f"Vendor {c.vendor_id} challan {c.challan_no}", c.gross_kg)
            for c in iw.vendor_consignments.all()]
    return out


def suggested_tonnage(iw: Iw) -> tuple[Decimal | None, Decimal]:
    """(tonnes as the clerks write them - total kg / 1000 cut to one decimal, e.g. 13,256 kg -> 13.2; total kg).
    The lead IW of a co-loaded truck counts the whole truck (22-Oct-2026)."""
    from inward.iw.coload import charge_iw, members
    ws = [w for m in members(iw) if charge_iw(m).pk == iw.pk for w in weights(m)] or weights(iw)
    total = sum((w.kg for w in ws if w.kg), Decimal(0))
    if not total:
        return None, Decimal(0)
    return (total / 1000).quantize(Decimal("0.1"), rounding="ROUND_DOWN"), total


@transaction.atomic
def save_weights(user: User, iw_pk: int, values: dict[str, str]) -> None:
    """values: '<KIND>:<pk>' -> kg as typed (blank clears)."""
    from inward.iw.linking import parse_kg
    from inward.iw.models import OutboundShip, ShipLink, VendorConsignment
    require(user, "UNLOADING_RECORD")
    iw = Iw.objects.select_for_update().get(pk=iw_pk)
    _require_before_mra(iw)
    model = {"SHIP": ShipLink, "OUTBOUND": OutboundShip, "VENDOR": VendorConsignment}
    changed = {}
    for key, raw in values.items():
        kind, _, pk = key.partition(":")
        if kind not in model or not pk.isdigit():
            continue
        obj = model[kind].objects.filter(iw=iw, pk=int(pk)).first()
        if obj is None:
            continue
        kg = parse_kg(raw)
        if kg != obj.gross_kg:
            changed[key] = {"before": obj.gross_kg, "after": kg}
            obj.gross_kg = kg
            obj.save(update_fields=["gross_kg"])
    if changed:
        audit.record("GROSS_WEIGHTS_SAVED", "iw", iw.pk, user=user, iw_id=iw.pk, after=changed)

# ---------------------------------------------------------------- unloading charge and settlement


@dataclass
class UnloadingInput:
    tonnage: object
    status: str
    confirm_tonnage: bool = False    # co-loaded truck charged per IW: the clerk confirms an unusually high tonnage
    paid_amount: object = ""
    payment_mode: str = ""
    payment_ref: str = ""
    exempt_reason: str = ""


def _check_own_tonnage(iw: Iw, data: UnloadingInput) -> None:
    """Co-loaded truck charged separately on each IW: stop the whole truck's tonnage being charged on one IW too."""
    from inward.iw.coload import charge_each, members
    group = members(iw)
    if len(group) < 2 or not charge_each(iw) or data.confirm_tonnage:
        return
    try:
        t = Decimal(str(data.tonnage).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return
    own, _ = suggested_tonnage(iw)
    truck = sum((w.kg for m in group for w in weights(m) if w.kg), Decimal(0)) / 1000
    if own is not None and t > own + Decimal("0.5") and truck and t >= truck * Decimal("0.9"):
        raise IwError(f"{t} t looks like the whole truck ({truck:.1f} t) - this IW's own consignment is {own} t. "
                      "The truck is charged separately on each IW; enter this IW's tonnage, or tick 'tonnage "
                      "confirmed' to save it anyway.")


def _sync_ledger(user: User, iw: Iw, rec: UnloadingRecord | None) -> None:
    entry = TransporterLedgerEntry.objects.filter(iw=iw).first()
    owed = rec.outstanding if rec else 0
    if owed and iw.transporter_id:
        if entry is None:
            entry = TransporterLedgerEntry(iw=iw, kind=TransporterLedgerEntry.Kind.CHARGE, created_by=user)
        entry.transporter_id, entry.amount = iw.transporter_id, owed
        entry.entry_date = timezone.localtime(iw.arrival_at).date()
        entry.remarks = f"Unloading {iw.iw_no} · {iw.vehicle_no}"
        entry.save()
    elif entry is not None:
        entry.delete()


@transaction.atomic
def record(user: User, iw_pk: int, data: UnloadingInput, version: int | None = None) -> UnloadingRecord:
    require(user, "UNLOADING_RECORD")
    iw = Iw.objects.select_for_update(of=("self",)).select_related("transporter").get(pk=iw_pk)
    _require_before_mra(iw)
    from inward.iw.coload import charge_iw
    lead = charge_iw(iw)
    if lead.pk != iw.pk:
        raise IwError(f"This truck is co-loaded with {lead}: the unloading of the whole truck is recorded once, on {lead}.")
    _check_own_tonnage(iw, data)
    rec = UnloadingRecord.objects.select_for_update().filter(iw=iw).first()
    suggested, _ = suggested_tonnage(iw)
    if rec and version is not None and int(version) != rec.version:
        raise IwError("The unloading record was changed by someone else. Reload the page.")
    tonnage = _dec(data.tonnage, "Tonnage (tonnes)")
    if tonnage <= 0:
        raise IwError("Tonnage must be more than zero (e.g. 13.2 for 13,256 kg).")
    arrival = timezone.localtime(iw.arrival_at).date()
    rate = rate_on("UNLOADING_PER_TON", arrival)
    if rate is None:
        raise IwError(f"No unloading rate is set in the Unloading Rate Master for {arrival:%d-%b-%Y}.")
    from inward.core.models import RateMaster
    eff = (RateMaster.objects.filter(rate_type="UNLOADING_PER_TON", effective_from__lte=arrival)
           .order_by("-effective_from").values_list("effective_from", flat=True).first())
    charge = (tonnage * rate).quantize(Decimal("0.01"))

    status = data.status
    if status not in (S.PAID, S.PART_PAID, S.OUTSTANDING, S.EXEMPT):
        raise IwError("Choose the payment position: Paid, Part paid, Outstanding or Exempt / Nil.")
    paid, mode, ref = Decimal(0), "", clean_text(data.payment_ref)[:60]
    if status in (S.PAID, S.PART_PAID):
        paid = _dec(data.paid_amount, "Amount paid", "0.01", required=status == S.PART_PAID) or charge
        mode = data.payment_mode if data.payment_mode in UnloadingRecord.Mode.values else ""
        if not mode:
            raise IwError("Choose how it was paid (cash, UPI, bank transfer or other).")
        if status == S.PAID and paid != charge:
            raise IwError(f"Paid means the full charge of Rs {charge}; for Rs {paid} choose Part paid.")
        if status == S.PART_PAID and not (0 < paid < charge):
            raise IwError(f"Part paid must be more than 0 and less than the charge of Rs {charge}.")
    if status in (S.PART_PAID, S.OUTSTANDING) and not iw.transporter_id:
        raise IwError("Set the transporter on the IW first - the outstanding amount is posted to its ledger.")

    exempt_req = rec.exempt_request if rec else None
    reason = clean_text(data.exempt_reason)
    if status == S.EXEMPT:
        if not reason:
            raise IwError("A reason is mandatory for Exempt / Nil unloading.")
        modes = modes_for(user, "UNLOADING_EXEMPT")
        if "APPROVE" in modes or "GRANT" in modes:
            exempt_req = None  # CFA Manager records it directly (audited)
        elif rec and rec.status == S.EXEMPT_REQUESTED and exempt_req and exempt_req.status == ApprovalRequest.Status.PENDING:
            status = S.EXEMPT_REQUESTED  # already waiting
        else:
            try:
                exempt_req = request_approval(user, "UNLOADING_EXEMPT", "iw", iw.pk, reason,
                                              payload={"iw_no": iw.iw_no, "charge": str(charge)})
            except ValueError as exc:
                raise IwError(str(exc)) from exc
            status = S.EXEMPT_REQUESTED
    elif exempt_req and exempt_req.status == ApprovalRequest.Status.PENDING:
        exempt_req.status = ApprovalRequest.Status.CANCELLED  # clerk changed to a payment instead
        exempt_req.save(update_fields=["status"])

    before = _snap(rec) if rec else None
    values = dict(tonnage=tonnage, tonnage_from_weights=suggested, rate=rate, rate_effective_from=eff, charge=charge,
                  status=status, paid_amount=paid,
                  payment_mode=mode, payment_ref=ref if status in (S.PAID, S.PART_PAID) else "",
                  exempt_reason=reason if status in (S.EXEMPT, S.EXEMPT_REQUESTED) else "", exempt_request=exempt_req)
    if rec is None:
        rec = UnloadingRecord.objects.create(iw=iw, recorded_by=user, **values)
    else:
        for k, v in values.items():
            setattr(rec, k, v)
        rec.version += 1
        rec.save()
    _sync_ledger(user, iw, rec)
    audit.record("UNLOADING_RECORDED", "iw", iw.pk, user=user, iw_id=iw.pk, before=before, after=_snap(rec))
    from inward.cash.services import sync_unloading
    sync_unloading(rec, user)   # cash paid -> waits for the cashier
    return rec


def _snap(rec: UnloadingRecord) -> dict:
    return {"tonnage": rec.tonnage, "from_weights": rec.tonnage_from_weights, "rate": rec.rate, "charge": rec.charge,
            "status": rec.status,
            "paid": rec.paid_amount, "mode": rec.payment_mode, "outstanding": rec.outstanding}


def apply_exempt(req: ApprovalRequest, approver: User) -> None:
    """Effect of an approved UNLOADING_EXEMPT request."""
    iw = Iw.objects.select_for_update().get(pk=int(req.entity_id))
    rec = UnloadingRecord.objects.select_for_update().filter(iw=iw, exempt_request=req).first()
    if rec is None or rec.status != S.EXEMPT_REQUESTED:
        raise IwError("The unloading record changed after this request; nothing to approve.")
    if not iw.is_editable:
        raise IwError(f"{iw.iw_no}: the MRA is already issued.")
    rec.status = S.EXEMPT
    rec.version += 1
    rec.save(update_fields=["status", "version", "updated_at"])
    _sync_ledger(approver, iw, rec)
    audit.record("UNLOADING_EXEMPT_APPROVED", "iw", iw.pk, user=approver, iw_id=iw.pk,
                 after={"charge": rec.charge, "reason": rec.exempt_reason}, approval_request=req.pk)


def gate_ok(iw: Iw) -> bool:
    """G04-ULC: unloading amount and payment / ledger position recorded (a co-loaded IW follows its lead IW)."""
    from inward.iw.coload import charge_iw
    rec = UnloadingRecord.objects.filter(iw=charge_iw(iw)).first()
    return bool(rec) and rec.status in UnloadingRecord.SETTLED

# ---------------------------------------------------------------- transporter ledger


@dataclass
class LedgerView:
    transporter: Transporter
    entries: list = field(default_factory=list)
    charges: Decimal = Decimal(0)
    receipts: Decimal = Decimal(0)
    open_iws: list = field(default_factory=list)  # (entry, still owed) oldest first

    @property
    def balance(self) -> Decimal:
        return self.charges - self.receipts


def ledger(transporter: Transporter) -> LedgerView:
    v = LedgerView(transporter, list(transporter.ledger.select_related("iw").order_by("entry_date", "id")))
    v.charges = sum((e.amount for e in v.entries if e.kind == TransporterLedgerEntry.Kind.CHARGE), Decimal(0))
    v.receipts = sum((e.amount for e in v.entries if e.kind == TransporterLedgerEntry.Kind.RECEIPT), Decimal(0))
    pool = v.receipts  # receipts settle the oldest outstanding IWs first
    for e in (x for x in v.entries if x.kind == TransporterLedgerEntry.Kind.CHARGE):
        used = min(pool, e.amount)
        pool -= used
        if e.amount - used > 0:
            v.open_iws.append((e, e.amount - used))
    return v


def balances() -> list[LedgerView]:
    ids = TransporterLedgerEntry.objects.values_list("transporter_id", flat=True).distinct()
    views = [ledger(t) for t in Transporter.objects.filter(pk__in=list(ids)).order_by("name")]
    return sorted(views, key=lambda v: (-v.balance, v.transporter.name))


def is_settled(iw: Iw) -> bool | None:
    """None = nothing posted to the ledger; True / False = outstanding fully received or not."""
    entry = TransporterLedgerEntry.objects.filter(iw=iw).select_related("transporter").first()
    if entry is None:
        return None
    return all(e.pk != entry.pk for e, _ in ledger(entry.transporter).open_iws)


@transaction.atomic
def add_receipt(user: User, transporter_pk: int, *, amount, entry_date=None, payment_mode="", reference="",
                remarks="") -> TransporterLedgerEntry:
    require(user, "UNLOADING_RECORD")
    t = Transporter.objects.select_for_update().get(pk=transporter_pk)
    amt = _dec(amount, "Amount received", "0.01")
    if amt <= 0:
        raise IwError("Amount received must be more than zero.")
    v = ledger(t)
    if amt > v.balance:
        raise IwError(f"Rs {amt} is more than the outstanding balance of Rs {v.balance}.")
    if payment_mode not in UnloadingRecord.Mode.values:
        raise IwError("Choose how it was received (cash, UPI, bank transfer or other).")
    day = entry_date or timezone.localdate()
    if day > timezone.localdate():
        raise PermissionDenied("A receipt cannot be dated in the future.")
    e = TransporterLedgerEntry.objects.create(transporter=t, kind=TransporterLedgerEntry.Kind.RECEIPT, amount=amt,
                                              entry_date=day, payment_mode=payment_mode,
                                              reference=clean_text(reference)[:60], remarks=clean_text(remarks)[:200],
                                              created_by=user)
    audit.record("TRANSPORTER_RECEIPT", "transporter", t.pk, user=user,
                 after={"amount": amt, "date": day, "mode": payment_mode, "reference": e.reference})
    from inward.cash.services import from_ledger_receipt
    from_ledger_receipt(e, user)   # cash -> waits for the cashier
    return e
