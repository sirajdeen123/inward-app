"""Idempotent reference-data seed. Never overwrites what the CFA Manager has changed."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import transaction

from inward.accounts.models import Permission, Role, RolePermission
from inward.core import audit
from inward.core.catalog import (
    BUSINESS_LINE_WAREHOUSE, DEFAULT_RATES, FEATURE_FLAGS, LOAD_TYPES, MATRIX, NOTIFICATION_PURPOSES, PACKAGE_TYPES,
    PERMISSIONS, ROLES,
)
from inward.core.models import (
    BusinessLineWarehouse, Depot, DepotOperator, FeatureFlag, LoadType, NotificationRecipient, PackageType, RateMaster,
)


@transaction.atomic
def seed_reference_data(go_live: date, depot_code: str = "035", depot_name: str = "Coimbatore",
                        cfa_name: str = "RAINBOW LOGISTICS") -> None:
    for code, name in ROLES.items():
        Role.objects.update_or_create(code=code, defaults={"name": name, "is_system": True})
    for code, desc in PERMISSIONS.items():
        Permission.objects.update_or_create(code=code, defaults={"description": desc})
    for perm, roles in MATRIX.items():  # adds missing defaults; never removes manager edits
        if not RolePermission.objects.filter(permission_id=perm).exists():
            for role, mode in roles.items():
                RolePermission.objects.create(role_id=role, permission_id=perm, mode=mode)
    for i, (code, name, sit, outbound, factory, vendor, non_stock) in enumerate(LOAD_TYPES, start=1):
        LoadType.objects.update_or_create(code=code, defaults=dict(
            name=name, sort_order=i, has_inbound_sit=sit, has_outbound=outbound, is_direct_factory=factory,
            is_vendor=vendor, is_non_stock=non_stock))
    for code, name, prio in PACKAGE_TYPES:
        PackageType.objects.update_or_create(code=code, defaults={"name": name, "sort_priority": prio})
    for line, warehouse in BUSINESS_LINE_WAREHOUSE.items():
        BusinessLineWarehouse.objects.get_or_create(business_line=line, defaults={"warehouse": warehouse})
    for key, (value, desc) in FEATURE_FLAGS.items():
        FeatureFlag.objects.get_or_create(key=key, defaults={"value": {"value": value}, "description": desc})
    for purpose, recipients in NOTIFICATION_PURPOSES.items():
        if not NotificationRecipient.objects.filter(purpose=purpose).exists():
            for label, kind in recipients:  # e-mail addresses are filled in by the CFA Manager
                known = (NotificationRecipient.objects.filter(role_label=label).exclude(email="")
                         .values_list("email", flat=True).first() or "")   # same person on another list
                NotificationRecipient.objects.create(purpose=purpose, role_label=label, kind=kind, mandatory=True,
                                                     email=known)
    depot, _ = Depot.objects.get_or_create(code=depot_code, defaults={"name": depot_name})
    if not depot.operators.exists():
        DepotOperator.objects.create(depot=depot, cfa_name=cfa_name, cfa_short_name=cfa_name, effective_from=go_live)
    for rate_type, amount in DEFAULT_RATES.items():
        if not RateMaster.objects.filter(rate_type=rate_type).exists():
            RateMaster.objects.create(rate_type=rate_type, amount=Decimal(amount), effective_from=go_live)
    from inward.core.catalog import DAMAGE_CATEGORIES
    from inward.damage.models import DamageCategory
    for code, name, photo, remarks, order in DAMAGE_CATEGORIES:   # never overwrites the supervisor's edits
        DamageCategory.objects.get_or_create(code=code, defaults={"name": name, "photo_required": photo,
                                                                  "remarks_required": remarks, "sort_order": order})
    audit.record("SEED_REFERENCE_DATA", "system", "seed", go_live=go_live)
