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
    verified_at = models.DateTimeField(null=True, blank=True)  # physical verification confirmed (SoT 7.2)
    verified_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "iw"
        ordering = ["-arrival_at", "-id"]

    def __str__(self) -> str:
        return self.iw_no or f"Draft #{self.pk}"

    @property
    def is_editable(self) -> bool:
        return self.status in self.EDITABLE


# ---------------------------------------------------------------- Screen 03: linked references (tracks)

class OutboundSource(models.TextChoices):
    """Where an outbound Ship's SKU rows come from (decision 16-Oct-2026). The Despatch Detail Report has no
    Ship ID yet (requested from the ERP team), so the clerk may also use the destination depot's SIT report
    (rows picked by Ship ID) or type the rows from the Ship document."""
    DESPATCH = "DESPATCH", "Despatch Detail Report"
    DEST_SIT = "DEST_SIT", "Destination depot SIT"
    MANUAL = "MANUAL", "Manual entry"


class TrackStatus(models.TextChoices):
    LINKED = "LINKED", "Linked"
    ROWS_ASSIGNED = "ROWS_ASSIGNED", "Rows assigned"
    VERIFIED = "VERIFIED", "Verified"
    MRA_DONE = "MRA_DONE", "MRA done"


class ShipLink(models.Model):
    """An inbound SIT Ship on this IW. Baseline quantities are frozen at link time (SoT 5.1, R-12)."""
    iw = models.ForeignKey(Iw, on_delete=models.CASCADE, related_name="ship_links")
    ship = models.ForeignKey("sit.SitShip", on_delete=models.PROTECT, related_name="links")
    baseline_import = models.ForeignKey("sit.SitImport", on_delete=models.PROTECT, related_name="+")
    is_split = models.BooleanField(default=False)
    split_reason = models.TextField(blank=True)
    lscrr_no = models.CharField(max_length=40, blank=True)
    lscrr_applied = models.BooleanField(null=True, blank=True)  # Direct Factory: required before Stock Receipt No
    status = models.CharField(max_length=14, choices=TrackStatus.choices, default=TrackStatus.LINKED)
    linked_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    linked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "iw_ship_link"
        constraints = [models.UniqueConstraint(fields=["iw", "ship"], name="uq_iw_ship_link")]


class ShipLinkLine(models.Model):
    """Expected quantity per SKU for this IW (whole SIT pending, or this vehicle's allocation when split)."""
    link = models.ForeignKey(ShipLink, on_delete=models.CASCADE, related_name="lines")
    sku = models.CharField(max_length=16)
    floor_sku = models.CharField(max_length=8)
    description = models.CharField(max_length=200)
    business_line = models.CharField(max_length=40, blank=True)
    uom = models.CharField(max_length=4, blank=True)
    measure_family = models.CharField(max_length=3, blank=True)
    is_empty_packing = models.BooleanField(default=False)
    pack_size = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    sit_quantity = models.DecimalField(max_digits=18, decimal_places=3)  # pending in SIT at link time
    quantity = models.DecimalField(max_digits=18, decimal_places=3)      # expected on this vehicle
    measure = models.DecimalField(max_digits=18, decimal_places=6)

    class Meta:
        db_table = "iw_ship_link_line"
        constraints = [models.UniqueConstraint(fields=["link", "sku"], name="uq_ship_link_sku")]


class ColourBankItem(models.Model):
    """Colour Bank machine: non-SIT asset, ends at MRA while the ERP-tracked flag is OFF (SoT 5.5)."""
    class Direction(models.TextChoices):
        INBOUND = "IN", "Inbound"
        OUTBOUND = "OUT", "Outbound"

    iw = models.ForeignKey(Iw, on_delete=models.CASCADE, related_name="colour_bank_items")
    direction = models.CharField(max_length=3, choices=Direction.choices, default=Direction.INBOUND)
    challan_no = models.CharField(max_length=40)
    description = models.CharField(max_length=160, blank=True)
    serial_no = models.CharField(max_length=60, blank=True)
    asset_no = models.CharField(max_length=60, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=14, choices=TrackStatus.choices, default=TrackStatus.LINKED)
    linked_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    linked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "iw_colour_bank_item"


