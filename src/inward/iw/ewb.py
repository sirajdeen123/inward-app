"""Outbound transhipment - E-way bill Transporter ID update (decision 6-Nov-2026).

The source generated the E-way bill(s); before the material can move on from the depot to the destination org, the
source must put the CFA's Transporter ID in them. So when an IW carries outbound transhipment Ships:

1. The clerk enters the E-way bill No (12 digits) of every outbound Ship.
2. One mail to the source (same addresses as the MRA), Cc the internal list, lists every Ship with its E-way bill
   No and the Transporter ID (Depot master -> operating CFA). A reminder may follow after 2 hours.
3. The MRA cannot be issued until that mail is queued for the current Ships - or the CFA Manager approved an MRA
   release without it (EWB_MRA_RELEASE, reason mandatory). A Ship added or changed later needs a new mail / release.
4. The IW cannot be closed until the clerk records that the source updated the E-way bill(s).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from inward.accounts.models import ApprovalRequest, User
from inward.accounts.services import require
from inward.core import audit
from inward.domain.normalize import clean_text
from inward.iw import comms
from inward.iw.models import EwbEvent, Iw, MailMessage, OutboundShip
from inward.iw.services import IwError

EWB_RE = re.compile(r"^\d{12}$")
REMIND_AFTER = timedelta(hours=2)
RELEASE_REASONS = ["Source has no e-mail on file", "Source already updated the E-way bill (confirmed by phone)",
                   "E-way bill not applicable (e.g. under the value limit)", "Other"]
CLOSED = (Iw.Status.CLOSED, Iw.Status.VOID)


def _go_live():
    from datetime import datetime

    from inward.core.models import FeatureFlag
    f = FeatureFlag.objects.filter(key="ewb_go_live").first()
    v = (f.value or {}).get("value") if f else None
    return datetime.fromisoformat(v) if v else None


def needed(iw: Iw) -> bool:
    """Outbound transhipment Ships on the IW - except an IW whose first MRA was issued before this rule went live."""
    if not (iw.load_type.has_outbound and iw.outbound_ships.exists()):
        return False
    first = iw.mras.order_by("issued_at").values_list("issued_at", flat=True).first()
    start = _go_live()
    return not (first and start and first < start)


def ship_set(iw: Iw) -> dict[str, str]:
    """Ship ID -> E-way bill No of the IW's outbound Ships now."""
    return {s.ship_id: s.ewb_no for s in iw.outbound_ships.order_by("linked_at")}


def transporter(iw: Iw) -> tuple[str, str]:
    from inward.iw.mra import _operator
    op = _operator(iw)
    return (op.transporter_id, op.transporter_name or op.cfa_name) if op else ("", "")


@dataclass
class State:
    needed: bool = False
    ships: list = field(default_factory=list)
    missing_ewb: list = field(default_factory=list)
    request: EwbEvent | None = None        # the last update request mailed
    last_mail: EwbEvent | None = None      # the last request or reminder
    covered: bool = False                  # the last request lists exactly the current Ships and E-way bill Nos
    release: EwbEvent | None = None        # an approved MRA release for the current Ships
    pending_release: ApprovalRequest | None = None
    confirmed: bool = False
    transporter_id: str = ""
    transporter_name: str = ""

    @property
    def mra_ok(self) -> bool:
        return not self.needed or self.covered or self.release is not None

    @property
    def can_remind(self) -> bool:
        return (self.covered and not self.confirmed and self.last_mail is not None
                and timezone.now() - self.last_mail.at >= REMIND_AFTER)

    @property
    def remind_at(self):
        return self.last_mail.at + REMIND_AFTER if self.last_mail else None


def state(iw: Iw) -> State:
    st = State(needed=needed(iw))
    if not st.needed:
        return st
    st.ships = list(iw.outbound_ships.select_related("destination").order_by("linked_at"))
    st.missing_ewb = [s.ship_id for s in st.ships if not s.ewb_no]
    now = ship_set(iw)
    events = list(iw.ewb_events.all())
    st.request = next((e for e in events if e.kind == EwbEvent.Kind.REQUEST), None)
    st.last_mail = next((e for e in events if e.kind in (EwbEvent.Kind.REQUEST, EwbEvent.Kind.REMINDER)), None)
    st.covered = bool(st.request) and st.request.ships == now and not st.missing_ewb
    st.release = next((e for e in events if e.kind == EwbEvent.Kind.RELEASE and e.ships == now), None)
    st.pending_release = ApprovalRequest.objects.filter(permission_id="EWB_MRA_RELEASE", entity_type="iw",
                                                        entity_id=str(iw.pk), status=ApprovalRequest.Status.PENDING).first()
    st.confirmed = bool(iw.ewb_confirmed_at)
    st.transporter_id, st.transporter_name = transporter(iw)
    return st


