"""Vendor challans on an IW (SoT 5.4): Vendor Code + PO No + Challan No; lines from the vendor portal report
or entered manually. The IRR is later matched only through the PO Stock Receipt No."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.db import transaction

from inward.accounts.models import User
from inward.accounts.services import require
from inward.core import audit, documents
from inward.core.models import Vendor
from inward.domain.normalize import clean_code, clean_text, floor_sku, is_valid_sku
from inward.importers.vendor_portal import VendorImport, parse_vendor_portal
from inward.iw.linking import LIVE, _refresh_status, _require_open
from inward.iw.models import Iw, VendorConsignment, VendorLine
from inward.iw.services import IwError

FAMILY = {"L": "LTR", "ML": "LTR", "K": "KG", "KG": "KG", "G": "KG"}


WAREHOUSES = {"RETAIL", "INDUSTRIAL"}


def _warehouse(value: str) -> str:
    wh = (value or "").strip().upper()
    if wh not in WAREHOUSES:
        raise IwError("Select the warehouse (Retail or Industrial) where this vendor material is unloaded.")
    return wh


def _require_vendor(iw: Iw) -> None:
    _require_open(iw)
    if not iw.load_type.is_vendor:
        raise IwError(f"Load type '{iw.load_type}' is not a vendor load.")


def _check_unique(vendor_code: str, challan_key: str, iw: Iw) -> None:
    clash = (VendorConsignment.objects.filter(vendor_id=vendor_code, challan_key=challan_key, iw__status__in=LIVE)
             .exclude(iw=iw).select_related("iw").first())
    if clash:
        raise IwError(f"Challan {challan_key} of vendor {vendor_code} is already on {clash.iw}.")
    if VendorConsignment.objects.filter(vendor_id=vendor_code, challan_key=challan_key, iw=iw).exists():
        raise IwError("This challan is already on this IW.")


def _vendor(code: str, name: str = "", oracle_id: str = "", site: str = "") -> Vendor:
    code = clean_code(code)
    if not code:
        raise IwError("Vendor code is mandatory (e.g. V54).")
    vendor = Vendor.objects.filter(code=code).first()
    if vendor is None:
        if not clean_text(name):
            raise IwError(f"Vendor {code} is new - enter its name too.")
        vendor = Vendor.objects.create(code=code, name=clean_text(name).upper(), oracle_vendor_id=oracle_id, site=site)
        audit.record("VENDOR_CAPTURED", "vendor", code, after={"name": vendor.name, "oracle_vendor_id": oracle_id})
    return vendor


def read_portal(path: Path) -> VendorImport:
    return parse_vendor_portal(path, settings.INWARD["DEPOT_CODE"])


@transaction.atomic
def add_from_portal(user: User, iw_pk: int, path: Path, filename: str, vendor_code: str, challan_no: str,
                    warehouse: str = "") -> VendorConsignment:
    require(user, "MATERIAL_LINK")
    iw = Iw.objects.select_for_update().select_related("load_type").get(pk=iw_pk)
    _require_vendor(iw)
    wh = _warehouse(warehouse)
    report = read_portal(path)
    vendor_code = clean_code(vendor_code)  # clerks may type v54
    lines = report.for_challan(vendor_code, challan_no)
    if not lines:
        raise IwError(f"Challan {challan_no} of vendor {vendor_code} is not in this portal report for depot "
                      f"{settings.INWARD['DEPOT_CODE']}.")
    first = lines[0]
    key = clean_code(challan_no)
    _check_unique(first.vendor_code, key, iw)
    vendor = _vendor(first.vendor_code, first.vendor_name, first.oracle_vendor_id)
    doc = documents.store(Path(path).read_bytes(), filename=filename, content_type="application/vnd.ms-excel",
                          doc_type="VENDOR_PORTAL_REPORT", user=user, links=[("iw", iw.pk, "vendor_portal")])
    cons = VendorConsignment.objects.create(iw=iw, vendor=vendor, po_no=first.po_number, challan_no=first.challan_no,
                                            challan_key=key, source=VendorConsignment.Source.PORTAL, warehouse=wh,
                                            portal_document=doc, linked_by=user)
    VendorLine.objects.bulk_create([
        VendorLine(consignment=cons, sku=ln.sku, floor_sku=floor_sku(ln.sku), description=ln.description, uom=ln.uom,
                   measure_family=FAMILY.get(ln.uom, ""), nop=ln.nop, ltr=ln.ltr, kg=ln.kg) for ln in lines])
    _refresh_status(iw)
    audit.record("VENDOR_CHALLAN_ADDED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"vendor": vendor.code, "po": cons.po_no, "challan": cons.challan_no, "source": "PORTAL", "warehouse": wh,
                        "lines": len(lines)})
    return cons


def _dec(value, what: str) -> Decimal:
    try:
        d = Decimal(str(value).strip() or "0")
    except InvalidOperation as exc:
        raise IwError(f"{what}: '{value}' is not a number.") from exc
    if d < 0:
        raise IwError(f"{what} cannot be negative.")
    return d


@transaction.atomic
def add_manual(user: User, iw_pk: int, *, vendor_code: str, vendor_name: str, po_no: str, challan_no: str,
               lines: list[tuple[str, str, str, str, str]], warehouse: str = "") -> VendorConsignment:
    """lines: (sku, description, uom L/K, packages, quantity in LTR or KG). Used when the vendor has not
    uploaded the portal data yet."""
    require(user, "MATERIAL_LINK")
    iw = Iw.objects.select_for_update().select_related("load_type").get(pk=iw_pk)
    _require_vendor(iw)
    wh = _warehouse(warehouse)
    po, challan = clean_text(po_no), clean_text(challan_no)
    if not po or not challan:
        raise IwError("PO number and vendor challan number are mandatory.")
    vendor = _vendor(vendor_code, vendor_name)
    key = clean_code(challan)
    _check_unique(vendor.code, key, iw)
    items = []
    for sku, desc, uom, nop, qty in lines:
        sku = clean_code(sku)
        if not sku and not clean_text(desc):
            continue
        if not is_valid_sku(sku):
            raise IwError(f"'{sku}' is not a 16-character SKU.")
        uom = clean_code(uom) or "L"
        if uom not in FAMILY:
            raise IwError(f"UOM '{uom}' must be L, ML, K or G.")
        n, q = _dec(nop, "Packages"), _dec(qty, "Quantity")
        if n <= 0:
            raise IwError(f"SKU {sku}: number of packages must be greater than zero.")
        fam = FAMILY[uom]
        items.append(VendorLine(sku=sku, floor_sku=floor_sku(sku), description=clean_text(desc), uom=uom,
                                measure_family=fam, nop=n, ltr=q if fam == "LTR" else 0, kg=q if fam == "KG" else 0))
    if not items:
        raise IwError("Enter at least one SKU line.")
    cons = VendorConsignment.objects.create(iw=iw, vendor=vendor, po_no=po, challan_no=challan, challan_key=key,
                                            source=VendorConsignment.Source.MANUAL, warehouse=wh, linked_by=user)
    for it in items:
        it.consignment = cons
    VendorLine.objects.bulk_create(items)
    _refresh_status(iw)
    audit.record("VENDOR_CHALLAN_ADDED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"vendor": vendor.code, "po": po, "challan": challan, "source": "MANUAL", "warehouse": wh, "lines": len(items)})
    return cons


@transaction.atomic
def remove(user: User, cons_pk: int) -> None:
    require(user, "MATERIAL_LINK")
    cons = VendorConsignment.objects.get(pk=cons_pk)
    iw = Iw.objects.select_for_update().get(pk=cons.iw_id)
    _require_open(iw)
    audit.record("VENDOR_CHALLAN_REMOVED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 before={"vendor": cons.vendor_id, "challan": cons.challan_no})
    cons.delete()
    _refresh_status(iw)
