from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserCreationForm

from inward.accounts.models import ApprovalRequest, Role, RolePermission, TrustedDevice, User, UserRole


class UserRoleInline(admin.TabularInline):
    model = UserRole
    extra = 0


class InwardUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "full_name", "email")

    def clean_username(self):
        name = (self.cleaned_data.get("username") or "").strip().lower()
        if User.objects.filter(username=name).exists():
            raise forms.ValidationError("A user with this name already exists (user names ignore capitals).")
        return name


@admin.register(User)
class InwardUserAdmin(UserAdmin):
    add_form = InwardUserCreationForm
    list_display = ("username", "full_name", "email", "roles", "is_active", "is_staff", "last_login")

    @admin.display(description="Roles")
    def roles(self, obj):
        codes = [r.role_id for r in obj.user_roles.all()]
        return ", ".join(codes) or "NO ROLE - cannot use the app"

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        from inward.accounts.services import _sync_staff_flag
        _sync_staff_flag(form.instance)   # managers / administrators get the Masters & users screen
    fieldsets = ((None, {"fields": ("username", "password")}),
                 ("Person", {"fields": ("full_name", "email")}),
                 ("Status", {"fields": ("is_active", "is_staff", "is_break_glass")}))
    add_fieldsets = ((None, {"fields": ("username", "full_name", "email", "password1", "password2")}),)
    inlines = [UserRoleInline]

    def get_readonly_fields(self, request, obj=None):
        return () if request.user.is_superuser else ("is_break_glass",)


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