class NonStockRef(models.Model):
    """Gift / promotional items that do not affect system stock (SoT 5.6)."""
    iw = models.ForeignKey(Iw, on_delete=models.CASCADE, related_name="non_stock_refs")
    sender = models.ForeignKey("core.Sender", on_delete=models.PROTECT, related_name="+")
    document_no = models.CharField(max_length=40)
    status = models.CharField(max_length=14, choices=TrackStatus.choices, default=TrackStatus.LINKED)
    linked_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    linked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "iw_non_stock_ref"


class NonStockLine(models.Model):
    ref = models.ForeignKey(NonStockRef, on_delete=models.CASCADE, related_name="lines")
    description = models.CharField(max_length=160)
    unit = models.CharField(max_length=20, default="Nos")
    quantity = models.DecimalField(max_digits=12, decimal_places=3)

    class Meta:
        db_table = "iw_non_stock_line"


class OutboundShip(models.Model):
    """Outbound transhipment Ship (not in SIT). The clerk ticks Despatch Detail Report rows until they tally
    with the LTR and KG (and packages, if entered) on the Ship document (SoT 5.3, revised 15-Oct-2026)."""
    iw = models.ForeignKey(Iw, on_delete=models.CASCADE, related_name="outbound_ships")
    ship_id = models.CharField(max_length=20)
    destination = models.ForeignKey(Organisation, on_delete=models.PROTECT, related_name="+")
    doc_nop = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)  # optional extra check
    doc_ltr = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    doc_kg = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    source = models.CharField(max_length=10, choices=OutboundSource.choices, default=OutboundSource.DESPATCH)
    status = models.CharField(max_length=14, choices=TrackStatus.choices, default=TrackStatus.LINKED)
    tallied_at = models.DateTimeField(null=True, blank=True)  # rows saved and totals matched
    linked_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    linked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "iw_outbound_ship"
        constraints = [models.UniqueConstraint(fields=["iw", "ship_id"], name="uq_iw_outbound_ship")]


