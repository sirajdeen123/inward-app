from __future__ import annotations

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from inward.core.models import Depot, DepotOperator


def depot_info() -> dict:
    """Depot code/name and the CFA operating it today (SoT 1, 7.4); cached for 5 minutes."""
    code = settings.INWARD["DEPOT_CODE"]
    info = cache.get(f"depot-info-{code}")
    if info is None:
        today = timezone.localdate()
        depot = Depot.objects.filter(code=code).first()
        op = (DepotOperator.objects.filter(depot_id=code, effective_from__lte=today)
              .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today))
              .order_by("-effective_from").first())
        info = {"code": code, "name": depot.name if depot else "", "cfa": op.cfa_name if op else "",
                "company": depot.company_name if depot else ""}
        cache.set(f"depot-info-{code}", info, 300)
    return info


def depot(request) -> dict:
    ctx = {"ADMIN_URL": settings.INWARD["ADMIN_URL"], "TEST_MODE": settings.INWARD["MODE"] == "TEST"}
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        try:
            from inward.accounts.approvals import pending_for
            ctx["approvals_pending"] = pending_for(user).count()
            from inward.accounts.services import modes_for
            ctx["can_cash"] = "GRANT" in modes_for(user, "CASH_REGISTER")
            ctx["can_damage"] = "GRANT" in modes_for(user, "DAMAGE_REPORT")
            ctx["can_rma"] = "GRANT" in modes_for(user, "RMA_WORK")
            ctx["cash_only"] = ctx["can_cash"] and "GRANT" not in modes_for(user, "SEARCH_REPORTS")
            if "GRANT" in modes_for(user, "PACKAGING_REVIEW"):
                from inward.core.models import PackagingChange
                ctx["packaging_review_pending"] = PackagingChange.objects.filter(review="PENDING").count()
            if "GRANT" in modes_for(user, "SEARCH_REPORTS"):
                from inward.iw.ops import mail_banner
                ctx["mail_banner"] = mail_banner()
            if "GRANT" in modes_for(user, "EMAIL_VERIFY"):
                ctx["can_notify"] = True
                from inward.iw.comms import unverified_emails
                ctx["emails_unverified"] = len(unverified_emails())
        except Exception:  # noqa: BLE001
            ctx["approvals_pending"] = 0
    try:
        ctx["depot"] = depot_info()
    except Exception:  # noqa: BLE001 - the banner must never break a page (e.g. before migrations)
        ctx["depot"] = {"code": settings.INWARD["DEPOT_CODE"], "name": "", "cfa": "", "company": ""}
    return ctx
