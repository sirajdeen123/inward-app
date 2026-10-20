"""SIT import (manual upload or 'Fetch from Gmail' button) and change detection (SoT 3)."""
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.db import transaction

from inward.core import audit, documents
from inward.core.models import ExceptionEvent, ImportBatch, Organisation
from inward.domain.sit_changes import CRITICAL, SitEvent, ShipState, detect_changes
from inward.importers.common import file_sha256
from inward.importers.sit import parse_sit
from inward.sit.models import SitChangeEvent, SitImport, SitIssue, SitLine, SitShip


class SitImportRejected(Exception):
    """The file is not imported; the message says why (already imported, older report, unreadable)."""


class AlreadyImported(SitImportRejected):
    def __init__(self, batch: ImportBatch):
        super().__init__(f"Already imported on {batch.uploaded_at:%d-%b-%Y %I:%M %p} ({batch.filename}).")
        self.batch = batch


def _linked_ships() -> tuple[set[str], set[str]]:
    """(Ships linked to an IW, Ships with a Stock Receipt No entered). Filled in once IW linking exists."""
    try:
        from inward.iw.services import ship_link_state
    except ImportError:
        return set(), set()
    return ship_link_state()


def _ship_states(sit_import: SitImport | None) -> dict[str, ShipState]:
    if sit_import is None:
        return {}
    dates = dict(SitShip.objects.values_list("ship_id", "shipment_date"))
    grouped: dict[str, dict] = {}
    for ship_id, sku, qty in sit_import.lines.values_list("ship_id", "sku", "quantity"):
        grouped.setdefault(ship_id, {})[sku] = qty
    return {s: ShipState(s, dates.get(s), q) for s, q in grouped.items()}


_REMOVED_STATUS = {
    SitEvent.SHIP_REMOVED_PROCESSED: SitShip.Status.PROCESSED,
    SitEvent.SHIP_REMOVED_NO_RECEIPT: SitShip.Status.REMOVED_NO_RECEIPT,
    SitEvent.SHIP_REMOVED_NEVER_LINKED: SitShip.Status.REMOVED_NEVER_LINKED,
    SitEvent.SHIP_OUT_OF_WINDOW: SitShip.Status.OUT_OF_WINDOW,
}
_PARTIAL_EVENTS = {SitEvent.SKU_REDUCED, SitEvent.SKU_REMOVED}


