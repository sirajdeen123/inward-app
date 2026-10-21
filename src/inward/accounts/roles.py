"""Roles & permissions, managed in the app by the CFA Manager (decision 22-Oct-2026).

Whoever holds USER_ADMIN (CFA Manager, Administrator) assigns roles to users and changes the role -> permission
matrix. Every change is audited. Guards against lock-out: nobody changes their own roles; the CFA Manager and
Administrator roles always keep USER_ADMIN; at least one active user keeps a role with USER_ADMIN.
"""
from __future__ import annotations

import re

from django.db import transaction

from inward.accounts.models import Permission, Role, RolePermission, User, UserRole
from inward.accounts.services import PermissionDenied, _sync_staff_flag, require
from inward.core import audit

# what one cell of the matrix can say (a role may hold REQUEST and APPROVE together)
CHOICES = [("", "-"), ("GRANT", "Do it"), ("REQUEST", "Ask (maker)"), ("APPROVE", "Approve (checker)"),
           ("REQUEST+APPROVE", "Ask + approve"), ("VIEW", "View only")]
VALID = {c for c, _ in CHOICES}
PROTECTED = {("CFA_MANAGER", "USER_ADMIN"), ("ADMINISTRATOR", "USER_ADMIN")}


class RoleError(ValueError):
    pass


def cell(modes: set[str]) -> str:
    """Set of modes -> the matrix choice ("" when none)."""
    if not modes:
        return ""
    if modes == {"REQUEST", "APPROVE"}:
        return "REQUEST+APPROVE"
    if len(modes) == 1:
        return next(iter(modes))
    return "+".join(sorted(modes))          # an unusual combination is shown as it is and left alone


def matrix() -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], set] = {}
    for role, perm, mode in RolePermission.objects.values_list("role_id", "permission_id", "mode"):
        out.setdefault((role, perm), set()).add(mode)
    return {k: cell(v) for k, v in out.items()}


def _admins_left(exclude_user: int | None = None, exclude_role: str | None = None) -> int:
    roles = set(RolePermission.objects.filter(permission_id="USER_ADMIN", mode="GRANT").values_list("role_id", flat=True))
    roles.discard(exclude_role)
    qs = UserRole.objects.filter(role_id__in=roles, user__is_active=True)
    if exclude_user:
        qs = qs.exclude(user_id=exclude_user)
    return qs.values("user_id").distinct().count()


@transaction.atomic
def set_matrix(actor: User, wanted: dict[tuple[str, str], str]) -> int:
    """wanted: (role, permission) -> choice. Only cells that differ are changed. Returns the number changed."""
    require(actor, "USER_ADMIN")
    now = matrix()
    changed = 0
    for (role, perm), choice in wanted.items():
        choice = choice or ""
        before = now.get((role, perm), "")
        if choice == before:
            continue
        if choice not in VALID:
            raise RoleError(f"{perm} / {role}: unknown choice '{choice}'.")
        if (role, perm) in PROTECTED and choice != "GRANT":
            raise RoleError(f"{perm} must stay 'Do it' for {role} - otherwise nobody could manage users and roles.")
        RolePermission.objects.filter(role_id=role, permission_id=perm).delete()
        for mode in (choice.split("+") if choice else []):
            RolePermission.objects.create(role_id=role, permission_id=perm, mode=mode)
        audit.record("ROLE_PERMISSION_CHANGED", "role_permission", f"{role}:{perm}", user=actor,
                     before={"modes": before or "none"}, after={"modes": choice or "none"})
        changed += 1
    return changed


@transaction.atomic
def set_user_roles(actor: User, target: User, codes: set[str]) -> bool:
    require(actor, "USER_ADMIN")
    if actor.pk == target.pk:
        raise PermissionDenied("Nobody can change their own roles - ask another manager / the Administrator.")
    known = set(Role.objects.values_list("code", flat=True))
    unknown = codes - known
    if unknown:
        raise RoleError(f"Unknown role(s): {', '.join(sorted(unknown))}.")
    current = set(UserRole.objects.filter(user=target).values_list("role_id", flat=True))
    if current == codes:
        return False
    if target.is_active and not codes:
        raise RoleError(f"{target.username}: an active user needs at least one role (switch the user off instead).")
    admin_roles = set(RolePermission.objects.filter(permission_id="USER_ADMIN", mode="GRANT").values_list("role_id", flat=True))
    if current & admin_roles and not codes & admin_roles and _admins_left(exclude_user=target.pk) == 0:
        raise RoleError("This is the last user who can manage users and roles - give the role to someone else first.")
    UserRole.objects.filter(user=target).exclude(role_id__in=codes).delete()
    for code in codes - current:
        UserRole.objects.create(user=target, role_id=code)
    _sync_staff_flag(target)
    audit.record("USER_ROLES_CHANGED", "app_user", target.pk, user=actor,
                 before={"roles": sorted(current)}, after={"roles": sorted(codes)})
    return True


@transaction.atomic
def add_role(actor: User, code: str, name: str) -> Role:
    """A new role starts with no permission - tick what it may do in the matrix."""
    require(actor, "USER_ADMIN")
    code = re.sub(r"[^A-Z0-9]+", "_", (code or name or "").strip().upper()).strip("_")[:40]
    name = (name or "").strip()[:80]
    if len(code) < 3 or len(name) < 3:
        raise RoleError("Enter the role name (at least 3 letters).")
    if Role.objects.filter(code=code).exists() or Role.objects.filter(name__iexact=name).exists():
        raise RoleError(f"The role {name} already exists.")
    role = Role.objects.create(code=code, name=name, is_system=False)
    audit.record("ROLE_CREATED", "role", code, user=actor, after={"name": name})
    return role


def permissions():
    return list(Permission.objects.order_by("code"))
