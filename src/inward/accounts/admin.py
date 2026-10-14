from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from inward.accounts.models import ApprovalRequest, Role, RolePermission, TrustedDevice, User, UserRole


class UserRoleInline(admin.TabularInline):
    model = UserRole
    extra = 0


@admin.register(User)
class InwardUserAdmin(UserAdmin):
    list_display = ("username", "full_name", "email", "is_active", "is_staff", "last_login")
    fieldsets = ((None, {"fields": ("username", "password")}),
                 ("Person", {"fields": ("full_name", "email")}),
                 ("Status", {"fields": ("is_active", "is_staff", "is_break_glass")}))
    add_fieldsets = ((None, {"fields": ("username", "full_name", "email", "password1", "password2")}),)
    inlines = [UserRoleInline]


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("code", "name")


@admin.register(RolePermission)
class RolePermissionAdmin(admin.ModelAdmin):
    list_display = ("permission", "role", "mode")
    list_filter = ("role", "mode")


@admin.register(ApprovalRequest)
class ApprovalRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "permission", "entity_type", "entity_id", "requested_by", "status", "created_at")
    list_filter = ("status", "permission")
    readonly_fields = [f.name for f in ApprovalRequest._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(TrustedDevice)
class TrustedDeviceAdmin(admin.ModelAdmin):
    list_display = ("user", "created_at", "expires_at", "revoked")
    readonly_fields = ("user", "token_hash", "user_agent", "created_at", "expires_at")