def import_sit_file(path: Path, *, source: str, user=None, message_id: str = "",
                    received_at: datetime | None = None, original_name: str | None = None) -> SitImport:
    path = Path(path)
    name = original_name or path.name
    sha = file_sha256(path)
    existing = ImportBatch.objects.filter(kind="SIT", sha256=sha).first()
    if existing is None and message_id:
        existing = ImportBatch.objects.filter(kind="SIT", message_id=message_id).first()
    if existing:
        raise AlreadyImported(existing)

    parsed = parse_sit(path)  # ImportFailed propagates with a clear message
    depot = settings.INWARD["DEPOT_CODE"]
    others = sorted({r.to_org_code for r in parsed.rows if r.to_org_code and r.to_org_code != depot})
    if others:  # live 20-Oct-2026: a Madurai (037) SIT was imported by mistake and every 035 Ship looked removed
        raise SitImportRejected(
            f"This SIT report is not for Depot {depot}: it has Ships to depot {', '.join(others)}. "
            f"Only the SIT of Depot {depot} is imported here - nothing was changed.")
    if settings.INWARD["MODE"] == "TEST":  # UAT only: every report is merged, in any date order (17-Oct-2026)
        return _import_uat_merge(path, parsed, sha, name, source, user, message_id, received_at)
    latest = SitImport.objects.filter(back_dated=False).select_related("batch").first()
    if latest and parsed.system_date and latest.system_date and parsed.system_date < latest.system_date:
        raise SitImportRejected(
            f"This report (system date {parsed.system_date:%d-%b-%Y}) is older than the current one "
            f"({latest.system_date:%d-%b-%Y}). Older data would look like stock coming back, so it is not imported.")

    with transaction.atomic():
        doc = documents.store(path.read_bytes(), filename=name, content_type="application/vnd.ms-excel",
                              doc_type="SIT_REPORT", user=user)
        batch = ImportBatch.objects.create(
            kind="SIT", source=source, filename=name, sha256=sha, message_id=message_id, document=doc,
            uploaded_by=user if getattr(user, "is_authenticated", False) else None,
            window_from=parsed.window_from, window_to=parsed.window_to, row_count=len(parsed.rows) + len(parsed.issues),
            accepted_count=len(parsed.rows), issue_count=len(parsed.issues))
        imp = SitImport.objects.create(batch=batch, report_from=parsed.window_from, report_to=parsed.window_to,
                                       system_date=parsed.system_date, received_at=received_at)
        SitLine.objects.bulk_create([
            SitLine(sit_import=imp, ship_id=ln.ship_id, sku=ln.sku, floor_sku=ln.floor_sku, description=ln.description,
                    business_line=ln.business_line, uom=ln.uom, measure_family=ln.family.value if ln.family else "",
                    is_empty_packing=ln.is_empty_packing, pack_size=ln.pack_size, quantity=ln.quantity,
                    measure=ln.measure)
            for ln in parsed.lines.values()])
        SitIssue.objects.bulk_create([SitIssue(sit_import=imp, row_no=i.row_no, ship_id=i.ref or "",
                                               message=i.message[:300]) for i in parsed.issues])

        # ---- change detection against the previous accepted import
        first_rows = {}
        for r in parsed.rows:
            first_rows.setdefault(r.ship_id, r)
        current = {s: ShipState(s, first_rows[s].shipment_date, {}) for s in first_rows}
        for (ship_id, sku), ln in parsed.lines.items():
            current[ship_id].quantities[sku] = ln.quantity
        linked, with_receipt = _linked_ships()
        excluded = set(SitShip.objects.filter(pre_go_live=True).values_list("ship_id", flat=True))
        events = detect_changes(_ship_states(latest), current, (parsed.window_from, parsed.window_to),
                                linked, with_receipt, excluded)
        SitChangeEvent.objects.bulk_create([
            SitChangeEvent(sit_import=imp, ship_id=e.ship_id, event=e.event.value, sku=e.sku or "", before=e.before,
                           after=e.after, critical=e.event in CRITICAL) for e in events])
        for e in events:
            if e.event in CRITICAL:
                ExceptionEvent.objects.create(code=f"SIT_{e.event.value}", severity="CRITICAL", entity_type="ship",
                                              entity_id=e.ship_id, message=_describe(e))

        # ---- organisations seen in SIT become selectable sources (never overwrites edited masters)
        for row in first_rows.values():
            _ensure_org(row.from_org_code, row.from_org_name)

        # ---- Ship monitoring state
        partial = {e.ship_id for e in events if e.event in _PARTIAL_EVENTS}
        blocked = parsed.blocked_ships
        known = {s.ship_id: s for s in SitShip.objects.filter(ship_id__in=list(current))}
        for ship_id, state in current.items():
            row = first_rows[ship_id]
            ship = known.get(ship_id)
            if ship is None:
                SitShip.objects.create(ship_id=ship_id, from_org_code=row.from_org_code, from_org_name=row.from_org_name,
                                       to_org_code=row.to_org_code, shipment_date=row.shipment_date,
                                       first_seen=imp, last_seen=imp, has_issues=ship_id in blocked)
            else:
                ship.last_seen = imp
                ship.has_issues = ship_id in blocked
                if ship_id in partial or ship.status == SitShip.Status.PARTIAL:
                    ship.status = SitShip.Status.PARTIAL
                else:
                    ship.status = SitShip.Status.IN_SIT
                ship.save(update_fields=["last_seen", "has_issues", "status"])
        for e in events:
            if e.event in _REMOVED_STATUS:
                SitShip.objects.filter(ship_id=e.ship_id).update(status=_REMOVED_STATUS[e.event])

        imp.ship_count = len(current)
        imp.line_count = len(parsed.lines)
        imp.event_count = len(events)
        imp.critical_count = sum(e.event in CRITICAL for e in events)
        imp.save(update_fields=["ship_count", "line_count", "event_count", "critical_count"])
        audit.record("SIT_IMPORTED", "sit_import", imp.pk, user=user,
                     after={"file": name, "source": source, "system_date": parsed.system_date, "ships": len(current),
                            "events": len(events), "critical": imp.critical_count, "issues": len(parsed.issues)})
    return imp


