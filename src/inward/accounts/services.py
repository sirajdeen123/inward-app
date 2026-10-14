"""Permission checks, maker-checker approvals and user administration (SoT 12.1, 19).

Enforced here in the service layer on every action, never only by hiding buttons.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from inward.accounts.models import ApprovalRequest, Role, RolePermission, User, UserRole
from inward.core import audit
from inward.core.catalog import STAFF_ROLES


class PermissionDenied(Exception):
    pass


def modes_for(user: User, permission: str) -> set[str]:
    return set(RolePermission.objects.filter(
        permission_id=permission, role__userrole__user=user).values_list("mode", flat=True))


def role_codes(user: User) -> list[str]:
    return list(UserRole.objects.filter(user=user).values_list("role_id", flat=True))


def require(user: User, permission: str, mode: str = "GRANT") -> None:
    if not user.is_active or mode not in modes_for(user, permission):
        raise PermissionDenied(f"{permission} ({mode}) is not allowed for this user")


@transaction.atomic
def request_approval(user: User, permission: str, entity_type: str, entity_id, reason: str,
                     payload: dict | None = None) -> ApprovalRequest:
    if not reason.strip():
        raise ValueError("A reason is mandatory")
    if not modes_for(user, permission) & {"REQUEST", "GRANT", "APPROVE"}:
        raise PermissionDenied(f"{permission}: not allowed to request")
    req = ApprovalRequest.objects.create(permission_id=permission, entity_type=entity_type, entity_id=str(entity_id),
                                         reason=reason.strip(), payload=payload or {}, requested_by=user)
    audit.record("APPROVAL_REQUESTED", "approval_request", req.id, user=user,
                 after={"permission": permission, "entity": f"{entity_type}:{entity_id}", "reason": reason})
    return req


@transaction.atomic
def decide(approver: User, request_id: int, approve: bool, note: str = "") -> ApprovalRequest:
    req = ApprovalRequest.objects.select_for_update().get(pk=request_id)
    if req.status != ApprovalRequest.Status.PENDING:
        raise ValueError("Request already decided")
    if req.requested_by_id == approver.id:
        raise PermissionDenied("Nobody can approve their own request")
    require(approver, req.permission_id, "APPROVE")
    req.status = ApprovalRequest.Status.APPROVED if approve else ApprovalRequest.Status.REJECTED
    req.decided_by = approver
    req.decided_at = timezone.now()
    req.decision_note = note
    req.save()
    audit.record("APPROVAL_" + req.status, "approval_request", req.id, user=approver, after={"note": note})
    return req


def _sync_staff_flag(user: User) -> None:
    is_staff = UserRole.objects.filter(user=user, role_id__in=STAFF_ROLES).exists()
    if user.is_staff != is_staff:
        user.is_staff = is_staff
        user.save(update_fields=["is_staff"])


@transaction.atomic
def assign_role(actor: User | None, target: User, role_code: str) -> None:
    """actor=None only for the server-side operator command that creates the first users."""
    if actor is not None:
        if actor.pk == target.pk:
            raise PermissionDenied("Nobody can change their own roles")
        require(actor, "USER_ADMIN")
    role = Role.objects.get(pk=role_code)
    _, created = UserRole.objects.get_or_create(user=target, role=role)
    if created:
        _sync_staff_flag(target)
        audit.record("ROLE_ASSIGNED", "app_user", target.pk, user=actor, after={"role": role_code})


@transaction.atomic
def create_user(actor: User | None, *, username: str, full_name: str, email: str, password: str,
                role_code: str, break_glass: bool = False) -> User:
    from django.contrib.auth.password_validation import validate_password

    user = User(username=username.strip().lower(), full_name=full_name, email=email, is_break_glass=break_glass)
    validate_password(password, user)
    user.set_password(password)
    user.save()
    audit.record("USER_CREATED", "app_user", user.pk, user=actor,
                 after={"username": user.username, "role": role_code, "break_glass": break_glass})
    assign_role(actor, user, role_code)
    return user
