"""Screen 03 - linking material and references to an IW (SoT 5). All rules live here."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Sum

from inward.accounts.models import User
from inward.accounts.services import require
from inward.core import audit
from inward.core.models import EmailStatus, Sender
from inward.domain.normalize import clean_text
from inward.domain.units import MeasureFamily, expected_measure
from inward.iw.models import ColourBankItem, Iw, NonStockLine, NonStockRef, ShipLink, ShipLinkLine
from inward.iw.services import IwError
from inward.sit.models import SitShip

LIVE = [s for s in Iw.Status.values if s not in (Iw.Status.DRAFT, Iw.Status.VOID)]
TRANSHIPMENT_TYPES = {"INBOUND_TS", "INTER_DEPOT_INBOUND_TS"}


def ship_link_state() -> tuple[set[str], set[str]]:
    """(Ships linked to a live IW, Ships with a Stock Receipt No entered - Screen 05)."""
    linked = set(ShipLink.objects.filter(iw__status__in=LIVE).values_list("ship_id", flat=True))
    return linked, set()


def _require_open(iw: Iw) -> None:
    if iw.status == Iw.Status.DRAFT:
        raise IwError("Create the IW (issue its number) before linking material.")
    if not iw.is_editable:
        raise IwError("Material can no longer be changed on this IW (MRA issued, closed or void).")
    if iw.status == Iw.Status.PHYSICALLY_VERIFIED:
        raise IwError("Physical verification is confirmed - reopen it (Unloading & SED) before changing material.")


def _refresh_status(iw: Iw) -> None:
    has_refs = (iw.ship_links.exists() or iw.colour_bank_items.exists() or iw.non_stock_refs.exists()
                or iw.outbound_ships.exists() or iw.vendor_consignments.exists())
    if has_refs and iw.status == Iw.Status.OPEN:
        iw.status = Iw.Status.MATERIAL_LINKED
    elif not has_refs and iw.status == Iw.Status.MATERIAL_LINKED:
        iw.status = Iw.Status.OPEN
    else:
        return
    iw.version += 1
    iw.save(update_fields=["status", "version", "updated_at"])

# ---------------------------------------------------------------- inbound SIT Ships


@dataclass
class ShipCandidate:
    ship: SitShip
    lines: list = field(default_factory=list)          # current SIT lines with .allocated_elsewhere, .available
    other_iws: list[Iw] = field(default_factory=list)  # live IWs this Ship is already linked to
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def needs_split(self) -> bool:
        return bool(self.other_iws)


def ship_candidate(iw: Iw, ship_id: str) -> ShipCandidate:
    ship = SitShip.objects.filter(pk=ship_id.strip()).first()
    if ship is None:
        raise IwError(f"Ship {ship_id} is not in the imported SIT. Normal Depot / RDC / Factory Ships are expected "
                      "in SIT - import the latest SIT report first.")
    cand = ShipCandidate(ship=ship)
    if not iw.load_type.has_inbound_sit:
        cand.blockers.append(f"Load type '{iw.load_type}' does not carry inbound SIT Ships.")
    if ship.status not in (SitShip.Status.IN_SIT, SitShip.Status.PARTIAL):
        cand.blockers.append(f"Ship is no longer pending in SIT ({ship.get_status_display()}).")
    if ship.has_issues:
        cand.blockers.append("The SIT report has row issues for this Ship (e.g. unknown UOM); resolve them first.")
    if ship.pre_go_live:
        cand.blockers.append("Ship is marked Pre-go-live (vehicle received before go-live).")
    if iw.ship_links.filter(ship=ship).exists():
        cand.blockers.append(f"Ship is already linked to {iw}.")
    other_links = ShipLink.objects.filter(ship=ship, iw__status__in=LIVE).exclude(iw=iw).select_related("iw")
    cand.other_iws = [lk.iw for lk in other_links]
    allocated = dict(ShipLinkLine.objects.filter(link__in=other_links).values("sku")
                     .annotate(q=Sum("quantity")).values_list("sku", "q"))
    for ln in ship.last_seen.lines.filter(ship_id=ship.ship_id).order_by("business_line", "pack_size", "sku"):
        ln.allocated_elsewhere = allocated.get(ln.sku, Decimal(0))
        ln.available = max(ln.quantity - ln.allocated_elsewhere, Decimal(0))
        cand.lines.append(ln)
    if cand.lines and all(ln.available == 0 for ln in cand.lines):
        cand.blockers.append("Everything pending on this Ship is already allocated to other IWs.")
    if (iw.source_org_id and ship.from_org_code != iw.source_org_id
            and iw.load_type_id not in TRANSHIPMENT_TYPES):
        cand.warnings.append(f"Ship comes from {ship.from_org_code} : {ship.from_org_name}, but the IW source is "
                             f"{iw.source_org}. Check the documents.")
    return cand


def _qty(value) -> Decimal:
    try:
        q = Decimal(str(value).strip() or "0")
    except InvalidOperation as exc:
        raise IwError(f"'{value}' is not a quantity.") from exc
    if q < 0:
        raise IwError("Quantities cannot be negative.")
    return q


@transaction.atomic
def link_ship(user: User, iw_pk: int, ship_id: str, *, split: bool = False, split_reason: str = "",
              allocations: dict[str, str] | None = None, lscrr_no: str = "", lscrr_applied: bool | None = None
              ) -> ShipLink:
    require(user, "MATERIAL_LINK")
    iw = Iw.objects.select_for_update().select_related("load_type").get(pk=iw_pk)
    _require_open(iw)
    cand = ship_candidate(iw, ship_id)
    if cand.blockers:
        raise IwError(" ".join(cand.blockers))
    if cand.needs_split and not split:
        raise IwError(f"Ship {cand.ship.ship_id} is already linked to {', '.join(map(str, cand.other_iws))}. "
                      "Only if this Ship really came on more than one vehicle, choose 'Split vehicle' and enter "
                      "the quantity on this vehicle.")
    if split and not split_reason.strip():
        raise IwError("A reason is mandatory for a split vehicle.")
    link = ShipLink.objects.create(iw=iw, ship=cand.ship, baseline_import=cand.ship.last_seen, is_split=split,
                                   split_reason=split_reason.strip(), lscrr_no=clean_text(lscrr_no).upper(),
                                   lscrr_applied=lscrr_applied if iw.load_type.is_direct_factory else None,
                                   linked_by=user)
    total = Decimal(0)
    for ln in cand.lines:
        qty = _qty((allocations or {}).get(ln.sku, "0")) if split else ln.available
        if qty > ln.available:
            raise IwError(f"SKU {ln.floor_sku} ({ln.sku}): {qty} is more than the {ln.available} still available "
                          "on this Ship.")
        if qty == 0:
            continue
        family = MeasureFamily(ln.measure_family) if ln.measure_family else None
        ShipLinkLine.objects.create(
            link=link, sku=ln.sku, floor_sku=ln.floor_sku, description=ln.description, business_line=ln.business_line,
            uom=ln.uom, measure_family=ln.measure_family, is_empty_packing=ln.is_empty_packing, pack_size=ln.pack_size,
            sit_quantity=ln.quantity, quantity=qty, measure=expected_measure(ln.pack_size, qty, family))
        total += qty
    if total == 0:
        raise IwError("Enter the quantity carried on this vehicle for at least one SKU.")
    _refresh_status(iw)
    audit.record("SHIP_LINKED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"ship": cand.ship.ship_id, "split": split, "split_reason": split_reason,
                        "other_iws": [str(i) for i in cand.other_iws], "baseline_import": cand.ship.last_seen_id,
                        "lscrr_no": link.lscrr_no, "lscrr_applied": link.lscrr_applied})
    return link


@transaction.atomic
def update_lscrr(user: User, link_pk: int, lscrr_no: str, applied: bool | None) -> ShipLink:
    require(user, "SED_TRX_LSCRR")
    link = ShipLink.objects.select_for_update().select_related("iw", "iw__load_type").get(pk=link_pk)
    if not link.iw.load_type.is_direct_factory:
        raise IwError("LSCRR applies to Direct Factory loads only.")
    if not link.iw.is_editable:
        raise IwError("LSCRR mode can only be changed by the System Supervisor after MRA (Screen 05).")
    before = {"lscrr_no": link.lscrr_no, "lscrr_applied": link.lscrr_applied}
    link.lscrr_no = clean_text(lscrr_no).upper()
    link.lscrr_applied = applied
    link.save(update_fields=["lscrr_no", "lscrr_applied"])
    audit.record("LSCRR_UPDATED", "iw", link.iw_id, user=user, iw_id=link.iw_id, before=before,
                 after={"ship": link.ship_id, "lscrr_no": link.lscrr_no, "lscrr_applied": applied})
    return link


@transaction.atomic
def unlink_ship(user: User, link_pk: int) -> None:
    require(user, "MATERIAL_LINK")
    link = ShipLink.objects.select_related("iw").get(pk=link_pk)
    iw = Iw.objects.select_for_update().get(pk=link.iw_id)
    _require_open(iw)
    audit.record("SHIP_UNLINKED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 before={"ship": link.ship_id, "lines": list(link.lines.values_list("sku", "quantity"))})
    link.delete()
    _refresh_status(iw)

# ---------------------------------------------------------------- Colour Bank and Non-Stock


@transaction.atomic
def add_colour_bank(user: User, iw_pk: int, *, direction: str, challan_no: str, description: str = "",
                    serial_no: str = "", asset_no: str = "", quantity: int = 1) -> ColourBankItem:
    require(user, "MATERIAL_LINK")
    iw = Iw.objects.select_for_update().get(pk=iw_pk)
    _require_open(iw)
    challan = clean_text(challan_no).upper()
    if not challan:
        raise IwError("Challan number is mandatory for a Colour Bank machine.")
    if quantity < 1:
        raise IwError("Quantity must be at least 1.")
    item = ColourBankItem.objects.create(iw=iw, direction=direction, challan_no=challan,
                                         description=clean_text(description), serial_no=clean_text(serial_no).upper(),
                                         asset_no=clean_text(asset_no).upper(), quantity=quantity, linked_by=user)
    _refresh_status(iw)
    audit.record("COLOUR_BANK_ADDED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"challan": challan, "direction": direction, "serial": item.serial_no, "asset": item.asset_no})
    return item


@transaction.atomic
def remove_colour_bank(user: User, item_pk: int) -> None:
    require(user, "MATERIAL_LINK")
    item = ColourBankItem.objects.get(pk=item_pk)
    iw = Iw.objects.select_for_update().get(pk=item.iw_id)
    _require_open(iw)
    audit.record("COLOUR_BANK_REMOVED", "iw", iw.pk, user=user, iw_id=iw.pk, before={"challan": item.challan_no})
    item.delete()
    _refresh_status(iw)


def sender_for(name: str, email: str) -> Sender:
    """Non-Stock sender; a new e-mail is used immediately but marked Unverified (SoT 7.6)."""
    name = clean_text(name)
    if not name:
        raise IwError("Sender name is mandatory.")
    sender = Sender.objects.filter(name__iexact=name).first()
    if sender is None:
        sender = Sender.objects.create(name=name, email=email.strip(), status=EmailStatus.UNVERIFIED)
        audit.record("SENDER_CAPTURED", "sender", sender.pk, after={"name": name, "email": sender.email})
    elif email.strip() and not sender.email:
        sender.email = email.strip()
        sender.save(update_fields=["email"])
    return sender


@transaction.atomic
def add_non_stock(user: User, iw_pk: int, *, sender_name: str, sender_email: str, document_no: str,
                  lines: list[tuple[str, str, str]]) -> NonStockRef:
    require(user, "MATERIAL_LINK")
    iw = Iw.objects.select_for_update().get(pk=iw_pk)
    _require_open(iw)
    doc = clean_text(document_no).upper()
    if not doc:
        raise IwError("Challan / document number is mandatory.")
    items = [(clean_text(d), clean_text(u) or "Nos", _qty(q)) for d, u, q in lines if clean_text(d)]
    if not items or any(q <= 0 for _, _, q in items):
        raise IwError("Enter at least one item with a quantity greater than zero.")
    ref = NonStockRef.objects.create(iw=iw, sender=sender_for(sender_name, sender_email), document_no=doc,
                                     linked_by=user)
    NonStockLine.objects.bulk_create([NonStockLine(ref=ref, description=d, unit=u, quantity=q) for d, u, q in items])
    _refresh_status(iw)
    audit.record("NON_STOCK_ADDED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"sender": ref.sender.name, "document": doc, "items": [(d, u, q) for d, u, q in items]})
    return ref


@transaction.atomic
def remove_non_stock(user: User, ref_pk: int) -> None:
    require(user, "MATERIAL_LINK")
    ref = NonStockRef.objects.get(pk=ref_pk)
    iw = Iw.objects.select_for_update().get(pk=ref.iw_id)
    _require_open(iw)
    audit.record("NON_STOCK_REMOVED", "iw", iw.pk, user=user, iw_id=iw.pk, before={"document": ref.document_no})
    ref.delete()
    _refresh_status(iw)
