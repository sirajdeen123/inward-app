"""Co-loaded consignments (decision 22-Oct-2026).

One truck can carry two (or more) consignments - each with its own source organisation and its own LR, which the
transporter treats as separate consignments. Each consignment is its own IW (its own MRA, SED, approval, ERP and
closure stay exactly as before); the IWs of the same truck are linked to the first one (the lead) so that the
physical work is done once:
- ONE unloading checklist for the whole truck (generated from any of them, the same content on each);
- the unloading charge for the whole truck is recorded once, on the lead IW; the others follow its position;
- package counts for each MRA stay per IW (each source acknowledges its own packages).
An IW that is not co-loaded behaves exactly as before.
"""
from __future__ import annotations

from inward.iw.models import Iw

LIVE_EXCLUDE = (Iw.Status.DRAFT, Iw.Status.VOID)


def lead_of(iw: Iw) -> Iw:
    return iw.coload_lead if iw.coload_lead_id else iw


def members(iw: Iw) -> list[Iw]:
    """All live IWs of the truck, the lead first ([iw] when the IW is not co-loaded)."""
    lead = lead_of(iw)
    group = [lead] + list(Iw.objects.filter(coload_lead=lead).exclude(status__in=LIVE_EXCLUDE).order_by("created_at", "pk"))
    group = [x for x in group if x.status not in LIVE_EXCLUDE] or [iw]
    if iw.pk not in {x.pk for x in group}:
        group.append(iw)
    return group


def is_group(iw: Iw) -> bool:
    return len(members(iw)) > 1


def charge_iw(iw: Iw) -> Iw:
    """The IW that carries the unloading charge: the lead for the whole truck - unless the truck is set to 'separately
    on each IW' or the lead was voided (then each IW its own)."""
    if iw.coload_lead_id and iw.coload_lead.status not in LIVE_EXCLUDE and not iw.coload_lead.coload_charge_each:
        return iw.coload_lead
    return iw


def charge_each(iw: Iw) -> bool:
    return lead_of(iw).coload_charge_each


def set_charge_each(user, iw_pk: int, each: bool) -> Iw:
    """Choose how a co-loaded truck's unloading is charged (22-Oct-2026): once for the truck on the first IW, or
    separately on each IW. Locked once any unloading charge of the truck is recorded."""
    from django.db import transaction

    from inward.accounts.services import require
    from inward.core import audit
    from inward.iw.models import UnloadingRecord
    from inward.iw.services import IwError
    require(user, "UNLOADING_RECORD")
    with transaction.atomic():
        lead = lead_of(Iw.objects.get(pk=iw_pk))
        lead = Iw.objects.select_for_update().get(pk=lead.pk)
        group = members(lead)
        if len(group) < 2:
            raise IwError("This IW is not on a co-loaded truck.")
        if lead.coload_charge_each == each:
            return lead
        done = UnloadingRecord.objects.filter(iw__in=group).select_related("iw").first()
        if done:
            raise IwError(f"The unloading charge is already recorded on {done.iw} - the charging choice can no longer "
                          "be changed.")
        lead.coload_charge_each = each
        lead.save(update_fields=["coload_charge_each", "updated_at"])
        audit.record("COLOAD_CHARGE_MODE", "iw", lead.pk, user=user, iw_id=lead.pk,
                     after={"truck": [str(m) for m in group],
                            "charge": "separately on each IW" if each else "once for the truck on " + str(lead)})
        return lead