def _import_uat_merge(path: Path, parsed, sha: str, name: str, source: str, user, message_id: str,
                      received_at) -> SitImport:
    """UAT only (INWARD_MODE=TEST). Any SIT report, older or newer, any number of times: a Ship ID already in the
    engine is a duplicate and is skipped - the Ship already there (linked to an IW or not) stays exactly as it
    is; Ship IDs not yet known are added (In SIT, linkable). Ships missing from the report are never removed.
    No date check, no change detection, no exceptions. Refused outright outside TEST mode; the live system
    keeps the normal date order and change detection (SoT 3.3)."""
    if settings.INWARD["MODE"] != "TEST":
        raise SitImportRejected("The merge import is available only in the UAT (TEST) environment.")
    latest = SitImport.objects.first()
    with transaction.atomic():
        doc = documents.store(path.read_bytes(), filename=name, content_type="application/vnd.ms-excel",
                              doc_type="SIT_REPORT", user=user)
        known = set(SitShip.objects.filter(ship_id__in=parsed.ship_ids).values_list("ship_id", flat=True))
        new = sorted(parsed.ship_ids - known)
        batch = ImportBatch.objects.create(
            kind="SIT", source=source, filename=name, sha256=sha, message_id=message_id, document=doc,
            uploaded_by=user if getattr(user, "is_authenticated", False) else None,
            window_from=parsed.window_from, window_to=parsed.window_to, row_count=len(parsed.rows) + len(parsed.issues),
            accepted_count=len(parsed.rows), issue_count=len(parsed.issues), status="BACK_DATED",
            summary={"uat_merge": True, "previous_import": latest.pk if latest else None, "new_ships": new[:500],
                     "known_ships": len(known)})
        imp = SitImport.objects.create(batch=batch, report_from=parsed.window_from, report_to=parsed.window_to,
                                       system_date=parsed.system_date, received_at=received_at, back_dated=True,
                                       ship_count=len(new), known_ships=len(known))
        new_set = set(new)
        lines = [ln for (ship_id, _), ln in parsed.lines.items() if ship_id in new_set]
        SitLine.objects.bulk_create([
            SitLine(sit_import=imp, ship_id=ln.ship_id, sku=ln.sku, floor_sku=ln.floor_sku, description=ln.description,
                    business_line=ln.business_line, uom=ln.uom, measure_family=ln.family.value if ln.family else "",
                    is_empty_packing=ln.is_empty_packing, pack_size=ln.pack_size, quantity=ln.quantity,
                    measure=ln.measure) for ln in lines])
        SitIssue.objects.bulk_create([SitIssue(sit_import=imp, row_no=i.row_no, ship_id=i.ref or "",
                                               message=i.message[:300]) for i in parsed.issues if (i.ref or "") in new_set])
        first_rows = {}
        for r in parsed.rows:
            if r.ship_id in new_set:
                first_rows.setdefault(r.ship_id, r)
        blocked = parsed.blocked_ships
        for ship_id in new:
            row = first_rows[ship_id]
            _ensure_org(row.from_org_code, row.from_org_name)
            SitShip.objects.create(ship_id=ship_id, from_org_code=row.from_org_code, from_org_name=row.from_org_name,
                                   to_org_code=row.to_org_code, shipment_date=row.shipment_date,
                                   first_seen=imp, last_seen=imp, has_issues=ship_id in blocked)
        imp.line_count = len(lines)
        imp.save(update_fields=["line_count"])
        audit.record("SIT_IMPORTED_UAT_MERGE", "sit_import", imp.pk, user=user,
                     after={"file": name, "system_date": parsed.system_date, "new_ships": new,
                            "duplicate_ships_skipped": len(known), "mode": "TEST"})
    return imp


