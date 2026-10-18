"""Maker-checker effects: what happens when a request is approved (VOID, reopen, exempt, ...).

Each feature registers a function for its permission code; `decide_and_apply` approves or rejects
and runs the effect in the same transaction, so an approval never exists without its effect.
"""
from __future__ import annotations

from typing import Callable

from django.db import transaction
from django.utils import timezone

from inward.accounts.models import ApprovalRequest, RolePermission, User
from inward.accounts.services import PermissionDenied, decide, modes_for, request_approval
from inward.core import audit

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



DIRECT_NOTE = "Done directly by the approver (no higher approver)"


@transaction.atomic
def submit(user: User, permission: str, entity_type: str, entity_id, reason: str,
           payload: dict | None = None) -> ApprovalRequest:
    """Raise a maker-checker request. When the requester holds APPROVE for it (the CFA Manager) nobody could approve
    it - the database forbids self-approval - so the effect is applied directly and audited, as Exempt already is
    (UAT 18-Oct-2026). The returned request is then unsaved, with status APPROVED."""
    if "APPROVE" not in modes_for(user, permission):
        return request_approval(user, permission, entity_type, entity_id, reason, payload)
    if not reason.strip():
        raise ValueError("A reason is mandatory")
    req = ApprovalRequest(permission_id=permission, entity_type=entity_type, entity_id=str(entity_id),
                          reason=reason.strip(), payload=payload or {}, requested_by=user,
                          status=ApprovalRequest.Status.APPROVED, decision_note=DIRECT_NOTE)
    if permission in _EFFECTS:
        _EFFECTS[permission](req, user)
    audit.record("APPROVER_DIRECT_ACTION", entity_type, entity_id, user=user,
                 after={"permission": permission, "reason": reason.strip(), "note": DIRECT_NOTE},
                 **({"iw_id": int(entity_id)} if entity_type == "iw" else {}))
    return req


def own_pending(user: User):
    """The user's own requests still waiting, with whether they hold APPROVE for them."""
    out = []
    for r in (ApprovalRequest.objects.filter(status=ApprovalRequest.Status.PENDING, requested_by=user)
              .select_related("permission").order_by("created_at")):
        r.can_self_apply = "APPROVE" in modes_for(user, r.permission_id)
        out.append(r)
    return out


@transaction.atomic
def settle_own(user: User, request_id: int, apply: bool) -> ApprovalRequest:
    """The requester's own pending request: withdraw it, or - when they are themselves the approver - cancel it and
    apply the effect directly (the request raised before 18-Oct-2026 could never be approved)."""
    req = ApprovalRequest.objects.select_for_update().get(pk=request_id)
    if req.requested_by_id != user.pk or req.status != ApprovalRequest.Status.PENDING:
        raise PermissionDenied("Only your own pending request can be withdrawn or applied here.")
    if apply and "APPROVE" not in modes_for(user, req.permission_id):
        raise PermissionDenied("You are not the approver of this request.")
    req.status = ApprovalRequest.Status.CANCELLED
    req.decided_at = timezone.now()
    req.decision_note = DIRECT_NOTE if apply else "Withdrawn by the requester"
    req.save()
    audit.record("APPROVAL_CANCELLED", "approval_request", req.pk, user=user, after={"note": req.decision_note})
    if apply:
        submit(user, req.permission_id, req.entity_type, req.entity_id, req.reason, req.payload)
    return req
