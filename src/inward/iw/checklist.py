"""Unloading checklist (SoT 7.1, decisions 14/15-Oct-2026).

Sheets: RETAIL WAREHOUSE, INDUSTRIAL WAREHOUSE (inbound Ships + vendor challans), OUTBOUND TRANSHIPMENT
(tallied outbound rows, per Ship) and OTHER ITEMS (Colour Bank, Non-Stock). Within a warehouse sheet,
groups follow the unloading order (cartons first ... empty packing last) and lines are sorted by pack size.
Pieces are converted to packages with the SKU packaging master; the packaging used is frozen in the
generated snapshot, so a printed checklist never changes.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.db.models import Max

from inward.accounts.models import User
from inward.accounts.services import require
from inward.core import audit
from inward.core.models import BusinessLineWarehouse, PackageType, SkuPackaging
from inward.core.packaging import to_packages
from inward.iw.linking import _require_open
from inward.iw.models import ChecklistSnapshot, Iw
from inward.iw.outbound import state as outbound_state
from inward.iw.services import IwError

WAREHOUSE_SHEETS = [("RETAIL", "RETAIL WAREHOUSE"), ("INDUSTRIAL", "INDUSTRIAL WAREHOUSE")]
NOT_SET = ("NOT_SET", "Packaging not set", 75)  # before empty packing, which is always last
EMPTY = "EMPTY_PACKING"


def _num(d) -> str:
    return format(Decimal(d).normalize(), "f") if d is not None else ""


@dataclass
class Line:
    source: str            # "Ship 52497520" / "Vendor V54 challan 641406/876/2627" / "Outbound Ship 52611001"
    sku: str
    floor_sku: str
    description: str
    business_line: str
    pack_size: Decimal | None
    uom: str
    pieces: Decimal
    measure_family: str
    measure: Decimal
    group: str = ""
    group_name: str = ""
    group_order: int = 0
    units: int | None = None
    full: int = 0
    loose: Decimal = Decimal(0)
    pieces_only: bool = False
    verify_note: str = ""
    packaging_missing: bool = False
    parts: list = field(default_factory=list)   # (source, pieces) when the same SKU came on several documents

    def as_dict(self) -> dict:
        return {"source": self.source, "sku": self.sku, "floor_sku": self.floor_sku,
                "description": self.description, "business_line": self.business_line,
                "pack_size": _num(self.pack_size), "uom": self.uom, "pieces": _num(self.pieces),
                "measure_family": self.measure_family, "measure": _num(self.measure), "group": self.group,
                "group_name": self.group_name, "units": self.units, "full": self.full, "loose": _num(self.loose),
                "pieces_only": self.pieces_only, "verify_note": self.verify_note,
                "packaging_missing": self.packaging_missing,
                "parts": [{"source": s, "pieces": _num(p)} for s, p in self.parts] if len(self.parts) > 1 else []}


def _merge(lines: list[Line]) -> list[Line]:
    """One row per SKU on a warehouse sheet (22-Oct-2026): the unloading team counts the SKU once; the Ship /
    challan split is kept in the 'From' column. Outbound sheets are never merged (they stay with their Ship)."""
    out: dict[tuple, Line] = {}
    for ln in lines:
        key = (ln.sku, ln.group == EMPTY)
        m = out.get(key)
        if m is None:
            ln.parts = [(ln.source, ln.pieces)]
            out[key] = ln
            continue
        m.pieces += ln.pieces
        m.measure += ln.measure
        m.full += ln.full
        m.loose += ln.loose
        m.parts.append((ln.source, ln.pieces))
    for m in out.values():
        if len(m.parts) > 1:
            m.source = " + ".join(s for s, _ in m.parts)
    return list(out.values())


@dataclass
class Build:
    sheets: list[dict] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    missing_packaging: list[Line] = field(default_factory=list)
    source_hash: str = ""


def _pack(line: Line, packs: dict[str, SkuPackaging], groups: dict[str, PackageType]) -> Line:
    p = packs.get(line.sku)
    if line.group == EMPTY:  # SIT BULKPC empty packing: pieces only, own section
        line.group_name, line.group_order, line.pieces_only = groups[EMPTY].name, groups[EMPTY].sort_priority, True
        return line
    pk = to_packages(line.pieces, p)
    if not pk.group:
        line.group, line.group_name, line.group_order = NOT_SET
        line.pieces_only, line.packaging_missing = True, True
        return line
    g = groups[pk.group]
    line.group, line.group_name, line.group_order = g.code, g.name, g.sort_priority
    line.units, line.full, line.loose, line.pieces_only = pk.units, pk.full, pk.loose, pk.pieces_only
    line.verify_note = p.verify_note if p else ""
    return line


def _group_lines(lines: list[Line]) -> list[dict]:
    groups: dict[str, dict] = {}
    for ln in sorted(lines, key=lambda x: (x.group_order, x.pack_size or 0, x.floor_sku, x.source)):
        g = groups.setdefault(ln.group, {"code": ln.group, "name": ln.group_name, "lines": [],
                                         "full": 0, "loose": Decimal(0), "pieces": Decimal(0)})
        g["lines"].append(ln)
        g["full"] += ln.full
        g["loose"] += ln.loose
        g["pieces"] += ln.pieces
    return list(groups.values())


def _sheet(key: str, title: str, lines: list[Line]) -> dict:
    ltr = sum((ln.measure for ln in lines if ln.measure_family == "LTR"), Decimal(0))
    kg = sum((ln.measure for ln in lines if ln.measure_family == "KG"), Decimal(0))
    return {"key": key, "title": title, "groups": _group_lines(lines), "lines": len(lines),
            "pieces": sum((ln.pieces for ln in lines), Decimal(0)), "ltr": ltr, "kg": kg}


def build(iw: Iw, own_only: bool = False) -> Build:
    """The truck's checklist: a co-loaded IW includes the other IWs of the same truck (own_only=False), each line's
    'From' naming its IW; own_only=True gives this IW's material alone (its MRA package counts)."""
    from inward.iw.coload import members
    group = [iw] if own_only else members(iw)
    if len(group) > 1:
        merged, hashes = Build(), []
        for m in group:
            part = build(m, own_only=True)
            hashes.append(part.source_hash)
            prefix = f"{m.iw_no} · "
            for s in part.sheets:
                if s["key"] == "OTHER":
                    tgt = next((x for x in merged.sheets if x["key"] == "OTHER"), None)
                    if tgt is None:
                        merged.sheets.append({"key": "OTHER", "title": s["title"],
                                              "other": {"colour_bank": [], "non_stock": []}})
                        tgt = merged.sheets[-1]
                    for c in s["other"]["colour_bank"]:
                        tgt["other"]["colour_bank"].append({**c, "challan": prefix + str(c["challan"])})
                    for n in s["other"]["non_stock"]:
                        tgt["other"]["non_stock"].append({**n, "document": prefix + str(n["document"])})
                    continue
                for g in s["groups"]:
                    for ln in g["lines"]:
                        ln.source = prefix + ln.source
                        ln.parts = [(prefix + src, n) for src, n in ln.parts] if len(ln.parts) > 1 else []
                merged.sheets.append(s)
            merged.blockers += [f"{m}: {x}" for x in part.blockers]
            merged.warnings += [f"{m}: {x}" for x in part.warnings]
            merged.missing_packaging += part.missing_packaging
        return _merge_sheets(merged, hashes)
    b = Build()
    warehouse_of = dict(BusinessLineWarehouse.objects.values_list("business_line", "warehouse"))
    groups = {g.code: g for g in PackageType.objects.all()}
    by_wh: dict[str, list[Line]] = {"RETAIL": [], "INDUSTRIAL": []}
    outbound_sheets: list[dict] = []
    raw = []

    for link in iw.ship_links.select_related("ship").prefetch_related("lines"):
        for ln in link.lines.all():
            wh = warehouse_of.get(ln.business_line)
            if wh is None:
                b.blockers.append(f"Business line '{ln.business_line or '(blank)'}' of SKU {ln.floor_sku} "
                                  "(Ship {}) is not mapped to a warehouse - map it under Masters.".format(link.ship_id))
                continue
            line = Line(f"Ship {link.ship_id}", ln.sku, ln.floor_sku, ln.description, ln.business_line, ln.pack_size,
                        ln.uom, ln.quantity, ln.measure_family, ln.measure, group=EMPTY if ln.is_empty_packing else "")
            by_wh[wh].append(line)
            raw.append(("S", link.ship_id, ln.sku, str(ln.quantity), wh))
    for cons in iw.vendor_consignments.prefetch_related("lines"):
        for ln in cons.lines.all():
            measure = ln.kg if ln.measure_family == "KG" else ln.ltr
            line = Line(f"Vendor {cons.vendor_id} challan {cons.challan_no}", ln.sku, ln.floor_sku, ln.description,
                        "", None, ln.uom, ln.nop, ln.measure_family, measure)
            by_wh[cons.warehouse].append(line)
            raw.append(("V", cons.challan_key, ln.sku, str(ln.nop), cons.warehouse))

    all_lines = by_wh["RETAIL"] + by_wh["INDUSTRIAL"]
    if iw.load_type.has_outbound:
        st = outbound_state(iw)
        if not st.ships:
            b.warnings.append("This load type carries outbound transhipment but no outbound Ship is entered.")
        elif not st.ready:
            b.blockers.append("Save the rows of every outbound Ship (they must tally with its document) before the checklist.")
        for s in st.ships:
            lines = [Line(f"Outbound Ship {s.ship.ship_id}", r.sku, r.floor_sku, r.description, r.business_line,
                          r.pack_size, r.uom, r.nop, "KG" if r.kg and not r.ltr else "LTR", r.kg if r.kg and not r.ltr
                          else r.ltr) for r in s.ship.rows.all()]
            all_lines += lines
            outbound_sheets.append((s.ship, lines))
            raw += [("O", s.ship.ship_id, ln.sku, str(ln.pieces), "") for ln in lines]

    packs = {p.sku: p for p in SkuPackaging.objects.filter(sku__in={ln.sku for ln in all_lines})}
    for ln in all_lines:
        _pack(ln, packs, groups)
    b.missing_packaging = sorted({ln.sku: ln for ln in all_lines if ln.packaging_missing}.values(), key=lambda x: x.sku)
    by_wh = {k: _merge(v) for k, v in by_wh.items()}   # after packing: packages are counted per document, then added

    for key, title in WAREHOUSE_SHEETS:
        if by_wh[key]:
            b.sheets.append(_sheet(key, title, by_wh[key]))
    for ship, lines in outbound_sheets:
        sheet = _sheet("OUTBOUND", f"OUTBOUND TRANSHIPMENT - Ship {ship.ship_id} to {ship.destination}", lines)
        sheet["keep_separate"] = True
        b.sheets.append(sheet)

    other = {"colour_bank": [{"direction": c.get_direction_display(), "challan": c.challan_no,
                              "description": c.description, "serial": c.serial_no, "asset": c.asset_no,
                              "quantity": c.quantity} for c in iw.colour_bank_items.all()],
             "non_stock": [{"sender": r.sender.name, "document": r.document_no,
                            "lines": [{"description": x.description, "unit": x.unit, "quantity": _num(x.quantity)}
                                      for x in r.lines.all()]} for r in iw.non_stock_refs.select_related("sender")]}
    raw += [("C", c["challan"], c["quantity"]) for c in other["colour_bank"]]
    raw += [("N", n["document"], len(n["lines"])) for n in other["non_stock"]]
    if other["colour_bank"] or other["non_stock"]:
        b.sheets.append({"key": "OTHER", "title": "OTHER ITEMS - Colour Bank / Non-Stock", "other": other})
    if not b.sheets:
        b.blockers.append("No material is linked to this IW yet.")
    b.source_hash = hashlib.sha256(json.dumps(sorted(map(str, raw))).encode()).hexdigest()
    return b


