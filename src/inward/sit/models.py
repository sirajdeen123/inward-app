"""SIT import and monitoring (SoT 3). Snapshots are kept per import; the source file is not needed later."""
from __future__ import annotations

from django.conf import settings
from django.db import models

from inward.core.models import ImportBatch


class SitImport(models.Model):
    """One accepted SIT report (manual upload or fetched from Gmail)."""
    batch = models.OneToOneField(ImportBatch, on_delete=models.PROTECT, related_name="sit")
    report_from = models.DateField()
    report_to = models.DateField()
    system_date = models.DateField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)  # e-mail date when fetched from Gmail
    ship_count = models.PositiveIntegerField(default=0)
    line_count = models.PositiveIntegerField(default=0)
    event_count = models.PositiveIntegerField(default=0)
    critical_count = models.PositiveIntegerField(default=0)
    # UAT only (INWARD_MODE=TEST): every import is a merge - Ships not yet known are added, duplicates skipped,
    # nothing else changes and no change detection runs (decisions 16 / 17-Oct-2026). Never set on live.
    back_dated = models.BooleanField(default=False)
    known_ships = models.PositiveIntegerField(default=0)  # back-dated: Ships already in the engine, left unchanged

    class Meta:
        db_table = "sit_import"
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"SIT #{self.pk} ({self.system_date or self.report_to})"


class SitLine(models.Model):
    """Pending quantity of one SKU of one Ship in one import (rows of the same SKU summed)."""
    sit_import = models.ForeignKey(SitImport, on_delete=models.CASCADE, related_name="lines")
    ship_id = models.CharField(max_length=20)
    sku = models.CharField(max_length=16)
    floor_sku = models.CharField(max_length=8)
    description = models.CharField(max_length=200)
    business_line = models.CharField(max_length=40, blank=True)
    uom = models.CharField(max_length=4, blank=True)
    measure_family = models.CharField(max_length=3, blank=True)  # LTR / KG / PCS
    is_empty_packing = models.BooleanField(default=False)
    pack_size = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    quantity = models.DecimalField(max_digits=18, decimal_places=3)
    measure = models.DecimalField(max_digits=18, decimal_places=6)

    class Meta:
        db_table = "sit_line"
        indexes = [models.Index(fields=["sit_import", "ship_id"], name="ix_sit_line_import_ship"),
                   models.Index(fields=["sku"], name="ix_sit_line_sku")]


class SitIssue(models.Model):
    sit_import = models.ForeignKey(SitImport, on_delete=models.CASCADE, related_name="issues")
    row_no = models.PositiveIntegerField()
    ship_id = models.CharField(max_length=20, blank=True)
    message = models.CharField(max_length=300)

    class Meta:
        db_table = "sit_issue"


class SitShip(models.Model):
    """Current monitoring state of a Ship ID across imports."""

    class Status(models.TextChoices):
        IN_SIT = "IN_SIT", "In SIT"
        PARTIAL = "PARTIAL", "Partially processed"
        PROCESSED = "PROCESSED", "Removed - processed"
        REMOVED_NO_RECEIPT = "REMOVED_NO_RECEIPT", "Removed - receipt not entered"
        REMOVED_NEVER_LINKED = "REMOVED_NEVER_LINKED", "Removed - never linked (CRITICAL)"
        OUT_OF_WINDOW = "OUT_OF_WINDOW", "Out of report window"

    ship_id = models.CharField(max_length=20, primary_key=True)
    from_org_code = models.CharField(max_length=10)
    from_org_name = models.CharField(max_length=120, blank=True)
    to_org_code = models.CharField(max_length=10)
    shipment_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.IN_SIT)
    first_seen = models.ForeignKey(SitImport, on_delete=models.PROTECT, related_name="+")
    last_seen = models.ForeignKey(SitImport, on_delete=models.PROTECT, related_name="+")
    pre_go_live = models.BooleanField(default=False)
    pre_go_live_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="+")
    has_issues = models.BooleanField(default=False)  # blocked for linking until resolved (SoT 3.2)

    class Meta:
        db_table = "sit_ship"
        ordering = ["-shipment_date", "ship_id"]

    def __str__(self) -> str:
        return self.ship_id


class SitChangeEvent(models.Model):
    sit_import = models.ForeignKey(SitImport, on_delete=models.CASCADE, related_name="events")
    ship_id = models.CharField(max_length=20, db_index=True)
    event = models.CharField(max_length=30)
    sku = models.CharField(max_length=16, blank=True)
    before = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    after = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    critical = models.BooleanField(default=False)

    class Meta:
        db_table = "sit_change_event"
        ordering = ["-critical", "ship_id", "sku"]
