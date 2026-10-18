"""Internal damage reports (decisions 18-Oct-2026).

Material damaged inside the warehouse (handling by labour), expired, or leaking (lid open) is listed by the floor
supervisor with photos, approved by REPLY MAIL from the Depot Manager or the Admin Officer (they do not use the
app), moved to the DMG sub-inventory in Oracle, and its TRX No(s) are reconciled against the IRR.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

USER = settings.AUTH_USER_MODEL


class DamageCounter(models.Model):
    fy_code = models.CharField(max_length=4, primary_key=True)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "damage_counter"


class DamageCategory(models.Model):
    """Reasons for internal damage - the supervisor adds new ones from the report screen (18-Oct-2026)."""
    code = models.CharField(max_length=20, primary_key=True)
    name = models.CharField(max_length=80, unique=True)
    photo_required = models.BooleanField(default=True)
    remarks_required = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=100)
    created_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "damage_category"
        ordering = ["sort_order", "name"]
        verbose_name_plural = "damage categories"

    def __str__(self) -> str:
        return self.name


class DamageReport(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft - being prepared"
        SUBMITTED = "SUBMITTED", "Approval requested by mail"
        REJECTED = "REJECTED", "Rejected - correct and send again"
        APPROVED = "APPROVED", "Approved - move to DMG in Oracle"
        TRANSFERRED = "TRANSFERRED", "TRX entered - reconcile with the IRR"
        RECONCILED = "RECONCILED", "Reconciled with the IRR"
        CANCELLED = "CANCELLED", "Cancelled"

    number = models.CharField(max_length=20, unique=True)          # IDR-2627-000001
    fy_code = models.CharField(max_length=4)
    found_on = models.DateField()
    location = models.CharField(max_length=120, blank=True)       # area / rack where it was found
    remarks = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    revision = models.PositiveIntegerField(default=0)              # approval mails sent (R1, R2 ...)
    request_mail = models.ForeignKey("iw.MailMessage", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    reminded_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_text = models.TextField(blank=True)
    reply_from = models.CharField(max_length=256, blank=True)
    reply = models.ForeignKey("core.Document", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    recon = models.JSONField(default=dict, blank=True)              # last reconciliation result
    accepted_reasons = models.JSONField(default=list, blank=True)   # reason codes the supervisor accepted
    cancel_reason = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "damage_report"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.number

    @property
    def tag(self) -> str:
        return f"IDR-APPR {self.number} R{self.revision}"

    @property
    def editable(self) -> bool:
        return self.status in (self.Status.DRAFT, self.Status.REJECTED)


class DamageLine(models.Model):
    report = models.ForeignKey(DamageReport, on_delete=models.CASCADE, related_name="lines")
    sku = models.CharField(max_length=16)
    floor_sku = models.CharField(max_length=8)
    description = models.CharField(max_length=200, blank=True)
    category = models.ForeignKey(DamageCategory, on_delete=models.PROTECT, related_name="+", db_column="category")
    quantity = models.DecimalField(max_digits=12, decimal_places=3)   # pieces
    lot_no = models.CharField(max_length=40, blank=True)
    locator = models.CharField(max_length=40, blank=True)
    remarks = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "damage_line"
        ordering = ["id"]


class DamagePhoto(models.Model):
    line = models.ForeignKey(DamageLine, on_delete=models.CASCADE, related_name="photos")
    original = models.ForeignKey("core.Document", on_delete=models.PROTECT, related_name="+")
    preview = models.ForeignKey("core.Document", null=True, on_delete=models.PROTECT, related_name="+")
    uploaded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "damage_photo"


class DamageTrx(models.Model):
    """Oracle Subinventory Transfer into DMG made after the approval."""
    report = models.ForeignKey(DamageReport, on_delete=models.PROTECT, related_name="trx")
    trx_no = models.CharField(max_length=40, unique=True)
    entered_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    entered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "damage_trx"
        ordering = ["id"]
