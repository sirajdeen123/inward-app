"""Import source organisations from Excel (21-Oct-2026).

Column A 'Source Name' as '001:Kolkata' (code:name, the SIT format). Organisations already in the app are left as
they are (reported as skipped); new ones are added as verified masters. Optional columns 'To Email' / 'Cc Email'
(comma separated) add MRA e-mails as Verified - for mapping the e-mails later with the same command.

    inward-manage <env> import_orgs "/path/Source Org.xlsx" [--apply]
Without --apply nothing is saved (dry run).
"""
from __future__ import annotations

import re

import openpyxl
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from inward.core import audit
from inward.core.models import EmailStatus, Organisation, OrganisationEmail
from inward.domain.normalize import clean_text, parse_org

CODE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,9}$")
EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def read(path) -> list[tuple[str, str, list[str], list[str]]]:
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise CommandError("The file is empty.")
    head = [clean_text(h).lower() for h in rows[0]]
    if "source name" not in head:
        raise CommandError("Column 'Source Name' not found in the first row.")
    i_src = head.index("source name")
    i_to = head.index("to email") if "to email" in head else None
    i_cc = head.index("cc email") if "cc email" in head else None

    def emails(row, i):
        if i is None or i >= len(row) or not row[i]:
            return []
        out = [a for a in re.split(r"[\s,;]+", str(row[i]).strip().lower()) if a]
        bad = [a for a in out if not EMAIL.match(a)]
        if bad:
            raise CommandError(f"Not a valid e-mail: {', '.join(bad)} (row {row[i_src]}).")
        return out

    out = []
    for row in rows[1:]:
        if i_src >= len(row) or not clean_text(row[i_src]):
            continue
        code, name = parse_org(row[i_src])
        code = code.upper()
        if not CODE.match(code) or not name:
            raise CommandError(f"'{row[i_src]}' is not 'code:name'.")
        out.append((code, name, emails(row, i_to), emails(row, i_cc)))
    return out


def merge(rows):
    """A code listed twice: the factories appear as 'Hindupur - Mfg' and 'Hindupur - Mfg-CESA Reporting' - the name
    without 'CESA Reporting' (as on the SIT / Ship documents) is kept, e-mails of both are combined."""
    by: dict[str, tuple] = {}
    merged = []
    for code, name, to, cc in rows:
        if code not in by:
            by[code] = (code, name, to, cc)
            continue
        merged.append(code)
        old = by[code]
        cesa_old, cesa_new = "CESA REPORTING" in old[1].upper(), "CESA REPORTING" in name.upper()
        if cesa_old == cesa_new:
            raise CommandError(f"Code {code} twice in the file with different names: '{old[1]}' / '{name}'.")
        keep = name if cesa_old else old[1]
        by[code] = (code, keep, old[2] + [a for a in to if a not in old[2]], old[3] + [a for a in cc if a not in old[3]])
    return list(by.values()), merged


class Command(BaseCommand):
    help = "Import source organisations (and optional MRA e-mails) from Excel; existing ones are skipped."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--apply", action="store_true", help="save (default: dry run)")

    def handle(self, path, apply=False, **opts):
        from inward.sit.services import _org_type
        rows, merged = merge(read(path))
        codes = [r[0] for r in rows]
        known = set(Organisation.objects.filter(code__in=codes).values_list("code", flat=True))
        new = [r for r in rows if r[0] not in known]
        with transaction.atomic():
            Organisation.objects.bulk_create([Organisation(code=c, name=n, org_type=_org_type(n), verified=True)
                                              for c, n, _, _ in new])
            mails = 0
            for code, _, to, cc in rows:
                for kind, addrs in (("TO", to), ("CC", cc)):
                    for a in addrs:
                        _, made = OrganisationEmail.objects.get_or_create(
                            organisation_id=code, email=a, kind=kind, defaults={"status": EmailStatus.VERIFIED})
                        mails += made
            audit.record("ORGANISATIONS_IMPORTED", "organisation", "import", after={
                "file": str(path).replace("\\", "/").rsplit("/", 1)[-1], "in_file": len(rows), "merged_cesa": merged, "added": len(new),
                "skipped_existing": len(known), "emails_added": mails, "codes_added": [r[0] for r in new]})
            msg = (f"{len(rows)} organisations in the file ({len(merged)} listed twice as '... CESA Reporting', kept "
                   f"once): {len(new)} new, {len(known)} already in the app (skipped), {mails} e-mail(s) added.")
            if not apply:
                transaction.set_rollback(True)
                msg = "DRY RUN (nothing saved) - " + msg
        self.stdout.write(msg)
