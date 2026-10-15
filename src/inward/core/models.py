"""Phase 0 schema: audit, documents, masters, numbering, imports, exceptions (SoT 9, 12, 16).

Transaction entities (IW, tracks, SED, MRA, communications, reconciliation) arrive with their phases.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

USER = settings.AUTH_USER_MODEL

# ---------------------------------------------------------------- audit (append-only, enforced by trigger)


class AuditEvent(models.Model):
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    action = models.CharField(max_length=60, db_index=True)
    entity_type = models.CharField(max_length=40)
    entity_id = models.CharField(max_length=64)
    iw_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    context = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "audit_event"
        indexes = [models.Index(fields=["entity_type", "entity_id"], name="ix_audit_entity")]

# ---------------------------------------------------------------- documents


class Document(models.Model):
    """Immutable document metadata; the file itself is content-addressed by SHA-256 (SoT 9)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    doc_type = models.CharField(max_length=40, db_index=True)
    original_filename = models.CharField(max_length=260)
    content_type = models.CharField(max_length=100)
    size_bytes = models.BigIntegerField()
    sha256 = models.CharField(max_length=64, db_index=True)
    storage_key = models.CharField(max_length=300)
    revision = models.PositiveIntegerField(default=1)
    supersedes = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    cloud_mirrored_at = models.DateTimeField(null=True, blank=True)  # OneDrive copy confirmed (SoT 9)

    class Meta:
        db_table = "document"


class DocumentLink(models.Model):
    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="links")
    entity_type = models.CharField(max_length=40)
    entity_id = models.CharField(max_length=64)
    role = models.CharField(max_length=40, default="evidence")

    class Meta:
        db_table = "document_link"
        constraints = [models.UniqueConstraint(fields=["document", "entity_type", "entity_id"], name="uq_document_link")]
        indexes = [models.Index(fields=["entity_type", "entity_id"], name="ix_document_link_entity")]

# ---------------------------------------------------------------- masters


class Depot(models.Model):
    code = models.CharField(max_length=10, primary_key=True)
    name = models.CharField(max_length=80)
    company_name = models.CharField(max_length=120, default="BERGER PAINTS INDIA LTD")  # principal, on every document
    address = models.TextField(blank=True)

    class Meta:
        db_table = "depot"

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class DepotOperator(models.Model):
    """Operating CFA of a depot; effective-dated so a CFA change never rewrites old documents."""
    depot = models.ForeignKey(Depot, on_delete=models.PROTECT, related_name="operators")
    cfa_name = models.CharField(max_length=120)
    cfa_short_name = models.CharField(max_length=60)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "depot_operator"


