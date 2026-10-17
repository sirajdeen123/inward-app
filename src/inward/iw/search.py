"""Screen 07 - Universal Search (SoT 11): one box finds any IW by any reference, from the database only (indexed
metadata, never by opening PDF / Excel). The likely reference type is detected from the text; normalised,
spacing- and punctuation-insensitive matching for vehicle, LR, transporter, organisation and Floor SKU."""
from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field

from django.db.models import Q

from inward.domain.normalize import clean_text, search_key
from inward.iw.models import (
    ColourBankItem, DespatchRow, ErpReceipt, Iw, NonStockRef, OutboundShip, SedLine, SedTrx, ShipLink, ShipLinkLine,
    VendorConsignment, VendorLine,
)

LIMIT = 200


@dataclass
class Hit:
    iw: Iw
    matched: list[str] = field(default_factory=list)


def detect(q: str) -> str:
    t = q.strip().upper()
    if re.fullmatch(r"IW-?\d{4}-?\d{1,6}", t):
        return "IW number"
    if re.fullmatch(r"[A-Z]{2}\s?\d{1,2}\s?[A-Z]{0,3}\s?\d{3,4}", t):
        return "Vehicle"
    if re.fullmatch(r"\d{8}", t):
        return "Ship ID"
    if re.fullmatch(r"\d{10}", t):
        return "Oracle TRX"
    if re.fullmatch(r"\d{4,6}", t):
        return "Receipt No"
    if re.fullmatch(r"F[0-9A-Z]{15}", t):
        return "Full SKU"
    if re.fullmatch(r"[0-9A-Z]{3}\s?[0-9A-Z]{3}", t):
        return "Floor SKU"
    if re.fullmatch(r"V\d+", t):
        return "Vendor code"
    if "@" in t:
        return "E-mail"
    return "Text"


def _floor(q: str) -> str:
    k = search_key(q)
    return f"{k[:3]} {k[3:]}" if len(k) == 6 else q.strip().upper()


def search(q: str, *, date_from=None, date_to=None, status: str = "", load_type: str = "") -> tuple[str, list[Hit]]:
    q = clean_text(q)
    if len(q) < 2:
        return "", []
    kind = detect(q)
    key, up = search_key(q), q.upper()
    hits: "OrderedDict[int, Hit]" = OrderedDict()

    def add(iw_ids, label):
        for pk in iw_ids:
            if pk is None:
                continue
            h = hits.setdefault(pk, Hit(None))
            if label not in h.matched:
                h.matched.append(label)

    iw_no = re.sub(r"^IW-?(\d{4})-?(\d+)$", lambda m: f"IW-{m.group(1)}-{int(m.group(2)):06d}", up)
    add(Iw.objects.filter(Q(iw_no__iexact=iw_no) | Q(iw_no__icontains=up)).values_list("id", flat=True), "IW number")
    if len(key) >= 3:
        add(Iw.objects.filter(vehicle_key__contains=key).values_list("id", flat=True), "vehicle")
        add(Iw.objects.filter(lr_key__contains=key).values_list("id", flat=True), "LR")
    add(Iw.objects.filter(Q(driver_name__icontains=q) | Q(driver_mobile__contains=key)).values_list("id", flat=True),
        "driver")
    add(Iw.objects.filter(transporter__name__icontains=q).values_list("id", flat=True), "transporter")
    add(Iw.objects.filter(Q(source_org__code__iexact=q) | Q(source_org__name__icontains=q)).values_list("id", flat=True),
        "source organisation")
    add(ShipLink.objects.filter(Q(ship_id=key) | Q(ship__from_org_code__iexact=q) | Q(ship__from_org_name__icontains=q)
                                ).values_list("iw_id", flat=True), "Ship / origin")
    add(ShipLink.objects.filter(lscrr_no__iexact=up).values_list("iw_id", flat=True), "LSCCR No")
    add(OutboundShip.objects.filter(Q(ship_id=key) | Q(destination__code__iexact=q) | Q(destination__name__icontains=q)
                                    ).values_list("iw_id", flat=True), "outbound Ship / destination")
    add(VendorConsignment.objects.filter(Q(vendor__code__iexact=up) | Q(po_no__iexact=q) | Q(challan_key__iexact=key)
                                         | Q(challan_no__icontains=q) | Q(vendor__name__icontains=q)
                                         ).values_list("iw_id", flat=True), "vendor / PO / challan")
    add(ColourBankItem.objects.filter(Q(challan_no__iexact=q) | Q(serial_no__iexact=q) | Q(asset_no__iexact=q)
                                      ).values_list("iw_id", flat=True), "Colour Bank")
    add(NonStockRef.objects.filter(Q(document_no__iexact=q) | Q(sender__name__icontains=q)).values_list("iw_id", flat=True),
        "Non-Stock")
    add(ErpReceipt.objects.filter(receipt_no=up).values_list("iw_id", flat=True), "ERP receipt")
    add(SedTrx.objects.filter(trx_no=up).values_list("iw_id", flat=True), "SED TRX")
    add(Iw.objects.filter(mras__isnull=False, iw_no__iexact=up.split(" R")[0]).values_list("id", flat=True), "MRA")
    if kind in ("Full SKU", "Floor SKU") or len(key) in (6, 16):
        fl = _floor(q)
        for model, path, label in ((ShipLinkLine, "link__iw_id", "SKU on Ship"), (VendorLine, "consignment__iw_id", "SKU on challan"),
                                   (SedLine, "iw_id", "SKU in SED"), (DespatchRow, "outbound_ship__iw_id", "SKU outbound")):
            add(model.objects.filter(Q(sku=up) | Q(floor_sku=fl)).values_list(path, flat=True)[:LIMIT], label)
    if len(q) >= 4 and kind == "Text":
        add(ShipLinkLine.objects.filter(description__icontains=q).values_list("link__iw_id", flat=True)[:LIMIT],
            "description")

    qs = Iw.objects.filter(pk__in=list(hits)[:LIMIT]).select_related("load_type", "source_org", "transporter")
    if date_from:
        qs = qs.filter(arrival_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(arrival_at__date__lte=date_to)
    if status:
        qs = qs.filter(status=status)
    if load_type:
        qs = qs.filter(load_type_id=load_type)
    out = []
    for iw in qs.order_by("-arrival_at"):
        h = hits[iw.pk]
        h.iw = iw
        out.append(h)
    return kind, out


def irr_lookup(q: str) -> list:
    """IRR references (TRX / receipt / Shipping ID) - shown beside the IW results."""
    from inward.irr.models import IrrTransaction
    t = clean_text(q).upper()
    if not re.fullmatch(r"[0-9A-Z/ \-]{4,40}", t):
        return []
    return list(IrrTransaction.objects.filter(Q(trx_number=t) | Q(receipt_no=t) | Q(shipping_id=t))
                .order_by("-trx_date")[:50])
