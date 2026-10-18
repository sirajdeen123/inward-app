"""Cash register rules (decisions 18-Oct-2026): the cashier confirms cash received from the clerk; a handover to
the manager is confirmed by the other side; bill photos are optional; nothing is edited or deleted."""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from inward.accounts.models import User
from inward.accounts.services import PermissionDenied, modes_for, require
from inward.cash.models import CashEntry
from inward.core import audit, documents
from inward.domain.normalize import clean_text

K, S = CashEntry.Kind, CashEntry.State
ZERO = Decimal(0)
CATEGORIES = ["Tea / refreshments", "Office upkeep / cleaning", "Stationery / printing", "Courier / postage",
              "Loading help / labour", "Electricity / water", "Repairs / maintenance", "Others"]
BILL_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "application/pdf": ".pdf"}


class CashError(ValueError):
    pass


def is_manager(user: User) -> bool:
    return "GRANT" in modes_for(user, "CASH_MANAGE")


def _amount(value, what="Amount") -> Decimal:
    try:
        a = Decimal(str(value).replace(",", "").strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise CashError(f"{what}: enter rupees, e.g. 150 or 150.50.") from exc
    if a <= 0:
        raise CashError(f"{what} must be more than zero.")
    return a


def _date(value) -> date:
    if isinstance(value, date):
        d = value
    elif str(value or "").strip():
        try:
            d = date.fromisoformat(str(value).strip())
        except ValueError as exc:
            raise CashError("Date not valid.") from exc
    else:
        d = timezone.localdate()
    if d > timezone.localdate():
        raise CashError("The date cannot be in the future.")
    return d


def balance(upto: date | None = None) -> Decimal:
    """Cash in hand: received cash in - expenses - confirmed handovers (up to and including `upto`)."""
    qs = CashEntry.objects.filter(state=S.DONE)
    if upto:
        qs = qs.filter(entry_date__lte=upto)
    inflow = qs.filter(kind__in=CashEntry.INFLOW).aggregate(s=Sum("amount"))["s"] or ZERO
    outflow = qs.exclude(kind__in=CashEntry.INFLOW).aggregate(s=Sum("amount"))["s"] or ZERO
    return inflow - outflow


@dataclass
class Position:
    in_hand: Decimal
    to_receive: Decimal
    pending_in: list = field(default_factory=list)
    pending_handover: list = field(default_factory=list)


def position() -> Position:
    pin = list(CashEntry.objects.filter(state=S.PENDING, kind__in=CashEntry.INFLOW)
               .select_related("iw", "iw__transporter", "created_by").order_by("created_at"))
    pho = list(CashEntry.objects.filter(state=S.PENDING, kind=K.HANDOVER).select_related("created_by"))
    return Position(balance(), sum((e.amount for e in pin), ZERO), pin, pho)

# ---------------------------------------------------------------- cash in from the clerk's entries


def _describe_iw(iw) -> str:
    return " · ".join(x for x in (iw.iw_no or f"IW {iw.pk}", iw.vehicle_no, str(iw.transporter or "")) if x)


@transaction.atomic
def sync_unloading(rec, user: User) -> None:
    """Called whenever an unloading record is saved: the cash part of it must reach the cashier exactly once."""
    from inward.iw.models import UnloadingRecord
    paid_cash = rec.paid_amount if (rec.payment_mode == UnloadingRecord.Mode.CASH and rec.status in
                                    (UnloadingRecord.Status.PAID, UnloadingRecord.Status.PART_PAID)) else ZERO
    key = f"UNL:{rec.pk}"
    live = CashEntry.objects.select_for_update().filter(source_key=key).exclude(state=S.CANCELLED)
    for e in live.filter(state=S.PENDING):            # not yet handed over: replaced by the new figure
        e.state, e.cancel_reason = S.CANCELLED, "Clerk changed the unloading payment before handing over"
        e.save(update_fields=["state", "cancel_reason"])
    received = live.filter(state=S.DONE).aggregate(s=Sum("amount"))["s"] or ZERO
    need = paid_cash - received
    if need:
        iw = rec.iw
        CashEntry.objects.create(
            kind=K.UNLOADING if not received else K.ADJUST, state=S.PENDING, entry_date=timezone.localdate(),
            amount=need, source_key=key, iw=iw, created_by=user,
            description=f"{_describe_iw(iw)} · unloading {rec.get_status_display().lower()}"
                        + ("" if not received else f" (correction: was Rs {received}, now Rs {paid_cash})"))


@transaction.atomic
def from_ledger_receipt(entry, user: User) -> None:
    if entry.payment_mode != "CASH":
        return
    CashEntry.objects.create(kind=K.LEDGER, state=S.PENDING, entry_date=entry.entry_date, amount=entry.amount,
                             source_key=f"LED:{entry.pk}", created_by=user,
                             description=f"{entry.transporter.name} · transporter ledger receipt"
                                         + (f" · ref {entry.reference}" if entry.reference else ""))


@transaction.atomic
def receive(user: User, entry_pk: int) -> CashEntry:
    require(user, "CASH_REGISTER")
    e = CashEntry.objects.select_for_update().get(pk=entry_pk)
    if e.state != S.PENDING or not e.is_inflow:
        raise CashError("This entry is not waiting to be received.")
    e.state, e.done_by, e.done_at, e.entry_date = S.DONE, user, timezone.now(), timezone.localdate()
    e.save(update_fields=["state", "done_by", "done_at", "entry_date"])
    audit.record("CASH_RECEIVED", "cash_entry", e.pk, user=user, iw_id=e.iw_id,
                 after={"amount": str(e.amount), "from": e.created_by.username, "for": e.description})
    return e

# ---------------------------------------------------------------- cashier entries


@transaction.atomic
def add_expense(user: User, *, entry_date="", category: str, amount, paid_to: str = "", description: str = "",
                bill: tuple[bytes, str, str] | None = None) -> CashEntry:
    require(user, "CASH_REGISTER")
    day = _date(entry_date)
    amt = _amount(amount)
    if category not in CATEGORIES:
        raise CashError("Choose the expense category.")
    if category == "Others" and not clean_text(description):
        raise CashError("Write what the expense was for.")
    if amt > balance():
        raise CashError(f"Rs {amt} is more than the cash in hand (Rs {balance()}).")
    doc = None
    if bill and bill[0]:
        data, name, ctype = bill
        ctype = (ctype or "").split(";")[0].lower()
        if ctype not in BILL_TYPES:
            raise CashError("The bill must be a photo (JPEG / PNG) or a PDF.")
        doc = documents.store(data, filename=name or f"bill{BILL_TYPES[ctype]}", content_type=ctype,
                              doc_type="CASH_BILL", user=user)
    e = CashEntry.objects.create(kind=K.EXPENSE, entry_date=day, amount=amt, category=category,
                                 paid_to=clean_text(paid_to)[:120], description=clean_text(description)[:240],
                                 bill=doc, created_by=user, done_by=user, done_at=timezone.now())
    audit.record("CASH_EXPENSE", "cash_entry", e.pk, user=user,
                 after={"date": str(day), "amount": str(amt), "category": category, "paid_to": e.paid_to,
                        "bill": str(doc.pk) if doc else None})
    return e


@transaction.atomic
def record_handover(user: User, *, amount, note: str = "") -> CashEntry:
    """Cashier -> manager. Recorded by either side; the other side confirms (both-sides rule)."""
    require(user, "CASH_REGISTER")
    amt = _amount(amount)
    if amt > balance():
        raise CashError(f"Rs {amt} is more than the cash in hand (Rs {balance()}).")
    side = "manager" if is_manager(user) else "cashier"
    e = CashEntry.objects.create(kind=K.HANDOVER, state=S.PENDING, entry_date=timezone.localdate(), amount=amt,
                                 description=clean_text(note)[:240] or f"Cash handed over to the manager (recorded by {side})",
                                 paid_to=side, created_by=user)
    audit.record("CASH_HANDOVER_RECORDED", "cash_entry", e.pk, user=user, after={"amount": str(amt), "by": side})
    return e


@transaction.atomic
def confirm_handover(user: User, entry_pk: int) -> CashEntry:
    require(user, "CASH_REGISTER")
    e = CashEntry.objects.select_for_update().get(pk=entry_pk)
    if e.kind != K.HANDOVER or e.state != S.PENDING:
        raise CashError("This handover is not waiting for confirmation.")
    if e.created_by_id == user.pk:
        raise PermissionDenied("The other side confirms a handover - not the person who recorded it.")
    if (e.paid_to == "cashier") != is_manager(user):
        raise PermissionDenied("A handover recorded by the cashier is confirmed by the manager, and the other way round.")
    if e.amount > balance():
        raise CashError(f"Rs {e.amount} is more than the cash in hand (Rs {balance()}).")
    e.state, e.done_by, e.done_at, e.entry_date = S.DONE, user, timezone.now(), timezone.localdate()
    e.save(update_fields=["state", "done_by", "done_at", "entry_date"])
    audit.record("CASH_HANDOVER_CONFIRMED", "cash_entry", e.pk, user=user, after={"amount": str(e.amount)})
    return e


@transaction.atomic
def set_opening(user: User, amount, entry_date="") -> CashEntry:
    require(user, "CASH_MANAGE")
    if CashEntry.objects.exclude(state=S.CANCELLED).filter(kind=K.OPENING).exists():
        raise CashError("The opening balance is already set.")
    e = CashEntry.objects.create(kind=K.OPENING, entry_date=_date(entry_date), amount=_amount(amount, "Opening balance"),
                                 description="Opening cash balance", created_by=user, done_by=user, done_at=timezone.now())
    audit.record("CASH_OPENING", "cash_entry", e.pk, user=user, after={"amount": str(e.amount)})
    return e


@transaction.atomic
def cancel(user: User, entry_pk: int, reason: str) -> CashEntry:
    """Expenses and handovers only: the cashier cancels their own entry the same day; the manager any time."""
    require(user, "CASH_REGISTER")
    e = CashEntry.objects.select_for_update().get(pk=entry_pk)
    if e.kind not in (K.EXPENSE, K.HANDOVER) or e.state == S.CANCELLED:
        raise CashError("Only an expense or a handover can be cancelled.")
    if e.kind == K.HANDOVER and e.state == S.DONE and not is_manager(user):
        raise PermissionDenied("A confirmed handover can be cancelled only by the manager.")
    if not is_manager(user) and (e.created_by_id != user.pk or timezone.localtime(e.created_at).date() != timezone.localdate()):
        raise PermissionDenied("You can cancel only your own entry, on the day it was made. Ask the manager.")
    if not clean_text(reason):
        raise CashError("A reason is mandatory.")
    before = e.state
    e.state, e.cancel_reason = S.CANCELLED, clean_text(reason)[:200]
    e.save(update_fields=["state", "cancel_reason"])
    audit.record("CASH_CANCELLED", "cash_entry", e.pk, user=user, before={"state": before},
                 after={"amount": str(e.amount), "kind": e.kind, "reason": e.cancel_reason})
    return e

# ---------------------------------------------------------------- monthly statement


def statement(year: int, month: int) -> dict:
    d1 = date(year, month, 1)
    d2 = date(year, month, calendar.monthrange(year, month)[1])
    opening = balance(date.fromordinal(d1.toordinal() - 1))
    rows, run = [], opening
    tin = tex = tho = ZERO
    for e in (CashEntry.objects.filter(state=S.DONE, entry_date__range=(d1, d2))
              .select_related("created_by", "done_by", "iw", "bill").order_by("entry_date", "done_at", "id")):
        run += e.signed
        if e.is_inflow:
            tin += e.amount
        elif e.kind == K.EXPENSE:
            tex += e.amount
        else:
            tho += e.amount
        rows.append({"e": e, "balance": run})
    by_cat = (CashEntry.objects.filter(state=S.DONE, kind=K.EXPENSE, entry_date__range=(d1, d2))
              .values("category").annotate(total=Sum("amount")).order_by("-total"))
    cancelled = CashEntry.objects.filter(state=S.CANCELLED, created_at__date__range=(d1, d2)).order_by("created_at")
    pending = CashEntry.objects.filter(state=S.PENDING).filter(Q(entry_date__lte=d2)).order_by("created_at")
    return {"from": d1, "to": d2, "opening": opening, "rows": rows, "cash_in": tin, "expenses": tex,
            "handovers": tho, "closing": run, "by_category": list(by_cat), "cancelled": list(cancelled),
            "pending": list(pending)}


def statement_xlsx(st: dict) -> bytes:
    import io

    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = f"Cash {st['from']:%b-%Y}"
    ws.append([f"Cash statement {st['from']:%d-%b-%Y} to {st['to']:%d-%b-%Y}"])
    ws.append(["Opening balance", float(st["opening"])])
    ws.append([])
    ws.append(["Date", "Type", "Category", "Details", "Paid to / from", "IW", "In Rs", "Out Rs", "Balance Rs",
               "Entered by", "Received / confirmed by"])
    for r in st["rows"]:
        e = r["e"]
        ws.append([e.entry_date.strftime("%d-%m-%Y"), e.get_kind_display(), e.category, e.description, e.paid_to,
                   e.iw.iw_no if e.iw_id else "", float(e.amount) if e.is_inflow else None,
                   None if e.is_inflow else float(e.amount), float(r["balance"]), e.created_by.username,
                   e.done_by.username if e.done_by_id else ""])
    ws.append([])
    ws.append(["Cash in", float(st["cash_in"])])
    ws.append(["Expenses", float(st["expenses"])])
    ws.append(["Handed over to the manager", float(st["handovers"])])
    ws.append(["Closing balance", float(st["closing"])])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
