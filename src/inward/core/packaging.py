"""SKU packaging master (SoT 7.1, decisions of 15-Oct-2026).

- Anyone with SKU_PACKAGING_EDIT may upload the company list or add / edit a SKU; changes apply at once.
- Every change is versioned (PackagingChange). Drastic changes are flagged to the CFA Manager, who
  accepts or reverts them: package group changed, units per package changed by half or more, or an upload
  changing more existing SKUs than `packaging_bulk_change_limit`.
- Drums / barrels / bags packed more than one per package carry a "Verify physically" flag until a clerk
  confirms them at unloading.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from inward.accounts.models import User
from inward.accounts.services import require
from inward.core import audit, documents
from inward.core.catalog import PACK_TYPE_MAP
from inward.core.models import FeatureFlag, PackageType, PackagingChange, PackagingUpload, SkuPackaging
from inward.domain.normalize import clean_code, clean_text, floor_sku, is_valid_sku, to_decimal
from inward.domain.units import classify_uom
from inward.importers.common import ImportFailed, RowIssue, is_blank_row, locate_header, read_first_sheet

REQUIRED = ["SKU Code", "SKU Name", "Pack Type", "Units Per Box"]
OPTIONAL = ["UOM", "Volume"]
SINGLE_PACKS = {"DRUM", "BARREL", "BAG"}
VERIFY_NOTE = "Verify physically: {units} units per {pack} in the company list - confirm at unloading"
NO_CONVERSION = {"EMPTY_PACKING", "SHOTS"}  # counted in pieces only


class PackagingError(Exception):
    pass

# ---------------------------------------------------------------- reading the company list


@dataclass
class PackRow:
    row_no: int
    sku: str
    description: str
    uom: str
    pack_size: Decimal | None
    pack_type_text: str
    group: str
    units: int | None


def _units(value) -> int | None:
    d = to_decimal(value)
    if d is None or d < 1 or d != d.to_integral_value():
        return None  # missing or fractional (colorant shots) -> no box conversion
    return int(d)


def read_company_list(path: Path) -> tuple[list[PackRow], list[RowIssue]]:
    sheet = read_first_sheet(path)
    header = locate_header(sheet, REQUIRED, OPTIONAL)
    rows, issues, seen = [], [], set()
    for idx in range(header.row_index + 1, len(sheet.rows)):
        raw = sheet.rows[idx]
        if is_blank_row(raw):
            continue
        g = lambda name: header.get(raw, name)  # noqa: E731
        sku = clean_code(g("SKU Code"))
        text = clean_text(g("Pack Type")).upper()
        if not is_valid_sku(sku):
            issues.append(RowIssue(idx + 1, f"'{sku}' is not a 16-character SKU"))
            continue
        if sku in seen:
            issues.append(RowIssue(idx + 1, f"SKU {sku} appears twice - the first row is used", sku))
            continue
        if text not in PACK_TYPE_MAP:
            issues.append(RowIssue(idx + 1, f"Unknown Pack Type '{text}'", sku))
            continue
        seen.add(sku)
        rows.append(PackRow(idx + 1, sku, clean_text(g("SKU Name")), clean_code(g("UOM")), to_decimal(g("Volume")),
                            text, PACK_TYPE_MAP[text], _units(g("Units Per Box"))))
    if not rows:
        raise ImportFailed("No SKU rows found in the packaging list")
    return rows, issues

# ---------------------------------------------------------------- drastic-change rule


def _state(p: SkuPackaging | None) -> dict | None:
    if p is None:
        return None
    return {"group": p.package_type_id, "pack_type": p.pack_type_text, "units": p.units_per_package,
            "description": p.description}


def drastic_reason(before: dict | None, group: str, units: int | None) -> str:
    if before is None:
        return ""
    if before["group"] != group:
        return f"package group {before['group']} -> {group}"
    old = before["units"]
    if old and units and (max(old, units) / min(old, units)) >= 1.5:
        return f"units per package {old} -> {units}"
    if old and not units:
        return f"units per package {old} -> not set"
    return ""


def _verify_note(group: str, pack_text: str, units: int | None) -> str:
    if group in SINGLE_PACKS and units and units > 1:
        return VERIFY_NOTE.format(units=units, pack=pack_text.lower() or group.lower())
    return ""


def _family(uom: str) -> str:
    result = classify_uom(uom, "", None)
    return result.family.value if result.family else ""

# ---------------------------------------------------------------- upload: preview then apply


@dataclass
class Diff:
    new: list[PackRow] = field(default_factory=list)
    changed: list[tuple[PackRow, dict, str]] = field(default_factory=list)  # (row, before, drastic reason)
    unchanged: int = 0

    @property
    def drastic(self):
        return [c for c in self.changed if c[2]]


def diff(rows: list[PackRow]) -> Diff:
    current = {p.sku: p for p in SkuPackaging.objects.filter(sku__in=[r.sku for r in rows])}
    d = Diff()
    for r in rows:
        p = current.get(r.sku)
        if p is None:
            d.new.append(r)
        elif (p.package_type_id, p.units_per_package, p.pack_type_text, p.description) == (r.group, r.units,
                                                                                            r.pack_type_text,
                                                                                            r.description):
            d.unchanged += 1
        else:
            before = _state(p)
            d.changed.append((r, before, drastic_reason(before, r.group, r.units)))
    return d


def _bulk_limit() -> int:
    flag = FeatureFlag.objects.filter(key="packaging_bulk_change_limit").first()
    return int((flag.value or {}).get("value") or 50) if flag else 50


@transaction.atomic
def create_upload(user: User, path: Path, filename: str) -> PackagingUpload:
    require(user, "SKU_PACKAGING_EDIT")
    rows, issues = read_company_list(path)
    d = diff(rows)
    doc = documents.store(Path(path).read_bytes(), filename=filename, content_type="application/vnd.ms-excel",
                          doc_type="SKU_PACKAGING_LIST", user=user)
    up = PackagingUpload.objects.create(
        document=doc, filename=filename, uploaded_by=user, bulk_flagged=len(d.changed) > _bulk_limit(),
        counts={"rows": len(rows), "new": len(d.new), "changed": len(d.changed), "drastic": len(d.drastic),
                "unchanged": d.unchanged, "errors": len(issues), "verify": sum(
                    1 for r in rows if _verify_note(r.group, r.pack_type_text, r.units))},
        sample={"drastic": [[r.sku, r.description, b["group"], b["units"], r.group, r.units, why]
                            for r, b, why in d.drastic[:50]],
                "changed": [[r.sku, r.description, b["group"], b["units"], r.group, r.units]
                            for r, b, why in d.changed if not why][:50],
                "new": [[r.sku, r.description, r.group, r.units] for r in d.new[:20]],
                "errors": [[i.row_no, i.message] for i in issues[:50]]})
    audit.record("PACKAGING_UPLOAD_PREVIEWED", "packaging_upload", up.pk, user=user, after=up.counts)
    return up


@transaction.atomic
def apply_upload(user: User, upload_pk: int) -> PackagingUpload:
    require(user, "SKU_PACKAGING_EDIT")
    up = PackagingUpload.objects.select_for_update().get(pk=upload_pk)
    if up.status != PackagingUpload.Status.PREVIEW:
        raise PackagingError("This upload is already applied or cancelled.")
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(up.filename).suffix.lower() or ".xlsx") as tmp:
        tmp.write(documents.read(up.document))  # re-read the stored, hash-checked file
    try:
        rows, _ = read_company_list(Path(tmp.name))
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    d = diff(rows)
    groups = {pt.code: pt for pt in PackageType.objects.all()}
    bulk = len(d.changed) > _bulk_limit()
    for r in d.new:
        p = SkuPackaging.objects.create(
            sku=r.sku, floor_sku=floor_sku(r.sku), description=r.description, uom=r.uom, pack_size=r.pack_size,
            measure_family=_family(r.uom), package_type=groups[r.group], pack_type_text=r.pack_type_text,
            units_per_package=r.units, verify_note=_verify_note(r.group, r.pack_type_text, r.units),
            source=SkuPackaging.Source.COMPANY_LIST, updated_by=user)
        PackagingChange.objects.create(sku=r.sku, action="ADDED", source="COMPANY_LIST", upload=up, after=_state(p),
                                       changed_by=user)
    for r, before, why in d.changed:
        p = SkuPackaging.objects.select_for_update().get(sku=r.sku)
        p.description, p.uom, p.pack_size = r.description, r.uom or p.uom, r.pack_size or p.pack_size
        p.measure_family = _family(p.uom) or p.measure_family
        p.package_type, p.pack_type_text, p.units_per_package = groups[r.group], r.pack_type_text, r.units
        p.verify_note = _verify_note(r.group, r.pack_type_text, r.units) if (
            before["units"] != r.units or before["group"] != r.group) else p.verify_note
        p.source, p.updated_by, p.version = SkuPackaging.Source.COMPANY_LIST, user, p.version + 1
        p.save()
        flagged = bool(why) or bulk
        PackagingChange.objects.create(
            sku=r.sku, action="UPDATED", source="COMPANY_LIST", upload=up, before=before, after=_state(p),
            drastic=flagged, drastic_why=why or "part of an upload changing many SKUs at once",
            review=PackagingChange.Review.PENDING if flagged else PackagingChange.Review.NONE, changed_by=user)
    up.status, up.applied_at, up.bulk_flagged = PackagingUpload.Status.APPLIED, timezone.now(), bulk
    up.save(update_fields=["status", "applied_at", "bulk_flagged"])
    audit.record("PACKAGING_UPLOAD_APPLIED", "packaging_upload", up.pk, user=user,
                 after={"new": len(d.new), "changed": len(d.changed), "drastic": len(d.drastic), "bulk": bulk})
    return up


@transaction.atomic
def cancel_upload(user: User, upload_pk: int) -> None:
    up = PackagingUpload.objects.select_for_update().get(pk=upload_pk)
    if up.status == PackagingUpload.Status.PREVIEW:
        up.status = PackagingUpload.Status.CANCELLED
        up.save(update_fields=["status"])
        audit.record("PACKAGING_UPLOAD_CANCELLED", "packaging_upload", up.pk, user=user)

# ---------------------------------------------------------------- clerk add / edit / verify


@transaction.atomic
def save_sku(user: User, sku: str, *, group: str, units, description: str = "", pack_size=None, uom: str = "",
             reason: str = "", verified: bool = False) -> PackagingChange:
    """Add a missing SKU or correct an existing one (clerk). An edit needs a reason."""
    require(user, "SKU_PACKAGING_EDIT")
    sku = clean_code(sku)
    if not is_valid_sku(sku):
        raise PackagingError(f"'{sku}' is not a 16-character SKU.")
    pt = PackageType.objects.filter(code=group).first()
    if pt is None:
        raise PackagingError("Select the package type.")
    n = _units(units)
    if pt.code not in NO_CONVERSION and n is None:
        raise PackagingError("Units per package must be a whole number of 1 or more.")
    n = n or 1
    p = SkuPackaging.objects.select_for_update().filter(sku=sku).first()
    if p is None:
        if not clean_text(description):
            raise PackagingError("Enter the SKU description.")
        p = SkuPackaging.objects.create(
            sku=sku, floor_sku=floor_sku(sku), description=clean_text(description), uom=clean_code(uom),
            pack_size=to_decimal(pack_size), measure_family=_family(clean_code(uom)), package_type=pt,
            pack_type_text=pt.name.upper(), units_per_package=n, source=SkuPackaging.Source.CLERK, updated_by=user)
        ch = PackagingChange.objects.create(sku=sku, action="ADDED", source="CLERK", after=_state(p),
                                            reason=reason.strip(), changed_by=user)
        audit.record("PACKAGING_ADDED", "sku_packaging", sku, user=user, after=_state(p))
        return ch
    before = _state(p)
    unchanged = before["group"] == pt.code and before["units"] == n
    if unchanged and verified and p.verify_note:
        p.verify_note = ""
        p.updated_by, p.version = user, p.version + 1
        p.save()
        ch = PackagingChange.objects.create(sku=sku, action="VERIFIED", source="CLERK", before=before,
                                            after=_state(p), reason=reason.strip() or "Physically verified",
                                            changed_by=user)
        audit.record("PACKAGING_VERIFIED", "sku_packaging", sku, user=user, after=_state(p))
        return ch
    if unchanged:
        raise PackagingError("Nothing changed.")
    if not reason.strip():
        raise PackagingError("A reason is mandatory when changing existing packaging.")
    why = drastic_reason(before, pt.code, n)
    p.package_type, p.units_per_package = pt, n
    if pt.code != before["group"]:
        p.pack_type_text = pt.name.upper()
    p.verify_note = ""  # the clerk has looked at the packing
    p.source, p.updated_by, p.version = SkuPackaging.Source.CLERK, user, p.version + 1
    p.save()
    ch = PackagingChange.objects.create(
        sku=sku, action="UPDATED", source="CLERK", before=before, after=_state(p), reason=reason.strip(),
        drastic=bool(why), drastic_why=why,
        review=PackagingChange.Review.PENDING if why else PackagingChange.Review.NONE, changed_by=user)
    audit.record("PACKAGING_UPDATED", "sku_packaging", sku, user=user, before=before, after=_state(p),
                 reason=reason.strip(), drastic=why)
    return ch

# ---------------------------------------------------------------- manager review


@transaction.atomic
def review(user: User, change_pk: int, accept: bool) -> PackagingChange:
    require(user, "PACKAGING_REVIEW")
    ch = PackagingChange.objects.select_for_update().get(pk=change_pk)
    if ch.review != PackagingChange.Review.PENDING:
        raise PackagingError("This change is already reviewed.")
    if not accept:
        p = SkuPackaging.objects.select_for_update().get(sku=ch.sku)
        if _state(p) != ch.after:
            raise PackagingError(f"SKU {ch.sku} was changed again after this change - review the latest change.")
        b = ch.before
        p.package_type = PackageType.objects.get(code=b["group"])
        p.pack_type_text, p.units_per_package, p.description = b["pack_type"], b["units"], b["description"]
        p.updated_by, p.version = user, p.version + 1
        p.save()
        PackagingChange.objects.create(sku=ch.sku, action="REVERTED", source="MANAGER", before=ch.after,
                                       after=_state(p), reason=f"Reverted change #{ch.pk}", changed_by=user)
    ch.review = PackagingChange.Review.ACCEPTED if accept else PackagingChange.Review.REVERTED
    ch.reviewed_by, ch.reviewed_at = user, timezone.now()
    ch.save(update_fields=["review", "reviewed_by", "reviewed_at"])
    audit.record("PACKAGING_CHANGE_" + ch.review, "sku_packaging", ch.sku, user=user, after={"change": ch.pk})
    return ch

# ---------------------------------------------------------------- pieces -> packages


@dataclass
class Packages:
    group: str            # PackageType code, or "" when packaging is not set
    full: int = 0         # full packages
    loose: Decimal = Decimal(0)
    units: int | None = None
    pieces_only: bool = False


def to_packages(pieces: Decimal, packaging: SkuPackaging | None) -> Packages:
    """33 pieces at 6 per carton -> 5 cartons + 3 loose (SoT 7.1)."""
    if packaging is None or not packaging.is_set:
        return Packages(group="", loose=pieces, pieces_only=True)
    if packaging.package_type_id in NO_CONVERSION:
        return Packages(group=packaging.package_type_id, loose=pieces, units=packaging.units_per_package,
                        pieces_only=True)
    u = packaging.units_per_package
    whole = int(pieces // u)
    return Packages(group=packaging.package_type_id, full=whole, loose=pieces - whole * u, units=u)


@transaction.atomic
def quick_add(user: User, sku: str, description: str) -> SkuPackaging:
    """A SKU met at unloading (e.g. a wrong item received) that is not in the list yet: added with packaging
    "not set" so it can be recorded now and completed later on the SKU packaging screen."""
    require(user, "SKU_PACKAGING_EDIT")
    sku = clean_code(sku)
    if not is_valid_sku(sku):
        raise PackagingError(f"'{sku}' is not a 16-character SKU.")
    desc = clean_text(description).upper()
    if not desc:
        raise PackagingError("Enter the description.")
    existing = SkuPackaging.objects.filter(sku=sku).first()
    if existing:
        return existing
    p = SkuPackaging.objects.create(sku=sku, floor_sku=floor_sku(sku), description=desc[:200],
                                    package_type=PackageType.objects.get(code="OTHER"), units_per_package=None,
                                    source=SkuPackaging.Source.CLERK, updated_by=user)
    PackagingChange.objects.create(sku=sku, action="ADDED", source="CLERK", after=_state(p), changed_by=user,
                                   reason="added at unloading - packaging still to be set")
    audit.record("SKU_PACKAGING_QUICK_ADD", "sku_packaging", sku, user=user, after={"description": desc})
    return p
