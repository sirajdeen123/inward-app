"""Outbound transhipment - tick and tally (SoT 5.3, revised 15-Oct-2026).

1. The clerk uploads the Berger Despatch Detail Report (no Ship ID column, date range, all destinations).
2. For each outbound Ship the clerk enters Ship ID, destination and the LTR and KG on the Ship document
   (packages / NOP optional).
3. The destination's free rows are listed: if they total exactly the entered quantity they are all ticked;
   if more, the clerk ticks the Ship's rows; if less, the Ship cannot be completed.
4. Save is accepted only when the ticked rows tally exactly with the entered LTR and KG (and NOP if given).
Unticked rows are simply not on this vehicle. Rows ticked for one Ship are not offered to another.

Other row sources (decision 16-Oct-2026, until the ERP adds the Ship ID to the Despatch report):
- DEST_SIT: the destination depot shares its SIT report; the Ship's rows are taken by Ship ID.
- MANUAL: the clerk types the SKU rows from the Ship document.
Both must tally with the entered LTR and KG (and packages, if given) before they are saved.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from inward.accounts.models import User
from inward.accounts.services import require
from inward.core import audit, documents
from inward.core.models import FeatureFlag, Organisation, SkuPackaging
from inward.domain.normalize import clean_code, clean_text, floor_sku, is_valid_sku, parse_org, round_measure
from inward.domain.units import MeasureFamily
from inward.importers.despatch import parse_despatch
from inward.importers.sit import parse_sit
from inward.iw.linking import LIVE, _refresh_status, _require_open
from inward.iw.models import DespatchReport, DespatchRow, Iw, OutboundShip, OutboundSource, TrackStatus
from inward.iw.services import IwError

SUGGEST_STATE_LIMIT = 200_000


def _dec(value, name: str, required: bool = False) -> Decimal | None:
    text = str(value or "").strip()
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
    return d


def _org(text: str) -> Organisation:
    code, name = parse_org(text)
    if not code:
        raise IwError("Destination organisation is mandatory (e.g. '094 : Pondicherry').")
    org = Organisation.objects.filter(code=code).first()
    if org is None:
        row = DespatchRow.objects.filter(to_org_code=code).exclude(to_org_name="").first()
        name = name or (row.to_org_name if row else "")
        if not name:
            raise IwError(f"Organisation {code} is not known yet - enter it as 'code : name'.")
        org = Organisation.objects.create(code=code, name=name, org_type=Organisation.OrgType.DEPOT)
    return org


def _require_outbound(iw: Iw) -> None:
    _require_open(iw)
    if not iw.load_type.has_outbound:
        raise IwError(f"Load type '{iw.load_type}' carries no outbound transhipment.")

# ---------------------------------------------------------------- report and Ships


@transaction.atomic
def upload_report(user: User, iw_pk: int, path: Path, filename: str) -> DespatchReport:
    require(user, "MATERIAL_LINK")
    iw = Iw.objects.select_for_update().select_related("load_type").get(pk=iw_pk)
    _require_outbound(iw)
    parsed = parse_despatch(path)
    doc = documents.store(Path(path).read_bytes(), filename=filename, content_type="application/vnd.ms-excel",
                          doc_type="DESPATCH_REPORT", user=user, links=[("iw", iw.pk, "despatch_report")])
    report = DespatchReport.objects.create(iw=iw, document=doc, filename=filename, window_from=parsed.window_from,
                                           window_to=parsed.window_to, uploaded_by=user)
    DespatchRow.objects.bulk_create([
        DespatchRow(report=report, row_no=r.row_no, to_org_code=r.to_org_code, to_org_name=r.to_org_name, sku=r.sku,
                    floor_sku=floor_sku(r.sku), description=r.description, business_line=r.business_line,
                    uom=r.uom, nop=r.nop, pack_size=r.pack_size, ltr=r.ltr, kg=r.kg, fingerprint=r.fingerprint)
        for r in parsed.rows])
    audit.record("DESPATCH_REPORT_UPLOADED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"file": filename, "rows": len(parsed.rows), "window": [parsed.window_from, parsed.window_to]})
    return report


def report_destinations(iw: Iw) -> list[tuple[str, str, int]]:
    """(code, name, row count) of destinations in the IW's uploaded reports."""
    seen: dict[str, list] = {}
    for code, name in (DespatchRow.objects.filter(report__iw=iw, report__kind=OutboundSource.DESPATCH)
                       .values_list("to_org_code", "to_org_name")):
        entry = seen.setdefault(code, [name, 0])
        entry[1] += 1
    return [(c, n, k) for c, (n, k) in sorted(seen.items())]


