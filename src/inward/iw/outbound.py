"""Outbound transhipment (SoT 5.3): every Despatch row is dispositioned and every outbound Ship balances.

The Berger Despatch Detail Report has no Ship ID column and covers a date range for all destinations,
so rows are assigned to outbound Ships by the clerk and checked against each Ship's document totals.
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
from inward.core.models import FeatureFlag, Organisation
from inward.domain.normalize import clean_text, floor_sku, parse_org
from inward.importers.despatch import parse_despatch
from inward.iw.linking import LIVE, _refresh_status, _require_open
from inward.iw.models import DespatchReport, DespatchRow, Iw, OutboundShip, TrackStatus
from inward.iw.services import IwError

EXCLUDE = "X"


def _dec(value, name: str) -> Decimal:
    try:
        d = Decimal(str(value).strip() or "0")
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
        if not name:
            raise IwError(f"Organisation {code} is not known yet - enter it as 'code : name'.")
        org = Organisation.objects.create(code=code, name=name, org_type=Organisation.OrgType.DEPOT)
    return org


def _require_outbound(iw: Iw) -> None:
    _require_open(iw)
    if not iw.load_type.has_outbound:
        raise IwError(f"Load type '{iw.load_type}' carries no outbound transhipment.")


@transaction.atomic
def add_ship(user: User, iw_pk: int, *, ship_id: str, destination: str, doc_nop, doc_ltr="0", doc_kg="0"
             ) -> OutboundShip:
    require(user, "MATERIAL_LINK")
    iw = Iw.objects.select_for_update().select_related("load_type").get(pk=iw_pk)
    _require_outbound(iw)
    sid = clean_text(ship_id).replace(" ", "")
    if not sid.isdigit():
        raise IwError("Outbound Ship ID must be a number from the Ship document.")
    if iw.outbound_ships.filter(ship_id=sid).exists():
        raise IwError(f"Outbound Ship {sid} is already on this IW.")
    nop = _dec(doc_nop, "Document packages")
    if nop <= 0:
        raise IwError("Enter the number of packages (NOP) from the outbound Ship document.")
    ship = OutboundShip.objects.create(iw=iw, ship_id=sid, destination=_org(destination), doc_nop=nop,
                                       doc_ltr=_dec(doc_ltr, "Document LTR"), doc_kg=_dec(doc_kg, "Document KG"),
                                       linked_by=user)
    _refresh_status(iw)
    audit.record("OUTBOUND_SHIP_ADDED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"ship": sid, "destination": ship.destination_id, "nop": nop, "ltr": ship.doc_ltr,
                        "kg": ship.doc_kg})
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

# ---------------------------------------------------------------- assignment and balance


def visible_rows(iw: Iw):
    """Rows whose TO ORG is a destination of an outbound Ship on this IW (others are hidden but counted)."""
    dests = list(iw.outbound_ships.values_list("destination_id", flat=True))
    return DespatchRow.objects.filter(report__iw=iw, to_org_code__in=dests)


def _reuse_window_days() -> int:
    flag = FeatureFlag.objects.filter(key="outbound_row_reuse_days").first()
    return int((flag.value or {}).get("value") or 60) if flag else 60


def reused_rows(iw: Iw) -> dict[int, str]:
    """row id -> other IW number, where an identical row is already assigned in another live IW recently."""
    since = timezone.now() - timedelta(days=_reuse_window_days())
    mine = {r.fingerprint: r.pk for r in visible_rows(iw)}
    hits = (DespatchRow.objects.filter(fingerprint__in=list(mine), outbound_ship__isnull=False,
                                       report__iw__status__in=LIVE, report__uploaded_at__gte=since)
            .exclude(report__iw=iw).select_related("report__iw"))
    result = {}
    for h in hits:
        result[mine[h.fingerprint]] = str(h.report.iw)
    return result


@transaction.atomic
def assign(user: User, iw_pk: int, choices: dict[int, str], acknowledged: set[int]) -> None:
    """choices: row id -> outbound Ship pk (as text), 'X' (not on this vehicle) or '' (undecided)."""
    require(user, "MATERIAL_LINK")
    iw = Iw.objects.select_for_update().select_related("load_type").get(pk=iw_pk)
    _require_outbound(iw)
    ships = {str(s.pk): s for s in iw.outbound_ships.all()}
    reuse = reused_rows(iw)
    rows = {r.pk: r for r in visible_rows(iw)}
    errors = []
    for row_id, choice in choices.items():
        row = rows.get(row_id)
        if row is None:
            continue
        if choice == EXCLUDE:
            row.outbound_ship, row.excluded = None, True
        elif choice in ships:
            ship = ships[choice]
            if ship.destination_id != row.to_org_code:
                errors.append(f"Row {row.row_no} ({row.floor_sku}) goes to {row.to_org_code}, but Ship {ship.ship_id} "
                              f"goes to {ship.destination_id}.")
                continue
            if row_id in reuse and row_id not in acknowledged and not row.reuse_acknowledged:
                errors.append(f"Row {row.row_no} ({row.floor_sku}) is identical to a row already assigned on "
                              f"{reuse[row_id]} - tick 'I checked' to confirm it is a different despatch.")
                continue
            row.outbound_ship, row.excluded = ship, False
            row.reuse_acknowledged = row.reuse_acknowledged or row_id in acknowledged
        else:
            row.outbound_ship, row.excluded = None, False
        row.save(update_fields=["outbound_ship", "excluded", "reuse_acknowledged"])
    if errors:
        transaction.set_rollback(True)
        raise IwError(" ".join(errors))
    _update_track_status(iw)
    audit.record("DESPATCH_ROWS_ASSIGNED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"assigned": sum(1 for c in choices.values() if c and c != EXCLUDE),
                        "excluded": sum(1 for c in choices.values() if c == EXCLUDE),
                        "acknowledged_reuse": sorted(acknowledged)})


@transaction.atomic
def assign_all_for_destination(user: User, iw_pk: int, destination: str) -> int:
    """Shortcut when a destination has exactly one outbound Ship on this IW."""
    iw = Iw.objects.get(pk=iw_pk)
    ships = list(iw.outbound_ships.filter(destination_id=destination))
    if len(ships) != 1:
        raise IwError("'Assign all' is only possible when the destination has exactly one outbound Ship.")
    rows = visible_rows(iw).filter(to_org_code=destination, outbound_ship__isnull=True, excluded=False)
    choices = {r.pk: str(ships[0].pk) for r in rows}
    assign(user, iw_pk, choices, acknowledged=set())
    return len(choices)


@dataclass
class ShipBalance:
    ship: OutboundShip
    nop: Decimal = Decimal(0)
    ltr: Decimal = Decimal(0)
    kg: Decimal = Decimal(0)

    @property
    def balanced(self) -> bool:
        return self.nop == self.ship.doc_nop and self.ltr == self.ship.doc_ltr and self.kg == self.ship.doc_kg


@dataclass
class OutboundState:
    balances: list[ShipBalance] = field(default_factory=list)
    undecided: int = 0
    hidden: int = 0
    reports: int = 0

    @property
    def ready(self) -> bool:
        return bool(self.balances) and self.undecided == 0 and all(b.balanced for b in self.balances)


def state(iw: Iw) -> OutboundState:
    st = OutboundState(reports=iw.despatch_reports.count())
    vis = visible_rows(iw)
    st.hidden = DespatchRow.objects.filter(report__iw=iw).count() - vis.count()
    st.undecided = vis.filter(outbound_ship__isnull=True, excluded=False).count()
    for ship in iw.outbound_ships.select_related("destination"):
        b = ShipBalance(ship)
        for r in ship.rows.all():
            b.nop += r.nop
            b.ltr += r.ltr
            b.kg += r.kg
        st.balances.append(b)
    return st


def _update_track_status(iw: Iw) -> None:
    st = state(iw)
    for b in st.balances:
        wanted = TrackStatus.ROWS_ASSIGNED if (b.balanced and st.undecided == 0) else TrackStatus.LINKED
        if b.ship.status != wanted:
            b.ship.status = wanted
            b.ship.save(update_fields=["status"])
