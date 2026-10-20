from django.contrib import admin

from inward.rma import models as m


@admin.register(m.Approver)
class ApproverAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "active", "redirect_to")
    search_fields = ("name", "email")


@admin.register(m.Territory)
class TerritoryAdmin(admin.ModelAdmin):
    list_display = ("territory_no", "sales_officer", "business_line", "email", "email_override")