@transaction.atomic
def add_ship(user: User, iw_pk: int, *, ship_id: str, destination: str, doc_ltr="", doc_kg="", doc_nop="",
             source: str = OutboundSource.DESPATCH) -> OutboundShip:
    require(user, "MATERIAL_LINK")
    iw = Iw.objects.select_for_update().select_related("load_type").get(pk=iw_pk)
    _require_outbound(iw)
    sid = clean_text(ship_id).replace(" ", "")
    if not sid.isdigit():
        raise IwError("Outbound Ship ID must be the number on the Ship document.")
    if iw.outbound_ships.filter(ship_id=sid).exists():
        raise IwError(f"Outbound Ship {sid} is already on this IW.")
    clash = OutboundShip.objects.filter(ship_id=sid, iw__status__in=LIVE).exclude(iw=iw).select_related("iw").first()
    if clash:
        raise IwError(f"Outbound Ship {sid} is already on {clash.iw}.")
    if source not in OutboundSource.values:
        raise IwError("Choose where the Ship's rows come from: Despatch report, destination SIT or manual entry.")
    if source == OutboundSource.DESPATCH and not iw.despatch_reports.filter(kind=OutboundSource.DESPATCH).exists():
        raise IwError("Upload the Despatch Detail Report first, or choose the destination SIT / manual entry.")
    ltr, kg = _dec(doc_ltr, "LTR") or Decimal(0), _dec(doc_kg, "KG") or Decimal(0)
    if ltr == 0 and kg == 0:
        raise IwError("Enter the LTR and / or KG shown on the outbound Ship document.")
    ship = OutboundShip.objects.create(iw=iw, ship_id=sid, destination=_org(destination), doc_ltr=ltr, doc_kg=kg,
                                       doc_nop=_dec(doc_nop, "Packages"), source=source,
                                       linked_by=user)
    _refresh_status(iw)
    audit.record("OUTBOUND_SHIP_ADDED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"ship": sid, "destination": ship.destination_id, "ltr": ltr, "kg": kg, "nop": ship.doc_nop,
                        "source": source})
    return ship


@transaction.atomic
def remove_ship(user: User, ship_pk: int) -> None:
    require(user, "MATERIAL_LINK")
    ship = OutboundShip.objects.get(pk=ship_pk)
    iw = Iw.objects.select_for_update().get(pk=ship.iw_id)
    _require_open(iw)
    ship.rows.update(outbound_ship=None)
    audit.record("OUTBOUND_SHIP_REMOVED", "iw", iw.pk, user=user, iw_id=iw.pk, before={"ship": ship.ship_id})
    ship.delete()
    _refresh_status(iw)

# ---------------------------------------------------------------- ticking and tally


def _reuse_window_days() -> int:
    flag = FeatureFlag.objects.filter(key="outbound_row_reuse_days").first()
    return int((flag.value or {}).get("value") or 60) if flag else 60


def available_rows(ship: OutboundShip) -> list[DespatchRow]:
    """Rows of the Ship's destination that are free or already ticked for this Ship; each row is marked with
    `.reused_on` when an identical row is already saved on another live IW recently."""
    rows = list(DespatchRow.objects.filter(report__iw_id=ship.iw_id, report__kind=OutboundSource.DESPATCH,
                                           to_org_code=ship.destination_id)
                .exclude(outbound_ship__isnull=False, outbound_ship__in=OutboundShip.objects.exclude(pk=ship.pk))
                .order_by("report_id", "row_no"))
    since = timezone.now() - timedelta(days=_reuse_window_days())
    hits = {}
    for h in (DespatchRow.objects.filter(fingerprint__in=[r.fingerprint for r in rows], outbound_ship__isnull=False,
                                         report__iw__status__in=LIVE, report__uploaded_at__gte=since)
              .exclude(report__iw_id=ship.iw_id).select_related("report__iw")):
        hits[h.fingerprint] = str(h.report.iw)
    for r in rows:
        r.reused_on = hits.get(r.fingerprint)
    return rows


