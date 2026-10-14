"""Append-only audit trail (SoT 1, 19). A database trigger rejects UPDATE / DELETE on audit_event."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from inward.core.models import AuditEvent


def jsonable(value):
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def record(action: str, entity_type: str, entity_id, *, user=None, before: dict | None = None,
           after: dict | None = None, iw_id: int | None = None, **context) -> AuditEvent:
    return AuditEvent.objects.create(
        action=action, entity_type=entity_type, entity_id=str(entity_id),
        user=user if getattr(user, "is_authenticated", False) else None, iw_id=iw_id,
        before=jsonable(before), after=jsonable(after), context=jsonable(context),
    )
