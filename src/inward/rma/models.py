"""Material Returns (RMA) - dealer returns received at the depot (decisions 21-Oct-2026).

The clerk raises the Goods Return Note (GRN) in Oracle when the material arrives and raises the RMA Portal request
himself (no hand-off to the Sales Officer). The portal routes the request through its approvers (ADMIN / DM / RSM /
RCM, "Pending At"); the app maps those portal names to e-mails and chases them, Cc the Admin Officer, the godown and
the dealer's Sales Officer. After approval the portal gives the RMA No, Oracle the Credit Note and the "Rma Receipt"
in the IRR; the return is reconciled per SKU (GRN / portal / CN / IRR), verified by the godown and closed.

Sources: GRN PDF (Oracle "Goods Return Note"), RMA Portal Report (summary + details), RMA CN INV Report, Customer
Master and TSI Master (dealer site -> territory -> Sales Officer), the IRR already imported by the IRR module.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models

USER = settings.AUTH_USER_MODEL


class Condition(models.TextChoices):
    GOOD = "GOOD", "Good stock"
    DAMAGED = "DAMAGED", "Damaged stock"


class TransportMode(models.TextChoices):
    CFA = "CFA", "CFA vehicle"
    DEALER = "DEALER", "Dealer vehicle"
    THIRD = "THIRD", "Third-party transport"


# ---------------------------------------------------------------- masters


class Approver(models.Model):
    """A name as the portal prints it under Pending At / Action By, mapped to an e-mail by the CFA Manager. A person
    who leaves is redirected to the replacement - old pending requests keep the old name in the portal."""
    key = models.CharField(max_length=120, unique=True)          # upper-case, single spaces
    name = models.CharField(max_length=120)
    email = models.EmailField(max_length=256, blank=True)
    active = models.BooleanField(default=True)
    redirect_to = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    first_seen = models.DateTimeField(auto_now_add=True)
    updated_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "rma_approver"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class CustomerSite(models.Model):
    """Customer Master (Bill-To sites of this depot): site -> territory."""
    site_code = models.CharField(max_length=20, primary_key=True)
    account_no = models.CharField(max_length=20, db_index=True)
    name = models.CharField(max_length=200)
    territory = models.CharField(max_length=30)                  # S.S1.035.19
    territory_no = models.CharField(max_length=10, db_index=True)  # 19
    bl_type = models.CharField(max_length=30, blank=True)
    customer_type = models.CharField(max_length=60, blank=True)
    city = models.CharField(max_length=80, blank=True)

    class Meta:
        db_table = "rma_customer_site"


class Territory(models.Model):
    """TSI Master: territory number -> Sales Officer (the e-mail can be corrected here; an upload keeps it)."""
    territory_no = models.CharField(max_length=10, primary_key=True)
    sales_officer = models.CharField(max_length=120, blank=True)
    business_line = models.CharField(max_length=30, blank=True)
    email = models.EmailField(max_length=256, blank=True)          # from the TSI Master
    email_override = models.EmailField(max_length=256, blank=True)  # set on screen (transfer / correction)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "rma_territory"
        ordering = ["territory_no"]

    @property
    def mail(self) -> str:
        return self.email_override or self.email


# ---------------------------------------------------------------- portal / CN data


class Request(models.Model):
    """One RMA Portal request (one per dealer invoice), refreshed from every RMA Portal Report upload."""
    request_id = models.CharField(max_length=20, primary_key=True)
    region = models.CharField(max_length=20, blank=True)
    depot = models.CharField(max_length=40, blank=True)
    dealer_code = models.CharField(max_length=20, db_index=True)
    dealer_name = models.CharField(max_length=200, blank=True)
    invoice_no = models.CharField(max_length=30, db_index=True)
    invoice_date = models.DateField(null=True, blank=True)
    total_value = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    return_type = models.CharField(max_length=60, blank=True)
    material_receipt_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=60, blank=True)
    pending_at = models.CharField(max_length=300, blank=True)
    requested_by = models.CharField(max_length=120, blank=True)
    requested_date = models.DateField(null=True, blank=True)
    admin_by = models.CharField(max_length=120, blank=True)
    admin_date = models.DateField(null=True, blank=True)
    rsm_by = models.CharField(max_length=120, blank=True)
    rsm_date = models.DateField(null=True, blank=True)
    rcm_by = models.CharField(max_length=120, blank=True)
    rcm_date = models.DateField(null=True, blank=True)
    rma_no = models.CharField(max_length=30, blank=True, db_index=True)
    rma_date = models.DateField(null=True, blank=True)
    cn_no = models.CharField(max_length=40, blank=True)
    cn_date = models.DateField(null=True, blank=True)
    cn_value = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    oracle_status = models.CharField(max_length=60, blank=True)
    oracle_error = models.CharField(max_length=300, blank=True)
    stage_since = models.DateField(null=True, blank=True)         # when the current status / Pending At began
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.ForeignKey("core.ImportBatch", null=True, on_delete=models.PROTECT, related_name="+")
    bill_to_site = models.CharField(max_length=20, blank=True)    # chosen when the dealer has several territories
    # go-live 22-Oct-2026: requests already processed / rejected before go-live are kept for reference only
    tracked = models.BooleanField(default=True)
    # closure (by the app, after reconciliation and the godown's physical check)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    close_kind = models.CharField(max_length=12, blank=True)       # RECONCILED / ACCEPTED / REJECTED
    close_note = models.CharField(max_length=500, blank=True)
    verified_by = models.CharField(max_length=120, blank=True)    # godown person who checked the material
    verified_on = models.DateField(null=True, blank=True)
    recon = models.JSONField(default=dict, blank=True)              # result kept at closure

    class Meta:
        db_table = "rma_request"
        ordering = ["-requested_date", "-request_id"]

    def __str__(self) -> str:
        return f"RMA request {self.request_id}"


class RequestLine(models.Model):
    request = models.ForeignKey(Request, on_delete=models.CASCADE, related_name="lines")
    sku = models.CharField(max_length=20)
    description = models.CharField(max_length=200, blank=True)
    quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    value = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    volume = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True)
    status = models.CharField(max_length=60, blank=True)
    sub_inventory = models.CharField(max_length=30, blank=True)
    locator = models.CharField(max_length=60, blank=True)

    class Meta:
        db_table = "rma_request_line"
        ordering = ["id"]


class StageEvent(models.Model):
    """Status / Pending At as seen on each upload - the approval trail, and the trigger for the 'new pending' mail."""
    request = models.ForeignKey(Request, on_delete=models.CASCADE, related_name="events")
    status = models.CharField(max_length=60, blank=True)
    pending_at = models.CharField(max_length=300, blank=True)
    seen_at = models.DateTimeField(auto_now_add=True)
    batch = models.ForeignKey("core.ImportBatch", null=True, on_delete=models.PROTECT, related_name="+")
    notified = models.BooleanField(default=False)   # 'new approval pending' mail queued (or not needed)

    class Meta:
        db_table = "rma_stage_event"
        ordering = ["id"]


class CnLine(models.Model):
    """RMA CN INV Report line. Return Order = the portal RMA No. A re-upload replaces the lines of each CN in it."""
    rma_no = models.CharField(max_length=30, db_index=True)
    cust_code = models.CharField(max_length=20, blank=True)
    cust_name = models.CharField(max_length=200, blank=True)
    bill_to = models.CharField(max_length=20, blank=True)
    cn_no = models.CharField(max_length=40, db_index=True)
    cn_date = models.DateField(null=True, blank=True)
    inv_no = models.CharField(max_length=40, blank=True)
    inv_date = models.DateField(null=True, blank=True)
    reason = models.CharField(max_length=120, blank=True)
    sku = models.CharField(max_length=20)
    pack = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    uom = models.CharField(max_length=10, blank=True)
    cn_nop = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    inv_nop = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    return_ltr = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True)
    return_kg = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True)
    cn_rate = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True)
    batch = models.ForeignKey("core.ImportBatch", on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "rma_cn_line"
        ordering = ["cn_no", "id"]


# ---------------------------------------------------------------- GRN (physical receipt)


class Grn(models.Model):
    """Oracle Goods Return Note raised by the clerk when the returned material arrives (Receipt No RTN/2627/287)."""
    grn_no = models.CharField(max_length=30, unique=True)
    grn_date = models.DateField()
    receipt_time = models.CharField(max_length=10, blank=True)
    transporter = models.CharField(max_length=120, blank=True)
    vehicle_no = models.CharField(max_length=60, blank=True)
    dealer_code = models.CharField(max_length=20, db_index=True)
    dealer_name = models.CharField(max_length=200, blank=True)
    site_code = models.CharField(max_length=20, blank=True)       # the "(1792722)" after the dealer name
    document = models.ForeignKey("core.Document", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    gate_entry_no = models.CharField(max_length=40, blank=True)
    gate_entry_date = models.DateField(null=True, blank=True)
    transport_mode = models.CharField(max_length=8, choices=TransportMode.choices, blank=True)
    lr_no = models.CharField(max_length=40, blank=True)
    lr_date = models.DateField(null=True, blank=True)
    freight = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    remarks = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "rma_grn"
        ordering = ["-grn_date", "-grn_no"]

    def __str__(self) -> str:
        return self.grn_no


class GrnLine(models.Model):
    """One SKU of one dealer invoice on the GRN; linked to the portal request raised for that invoice."""
    grn = models.ForeignKey(Grn, on_delete=models.CASCADE, related_name="lines")
    invoice_no = models.CharField(max_length=30)
    invoice_date = models.DateField(null=True, blank=True)
    sku = models.CharField(max_length=20)
    description = models.CharField(max_length=200, blank=True)
    inv_nop = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    ret_nop = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    drums = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cartons = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    bags = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    loose = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    weight = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    condition = models.CharField(max_length=8, choices=Condition.choices, blank=True)
    request = models.ForeignKey(Request, null=True, blank=True, on_delete=models.SET_NULL, related_name="grn_lines")

    class Meta:
        db_table = "rma_grn_line"
        ordering = ["id"]


class IrrLink(models.Model):
    """The Oracle 'Rma Receipt' TRX in the IRR that received this request's material."""
    request = models.ForeignKey(Request, on_delete=models.CASCADE, related_name="irr_links")
    trx_number = models.CharField(max_length=80, unique=True)
    entered_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    auto = models.BooleanField(default=False)     # linked by the engine (one exact match)
    entered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "rma_irr_link"
        ordering = ["id"]


# ---------------------------------------------------------------- mails


class ApproverMail(models.Model):
    """Approval-pending mail to one approver: the 09:30 digest (once a day) or a same-day 'new pending' mail."""
    approver = models.ForeignKey(Approver, on_delete=models.PROTECT, related_name="mails")   # first of the group
    group = models.CharField(max_length=200, default="")   # approver ids of the Pending At group, e.g. "3,7"
    kind = models.CharField(max_length=8)          # DIGEST / NEW / URGENT (sent by hand, 22-Oct-2026)
    day = models.DateField()
    requests = models.JSONField(default=list)
    mail = models.ForeignKey("iw.MailMessage", null=True, on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(max_length=300, blank=True)           # URGENT: why it is urgent
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "rma_approver_mail"
        constraints = [models.UniqueConstraint(fields=["group", "day"], condition=models.Q(kind="DIGEST"),
                                               name="uq_rma_digest_group_day")]