@dataclass
class Totals:
    ltr: Decimal = Decimal(0)
    kg: Decimal = Decimal(0)
    nop: Decimal = Decimal(0)

    @classmethod
    def of(cls, rows) -> "Totals":
        t = cls()
        for r in rows:
            t.ltr += r.ltr
            t.kg += r.kg
            t.nop += r.nop
        return t


def matches(ship: OutboundShip, t: Totals) -> bool:
    ok = round_measure(t.ltr) == round_measure(ship.doc_ltr) and round_measure(t.kg) == round_measure(ship.doc_kg)
    return ok and (ship.doc_nop is None or t.nop == ship.doc_nop)


def covers(ship: OutboundShip, t: Totals) -> bool:
    """The rows on offer contain at least the entered quantity."""
    return t.ltr >= ship.doc_ltr and t.kg >= ship.doc_kg and (ship.doc_nop is None or t.nop >= ship.doc_nop)


@transaction.atomic
def save_selection(user: User, ship_pk: int, row_ids: set[int], acknowledged: set[int]) -> OutboundShip:
    require(user, "MATERIAL_LINK")
    ship = OutboundShip.objects.select_for_update().get(pk=ship_pk)
    iw = Iw.objects.select_for_update().select_related("load_type").get(pk=ship.iw_id)
    _require_outbound(iw)
    offer = {r.pk: r for r in available_rows(ship)}
    unknown = row_ids - set(offer)
    if unknown:
        raise IwError("Some ticked rows are no longer available (taken by another Ship). Reload the page.")
    chosen = [offer[i] for i in sorted(row_ids)]
    unconfirmed = [r for r in chosen if r.reused_on and r.pk not in acknowledged and not r.reuse_acknowledged]
    if unconfirmed:
        raise IwError("Rows " + ", ".join(f"{r.row_no} ({r.floor_sku}, already on {r.reused_on})" for r in unconfirmed)
                      + " need the 'I checked' tick - confirm they are a different despatch.")
    t = Totals.of(chosen)
    if not matches(ship, t):
        nop = f", packages {t.nop} vs {ship.doc_nop}" if ship.doc_nop is not None else ""
        raise IwError(f"Ticked rows do not tally with Ship {ship.ship_id}: LTR {t.ltr} vs {ship.doc_ltr}, "
                      f"KG {t.kg} vs {ship.doc_kg}{nop}.")
    DespatchRow.objects.filter(outbound_ship=ship).exclude(pk__in=row_ids).update(outbound_ship=None)
    for r in chosen:
        r.outbound_ship = ship
        r.reuse_acknowledged = r.reuse_acknowledged or r.pk in acknowledged
        r.save(update_fields=["outbound_ship", "reuse_acknowledged"])
    ship.status = TrackStatus.ROWS_ASSIGNED
    ship.tallied_at = timezone.now()
    ship.save(update_fields=["status", "tallied_at"])
    audit.record("OUTBOUND_SHIP_TALLIED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"ship": ship.ship_id, "rows": [r.row_no for r in chosen], "ltr": t.ltr, "kg": t.kg,
                        "nop": t.nop, "acknowledged_reuse": sorted(acknowledged & row_ids)})
    return ship


@transaction.atomic
def reopen(user: User, ship_pk: int) -> None:
    """Edit a saved Ship's rows again (before MRA); it must be saved again to count as tallied."""
    require(user, "MATERIAL_LINK")
    ship = OutboundShip.objects.select_for_update().get(pk=ship_pk)
    _require_open(Iw.objects.get(pk=ship.iw_id))
    if ship.source != OutboundSource.DESPATCH:
        raise IwError("Only Despatch-report Ships are re-ticked; load the destination SIT again or edit the manual rows.")
    ship.status = TrackStatus.LINKED
    ship.tallied_at = None
    ship.save(update_fields=["status", "tallied_at"])
    audit.record("OUTBOUND_SHIP_REOPENED", "iw", ship.iw_id, user=user, iw_id=ship.iw_id, after={"ship": ship.ship_id})


# ---------------------------------------------------------------- destination SIT and manual entry