class Organisation(models.Model):
    class OrgType(models.TextChoices):
        DEPOT = "DEPOT"
        RDC = "RDC"
        FACTORY = "FACTORY"
        OTHER = "OTHER"

    code = models.CharField(max_length=10, primary_key=True)
    name = models.CharField(max_length=120)
    org_type = models.CharField(max_length=10, choices=OrgType.choices, default=OrgType.OTHER)
    state = models.CharField(max_length=60, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "organisation"

    def __str__(self) -> str:
        return f"{self.code} : {self.name}"


class EmailStatus(models.TextChoices):
    UNVERIFIED = "UNVERIFIED"  # entered by a clerk at MRA stage; usable immediately (SoT 7.6)
    VERIFIED = "VERIFIED"      # confirmed by the CFA Manager
    RETIRED = "RETIRED"


class OrganisationEmail(models.Model):
    organisation = models.ForeignKey(Organisation, on_delete=models.PROTECT, related_name="emails")
    email = models.EmailField(max_length=256)
    kind = models.CharField(max_length=2, choices=[("TO", "To"), ("CC", "Cc")])
    status = models.CharField(max_length=10, choices=EmailStatus.choices, default=EmailStatus.UNVERIFIED)
    entered_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    verified_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "organisation_email"
        constraints = [models.UniqueConstraint(fields=["organisation", "email", "kind"], name="uq_organisation_email")]


class Sender(models.Model):
    """Non-Stock senders (Others load type); same Unverified rule as organisation e-mails (SoT 12)."""
    name = models.CharField(max_length=160, unique=True)
    email = models.EmailField(max_length=256)
    status = models.CharField(max_length=10, choices=EmailStatus.choices, default=EmailStatus.UNVERIFIED)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "sender"

    def __str__(self) -> str:
        return self.name


class NotificationRecipient(models.Model):
    """Controlled internal recipients; mandatory ones cannot be removed by a clerk (SoT 7.6, 7.8, 23)."""
    purpose = models.CharField(max_length=40, db_index=True)
    role_label = models.CharField(max_length=60)
    kind = models.CharField(max_length=2, choices=[("TO", "To"), ("CC", "Cc")], default="TO")
    email = models.EmailField(max_length=256, blank=True)
    mandatory = models.BooleanField(default=True)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "notification_recipient"


class Transporter(models.Model):
    name = models.CharField(max_length=160)
    search_key = models.CharField(max_length=160, unique=True)
    verified = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "transporter"

    def __str__(self) -> str:
        return self.name


class Vendor(models.Model):
    code = models.CharField(max_length=20, primary_key=True)  # e.g. V54
    name = models.CharField(max_length=160)
    oracle_vendor_id = models.CharField(max_length=20, blank=True)
    site = models.CharField(max_length=80, blank=True)
    email = models.EmailField(max_length=256, blank=True)  # MRA recipient for vendor loads (SoT 7.6)
    email_status = models.CharField(max_length=10, choices=EmailStatus.choices, default=EmailStatus.UNVERIFIED)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "vendor"

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class Warehouse(models.TextChoices):
    RETAIL = "RETAIL", "Retail warehouse"
    INDUSTRIAL = "INDUSTRIAL", "Industrial warehouse"


class BusinessLineWarehouse(models.Model):
    """Business line -> warehouse for the split unloading checklist (SoT 7.1)."""
    business_line = models.CharField(max_length=40, primary_key=True)
    warehouse = models.CharField(max_length=12, choices=Warehouse.choices)

    class Meta:
        db_table = "business_line_warehouse"


class PackageType(models.Model):
    code = models.CharField(max_length=20, primary_key=True)
    name = models.CharField(max_length=40)
    sort_priority = models.PositiveIntegerField()

    class Meta:
        db_table = "package_type"
        ordering = ["sort_priority"]

    def __str__(self) -> str:
        return self.name


class SkuPackaging(models.Model):
    """How pieces of a SKU are packed - converts document pieces into physical packages (SoT 7.1).
    Loaded from the company list and corrected by clerks; every change is versioned in PackagingChange."""

    class Source(models.TextChoices):
        COMPANY_LIST = "COMPANY_LIST", "Company list"
        CLERK = "CLERK", "Added / edited by clerk"

    sku = models.CharField(max_length=16, primary_key=True)
    floor_sku = models.CharField(max_length=8, db_index=True)
    description = models.CharField(max_length=200)
    business_line = models.CharField(max_length=40, blank=True)
    uom = models.CharField(max_length=4, blank=True)
    pack_size = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    measure_family = models.CharField(max_length=3, blank=True, choices=[("LTR", "LTR"), ("KG", "KG"), ("PCS", "PCS")])
    package_type = models.ForeignKey(PackageType, on_delete=models.PROTECT)
    pack_type_text = models.CharField(max_length=30, blank=True)   # as written in the company list
    units_per_package = models.PositiveIntegerField(null=True, blank=True)  # null = packaging not set
    verify_note = models.CharField(max_length=200, blank=True)     # "Verify physically" flag shown at unloading
    source = models.CharField(max_length=14, choices=Source.choices, default=Source.COMPANY_LIST)
    active = models.BooleanField(default=True)
    updated_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "sku_packaging"
        constraints = [models.CheckConstraint(condition=models.Q(units_per_package__isnull=True)
                                              | models.Q(units_per_package__gte=1), name="units_positive")]

    @property
    def is_set(self) -> bool:
        return self.units_per_package is not None


class PackagingUpload(models.Model):
    """One upload of the company SKU packaging list: previewed first, applied on confirmation."""

    class Status(models.TextChoices):
        PREVIEW = "PREVIEW", "Preview"
        APPLIED = "APPLIED", "Applied"
        CANCELLED = "CANCELLED", "Cancelled"

    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="+")
    filename = models.CharField(max_length=260)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PREVIEW)
    counts = models.JSONField(default=dict)   # new / changed / drastic / unchanged / errors
    sample = models.JSONField(default=dict)   # first changed / drastic / error rows for the preview screen
    uploaded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    bulk_flagged = models.BooleanField(default=False)  # too many existing SKUs changed at once

    class Meta:
        db_table = "packaging_upload"
        ordering = ["-uploaded_at"]


