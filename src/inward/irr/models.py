"""Accumulated IRR history (SoT 8.2). Rows are never deduplicated by content: legitimately identical rows exist,
so each (Type, TRX Number, TRX Date) group is merged as a multiset (inward.domain.irr_merge)."""
from __future__ import annotations

from django.db import models


class IrrTransaction(models.Model):
    batch = models.ForeignKey("core.ImportBatch", on_delete=models.PROTECT, related_name="irr_rows")
    type = models.CharField(max_length=40)
    type_key = models.CharField(max_length=40, db_index=True)  # upper-case Type
    trx_number = models.CharField(max_length=80, db_index=True)
    trx_date = models.DateField(db_index=True)
    receipt_no = models.CharField(max_length=40, blank=True, db_index=True)  # "42637 / 51882836" -> "42637"
    trx_reference = models.CharField(max_length=60, blank=True)              # -> "51882836" (Ship / challan)
    source_subinv = models.CharField(max_length=20, blank=True)
    source_locator = models.CharField(max_length=60, blank=True)
    dest_subinv = models.CharField(max_length=20, blank=True)
    dest_locator = models.CharField(max_length=60, blank=True)
    lot_number = models.CharField(max_length=60, blank=True)
    reason_code = models.CharField(max_length=80, blank=True)
    reason_description = models.CharField(max_length=120, blank=True)
    shipping_id_date = models.DateField(null=True, blank=True)
    shipping_id = models.CharField(max_length=30, blank=True, db_index=True)
    item_description = models.CharField(max_length=200, blank=True)
    sku = models.CharField(max_length=20, db_index=True)
    no_of_pack = models.DecimalField(max_digits=16, decimal_places=3)
    volume_ltr = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    weight_kg = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    transaction_type = models.CharField(max_length=20, blank=True)  # Issue / Receipt

    class Meta:
        db_table = "irr_transaction"
        indexes = [models.Index(fields=["type_key", "trx_number", "trx_date"], name="ix_irr_group")]
