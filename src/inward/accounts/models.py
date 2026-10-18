"""Users, roles, permission matrix, maker-checker approvals, trusted devices (SoT 12.1, 14.5)."""
from __future__ import annotations

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import F, Q


class User(AbstractUser):
    full_name = models.CharField(max_length=128)
    is_break_glass = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "app_user"

    def __str__(self) -> str:
        return self.full_name or self.username

    def save(self, *args, **kwargs):
        # user names are case-insensitive: sign-in lower-cases what is typed (UAT 18-Oct-2026: "RUBAN" could not sign in)
        self.username = (self.username or "").strip().lower()
        super().save(*args, **kwargs)


class Role(models.Model):
    code = models.CharField(max_length=40, primary_key=True)
    name = models.CharField(max_length=80)
    is_system = models.BooleanField(default=True)

    class Meta:
        db_table = "role"

    def __str__(self) -> str:
        return self.name


class Permission(models.Model):
    code = models.CharField(max_length=60, primary_key=True)
    description = models.CharField(max_length=200)

    class Meta:
        db_table = "permission"

    def __str__(self) -> str:
        return self.code


class RolePermission(models.Model):
    class Mode(models.TextChoices):
        GRANT = "GRANT"      # may do it
        REQUEST = "REQUEST"  # may ask (maker)
        APPROVE = "APPROVE"  # may decide requests (checker)
        VIEW = "VIEW"

    role = models.ForeignKey(Role, on_delete=models.CASCADE)
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE)
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.GRANT)

    class Meta:
        db_table = "role_permission"
        constraints = [models.UniqueConstraint(fields=["role", "permission", "mode"], name="uq_role_permission_mode")]


class UserRole(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="user_roles")
    role = models.ForeignKey(Role, on_delete=models.PROTECT)

    class Meta:
        db_table = "user_role"
        constraints = [models.UniqueConstraint(fields=["user", "role"], name="uq_user_role")]


class ApprovalRequest(models.Model):
    """Maker-checker requests: reopen, void, exempt unloading, master changes, 2FA reset."""

    class Status(models.TextChoices):
        PENDING = "PENDING"
        APPROVED = "APPROVED"
        REJECTED = "REJECTED"
        CANCELLED = "CANCELLED"

    permission = models.ForeignKey(Permission, on_delete=models.PROTECT)
    entity_type = models.CharField(max_length=40)
    entity_id = models.CharField(max_length=64)
    payload = models.JSONField(default=dict, blank=True)
    reason = models.TextField()
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)

    class Meta:
        db_table = "approval_request"
        constraints = [models.CheckConstraint(condition=Q(decided_by__isnull=True) | ~Q(decided_by=F("requested_by")),
                                              name="no_self_approval")]


class TrustedDevice(models.Model):
    """A browser that may skip the 2FA code for TRUSTED_DEVICE_DAYS; only a hash of the token is stored."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="trusted_devices")
    token_hash = models.CharField(max_length=64, unique=True)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked = models.BooleanField(default=False)

    class Meta:
        db_table = "trusted_device"
