"""Screen 04 - physical verification and SED capture (SoT 7.2, decisions 16-Oct-2026).

Exceptions only: the clerk records Short / Excess / Damage for the SKUs that differ from the documents and then
confirms physical verification; every other checklist line counts as received as per document. Quantities are
pieces. An Excess may be a SKU that is not on any document (wrong item received). Damage needs remarks and at
least one photo. Financial values are entered by the clerk (SED_VALUE); for container-only damage the container
charge (carton / drum, effective-dated Damage Charge Master) pre-fills the value and is snapshotted in the line.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from inward.accounts.models import User
from inward.accounts.services import PermissionDenied, modes_for, require
from inward.core import audit, documents
from inward.core.models import RateMaster, SkuPackaging
from inward.domain.normalize import clean_code, clean_text, floor_sku, is_valid_sku
from inward.iw.checklist import is_stale
from inward.iw.models import DespatchRow, Iw, OutboundShip, SedLine, SedPhoto, ShipLink, VendorConsignment
from inward.iw.services import IwError

CONTAINER_RATES = {"CARTON": "DAMAGE_CARTON", "DRUM": "DAMAGE_DRUM"}
PHOTO_MAX_BYTES = 25 * 1024 * 1024
PREVIEW_PX = 1280


def rate_on(rate_type: str, on: date) -> Decimal | None:
    r = (RateMaster.objects.filter(rate_type=rate_type, effective_from__lte=on)
         .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=on)).order_by("-effective_from").first())
    return r.amount if r else None

# ---------------------------------------------------------------- what can carry SED


@dataclass
class Option:
    source_key: str
    source_label: str
    sku: str
    floor_sku: str
    description: str
    doc_pieces: Decimal

    @property
    def key(self) -> str:
        return f"{self.source_key}|{self.sku}"


def sources(iw: Iw) -> list[tuple[str, str]]:
    out = [(f"SHIP:{l.pk}", f"Ship {l.ship_id}") for l in iw.ship_links.order_by("linked_at")]
    out += [(f"VENDOR:{c.pk}", f"Vendor {c.vendor_id} challan {c.challan_no}") for c in iw.vendor_consignments.all()]
    out += [(f"OUTBOUND:{s.pk}", f"Outbound Ship {s.ship_id}") for s in iw.outbound_ships.order_by("linked_at")]
    return out


def options(iw: Iw) -> list[Option]:
    """Every SKU line of the linked material, with its document pieces."""
    out = []
    for link in iw.ship_links.order_by("linked_at").prefetch_related("lines"):
        out += [Option(f"SHIP:{link.pk}", f"Ship {link.ship_id}", ln.sku, ln.floor_sku, ln.description, ln.quantity)
                for ln in link.lines.order_by("floor_sku")]
    for cons in iw.vendor_consignments.prefetch_related("lines"):
        out += [Option(f"VENDOR:{cons.pk}", f"Vendor {cons.vendor_id} challan {cons.challan_no}", ln.sku, ln.floor_sku,
                       ln.description, ln.nop) for ln in cons.lines.order_by("floor_sku")]
    for ship in iw.outbound_ships.order_by("linked_at"):
        per: dict[str, Option] = {}
        for r in DespatchRow.objects.filter(outbound_ship=ship).order_by("floor_sku"):
            o = per.setdefault(r.sku, Option(f"OUTBOUND:{ship.pk}", f"Outbound Ship {ship.ship_id}", r.sku, r.floor_sku,
                                             r.description, Decimal(0)))
            o.doc_pieces += r.nop
        out += list(per.values())
    return out


def _resolve_source(iw: Iw, source_key: str) -> tuple[str, dict, str]:
    kind, _, pk = (source_key or "").partition(":")
    if not pk.isdigit():
        raise IwError("Choose the Ship / challan the SKU came with.")
    if kind == "SHIP":
        link = ShipLink.objects.filter(iw=iw, pk=pk).first()
        if link:
            return SedLine.Source.SHIP, {"ship_link": link}, f"Ship {link.ship_id}"
    elif kind == "VENDOR":
        cons = VendorConsignment.objects.filter(iw=iw, pk=pk).first()
        if cons:
            return SedLine.Source.VENDOR, {"vendor_consignment": cons}, f"Vendor {cons.vendor_id} challan {cons.challan_no}"
    elif kind == "OUTBOUND":
        ship = OutboundShip.objects.filter(iw=iw, pk=pk).first()
        if ship:
            return SedLine.Source.OUTBOUND, {"outbound_ship": ship}, f"Outbound Ship {ship.ship_id}"
    raise IwError("That Ship / challan is not on this IW any more. Reload the page.")

# ---------------------------------------------------------------- SED lines


def _pieces(value, name: str) -> Decimal:
    text = str(value or "").strip()
    try:
        d = Decimal(text or "0")
    except InvalidOperation as exc:
        raise IwError(f"{name}: '{value}' is not a number.") from exc
    if d < 0 or d != d.to_integral_value():
        raise IwError(f"{name} must be whole pieces (0 or more).")
    return d


def _money(value, name: str) -> Decimal | None:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        d = Decimal(text)
    except InvalidOperation as exc:
        raise IwError(f"{name}: '{value}' is not an amount.") from exc
    if d < 0:
        raise IwError(f"{name} cannot be negative.")
    return d.quantize(Decimal("0.01"))


def _require_unloading(iw: Iw) -> None:
    if iw.status == Iw.Status.PHYSICALLY_VERIFIED:
        raise IwError("Physical verification is already confirmed. Reopen it to change the SED.")
    if iw.status != Iw.Status.UNLOADING_IN_PROGRESS:
        raise IwError("Generate the unloading checklist first; SED is recorded during unloading.")


@dataclass
class SedInput:
    source_key: str
    sku: str
    short_qty: object = ""
    excess_qty: object = ""
    damage_qty: object = ""
    damage_kind: str = ""
    damage_pct: object = ""
    container_type: str = ""
    container_count: object = ""
    short_value: object = ""
    damage_value: object = ""
    short_remarks: str = ""
    excess_remarks: str = ""
    damage_remarks: str = ""
    description: str = ""  # only for an extra SKU


@transaction.atomic
def save_line(user: User, iw_pk: int, data: SedInput, line_pk: int | None = None, version: int | None = None
              ) -> SedLine:
    require(user, "PHYSICAL_SED_QTY")
    iw = Iw.objects.select_for_update().get(pk=iw_pk)
    _require_unloading(iw)
    line = None
    if line_pk:
        line = SedLine.objects.select_for_update().get(pk=line_pk, iw=iw)
        if version is not None and int(version) != line.version:
            raise IwError("This SED line was changed by someone else. Reload the page and enter it again.")
        source_key, sku = line.source_key, line.sku
    else:
        source_key, sku = data.source_key, clean_code(data.sku)
    source, fk, label = _resolve_source(iw, source_key)
    match = next((o for o in options(iw) if o.source_key == source_key and o.sku == sku), None)
    if match is None and not is_valid_sku(sku):
        raise IwError(f"'{sku}' - enter the full 16-character SKU.")
    extra = match is None
    doc = match.doc_pieces if match else Decimal(0)

    short, excess = _pieces(data.short_qty, "Short"), _pieces(data.excess_qty, "Excess")
    damage = _pieces(data.damage_qty, "Damage")
    if not (short or excess or damage):
        raise IwError("Enter a Short, Excess or Damage quantity (or remove the line).")
    if short and excess:
        raise IwError(f"SKU {floor_sku(sku)}: a line cannot be both short and excess - enter the net difference.")
    if extra and short:
        raise IwError(f"SKU {sku} is not on {label}; it can only be Excess (wrong item received).")
    if extra and damage > excess:
        raise IwError(f"SKU {sku} is not on {label}: damaged pieces cannot exceed the excess received.")
    if not extra and short + damage > doc:
        raise IwError(f"SKU {floor_sku(sku)}: short + damage ({short + damage}) is more than the document "
                      f"quantity ({doc}).")

    kind = container = ""
    pct = count = rate = None
    damage_remarks = clean_text(data.damage_remarks)
    if damage:
        kind = data.damage_kind if data.damage_kind in SedLine.DamageKind.values else ""
        if not kind:
            raise IwError(f"SKU {floor_sku(sku)}: choose whether the material or only the container is damaged.")
        if not damage_remarks:
            raise IwError(f"SKU {floor_sku(sku)}: damage remarks are mandatory.")
        if str(data.damage_pct or "").strip():
            pct = _money(data.damage_pct, "Damage %")
            if pct > 100:
                raise IwError("Damage % cannot be more than 100.")
        if kind == SedLine.DamageKind.CONTAINER:
            container = (data.container_type or "").upper()
            if container not in CONTAINER_RATES:
                raise IwError(f"SKU {floor_sku(sku)}: choose the damaged container - carton or drum.")
            count = int(_pieces(data.container_count, "Damaged containers"))
            if not count:
                raise IwError(f"SKU {floor_sku(sku)}: enter the number of damaged containers.")
            rate = rate_on(CONTAINER_RATES[container], timezone.localtime(iw.arrival_at).date())
            if rate is None:
                raise IwError(f"No {container.lower()} damage charge is set in the Damage Charge Master for this date.")

    short_value = _money(data.short_value, "Short value") if short else None
    damage_value = _money(data.damage_value, "Damage value") if damage else None
    if "GRANT" not in modes_for(user, "SED_VALUE"):  # e.g. System Supervisor: quantities only
        old_short, old_damage = (line.short_value, line.damage_value) if line else (None, None)
        if (short_value is not None and short_value != old_short) or                 (damage_value is not None and damage_value != old_damage):
            raise PermissionDenied("You may record SED quantities but not their financial values.")
        short_value = old_short if short else None
        damage_value = old_damage if damage else None
    if damage_value is None and rate is not None:
        damage_value = (rate * count).quantize(Decimal("0.01"))  # the container charge pre-fills the value

    pcs = lambda q: f"{q.normalize():f} pcs"  # noqa: E731
    values = dict(
        source=source, source_key=source_key, source_label=label, ship_link=fk.get("ship_link"),
        vendor_consignment=fk.get("vendor_consignment"), outbound_ship=fk.get("outbound_ship"),
        sku=sku, floor_sku=floor_sku(sku), is_extra=extra, doc_pieces=doc,
        description=(match.description if match else clean_text(data.description).upper()
                     or SkuPackaging.objects.filter(sku=sku).values_list("description", flat=True).first() or "")[:200],
        short_qty=short, excess_qty=excess, damage_qty=damage, damage_kind=kind, damage_pct=pct,
        container_type=container, container_count=count, container_rate=rate, short_value=short_value,
        damage_value=damage_value,
        short_remarks=(clean_text(data.short_remarks) or f"Short received - {pcs(short)}") if short else "",
        excess_remarks=(clean_text(data.excess_remarks)
                        or (f"Wrong item received - {pcs(excess)}" if extra else f"Excess received - {pcs(excess)}"))
        if excess else "",
        damage_remarks=damage_remarks if damage else "", updated_by=user)
    before = None
    if line is None:
        if SedLine.objects.filter(iw=iw, source_key=source_key, sku=sku).exists():
            raise IwError(f"SKU {floor_sku(sku)} of {label} already has an SED line - edit that line.")
        line = SedLine.objects.create(iw=iw, created_by=user, **values)
    else:
        before = _snap(line)
        for k, v in values.items():
            setattr(line, k, v)
        line.version += 1
        line.save()
    audit.record("SED_LINE_SAVED", "iw", iw.pk, user=user, iw_id=iw.pk, before=before, after=_snap(line))
    return line


def _snap(line: SedLine) -> dict:
    return {"source": line.source_label, "sku": line.sku, "short": line.short_qty, "excess": line.excess_qty,
            "damage": line.damage_qty, "damage_kind": line.damage_kind, "container": line.container_type,
            "containers": line.container_count, "rate": line.container_rate, "short_value": line.short_value,
            "damage_value": line.damage_value, "extra": line.is_extra}


@transaction.atomic
def delete_line(user: User, line_pk: int) -> None:
    require(user, "PHYSICAL_SED_QTY")
    line = SedLine.objects.select_for_update().get(pk=line_pk)
    iw = Iw.objects.select_for_update().get(pk=line.iw_id)
    _require_unloading(iw)
    audit.record("SED_LINE_REMOVED", "iw", iw.pk, user=user, iw_id=iw.pk, before=_snap(line))
    line.delete()

# ---------------------------------------------------------------- damage photos


def _preview(data: bytes) -> bytes:
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((PREVIEW_PX, PREVIEW_PX))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=80, optimize=True)
        return out.getvalue()


@transaction.atomic
def add_photo(user: User, line_pk: int, data: bytes, filename: str) -> SedPhoto:
    require(user, "PHYSICAL_SED_QTY")
    from PIL import Image, UnidentifiedImageError
    line = SedLine.objects.select_for_update().get(pk=line_pk)
    iw = Iw.objects.select_for_update().get(pk=line.iw_id)
    _require_unloading(iw)
    if not line.damage_qty:
        raise IwError("Photos are attached to damage lines.")
    if len(data) > PHOTO_MAX_BYTES:
        raise IwError("Photo is larger than 25 MB.")
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
            fmt = (img.format or "").upper()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise IwError(f"'{filename}' is not a photo (JPEG / PNG / WebP / HEIC saved as JPEG).") from exc
    content_type = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}.get(fmt)
    if content_type is None:
        raise IwError(f"Photo format {fmt or 'unknown'} is not accepted; use JPEG or PNG.")
    links = [("iw", iw.pk, "damage_photo"), ("sed_line", line.pk, "damage_photo")]
    original = documents.store(data, filename=filename, content_type=content_type, doc_type="DAMAGE_PHOTO",
                               user=user, links=links)
    preview = documents.store(_preview(data), filename=f"preview-{filename.rsplit('.', 1)[0]}.jpg",
                              content_type="image/jpeg", doc_type="DAMAGE_PHOTO_PREVIEW", user=user,
                              links=[("sed_line", line.pk, "damage_photo_preview")])
    photo = SedPhoto.objects.create(line=line, original=original, preview=preview, uploaded_by=user)
    audit.record("DAMAGE_PHOTO_ADDED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"sku": line.sku, "source": line.source_label, "document": str(original.pk),
                        "sha256": original.sha256})
    return photo


@transaction.atomic
def remove_photo(user: User, photo_pk: int) -> None:
    """Unlinks the photo from the SED line; the stored file stays in the document store (write-once evidence)."""
    require(user, "PHYSICAL_SED_QTY")
    photo = SedPhoto.objects.select_related("line").get(pk=photo_pk)
    iw = Iw.objects.select_for_update().get(pk=photo.line.iw_id)
    _require_unloading(iw)
    audit.record("DAMAGE_PHOTO_REMOVED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 before={"sku": photo.line.sku, "document": str(photo.original_id)})
    photo.delete()

# ---------------------------------------------------------------- summary and verification


@dataclass
class Totals:
    lines: int = 0
    short: Decimal = Decimal(0)
    excess: Decimal = Decimal(0)
    damage: Decimal = Decimal(0)
    short_value: Decimal = Decimal(0)
    damage_value: Decimal = Decimal(0)
    values_missing: int = 0

    @property
    def claimable(self) -> Decimal:
        return self.short_value + self.damage_value

    @property
    def any(self) -> bool:
        return self.lines > 0


def totals(iw: Iw) -> Totals:
    t = Totals()
    for ln in iw.sed_lines.all():
        t.lines += 1
        t.short += ln.short_qty
        t.excess += ln.excess_qty
        t.damage += ln.damage_qty
        t.short_value += ln.short_value or 0
        t.damage_value += ln.damage_value or 0
        if (ln.short_qty and ln.short_value is None) or (ln.damage_qty and ln.damage_value is None):
            t.values_missing += 1
    return t


def verification_blockers(iw: Iw) -> list[str]:
    out = []
    snap = iw.checklists.first()
    if snap is None:
        out.append("Generate and print the unloading checklist first.")
    elif is_stale(iw, snap):
        out.append("The material changed after the checklist was generated - regenerate it and check again.")
    for ln in iw.sed_lines.filter(damage_qty__gt=0).prefetch_related("photos"):
        if not ln.photos.all():
            out.append(f"Damage on {ln.floor_sku} ({ln.source_label}) needs at least one photo.")
    return out


# The clerk must actively declare the result twice - here (two statements) and again when issuing the MRA -
# so a forgotten SED or damage photo cannot slip through (business owner's decision, 16-Oct-2026).
DECLARATIONS = {
    "NIL": [("nil_short_excess", "Every package was counted against the unloading checklist: there is NO short and NO "
                                 "excess in this consignment."),
            ("nil_damage", "No package, tin, drum or carton is damaged: there is NO damage, so no photo is needed.")],
    "SED": [("sed_complete", "Every short, excess and damaged SKU of this consignment is recorded above - nothing is "
                             "left out."),
            ("sed_photos", "Every damaged item is photographed and the photos are attached to its line.")],
}
CONFIRM_ALL = {key for items in DECLARATIONS.values() for key, _ in items}


def declaration_kind(iw: Iw) -> str:
    return "SED" if iw.sed_lines.exists() else "NIL"


REJECTED_TEXT = ("An entry above was attempted but NOT saved. I have checked again: there is really no short, excess "
                 "or damage on it.")


def rejected_attempts(iw: Iw) -> list[dict]:
    """Short / Excess / Damage entries that were refused (and so not saved) since the checklist was generated and are
    still not on an SED line - a NIL declaration must not slip through after a failed entry (UAT 17-Oct-2026)."""
    from inward.core.models import AuditEvent
    snap = iw.checklists.first()
    if snap is None:
        return []
    field_of = {"SHORT": "short_qty", "EXCESS": "excess_qty", "DAMAGE": "damage_qty"}
    out, seen = [], set()
    for e in (AuditEvent.objects.filter(iw_id=iw.pk, action="SED_SAVE_REJECTED", occurred_at__gte=snap.generated_at)
              .select_related("user").order_by("-occurred_at")):
        a = e.after or {}
        kind, sku = a.get("kind", ""), a.get("sku", "")
        if (kind, sku) in seen:
            continue
        seen.add((kind, sku))
        f = field_of.get(kind)
        if f and sku and iw.sed_lines.filter(sku=sku, **{f"{f}__gt": 0}).exists():
            continue   # saved later
        out.append({"kind": kind or "SED", "sku": floor_sku(sku) if len(sku) == 16 else sku or "-",
                    "error": a.get("error", ""), "at": timezone.localtime(e.occurred_at),
                    "by": e.user.username if e.user else ""})
    return out


@transaction.atomic
def confirm_verification(user: User, iw_pk: int, confirmations=()) -> Iw:
    require(user, "PHYSICAL_SED_QTY")
    iw = Iw.objects.select_for_update().get(pk=iw_pk)
    _require_unloading(iw)
    blockers = verification_blockers(iw)
    if blockers:
        raise IwError(" ".join(blockers))
    kind = declaration_kind(iw)
    missing = [text for key, text in DECLARATIONS[kind] if key not in set(confirmations)]
    if missing:
        raise IwError("Tick both declarations before confirming: " + " / ".join(missing))
    rejected = rejected_attempts(iw)
    if rejected and "rejected_checked" not in set(confirmations):
        r = rejected[0]
        raise IwError(f"A {r['kind'].lower()} entry for {r['sku']} at {r['at']:%H:%M} was NOT saved ({r['error']}). "
                      "Record it again, or tick the extra declaration if there is really nothing to record.")
    t = totals(iw)
    photos = SedPhoto.objects.filter(line__iw=iw).count()
    now = timezone.now()
    iw.sed_declaration = {"kind": kind, "statements": [text for _, text in DECLARATIONS[kind]],
                          "by": user.full_name or user.username, "by_id": user.pk, "at": now.isoformat(),
                          "checklist_revision": iw.checklists.first().revision, "lines": t.lines, "photos": photos,
                          "short": str(t.short), "excess": str(t.excess), "damage": str(t.damage)}
    if rejected:
        iw.sed_declaration["statements"].append(REJECTED_TEXT)
        iw.sed_declaration["rejected_attempts"] = [f"{r['at']:%d-%b-%Y %H:%M} {r['kind']} {r['sku']}: {r['error']}"
                                                   for r in rejected]
    iw.status = Iw.Status.PHYSICALLY_VERIFIED
    iw.verified_at, iw.verified_by = now, user
    iw.version += 1
    iw.save(update_fields=["status", "verified_at", "verified_by", "sed_declaration", "version", "updated_at"])
    audit.record("PHYSICAL_VERIFICATION_CONFIRMED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"declaration": iw.sed_declaration})
    return iw


@transaction.atomic
def reopen_verification(user: User, iw_pk: int, reason: str) -> Iw:
    require(user, "PHYSICAL_SED_QTY")
    iw = Iw.objects.select_for_update().get(pk=iw_pk)
    if iw.status != Iw.Status.PHYSICALLY_VERIFIED:
        raise IwError("Physical verification can be reopened only before the MRA is issued.")
    if not clean_text(reason):
        raise IwError("A reason is mandatory to reopen physical verification.")
    iw.status = Iw.Status.UNLOADING_IN_PROGRESS
    iw.verified_at = iw.verified_by = None
    iw.sed_declaration = {}
    iw.version += 1
    iw.save(update_fields=["status", "verified_at", "verified_by", "sed_declaration", "version", "updated_at"])
    audit.record("PHYSICAL_VERIFICATION_REOPENED", "iw", iw.pk, user=user, iw_id=iw.pk, after={"reason": clean_text(reason)})
    return iw
