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


# ---------------------------------------------------------------- Screen 03: linked references (tracks)

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
    """Outbound transhipment Ship (not in SIT). Rows of the Despatch Detail Report are assigned to it and must
    balance with the totals on its document (SoT 5.3)."""
    iw = models.ForeignKey(Iw, on_delete=models.CASCADE, related_name="outbound_ships")
    ship_id = models.CharField(max_length=20)
    destination = models.ForeignKey(Organisation, on_delete=models.PROTECT, related_name="+")
    doc_nop = models.DecimalField(max_digits=14, decimal_places=3)
    doc_ltr = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    doc_kg = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    status = models.CharField(max_length=14, choices=TrackStatus.choices, default=TrackStatus.LINKED)
    linked_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    linked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "iw_outbound_ship"
        constraints = [models.UniqueConstraint(fields=["iw", "ship_id"], name="uq_iw_outbound_ship")]


class DespatchReport(models.Model):
    iw = models.ForeignKey(Iw, on_delete=models.CASCADE, related_name="despatch_reports")
    document = models.ForeignKey("core.Document", on_delete=models.PROTECT, related_name="+")
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
    excluded = models.BooleanField(default=False)  # "Not on this vehicle"
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
