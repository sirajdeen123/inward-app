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
    """The IW that carries the truck's unloading charge: the lead, unless the lead was voided (then each IW its own)."""
    if iw.coload_lead_id and iw.coload_lead.status not in LIVE_EXCLUDE:
        return iw.coload_lead
    return iw
