"""Clear the transaction data of a TEST environment (UAT) before a fresh end-to-end test.

Removes IWs and everything under them (links, checklists, SED, photos' links, MRAs, mails, approvals, unloading,
ledger, receipts, reconciliation, closures), SIT / IRR / Despatch imports, exceptions, report runs and the IW
counters. Keeps users, roles, two-factor devices, all masters (organisations and their e-mails, vendors,
transporters, senders, SKU packaging, rates, notification recipients) and the audit trail (append-only).
Stored files stay in the write-once document store. Refuses to run unless INWARD_MODE is TEST.

    python manage.py reset_uat            # shows what would be removed
    python manage.py reset_uat --yes      # removes it
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from inward.accounts.models import ApprovalRequest
from inward.core.models import DocumentLink, ExceptionEvent, ImportBatch, IwCounter
from inward.irr.models import IrrTransaction
from inward.iw import models as m
from inward.sit import models as s

ORDER = [  # children first
    m.InboundMail, m.SedApproval, m.MailRelease, m.ReportRun, m.MailMessage, m.IwClosure, m.ReconRun, m.SedTrx,
    m.ErpReceipt, m.SedPhoto, m.SedLine, m.TransporterLedgerEntry, m.UnloadingRecord, m.PackageCount,
    m.MraRevision, m.ChecklistSnapshot, m.DespatchRow, m.DespatchReport, m.OutboundShip, m.VendorLine,
    m.VendorConsignment, m.NonStockLine, m.NonStockRef, m.ColourBankItem, m.ShipLinkLine, m.ShipLink, m.Iw,
    IrrTransaction, s.SitChangeEvent, s.SitIssue, s.SitLine, s.SitShip, s.SitImport,
]


class Command(BaseCommand):
    help = "Clear transaction data in a TEST (UAT) environment; masters, users and the audit trail are kept."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="really delete")

    def handle(self, *args, yes=False, **opts):
        if settings.INWARD["MODE"] != "TEST":
            raise CommandError("Refused: this environment is not in TEST mode.")
        extra = [("approval requests (IW)", ApprovalRequest.objects.filter(entity_type="iw")),
                 ("document links (IW / SED)", DocumentLink.objects.filter(entity_type__in=["iw", "sed_line"])),
                 ("import batches (SIT / IRR)", ImportBatch.objects.filter(kind__in=["SIT", "IRR", "DESPATCH"])),
                 ("exceptions", ExceptionEvent.objects.all()), ("IW counters", IwCounter.objects.all())]
        for model in ORDER:
            self.stdout.write(f"{model._meta.db_table:28} {model.objects.count():>8}")
        for label, qs in extra:
            self.stdout.write(f"{label:28} {qs.count():>8}")
        if not yes:
            self.stdout.write("Dry run - add --yes to delete.")
            return
        with transaction.atomic():
            m.MailMessage.attachments.through.objects.all().delete()
            for model in ORDER:
                model.objects.all().delete()
            for _, qs in extra:
                qs.delete()
        self.stdout.write(self.style.SUCCESS("UAT transaction data cleared; numbering restarts at IW-<FY>-000001."))