def mra_blocker(iw: Iw) -> str | None:
    st = state(iw)
    if st.mra_ok:
        return None
    if st.missing_ewb:
        return (f"Enter the E-way bill No of outbound Ship {', '.join(st.missing_ewb)} and send the Transporter ID "
                "update mail to the source (E-way bill).")
    return ("Send the E-way bill Transporter ID update mail to the source" +
            (" again - the outbound Ships changed after the last mail" if st.request else "") +
            " (E-way bill), or ask the CFA Manager to release the MRA without it.")


def closure_ok(iw: Iw) -> tuple[bool, str]:
    st = state(iw)
    if not st.needed or st.confirmed:
        return True, ""
    return False, "record that the source updated the Transporter ID (E-way bill screen)"

# ---------------------------------------------------------------- clerk actions


def _open_iw(iw_pk: int) -> Iw:
    iw = Iw.objects.select_for_update(of=("self",)).select_related("load_type", "source_org").get(pk=iw_pk)
    if iw.status in CLOSED:
        raise IwError(f"{iw} is {iw.get_status_display().lower()}.")
    return iw


@transaction.atomic
def set_ewb(user: User, ship_pk: int, ewb_no: str) -> OutboundShip:
    require(user, "EWB_MAIL")
    ship = OutboundShip.objects.select_related("iw").get(pk=ship_pk)
    _open_iw(ship.iw_id)
    no = clean_text(ewb_no).replace(" ", "")
    if not EWB_RE.match(no):
        raise IwError(f"E-way bill No of Ship {ship.ship_id} must be 12 digits.")
    if no != ship.ewb_no:
        before = ship.ewb_no
        ship.ewb_no = no
        ship.save(update_fields=["ewb_no"])
        audit.record("EWB_NO_SET", "iw", ship.iw_id, user=user, iw_id=ship.iw_id,
                     before={"ship": ship.ship_id, "ewb_no": before}, after={"ship": ship.ship_id, "ewb_no": no})
    return ship


def recipients(iw: Iw) -> tuple[list[str], list[str]]:
    """The source's addresses (as for the MRA), Cc the internal 'E-way bill' list."""
    to, cc = [], []
    for t in comms.mra_targets(iw):
        to += [e for e in t.to if e not in to]
        cc += [e for e in t.cc if e not in cc]
    i_to, i_cc, _ = comms.internal("EWB_TRANSPORTER")
    for e in i_to + i_cc:
        if e not in to and e not in cc:
            cc.append(e)
    return to, cc


def preview(iw: Iw, reminder: bool = False) -> dict:
    st = state(iw)
    to, cc = recipients(iw)
    subject = (("REMINDER: " if reminder else "") + f"Request to update Transporter ID in E-way Bill - {iw.vehicle_no} - "
               f"{iw.iw_no} - Depot {comms._branding(iw)['depot_code']}")
    ctx = {"st": st, "reminder": reminder, "first": st.request}
    from django.template.loader import render_to_string
    html = render_to_string("iw/mail/ewb_transporter.html", {"iw": iw, "b": comms._branding(iw), "stamp": "(stamp)", **ctx})
    return {"to": to, "cc": cc, "subject": subject, "html": html, "ctx": ctx, "st": st}