class PackagingChange(models.Model):
    """Version history of SKU packaging; drastic changes wait for the CFA Manager's review (accept / revert)."""

    class Review(models.TextChoices):
        NONE = "NONE", "Not needed"
        PENDING = "PENDING", "To review"
        ACCEPTED = "ACCEPTED", "Accepted"
        REVERTED = "REVERTED", "Reverted"

    sku = models.CharField(max_length=16, db_index=True)
    action = models.CharField(max_length=20)  # ADDED / UPDATED / VERIFIED / REVERTED
    source = models.CharField(max_length=14)  # COMPANY_LIST / CLERK / MANAGER
    upload = models.ForeignKey(PackagingUpload, null=True, blank=True, on_delete=models.PROTECT, related_name="changes")
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    reason = models.CharField(max_length=300, blank=True)
    drastic = models.BooleanField(default=False)
    drastic_why = models.CharField(max_length=200, blank=True)
    review = models.CharField(max_length=10, choices=Review.choices, default=Review.NONE, db_index=True)
    changed_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    changed_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "packaging_change"
        ordering = ["-changed_at"]


class NonStockItem(models.Model):
    description = models.CharField(max_length=160, unique=True)
    unit = models.CharField(max_length=20)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "non_stock_item"


class RateMaster(models.Model):
    """Effective-dated rates, snapshotted into transactions, never hard-coded (SoT 6, 7.2)."""
    rate_type = models.CharField(max_length=30)  # UNLOADING_PER_TON / DAMAGE_CARTON / DAMAGE_DRUM
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "rate_master"
        constraints = [models.UniqueConstraint(fields=["rate_type", "effective_from"], name="uq_rate_effective")]


class LoadType(models.Model):
    code = models.CharField(max_length=40, primary_key=True)
    name = models.CharField(max_length=80)
    sort_order = models.PositiveIntegerField()
    has_inbound_sit = models.BooleanField()
    has_outbound = models.BooleanField()
    is_direct_factory = models.BooleanField(default=False)
    is_vendor = models.BooleanField(default=False)
    is_non_stock = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = "load_type"
        ordering = ["sort_order"]

    def __str__(self) -> str:
        return self.name


class FeatureFlag(models.Model):
    key = models.CharField(max_length=60, primary_key=True)
    value = models.JSONField()
    description = models.CharField(max_length=200)

    class Meta:
        db_table = "feature_flag"

# ---------------------------------------------------------------- numbering


class IwCounter(models.Model):
    """One row per FY; incremented inside IW creation so numbers are unique and gap-free (SoT 4, 14.1)."""
    fy_code = models.CharField(max_length=4, primary_key=True)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "iw_counter"

# ---------------------------------------------------------------- imports and exceptions


class ImportBatch(models.Model):
    kind = models.CharField(max_length=12)    # SIT / IRR / DESPATCH / VENDOR
    source = models.CharField(max_length=10)  # MANUAL / GMAIL
    filename = models.CharField(max_length=260)
    sha256 = models.CharField(max_length=64)
    message_id = models.CharField(max_length=400, blank=True)
    document = models.ForeignKey(Document, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    uploaded_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)
    window_from = models.DateField(null=True, blank=True)
    window_to = models.DateField(null=True, blank=True)
    row_count = models.PositiveIntegerField(default=0)
    accepted_count = models.PositiveIntegerField(default=0)
    issue_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=12, default="ACCEPTED")
    summary = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "import_batch"
        constraints = [models.UniqueConstraint(fields=["kind", "sha256"], name="uq_import_kind_sha256")]


class ExceptionEvent(models.Model):
    """Exception lifecycle with escalation counters, to avoid unmanaged repeat spam (SoT 13)."""
    code = models.CharField(max_length=60, db_index=True)
    severity = models.CharField(max_length=10)  # INFO / WARN / CRITICAL
    entity_type = models.CharField(max_length=40)
    entity_id = models.CharField(max_length=64)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    first_escalated_at = models.DateTimeField(null=True, blank=True)
    last_escalated_at = models.DateTimeField(null=True, blank=True)
    escalation_count = models.PositiveIntegerField(default=0)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    resolution_note = models.TextField(blank=True)

    class Meta:
        db_table = "exception_event"