class DespatchReport(models.Model):
    """Outbound rows source. DESPATCH reports are shared by the IW's Ships (rows are ticked); a DEST_SIT or
    MANUAL report belongs to one Ship and its rows are assigned to that Ship directly."""
    iw = models.ForeignKey(Iw, on_delete=models.CASCADE, related_name="despatch_reports")
    kind = models.CharField(max_length=10, choices=OutboundSource.choices, default=OutboundSource.DESPATCH)
    ship = models.ForeignKey(OutboundShip, null=True, blank=True, on_delete=models.CASCADE, related_name="own_reports")
    document = models.ForeignKey("core.Document", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    filename = models.CharField(max_length=260)
    window_from = models.DateField(null=True, blank=True)
    window_to = models.DateField(null=True, blank=True)
    uploaded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "iw_despatch_report"


class DespatchRow(models.Model):
    report = models.ForeignKey(DespatchReport, on_delete=models.CASCADE, related_name="rows")
    row_no = models.PositiveIntegerField()
    to_org_code = models.CharField(max_length=10)
    to_org_name = models.CharField(max_length=120, blank=True)
    sku = models.CharField(max_length=16)
    floor_sku = models.CharField(max_length=8)
    description = models.CharField(max_length=200)
    business_line = models.CharField(max_length=40, blank=True)
    uom = models.CharField(max_length=4, blank=True)
    nop = models.DecimalField(max_digits=14, decimal_places=3)
    pack_size = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    ltr = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    kg = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    fingerprint = models.CharField(max_length=120, db_index=True)
    outbound_ship = models.ForeignKey(OutboundShip, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="rows")
    reuse_acknowledged = models.BooleanField(default=False)

    class Meta:
        db_table = "iw_despatch_row"
        ordering = ["report_id", "row_no"]


class VendorConsignment(models.Model):
    """Vendor challan on an IW. Unique key Vendor Code + Challan No; IRR later matches only the PO Stock
    Receipt No (SoT 5.4, 8.5)."""
    class Source(models.TextChoices):
        PORTAL = "PORTAL", "Vendor portal report"
        MANUAL = "MANUAL", "Entered manually"

    iw = models.ForeignKey(Iw, on_delete=models.CASCADE, related_name="vendor_consignments")
    vendor = models.ForeignKey("core.Vendor", on_delete=models.PROTECT, related_name="+")
    po_no = models.CharField(max_length=30)
    challan_no = models.CharField(max_length=60)
    challan_key = models.CharField(max_length=60, db_index=True)
    source = models.CharField(max_length=6, choices=Source.choices)
    warehouse = models.CharField(max_length=12, choices=[("RETAIL", "Retail warehouse"),
                                                        ("INDUSTRIAL", "Industrial warehouse")])  # chosen by the clerk
    portal_document = models.ForeignKey("core.Document", null=True, blank=True, on_delete=models.PROTECT,
                                        related_name="+")
    status = models.CharField(max_length=14, choices=TrackStatus.choices, default=TrackStatus.LINKED)
    linked_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    linked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "iw_vendor_consignment"


class VendorLine(models.Model):
    consignment = models.ForeignKey(VendorConsignment, on_delete=models.CASCADE, related_name="lines")
    sku = models.CharField(max_length=16)
    floor_sku = models.CharField(max_length=8)
    description = models.CharField(max_length=200)
    uom = models.CharField(max_length=4, blank=True)
    measure_family = models.CharField(max_length=3, blank=True)
    nop = models.DecimalField(max_digits=14, decimal_places=3)
    ltr = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    kg = models.DecimalField(max_digits=18, decimal_places=3, default=0)

    class Meta:
        db_table = "iw_vendor_line"


class ChecklistSnapshot(models.Model):
    """A generated unloading checklist (SoT 7.1, iw_package_snapshot). Frozen once generated; regenerating
    creates a new revision so a printed checklist never changes afterwards."""
    iw = models.ForeignKey(Iw, on_delete=models.CASCADE, related_name="checklists")
    revision = models.PositiveIntegerField()
    content = models.JSONField()                 # sheets -> groups -> lines, with the packaging used
    content_hash = models.CharField(max_length=64)  # of the material it was built from (to detect changes)
    generated_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "iw_checklist"
        ordering = ["-revision"]
        constraints = [models.UniqueConstraint(fields=["iw", "revision"], name="uq_iw_checklist_revision")]


# ---------------------------------------------------------------- Screen 04: SED (SoT 7.2)


class SedLine(models.Model):
    """Short / Excess / Damage of one SKU of one linked reference, in pieces. Physical verification records
    exceptions only: every checklist line without an SED line was received as per the document (16-Oct-2026)."""

    class Source(models.TextChoices):
        SHIP = "SHIP", "Inbound Ship"
        VENDOR = "VENDOR", "Vendor challan"
        OUTBOUND = "OUTBOUND", "Outbound Ship"

    class DamageKind(models.TextChoices):
        MATERIAL = "MATERIAL", "Material damaged"
        CONTAINER = "CONTAINER", "Container only (material usable after decanting)"

    iw = models.ForeignKey(Iw, on_delete=models.CASCADE, related_name="sed_lines")
    source = models.CharField(max_length=10, choices=Source.choices)
    source_key = models.CharField(max_length=30)  # "SHIP:<link pk>" / "VENDOR:<consignment pk>" / "OUTBOUND:<ship pk>"
    source_label = models.CharField(max_length=120)
    ship_link = models.ForeignKey(ShipLink, null=True, blank=True, on_delete=models.CASCADE, related_name="sed_lines")
    vendor_consignment = models.ForeignKey(VendorConsignment, null=True, blank=True, on_delete=models.CASCADE,
                                           related_name="sed_lines")
    outbound_ship = models.ForeignKey(OutboundShip, null=True, blank=True, on_delete=models.CASCADE,
                                      related_name="sed_lines")
    sku = models.CharField(max_length=16)
    floor_sku = models.CharField(max_length=8)
    description = models.CharField(max_length=200, blank=True)
    is_extra = models.BooleanField(default=False)  # SKU not on the document (wrong item received): excess only
    doc_pieces = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    short_qty = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    excess_qty = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    damage_qty = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    damage_kind = models.CharField(max_length=10, choices=DamageKind.choices, blank=True)
    damage_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    container_type = models.CharField(max_length=10, blank=True)  # CARTON / DRUM (container-only damage)
    container_count = models.PositiveIntegerField(null=True, blank=True)
    container_rate = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)  # snapshot
    short_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)   # Rs
    damage_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)  # Rs
    short_remarks = models.CharField(max_length=300, blank=True)
    excess_remarks = models.CharField(max_length=300, blank=True)
    damage_remarks = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "iw_sed_line"
        ordering = ["source_key", "floor_sku", "sku"]
        constraints = [models.UniqueConstraint(fields=["iw", "source_key", "sku"], name="uq_sed_source_sku")]

    @property
    def claimable(self):
        return (self.short_value or 0) + (self.damage_value or 0)


