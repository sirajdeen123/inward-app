"""Optimistic concurrency: a stale edit is rejected, never silently overwritten (SoT 14.1)."""
from __future__ import annotations

from django.db.models import F, Model


class StaleEdit(Exception):
    """Someone else saved this record after it was opened; reload and try again."""


def update_versioned(instance: Model, **changes) -> None:
    """Apply `changes` only if the row still has the version the user started from."""
    model = type(instance)
    rows = model.objects.filter(pk=instance.pk, version=instance.version).update(
        **changes, version=F("version") + 1)
    if rows == 0:
        raise StaleEdit(f"{model.__name__} {instance.pk} was changed by someone else")
    for field, value in changes.items():
        setattr(instance, field, value)
    instance.version += 1