@transaction.atomic
def send(user: User, iw_pk: int, reminder: bool = False) -> MailMessage:
    require(user, "EWB_MAIL")
    iw = _open_iw(iw_pk)
    st = state(iw)
    if not st.needed:
        raise IwError("This IW carries no outbound transhipment Ship.")
    if st.missing_ewb:
        raise IwError(f"Enter the E-way bill No of every outbound Ship first ({', '.join(st.missing_ewb)}).")
    if not st.transporter_id:
        raise IwError("The Transporter ID is not set in the Depot master (operating CFA) - ask the Administrator.")
    if reminder:
        if not st.covered:
            raise IwError("Send the update request first.")
        if st.confirmed:
            raise IwError("The source has already updated the E-way bill(s).")
        if not st.can_remind:
            raise IwError(f"A mail went at {timezone.localtime(st.last_mail.at):%I:%M %p} - a reminder is possible from "
                          f"{timezone.localtime(st.remind_at):%I:%M %p}.")
    p = preview(iw, reminder)
    if not p["to"]:
        raise IwError(f"No e-mail on file for the source {iw.source_org or ''} - enter it on the IW (as for the MRA), or "
                      "ask the CFA Manager to release the MRA without this mail.")
    kind = EwbEvent.Kind.REMINDER if reminder else EwbEvent.Kind.REQUEST
    n = iw.ewb_events.filter(kind=kind).count() + 1
    m = comms._queue(iw, MailMessage.Purpose.EWB_TRANSPORTER, None, f"EWB {iw.iw_no} {kind[:3]}{n}", p["to"], p["cc"],
                     p["subject"], "iw/mail/ewb_transporter.html", p["ctx"], [], user)
    ev = EwbEvent.objects.create(iw=iw, kind=kind, ships=ship_set(iw), mail=m, by=user)
    if not reminder and iw.ewb_confirmed_at:      # a new request (Ships changed) needs a new confirmation
        iw.ewb_confirmed_at, iw.ewb_confirmed_by, iw.ewb_confirm_note = None, None, ""
        iw.save(update_fields=["ewb_confirmed_at", "ewb_confirmed_by", "ewb_confirm_note", "updated_at"])
    audit.record("EWB_MAIL_QUEUED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"kind": kind, "ships": ev.ships, "to": m.to, "cc": m.cc, "stamp": m.stamp})
    return m


@transaction.atomic
def confirm(user: User, iw_pk: int, note: str = "") -> Iw:
    require(user, "EWB_MAIL")
    iw = _open_iw(iw_pk)
    st = state(iw)
    if not st.needed:
        raise IwError("This IW carries no outbound transhipment Ship.")
    if st.missing_ewb:
        raise IwError(f"Enter the E-way bill No of every outbound Ship first ({', '.join(st.missing_ewb)}).")
    if not (st.covered or st.release):
        raise IwError("Send the Transporter ID update mail first (or get the MRA release approved).")
    iw.ewb_confirmed_at, iw.ewb_confirmed_by, iw.ewb_confirm_note = timezone.now(), user, clean_text(note)[:200]
    iw.save(update_fields=["ewb_confirmed_at", "ewb_confirmed_by", "ewb_confirm_note", "updated_at"])
    audit.record("EWB_UPDATE_CONFIRMED", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"ships": ship_set(iw), "note": iw.ewb_confirm_note})
    return iw

# ---------------------------------------------------------------- MRA release without the mail (maker-checker)


def request_release(user: User, iw: Iw, reason_kind: str, remark: str = "") -> ApprovalRequest:
    st = state(iw)
    if not st.needed or st.mra_ok:
        raise IwError("No release needed - the MRA is not blocked by the E-way bill mail.")
    if st.pending_release:
        raise IwError("A release request for this IW is already waiting for the CFA Manager.")
    if reason_kind not in RELEASE_REASONS:
        raise IwError("Choose the reason.")
    remark = clean_text(remark)
    if reason_kind == "Other" and len(remark) < 5:
        raise IwError("Write the reason (Other).")
    reason = reason_kind + (f" - {remark}" if remark else "")
    from inward.accounts.approvals import submit
    return submit(user, "EWB_MRA_RELEASE", "iw", iw.pk, reason, payload={"iw_no": iw.iw_no, "ships": ship_set(iw)})


def apply_release(req: ApprovalRequest, approver: User) -> None:
    """Effect of an approved EWB_MRA_RELEASE: the MRA may be issued for the Ships of the request. The mail stays
    pending and the closure still needs the source's update."""
    iw = Iw.objects.select_for_update(of=("self",)).get(pk=int(req.entity_id))
    ships = (req.payload or {}).get("ships") or ship_set(iw)
    if ships != ship_set(iw):
        raise IwError(f"The outbound Ships of {iw.iw_no} changed after the request - ask again.")
    ev = EwbEvent.objects.create(iw=iw, kind=EwbEvent.Kind.RELEASE, ships=ships, reason=req.reason[:300],
                                 by=approver)
    audit.record("EWB_MRA_RELEASED", "iw", iw.pk, user=approver, iw_id=iw.pk,
                 after={"ships": ships, "reason": req.reason, "event": ev.pk}, requested_by=req.requested_by_id,
                 approval_request=req.pk or None)