def _merge_sheets(merged: Build, hashes: list[str]) -> Build:
    """Sheets of the same warehouse from the truck's IWs become one sheet; a SKU on both is one row."""
    out, order = {}, []
    for s in merged.sheets:
        if s["key"] in ("OTHER",) or s.get("keep_separate"):
            out[id(s)] = s
            order.append(id(s))
            continue
        if s["key"] not in out:
            out[s["key"]] = {"key": s["key"], "title": s["title"], "lines": []}
            order.append(s["key"])
        out[s["key"]]["lines"] += [ln for g in s["groups"] for ln in g["lines"]]
    sheets = []
    for k in order:
        s = out[k]
        if "groups" in s or s["key"] == "OTHER":
            sheets.append(s)
            continue
        rows: dict[tuple, Line] = {}
        for ln in s["lines"]:
            key = (ln.sku, ln.group)
            parts = ln.parts or [(ln.source, ln.pieces)]
            m = rows.get(key)
            if m is None:
                ln.parts = list(parts)
                rows[key] = ln
                continue
            m.pieces += ln.pieces
            m.measure += ln.measure
            m.full += ln.full
            m.loose += ln.loose
            m.parts += parts
        for m in rows.values():
            if len(m.parts) > 1:
                m.source = " + ".join(src for src, _ in m.parts)
            else:
                m.parts = []
        sheets.append(_sheet(s["key"], s["title"], list(rows.values())))
    merged.sheets = sheets
    merged.missing_packaging = sorted({ln.sku: ln for ln in merged.missing_packaging}.values(), key=lambda x: x.sku)
    merged.source_hash = hashlib.sha256(json.dumps(sorted(hashes)).encode()).hexdigest()
    return merged


