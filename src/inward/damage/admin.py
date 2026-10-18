from django.contrib import admin

from inward.damage.models import DamageCategory


@admin.register(DamageCategory)
class DamageCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "photo_required", "remarks_required", "active", "sort_order", "created_by")
    readonly_fields = ("code", "created_by", "created_at")
