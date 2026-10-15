"""IW vehicle / consignment header (SoT 4, 18.2). Tracks (linked references) arrive with Screen 03."""
from __future__ import annotations

from django.conf import settings
from django.db import models

from inward.core.models import LoadType, Organisation, Transporter

USER = settings.AUTH_USER_MODEL


class Iw(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        OPEN = "OPEN", "Open"
        MATERIAL_LINKED = "MATERIAL_LINKED", "Material linked"
        UNLOADING_IN_PROGRESS = "UNLOADING_IN_PROGRESS", "Unloading in progress"
        PHYSICALLY_VERIFIED = "PHYSICALLY_VERIFIED", "Physically verified"
        MRA_ISSUED = "MRA_ISSUED", "MRA issued"
        ERP_IN_PROGRESS = "ERP_IN_PROGRESS", "ERP in progress"
        RECON_COMPLETE = "RECON_COMPLETE", "Reconciliation complete"
        DOCUMENTS_COMPLETE = "DOCUMENTS_COMPLETE", "Documents complete"
        CLOSED = "CLOSED", "Closed"
        REOPENED = "REOPENED_ACTION_REQUIRED", "Reopened - action required"
        VOID = "VOID", "Void"

    # header may be edited and voided only before the MRA is issued (SoT 4, 18.2)
    EDITABLE = {Status.DRAFT, Status.OPEN, Status.MATERIAL_LINKED, Status.UNLOADING_IN_PROGRESS,
                Status.PHYSICALLY_VERIFIED}

    iw_no = models.CharField(max_length=20, unique=True, null=True, blank=True)
    fy_code = models.CharField(max_length=4, blank=True)
    status = models.CharField(max_length=26, choices=Status.choices, default=Status.DRAFT, db_index=True)
    arrival_at = models.DateTimeField(db_index=True)
    load_type = models.ForeignKey(LoadType, on_delete=models.PROTECT)
    source_org = models.ForeignKey(Organisation, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    vehicle_no = models.CharField(max_length=20)
    vehicle_key = models.CharField(max_length=20, db_index=True)  # normalised for search
    driver_name = models.CharField(max_length=80, blank=True)
    driver_mobile = models.CharField(max_length=15, blank=True)
    transporter = models.ForeignKey(Transporter, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    lr_no = models.CharField(max_length=40, blank=True)
    lr_key = models.CharField(max_length=40, blank=True, db_index=True)
    remarks = models.TextField(blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    void_reason = models.TextField(blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "iw"
        ordering = ["-arrival_at", "-id"]

    def __str__(self) -> str:
        return self.iw_no or f"Draft #{self.pk}"

    @property
    def is_editable(self) -> bool:
        return self.status in self.EDITABLE
