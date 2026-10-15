"""Maker-checker effects: what happens when a request is approved (VOID, reopen, exempt, ...).

Each feature registers a function for its permission code; `decide_and_apply` approves or rejects
and runs the effect in the same transaction, so an approval never exists without its effect.
"""
from __future__ import annotations

from typing import Callable

from django.db import transaction

from inward.accounts.models import ApprovalRequest, RolePermission, User
from inward.accounts.services import decide

_EFFECTS: dict[str, Callable[[ApprovalRequest, User], None]] = {}


def register(permission: str, effect: Callable[[ApprovalRequest, User], None]) -> None:
    _EFFECTS[permission] = effect


@transaction.atomic
def decide_and_apply(approver: User, request_id: int, approve: bool, note: str = "") -> ApprovalRequest:
    req = decide(approver, request_id, approve, note)
    if approve and req.permission_id in _EFFECTS:
        _EFFECTS[req.permission_id](req, approver)
    return req


def pending_for(user: User):
    """Pending requests this user may decide (APPROVE mode), never their own."""
    perms = RolePermission.objects.filter(role__userrole__user=user, mode="APPROVE").values_list("permission_id",
                                                                                                flat=True)
    return (ApprovalRequest.objects.filter(status=ApprovalRequest.Status.PENDING, permission_id__in=list(perms))
            .exclude(requested_by=user).select_related("requested_by", "permission").order_by("created_at"))
