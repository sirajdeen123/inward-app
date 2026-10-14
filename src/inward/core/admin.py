"""Django admin for masters (CFA Manager / Administrator only, at a non-default address)."""
from django.contrib import admin

from inward.core import models as m


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(m.AuditEvent)
class AuditEventAdmin(ReadOnlyAdmin):
    list_display = ("occurred_at", "action", "entity_type", "entity_id", "user")
    list_filter = ("action", "entity_type")
    search_fields = ("entity_id", "action")


@admin.register(m.Document)
class DocumentAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "doc_type", "original_filename", "revision", "sha256", "cloud_mirrored_at")
    list_filter = ("doc_type",)


@admin.register(m.ImportBatch)
class ImportBatchAdmin(ReadOnlyAdmin):
    list_display = ("uploaded_at", "kind", "source", "filename", "row_count", "status")
    list_filter = ("kind", "status")


class DepotOperatorInline(admin.TabularInline):
    model = m.DepotOperator
    extra = 0


@admin.register(m.Depot)
class DepotAdmin(admin.ModelAdmin):
    list_display = ("code", "name")
    inlines = [DepotOperatorInline]


class OrganisationEmailInline(admin.TabularInline):
    model = m.OrganisationEmail
    extra = 0
    readonly_fields = ("entered_by", "verified_by")


@admin.register(m.Organisation)
class OrganisationAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "org_type", "active")
    search_fields = ("code", "name")
    inlines = [OrganisationEmailInline]


@admin.register(m.Sender)
class SenderAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "status", "active")


@admin.register(m.NotificationRecipient)
class NotificationRecipientAdmin(admin.ModelAdmin):
    list_display = ("purpose", "role_label", "kind", "email", "mandatory", "active")
    list_filter = ("purpose",)


@admin.register(m.BusinessLineWarehouse)
class BusinessLineWarehouseAdmin(admin.ModelAdmin):
    list_display = ("business_line", "warehouse")


@admin.register(m.SkuPackaging)
class SkuPackagingAdmin(admin.ModelAdmin):
    list_display = ("sku", "floor_sku", "description", "business_line", "package_type", "units_per_package")
    search_fields = ("sku", "floor_sku", "description")


@admin.register(m.RateMaster)
class RateMasterAdmin(admin.ModelAdmin):
    list_display = ("rate_type", "amount", "effective_from", "effective_to")


for model, fields in [
    (m.Transporter, ("name", "verified", "active")),
    (m.Vendor, ("code", "name", "oracle_vendor_id", "active")),
    (m.PackageType, ("code", "name", "sort_priority")),
    (m.NonStockItem, ("description", "unit", "active")),
    (m.LoadType, ("code", "name", "sort_order", "active")),
    (m.FeatureFlag, ("key", "value", "description")),
    (m.ExceptionEvent, ("created_at", "code", "severity", "entity_type", "entity_id", "resolved_at")),
]:
    admin.site.register(model, type(f"{model.__name__}Admin", (admin.ModelAdmin,), {"list_display": fields}))