def _lock_own(user: User, ship_pk: int) -> tuple[OutboundShip, Iw]:
    require(user, "MATERIAL_LINK")
    ship = OutboundShip.objects.select_for_update().select_related("destination").get(pk=ship_pk)
    iw = Iw.objects.select_for_update().select_related("load_type").get(pk=ship.iw_id)
    _require_outbound(iw)
    return ship, iw


def _tally_or_fail(ship: OutboundShip, rows: list[DespatchRow], what: str) -> Totals:
    t = Totals.of(rows)
    if not matches(ship, t):
        nop = f", packages {t.nop} vs {ship.doc_nop}" if ship.doc_nop is not None else ""
        raise IwError(f"{what} does not tally with Ship {ship.ship_id}: LTR {t.ltr} vs {ship.doc_ltr}, "
                      f"KG {t.kg} vs {ship.doc_kg}{nop}. Nothing was saved.")
    return t


def _save_own(user: User, ship: OutboundShip, iw: Iw, kind: str, rows: list[DespatchRow], t: Totals, *,
              document=None, filename: str = "", window=(None, None)) -> DespatchReport:
    ship.own_reports.all().delete()  # a re-load or re-entry replaces the earlier rows of this Ship
    report = DespatchReport.objects.create(iw=iw, kind=kind, ship=ship, document=document, filename=filename,
                                           window_from=window[0], window_to=window[1], uploaded_by=user)
    for i, r in enumerate(rows, 1):
        r.report, r.row_no, r.outbound_ship = report, i, ship
        r.fingerprint = f"{r.to_org_code}|{r.sku}|{r.nop}|{r.ltr}|{r.kg}"
    DespatchRow.objects.bulk_create(rows)
    ship.source = kind
    ship.status = TrackStatus.ROWS_ASSIGNED
    ship.tallied_at = timezone.now()
    ship.save(update_fields=["source", "status", "tallied_at"])
    audit.record("OUTBOUND_SHIP_TALLIED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"ship": ship.ship_id, "source": kind, "file": filename, "rows": len(rows), "ltr": t.ltr,
                        "kg": t.kg, "nop": t.nop})
    return report


@transaction.atomic
def load_destination_sit(user: User, ship_pk: int, path: Path, filename: str) -> DespatchReport:
    """Take the Ship's rows from the SIT report shared by the destination depot (rows picked by Ship ID)."""
    ship, iw = _lock_own(user, ship_pk)
    sit = parse_sit(path)
    if ship.ship_id not in sit.ship_ids:
        others = sorted(sit.ship_ids)
        raise IwError(f"Ship {ship.ship_id} is not in this SIT report. Ships in the file: "
                      f"{', '.join(others[:15]) or 'none'}{' ...' if len(others) > 15 else ''}.")
    wrong = {r.to_org_code for r in sit.rows if r.ship_id == ship.ship_id} - {ship.destination_id}
    if wrong:
        raise IwError(f"In this SIT report Ship {ship.ship_id} goes to {', '.join(sorted(wrong))}, "
                      f"not to {ship.destination}. Check the Ship ID and destination.")
    if ship.ship_id in sit.blocked_ships:
        raise IwError(f"Ship {ship.ship_id} has row errors in this SIT report: "
                      + "; ".join(i.message for i in sit.issues if i.ref == ship.ship_id))
    rows = [DespatchRow(
        to_org_code=ship.destination_id, to_org_name=ship.destination.name, sku=ln.sku, floor_sku=floor_sku(ln.sku),
        description=ln.description, business_line=ln.business_line, uom=ln.uom, nop=ln.quantity,
        pack_size=ln.pack_size, ltr=ln.measure if ln.family == MeasureFamily.VOLUME else 0,
        kg=ln.measure if ln.family == MeasureFamily.WEIGHT else 0)
        for (sid, _), ln in sit.lines.items() if sid == ship.ship_id]
    t = _tally_or_fail(ship, rows, "The destination SIT")
    doc = documents.store(Path(path).read_bytes(), filename=filename, content_type="application/vnd.ms-excel",
                          doc_type="DESTINATION_SIT_REPORT", user=user, links=[("iw", iw.pk, "destination_sit")])
    report = _save_own(user, ship, iw, OutboundSource.DEST_SIT, rows, t, document=doc, filename=filename,
                       window=(sit.window_from, sit.window_to))
    _refresh_status(iw)
    return report


