"""Roles & permissions managed by the CFA Manager in the app (22-Oct-2026)."""
import pytest
from django.urls import reverse

from inward.accounts import roles as R
from inward.accounts.models import RolePermission, UserRole
from inward.accounts.services import PermissionDenied, modes_for
from inward.core.models import AuditEvent
from tests.helpers import verified_client

pytestmark = pytest.mark.django_db


@pytest.fixture
def people(seeded, make_user):
    return {"cfa": make_user("cfa_r", "CFA_MANAGER"), "sup": make_user("sup_r", "SYSTEM_SUPERVISOR"),
            "clerk": make_user("clerk_r", "DEPOT_CLERK")}


def test_cfa_assigns_and_removes_roles(people):
    cfa, sup = people["cfa"], people["sup"]
    assert R.set_user_roles(cfa, sup, {"SYSTEM_SUPERVISOR", "DEPOT_CLERK"})
    assert set(UserRole.objects.filter(user=sup).values_list("role_id", flat=True)) == {"SYSTEM_SUPERVISOR", "DEPOT_CLERK"}
    assert "GRANT" in modes_for(sup, "IW_CLOSE")
    R.set_user_roles(cfa, sup, {"SYSTEM_SUPERVISOR"})                      # a role can be taken away again
    assert "GRANT" not in modes_for(sup, "IW_CLOSE")
    with pytest.raises(PermissionDenied, match="own roles"):
        R.set_user_roles(cfa, cfa, {"DEPOT_CLERK"})
    with pytest.raises(R.RoleError, match="at least one role"):
        R.set_user_roles(cfa, sup, set())
    with pytest.raises(PermissionDenied):
        R.set_user_roles(people["clerk"], sup, {"DEPOT_CLERK"})             # a clerk cannot


def test_cfa_changes_the_matrix_with_guards(people):
    cfa, sup = people["cfa"], people["sup"]
    assert "GRANT" not in modes_for(sup, "IW_CLOSE")
    assert R.set_matrix(cfa, {("SYSTEM_SUPERVISOR", "IW_CLOSE"): "GRANT", ("DEPOT_CLERK", "IW_VOID"): "REQUEST+APPROVE"}) == 2
    assert "GRANT" in modes_for(sup, "IW_CLOSE")
    assert R.matrix()[("DEPOT_CLERK", "IW_VOID")] == "REQUEST+APPROVE"
    R.set_matrix(cfa, {("SYSTEM_SUPERVISOR", "IW_CLOSE"): ""})
    assert not RolePermission.objects.filter(role_id="SYSTEM_SUPERVISOR", permission_id="IW_CLOSE").exists()
    with pytest.raises(R.RoleError, match="must stay"):
        R.set_matrix(cfa, {("CFA_MANAGER", "USER_ADMIN"): ""})
    assert AuditEvent.objects.filter(action="ROLE_PERMISSION_CHANGED").count() == 3
    role = R.add_role(cfa, "", "Godown Supervisor")
    assert role.code == "GODOWN_SUPERVISOR" and not role.is_system


def test_screen(client, people):
    c = verified_client(client, people["cfa"])
    page = c.get(reverse("accounts:roles")).content.decode()
    assert "Users and their roles" in page and "What each role may do" in page and "Roles &amp; permissions" in page
    sup = people["sup"]
    c.post(reverse("accounts:roles"), {"action": "users", f"seen_{sup.pk}": "1", f"u{sup.pk}_SYSTEM_SUPERVISOR": "1",
                                       f"u{sup.pk}_DEPOT_CLERK": "1"})
    assert UserRole.objects.filter(user=sup, role_id="DEPOT_CLERK").exists()
    c.post(reverse("accounts:roles"), {"action": "matrix", "m_SYSTEM_SUPERVISOR_IW_CLOSE": "GRANT"})
    assert "GRANT" in modes_for(sup, "IW_CLOSE")
    client.logout()
    assert verified_client(client, people["clerk"]).get(reverse("accounts:roles")).status_code == 403