def _serialise(b: Build) -> dict:
    sheets = []
    for s in b.sheets:
        if s["key"] == "OTHER":
            sheets.append({"key": "OTHER", "title": s["title"], "other": s["other"]})
            continue
        sheets.append({"key": s["key"], "title": s["title"], "keep_separate": s.get("keep_separate", False),
                       "lines": s["lines"], "pieces": _num(s["pieces"]), "ltr": _num(s["ltr"]), "kg": _num(s["kg"]),
                       "groups": [{"code": g["code"], "name": g["name"], "full": g["full"], "loose": _num(g["loose"]),
                                   "pieces": _num(g["pieces"]), "lines": [ln.as_dict() for ln in g["lines"]]}
                                  for g in s["groups"]]})
    return {"sheets": sheets, "warnings": b.warnings}


@transaction.atomic
def generate(user: User, iw_pk: int) -> ChecklistSnapshot:
    require(user, "PHYSICAL_SED_QTY")
    iw = Iw.objects.select_for_update().select_related("load_type").get(pk=iw_pk)
    _require_open(iw)
    if iw.status == Iw.Status.PHYSICALLY_VERIFIED:
        raise IwError("Physical verification is already recorded; the checklist can no longer be regenerated.")
    b = build(iw)
    if b.blockers:
        raise IwError(" ".join(b.blockers))
    from inward.iw.coload import members
    group = members(iw)
    for other in group:          # co-loaded truck: the same checklist on every IW of the truck still open for it
        if other.pk == iw.pk or other.status not in (Iw.Status.OPEN, Iw.Status.MATERIAL_LINKED,
                                                     Iw.Status.UNLOADING_IN_PROGRESS):
            continue
        _snapshot_for(user, Iw.objects.select_for_update().get(pk=other.pk), b, group)
    return _snapshot_for(user, iw, b, group)


def _snapshot_for(user: User, iw: Iw, b: Build, group: list) -> ChecklistSnapshot:
    content = _serialise(b)
    if len(group) > 1:
        content["own"] = _serialise(build(iw, own_only=True))     # this IW's packages, for its own MRA
        content["truck"] = [str(m) for m in group]
    rev = (iw.checklists.aggregate(m=Max("revision"))["m"] or 0) + 1
    snap = ChecklistSnapshot.objects.create(iw=iw, revision=rev, content=content, content_hash=b.source_hash,
                                            generated_by=user)
    if iw.status in (Iw.Status.OPEN, Iw.Status.MATERIAL_LINKED):
        iw.status = Iw.Status.UNLOADING_IN_PROGRESS
        iw.version += 1
        iw.save(update_fields=["status", "version", "updated_at"])
    audit.record("CHECKLIST_GENERATED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"revision": rev, "sheets": [s["title"] for s in snap.content["sheets"]],
                        "packaging_missing": [ln.sku for ln in b.missing_packaging]})
    return snap


def is_stale(iw: Iw, snap: ChecklistSnapshot | None) -> bool:
    return bool(snap) and build(iw).source_hash != snap.content_hash