UOM_FAMILY = {"L": "LTR", "ML": "LTR", "K": "KG", "KG": "KG", "G": "KG"}


@transaction.atomic
def save_manual(user: User, ship_pk: int, lines: list[tuple[str, str, str, str, str]]) -> DespatchReport:
    """lines: (sku, description, uom L/K, pieces, quantity in LTR or KG) typed from the Ship document."""
    ship, iw = _lock_own(user, ship_pk)
    entered = []
    for sku, desc, uom, nop, qty in lines:
        sku = clean_code(sku)
        if not sku and not clean_text(desc) and not str(nop or "").strip():
            continue
        if not is_valid_sku(sku):
            raise IwError(f"'{sku or desc}' - enter the 16-character SKU.")
        uom = clean_code(uom) or "L"
        if uom not in UOM_FAMILY:
            raise IwError(f"SKU {sku}: UOM '{uom}' must be L, ML, K or G.")
        n, q = _dec(nop, f"SKU {sku} pieces", True), _dec(qty, f"SKU {sku} quantity", True)
        if n <= 0:
            raise IwError(f"SKU {sku}: pieces must be greater than zero.")
        entered.append((sku, clean_text(desc), uom, n, q))
    if not entered:
        raise IwError("Enter at least one SKU row from the Ship document.")
    names = dict(SkuPackaging.objects.filter(sku__in=[e[0] for e in entered]).values_list("sku", "description"))
    rows = []
    for sku, desc, uom, n, q in entered:
        fam = UOM_FAMILY[uom]
        rows.append(DespatchRow(
            to_org_code=ship.destination_id, to_org_name=ship.destination.name, sku=sku, floor_sku=floor_sku(sku),
            description=(desc or names.get(sku, "")).upper()[:200], uom=uom, nop=n, pack_size=(q / n) if q else None,
            ltr=q if fam == "LTR" else 0, kg=q if fam == "KG" else 0))
    t = _tally_or_fail(ship, rows, "The entered rows")
    report = _save_own(user, ship, iw, OutboundSource.MANUAL, rows, t, filename="Manual entry")
    _refresh_status(iw)
    return report


def suggest(ship: OutboundShip, rows: list[DespatchRow]) -> list[int] | None:
    """Find rows whose LTR, KG (and NOP if entered) add up exactly to the Ship document - a suggestion only.
    Rows already used on another IW are left out. Returns None if no exact combination is found."""
    target = (int(round_measure(ship.doc_ltr) * 1000), int(round_measure(ship.doc_kg) * 1000),
              int(ship.doc_nop * 1000) if ship.doc_nop is not None else None)
    candidates = [r for r in rows if not r.reused_on]
    states: dict[tuple[int, int, int], tuple[int, ...]] = {(0, 0, 0): ()}
    for r in candidates:
        step = (int(round_measure(r.ltr) * 1000), int(round_measure(r.kg) * 1000), int(r.nop * 1000))
        for (l, k, n), picked in list(states.items()):
            new = (l + step[0], k + step[1], n + step[2])
            if new[0] > target[0] or new[1] > target[1] or (target[2] is not None and new[2] > target[2]):
                continue
            if new not in states:
                states[new] = picked + (r.pk,)
                if new[:2] == target[:2] and (target[2] is None or new[2] == target[2]):
                    return list(states[new])
        if len(states) > SUGGEST_STATE_LIMIT:
            return None
    return None

# ---------------------------------------------------------------- IW-level summary


@dataclass
class ShipSummary:
    ship: OutboundShip
    ticked: Totals

    @property
    def tallied(self) -> bool:
        return self.ship.status == TrackStatus.ROWS_ASSIGNED


@dataclass
class OutboundState:
    ships: list[ShipSummary] = field(default_factory=list)
    reports: int = 0

    @property
    def ready(self) -> bool:
        return bool(self.ships) and all(s.tallied for s in self.ships)


def state(iw: Iw) -> OutboundState:
    st = OutboundState(reports=iw.despatch_reports.count())
    for ship in iw.outbound_ships.select_related("destination").order_by("linked_at"):
        st.ships.append(ShipSummary(ship, Totals.of(ship.rows.all())))
    return st