def describe(imp: SitImport) -> str:
    """The clerk's confirmation line after an import."""
    if imp.back_dated:
        when = f"system date {imp.system_date:%d-%b-%Y}" if imp.system_date else "no system date"
        return (f"UAT merge ({when}): {imp.ship_count} new Ship(s) added with {imp.line_count} SKU lines; "
                f"{imp.known_ships} duplicate Ship(s) already in the engine skipped and left unchanged.")
    return (f"{imp.ship_count} Ships, {imp.line_count} SKU lines, {imp.event_count} changes "
            f"({imp.critical_count} critical).")


def _org_type(name: str) -> str:
    upper = name.upper()
    if "MFG" in upper or "FACTORY" in upper:
        return Organisation.OrgType.FACTORY
    if re.search(r"(^|[^A-Z])RDC([^A-Z]|$)", upper):   # "RDC - WB", "Bangalore-RDC", "Hubli RDC"
        return Organisation.OrgType.RDC
    return Organisation.OrgType.DEPOT


def _ensure_org(code: str, name: str) -> None:
    if code and not Organisation.objects.filter(code=code).exists():
        Organisation.objects.create(code=code, name=name or code, org_type=_org_type(name or ""))


def _describe(e) -> str:
    labels = {
        SitEvent.SHIP_REMOVED_NEVER_LINKED: "Ship removed from SIT but never linked to an IW (ERP processed without vehicle IW)",
        SitEvent.QTY_INCREASED: f"Quantity increased for SKU {e.sku}: {e.before} -> {e.after} (a Ship never grows)",
        SitEvent.SKU_ADDED: f"SKU {e.sku} added to an existing Ship (a Ship never grows)",
    }
    return labels.get(e.event, e.event.value)


@dataclass
class FetchResult:
    found: bool
    subject: str = ""
    received_at: datetime | None = None
    imported: list[SitImport] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


def fetch_from_gmail(user) -> FetchResult:
    """'Fetch from Gmail' button: import the newest SIT mail in the inbox, if not imported already."""
    from inward.mail.gmail import from_settings

    with tempfile.TemporaryDirectory() as tmp:
        mail = from_settings().fetch_latest(settings.INWARD["SIT_SUBJECT"], Path(tmp))
        if mail is None:
            return FetchResult(found=False, messages=[f"No e-mail with '{settings.INWARD['SIT_SUBJECT']}' in the inbox."])
        result = FetchResult(found=True, subject=mail.subject, received_at=mail.received_at)
        if not mail.attachments:
            result.messages.append("The e-mail has no .xls / .xlsx attachment.")
        for path in mail.attachments:
            original = path.name.split("_", 2)[-1]  # strip the received-time prefix added on download
            try:
                result.imported.append(import_sit_file(path, source="GMAIL", user=user, message_id=mail.message_id,
                                                       received_at=mail.received_at, original_name=original))
            except SitImportRejected as exc:
                result.messages.append(str(exc))
    return result


@transaction.atomic
def mark_pre_go_live(user, ship_ids: list[str]) -> int:
    """Ships of vehicles received before go-live: excluded from SIT exceptions (SoT 21.1)."""
    ships = list(SitShip.objects.select_for_update().filter(ship_id__in=ship_ids, pre_go_live=False))
    for ship in ships:
        ship.pre_go_live = True
        ship.pre_go_live_by = user
        ship.save(update_fields=["pre_go_live", "pre_go_live_by"])
    if ships:
        audit.record("SIT_PRE_GO_LIVE_MARKED", "sit_ship", ",".join(s.ship_id for s in ships)[:64], user=user,
                     after={"ships": [s.ship_id for s in ships]})
    return len(ships)
