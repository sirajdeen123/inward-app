"""Source organisations added or updated by any user from the IW screen (UAT 18-Oct-2026).

A new organisation is usable at once (IW source, MRA mail) but stays Unverified until the CFA Manager confirms it
on Verify e-mails; it is also listed in the 09:30 status mail. E-mails typed here are added as Unverified, like the
ones captured at MRA stage. The name / type of an organisation the manager already verified (or that came from the
SIT) is never changed here - only e-mails can be added to it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.db import transaction

from inward.accounts.models import User
from inward.accounts.services import require
from inward.core import audit
from inward.core.models import EmailStatus, Organisation, OrganisationEmail
from inward.domain.normalize import clean_text
from inward.iw.services import IwError

CODE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,9}$")


@dataclass
class Saved:
    org: Organisation
    created: bool
    emails: list[str] = field(default_factory=list)
    note: str = ""


@transaction.atomic
def save_org(user: User, code: str, name: str, org_type: str = "", emails: str = "") -> Saved:
    from inward.iw.comms import _clean_emails
    require(user, "IW_EDIT")
    code = clean_text(code).upper().replace(" ", "")
    name = clean_text(name).upper()
    if not CODE.match(code):
        raise IwError("Organisation code: letters / digits as on the Ship document, e.g. 390 or 094.")
    kind = org_type if org_type in Organisation.OrgType.values else Organisation.OrgType.OTHER
    addresses = _clean_emails(emails)
    if emails.strip() and not addresses:
        raise IwError("E-mail address not valid.")
    org = Organisation.objects.select_for_update().filter(pk=code).first()
    created, note = org is None, ""
    if created:
        if not name:
            raise IwError("Enter the organisation name.")
        org = Organisation.objects.create(code=code, name=name, org_type=kind, verified=False, created_by=user)
        audit.record("ORGANISATION_CREATED", "organisation", code, user=user,
                     after={"name": name, "type": kind, "verified": False})
    elif name and (name != org.name or kind != org.org_type):
        if org.verified:
            note = f"Name / type of {org} are kept - ask the CFA Manager to change a verified organisation."
        else:
            before = {"name": org.name, "type": org.org_type}
            org.name, org.org_type = name, kind
            org.save(update_fields=["name", "org_type"])
            audit.record("ORGANISATION_UPDATED", "organisation", code, user=user, before=before,
                         after={"name": name, "type": kind})
    added = []
    for addr in addresses:
        _, new = OrganisationEmail.objects.get_or_create(organisation=org, email=addr, kind="TO",
                                                         defaults={"status": EmailStatus.UNVERIFIED, "entered_by": user})
        if new:
            added.append(addr)
            audit.record("EMAIL_CAPTURED", "org", code, user=user, after={"email": addr, "status": "UNVERIFIED"})
    return Saved(org, created, added, note)


@transaction.atomic
def verify_org(user: User, code: str, corrected_name: str = "") -> Organisation:
    require(user, "EMAIL_VERIFY")
    org = Organisation.objects.select_for_update().get(pk=code)
    before = {"name": org.name, "verified": org.verified}
    if clean_text(corrected_name):
        org.name = clean_text(corrected_name).upper()
    org.verified = True
    org.save(update_fields=["name", "verified"])
    audit.record("ORGANISATION_VERIFIED", "organisation", code, user=user, before=before,
                 after={"name": org.name, "verified": True})
    return org
