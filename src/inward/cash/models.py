"""Cash register kept by the cashier (UAT 18-Oct-2026).

Cash comes in when a clerk records an unloading charge (or a transporter-ledger receipt) paid in CASH: the entry
waits as "to receive from clerk" until the cashier ticks it received. The cashier records petty expenses (tea,
office upkeep, ...). Cash handed to the manager is recorded by one side and confirmed by the other. Entries are
never deleted or edited - a mistake is cancelled with a reason and stays visible.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

USER = settings.AUTH_USER_MODEL


class CashEntry(models.Model):
    class Kind(models.TextChoices):
        OPENING = "OPENING", "Opening balance"
        UNLOADING = "UNLOADING", "Unloading charge (cash)"
        LEDGER = "LEDGER", "Transporter ledger receipt (cash)"
        ADJUST = "ADJUST", "Correction of a clerk's cash entry"
        EXPENSE = "EXPENSE", "Expense"
        HANDOVER = "HANDOVER", "Handed over to the manager"

    class State(models.TextChoices):
        PENDING = "PENDING", "Waiting"      # cash in: to receive from the clerk; handover: to be confirmed
        DONE = "DONE", "Done"
        CANCELLED = "CANCELLED", "Cancelled"

    INFLOW = (Kind.OPENING, Kind.UNLOADING, Kind.LEDGER, Kind.ADJUST)

    kind = models.CharField(max_length=10, choices=Kind.choices)
    state = models.CharField(max_length=10, choices=State.choices, default=State.DONE)
    entry_date = models.DateField()           # cash-in / handover: the day it was received / confirmed
    amount = models.DecimalField(max_digits=14, decimal_places=2)   # positive; an ADJUST may be negative
    category = models.CharField(max_length=40, blank=True)
    paid_to = models.CharField(max_length=120, blank=True)
    description = models.CharField(max_length=240, blank=True)
    source_key = models.CharField(max_length=30, blank=True, db_index=True)   # "UNL:<record pk>" / "LED:<entry pk>"
    iw = models.ForeignKey("iw.Iw", null=True, blank=True, on_delete=models.PROTECT, related_name="cash_entries")
    bill = models.ForeignKey("core.Document", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    done_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    done_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        db_table = "cash_entry"
        ordering = ["entry_date", "id"]

    @property
    def is_inflow(self) -> bool:
        return self.kind in self.INFLOW

    @property
    def signed(self):
        return self.amount if self.is_inflow else -self.amount
