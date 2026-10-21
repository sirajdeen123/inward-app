"""Masters & users screen (Django admin) follows the app's roles (UAT 17-Oct-2026).

The admin checks Django model permissions, which no Inward user has - so a CFA Manager saw an empty screen. This
backend grants them from the role: CFA Manager and Administrator may view, add and change the masters and users;
the audit trail, documents and imports stay view-only (their admin classes refuse changes anyway). Nothing is ever
deleted from the admin - masters are switched off with their Active tick. The permission matrix (role permissions)
is changed on the app's Roles & permissions screen by the CFA Manager or the Administrator (22-Oct-2026).
"""
from __future__ import annotations

ADMIN_ROLES = {"CFA_MANAGER", "ADMINISTRATOR"}
APPS = {"core", "accounts", "damage"}
ADMINISTRATOR_ONLY: set[str] = set()   # 22-Oct-2026: the CFA Manager manages roles too (Roles & permissions screen)


def _roles(user) -> set[str]:
    if not getattr(user, "is_active", False) or not getattr(user, "is_staff", False):
        return set()
    cached = getattr(user, "_admin_roles", None)
    if cached is None:
        from inward.accounts.services import role_codes
        cached = user._admin_roles = set(role_codes(user)) & ADMIN_ROLES
    return cached


class RoleAdminBackend:
    """Authorisation only (authenticate() always returns None)."""

    def authenticate(self, request, **credentials):
        return None

    def has_perm(self, user_obj, perm, obj=None):
        roles = _roles(user_obj)
        if not roles or "." not in perm:
            return False
        app, codename = perm.split(".", 1)
        if app not in APPS or codename.startswith("delete_"):
            return False
        if perm in ADMINISTRATOR_ONLY:
            return "ADMINISTRATOR" in roles
        return True

    def has_module_perms(self, user_obj, app_label):
        return bool(_roles(user_obj)) and app_label in APPS