class SedPhoto(models.Model):
    """Damage evidence: original kept as taken (SHA-256) plus a compressed preview (SoT 7.2)."""
    line = models.ForeignKey(SedLine, on_delete=models.CASCADE, related_name="photos")
    original = models.ForeignKey("core.Document", on_delete=models.PROTECT, related_name="+")
    preview = models.ForeignKey("core.Document", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    uploaded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "iw_sed_photo"
        ordering = ["uploaded_at"]


# ---------------------------------------------------------------- Screen 04: package counts and unloading (SoT 6, 7.5)


class PackageCount(models.Model):
    """Document package counts per warehouse for the MRA, pre-filled from the checklist and confirmed by the clerk."""
    iw = models.ForeignKey(Iw, on_delete=models.CASCADE, related_name="package_counts")
    warehouse = models.CharField(max_length=10)  # RETAIL / INDUSTRIAL / OUTBOUND / OTHER
    group = models.CharField(max_length=20)      # package group code, LOOSE, COLOUR_BANK, NON_STOCK
    label = models.CharField(max_length=60)
    unit = models.CharField(max_length=10, default="packages")  # packages / pieces / units
    count = models.DecimalField(max_digits=14, decimal_places=3)
    checklist_count = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "iw_package_count"
        ordering = ["warehouse", "sort_order"]
        constraints = [models.UniqueConstraint(fields=["iw", "warehouse", "group"], name="uq_iw_package_count")]


class UnloadingRecord(models.Model):
    """Unloading charge = tonnage x effective rate (snapshotted), and its settlement (SoT 6, 7.5)."""

    class Status(models.TextChoices):
        PAID = "PAID", "Paid"
        PART_PAID = "PART_PAID", "Part paid"
        OUTSTANDING = "OUTSTANDING", "Outstanding - transporter ledger"
        EXEMPT_REQUESTED = "EXEMPT_REQ", "Exempt / Nil - awaiting CFA Manager"
        EXEMPT = "EXEMPT", "Exempt / Nil"

    class Mode(models.TextChoices):
        CASH = "CASH", "Cash"
        UPI = "UPI", "UPI"
        BANK = "BANK", "Bank transfer"
        OTHER = "OTHER", "Other"

    SETTLED = {Status.PAID, Status.PART_PAID, Status.OUTSTANDING, Status.EXEMPT}  # G04-ULC passes

    iw = models.OneToOneField(Iw, on_delete=models.CASCADE, related_name="unloading")
    tonnage = models.DecimalField(max_digits=10, decimal_places=3)
    rate = models.DecimalField(max_digits=14, decimal_places=2)
    rate_effective_from = models.DateField()
    charge = models.DecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(max_length=12, choices=Status.choices)
    paid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    payment_mode = models.CharField(max_length=6, choices=Mode.choices, blank=True)
    payment_ref = models.CharField(max_length=60, blank=True)
    exempt_reason = models.TextField(blank=True)
    exempt_request = models.ForeignKey("accounts.ApprovalRequest", null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="+")
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    recorded_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "iw_unloading"

    @property
    def outstanding(self):
        if self.status in (self.Status.EXEMPT, self.Status.EXEMPT_REQUESTED):
            return 0
        return self.charge - self.paid_amount


class TransporterLedgerEntry(models.Model):
    """Transporter unloading ledger: CHARGE = outstanding unloading of an IW (debit), RECEIPT = money received
    later (credit). It runs independently of IW closure (SoT 7.5)."""

    class Kind(models.TextChoices):
        CHARGE = "CHARGE", "Unloading outstanding"
        RECEIPT = "RECEIPT", "Receipt"

    transporter = models.ForeignKey(Transporter, on_delete=models.PROTECT, related_name="ledger")
    iw = models.OneToOneField(Iw, null=True, blank=True, on_delete=models.PROTECT, related_name="ledger_charge")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    entry_date = models.DateField()
    payment_mode = models.CharField(max_length=6, blank=True)
    reference = models.CharField(max_length=60, blank=True)
    remarks = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "transporter_ledger"
        ordering = ["entry_date", "id"]
