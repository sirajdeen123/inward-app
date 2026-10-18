"""IRR reconciliation engine - separate from the inward flow (18-Oct-2026).

Every IRR row gets one result: MATCHED (its other half / chain found in the IRR), EXPLAINED (linked to an IW, an
internal damage report, or marked by a user with a reason), INFO (needs no pair: receipts from outside, returns,
zero-quantity residual rows) or OPEN (an exception to look at). Results are rebuilt on every run; the manual marks
are kept in their own table and survive every run.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

USER = settings.AUTH_USER_MODEL


class IrrReconRun(models.Model):
    run_at = models.DateTimeField(auto_now_add=True)
    run_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    rows = models.PositiveIntegerField(default=0)
    summary = models.JSONField(default=dict)   # {family: {status: count}}

    class Meta:
        db_table = "irr_recon_run"
        ordering = ["-run_at"]


class IrrMatch(models.Model):
    class Status(models.TextChoices):
        MATCHED = "MATCHED", "Matched"
        EXPLAINED = "EXPLAINED", "Explained"
        INFO = "INFO", "Information"
        OPEN = "OPEN", "Open exception"

    row = models.OneToOneField("irr.IrrTransaction", on_delete=models.CASCADE, primary_key=True, related_name="match")
    family = models.CharField(max_length=20, db_index=True)
    status = models.CharField(max_length=10, choices=Status.choices, db_index=True)
    group = models.CharField(max_length=60, blank=True, db_index=True)   # rows reconciled together
    note = models.CharField(max_length=300, blank=True)
    link = models.CharField(max_length=40, blank=True)                    # IW-2627-000001 / IDR-2627-000001

    class Meta:
        db_table = "irr_match"


class IrrMark(models.Model):
    """A user's explanation of an exception (kept across engine runs)."""
    row = models.OneToOneField("irr.IrrTransaction", on_delete=models.CASCADE, primary_key=True, related_name="mark")
    reason = models.CharField(max_length=300)
    marked_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    marked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "irr_mark"
