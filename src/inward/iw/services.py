"""IW creation, edit and VOID (SoT 4, 14.1, 18.2). All rules live here, not in the views."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from inward.accounts.models import ApprovalRequest, User
from inward.accounts.services import PermissionDenied, request_approval, require
from inward.core import audit
from inward.core.models import LoadType, Organisation, Transporter
from inward.core.numbering import next_iw_number
from inward.core.versioning import StaleEdit
from inward.domain.fy import fy_code
from inward.domain.normalize import clean_text, search_key
from inward.iw.models import Iw

MOBILE = re.compile(r"^(?:\+?91)?([6-9]\d{9})$")
FUTURE_TOLERANCE = timedelta(minutes=10)


class IwError(Exception):
    """A business rule stops the action; the message is shown to the user."""


@dataclass
class IwData:
    arrival_at: datetime
    load_type: LoadType
    source_org: Organisation | None
    vehicle_no: str
    driver_name: str = ""
    driver_mobile: str = ""
    transporter_name: str = ""
    lr_no: str = ""
    remarks: str = ""


def ship_link_state() -> tuple[set[str], set[str]]:
    """(Ships linked to a live IW, Ships with a Stock Receipt No). Filled by Screen 03 / 05."""
    return set(), set()


def normalise_mobile(value: str) -> str:
    digits = re.sub(r"[\s\-()]", "", value or "")
    if not digits:
        return ""
    m = MOBILE.match(digits)
    if not m:
        raise IwError("Driver mobile must be a 10-digit Indian mobile number (optionally with +91).")
    return m.group(1)


def transporter_for(name: str) -> Transporter | None:
    """Find a transporter by normalised name; a new name is captured as unverified (SoT 4)."""
    name = clean_text(name)
    if not name:
        return None
    key = search_key(name)
    found = Transporter.objects.filter(search_key=key).first()
    if found:
        return found
    t = Transporter.objects.create(name=name.upper(), search_key=key, verified=False)
    audit.record("TRANSPORTER_CAPTURED", "transporter", t.pk, after={"name": t.name, "verified": False})
    return t


def _validate(data: IwData) -> None:
    if data.arrival_at > timezone.now() + FUTURE_TOLERANCE:
        raise IwError("Arrival date-time cannot be in the future.")
    if not search_key(data.vehicle_no):
        raise IwError("Vehicle number is required.")
    needs_org = data.load_type.has_inbound_sit and not data.load_type.is_vendor
    if needs_org and data.source_org is None:
        raise IwError("Source of load (organisation) is required for this load type.")


def _apply(iw: Iw, data: IwData) -> None:
    iw.arrival_at = data.arrival_at
    iw.load_type = data.load_type
    iw.source_org = data.source_org
    iw.vehicle_no = clean_text(data.vehicle_no).upper()
    iw.vehicle_key = search_key(data.vehicle_no)
    iw.driver_name = clean_text(data.driver_name)
    iw.driver_mobile = normalise_mobile(data.driver_mobile)
    iw.transporter = transporter_for(data.transporter_name)
    iw.lr_no = clean_text(data.lr_no).upper()
    iw.lr_key = search_key(data.lr_no)
    iw.remarks = data.remarks.strip()


def _snapshot(iw: Iw) -> dict:
    return {"iw_no": iw.iw_no, "status": iw.status, "arrival_at": iw.arrival_at, "load_type": iw.load_type_id,
            "source_org": iw.source_org_id, "vehicle_no": iw.vehicle_no, "driver_name": iw.driver_name,
            "driver_mobile": iw.driver_mobile, "transporter": iw.transporter_id, "lr_no": iw.lr_no}


def open_iws_for_vehicle(vehicle_no: str, exclude_pk: int | None = None):
    qs = Iw.objects.filter(vehicle_key=search_key(vehicle_no), status__in=Iw.EDITABLE).exclude(status=Iw.Status.DRAFT)
    return qs.exclude(pk=exclude_pk) if exclude_pk else qs


@transaction.atomic
def save_draft(user: User, data: IwData, iw: Iw | None = None) -> Iw:
    require(user, "IW_EDIT")
    iw = iw or Iw(created_by=user, status=Iw.Status.DRAFT)
    if iw.status != Iw.Status.DRAFT:
        raise IwError("Only a draft can be saved as draft.")
    _apply(iw, data)
    iw.save()
    audit.record("IW_DRAFT_SAVED", "iw", iw.pk, user=user, iw_id=iw.pk, after=_snapshot(iw))
    return iw


@transaction.atomic
def create(user: User, data: IwData, draft: Iw | None = None) -> Iw:
    """Issue the IW number (FY of the arrival date) and open the IW, in one transaction."""
    require(user, "IW_EDIT")
    _validate(data)
    iw = draft or Iw(created_by=user)
    if iw.pk and iw.status != Iw.Status.DRAFT:
        raise IwError("This IW is already created.")
    _apply(iw, data)
    iw.iw_no = next_iw_number(data.arrival_at)
    iw.fy_code = fy_code(data.arrival_at)
    iw.status = Iw.Status.OPEN
    iw.save()
    audit.record("IW_CREATED", "iw", iw.pk, user=user, iw_id=iw.pk, after=_snapshot(iw))
    return iw


@transaction.atomic
def update(user: User, iw_pk: int, version: int, data: IwData) -> Iw:
    """Edit before MRA. Rejects stale edits and arrival changes that would move the IW to another FY."""
    require(user, "IW_EDIT")
    iw = Iw.objects.select_for_update().get(pk=iw_pk)
    if iw.version != version:
        raise StaleEdit("Someone else changed this IW after you opened it. Reload and try again.")
    if not iw.is_editable:
        raise IwError("The IW can no longer be edited (MRA issued, closed or void).")
    _validate(data)
    if iw.iw_no and fy_code(data.arrival_at) != iw.fy_code:
        raise IwError(f"The new arrival date belongs to another financial year than {iw.iw_no}. "
                      "VOID this IW and create a new one instead.")
    before = _snapshot(iw)
    _apply(iw, data)
    iw.version += 1
    iw.save()
    audit.record("IW_UPDATED", "iw", iw.pk, user=user, iw_id=iw.pk, before=before, after=_snapshot(iw))
    return iw


def request_void(user: User, iw: Iw, reason: str) -> ApprovalRequest:
    if not iw.is_editable or iw.status == Iw.Status.DRAFT:
        raise IwError("Only an open IW before MRA can be voided (a draft can simply be discarded).")
    if ApprovalRequest.objects.filter(permission_id="IW_VOID", entity_type="iw", entity_id=str(iw.pk),
                                      status=ApprovalRequest.Status.PENDING).exists():
        raise IwError("A VOID request for this IW is already waiting for approval.")
    return request_approval(user, "IW_VOID", "iw", iw.pk, reason, payload={"iw_no": iw.iw_no})


def apply_void(req: ApprovalRequest, approver: User) -> None:
    """Effect of an approved IW_VOID request: number kept, never reused (SoT 4)."""
    iw = Iw.objects.select_for_update().get(pk=int(req.entity_id))
    if not iw.is_editable:
        raise IwError(f"{iw.iw_no} can no longer be voided (status {iw.get_status_display()}).")
    before = _snapshot(iw)
    iw.status = Iw.Status.VOID
    iw.void_reason = req.reason
    iw.voided_at = timezone.now()
    iw.version += 1
    iw.save()
    audit.record("IW_VOIDED", "iw", iw.pk, user=approver, iw_id=iw.pk, before=before, after=_snapshot(iw),
                 requested_by=req.requested_by_id, approval_request=req.pk)


@transaction.atomic
def discard_draft(user: User, iw: Iw) -> None:
    require(user, "IW_EDIT")
    if iw.status != Iw.Status.DRAFT:
        raise PermissionDenied("Only drafts can be discarded.")
    audit.record("IW_DRAFT_DISCARDED", "iw", iw.pk, user=user, before=_snapshot(iw))
    iw.delete()
