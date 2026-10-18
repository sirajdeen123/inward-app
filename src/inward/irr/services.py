"""IRR import (SoT 8.2): validate -> normalise -> merge into history, atomically. Identical file -> Already
imported. Each (Type, TRX Number, TRX Date) group is compared with the history as a multiset: new groups and
later rows of the same day are added, identical / subset groups skipped, conflicting groups held for review.
An IRR upload never closes anything; reconciliation is started by the clerk."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from django.db import transaction

from inward.accounts.models import User
from inward.accounts.services import require
from inward.core import audit, documents
from inward.core.models import ImportBatch
from inward.domain.irr_merge import GroupAction, plan_merge
from inward.importers.common import file_sha256
from inward.importers.irr import IrrRow, parse_irr
from inward.irr.models import IrrTransaction

Q6 = Decimal("0.000001")


class AlreadyImported(Exception):
    def __init__(self, batch: ImportBatch):
        super().__init__(f"Already imported on {batch.uploaded_at:%d-%b-%Y %I:%M %p} ({batch.filename}).")
        self.batch = batch


def _d(v):
    return None if v is None else Decimal(v).quantize(Q6).normalize()


def _norm(r: IrrRow) -> IrrRow:
    """Same text lengths and numeric form as rows read back from the database, so multisets compare exactly."""
    return replace(r, no_of_pack=_d(r.no_of_pack), volume_ltr=_d(r.volume_ltr), weight_kg=_d(r.weight_kg),
                   source_locator=r.source_locator[:60], dest_locator=r.dest_locator[:60], lot_number=r.lot_number[:60],
                   reason_code=r.reason_code[:80], reason_description=r.reason_description[:120],
                   shipping_id=r.shipping_id[:30], item_description=r.item_description[:200], sku=r.sku[:20],
                   transaction_type=r.transaction_type[:20])


def _row(t: IrrTransaction) -> IrrRow:
    return IrrRow(type=t.type, trx_number=t.trx_number, trx_date=t.trx_date, source_subinv=t.source_subinv,
                  lot_number=t.lot_number, source_locator=t.source_locator, dest_subinv=t.dest_subinv,
                  dest_locator=t.dest_locator, reason_code=t.reason_code, reason_description=t.reason_description,
                  shipping_id_date=t.shipping_id_date, shipping_id=t.shipping_id, item_description=t.item_description,
                  sku=t.sku, no_of_pack=_d(t.no_of_pack), volume_ltr=_d(t.volume_ltr), weight_kg=_d(t.weight_kg),
                  transaction_type=t.transaction_type)


@transaction.atomic
def import_irr(user: User, path: Path, filename: str) -> ImportBatch:
    require(user, "IMPORT_RECONCILE")
    sha = file_sha256(path)
    existing = ImportBatch.objects.filter(kind="IRR", sha256=sha).first()
    if existing:
        raise AlreadyImported(existing)
    parsed = parse_irr(path)
    incoming: dict = {}
    for r in parsed.rows:
        r = _norm(r)
        incoming.setdefault(r.group_key, Counter())[r] += 1
    history: dict = {}
    for t in IrrTransaction.objects.filter(trx_date__in={k[2] for k in incoming}):
        r = _row(t)
        if r.group_key in incoming:
            history.setdefault(r.group_key, Counter())[r] += 1
    plan = plan_merge(history, incoming)
    doc = documents.store(Path(path).read_bytes(), filename=filename, content_type="application/vnd.ms-excel",
                          doc_type="IRR_REPORT", user=user)
    review = [{"type": k[0], "trx": k[1], "date": k[2].isoformat()} for k, a in plan.actions.items()
              if a is GroupAction.REVIEW]
    batch = ImportBatch.objects.create(
        kind="IRR", source="MANUAL", filename=filename, sha256=sha, document=doc, uploaded_by=user,
        window_from=parsed.report_from, window_to=parsed.report_to,
        row_count=len(parsed.rows) + len(parsed.issues), accepted_count=len(plan.rows_to_add),
        issue_count=len(parsed.issues), status="REVIEW" if review else "ACCEPTED",
        summary={"new_groups": plan.count(GroupAction.NEW), "extended": plan.count(GroupAction.EXTENDED),
                 "identical": plan.count(GroupAction.IDENTICAL), "subset": plan.count(GroupAction.SUBSET),
                 "review": review[:200], "review_count": len(review),
                 "last_day_possibly_incomplete": parsed.last_day_possibly_incomplete,
                 "run_at": parsed.run_at.isoformat() if parsed.run_at else None,
                 "issues": [{"row": i.row_no, "message": i.message} for i in parsed.issues[:100]]})
    IrrTransaction.objects.bulk_create([
        IrrTransaction(batch=batch, type=r.type, type_key=r.type.upper(), trx_number=r.trx_number,
                       trx_date=r.trx_date, receipt_no=r.receipt_no or "", trx_reference=(r.trx_reference or "")[:60],
                       source_subinv=r.source_subinv, source_locator=r.source_locator, dest_subinv=r.dest_subinv,
                       dest_locator=r.dest_locator, lot_number=r.lot_number, reason_code=r.reason_code,
                       reason_description=r.reason_description, shipping_id_date=r.shipping_id_date,
                       shipping_id=r.shipping_id, item_description=r.item_description, sku=r.sku,
                       no_of_pack=r.no_of_pack, volume_ltr=r.volume_ltr, weight_kg=r.weight_kg,
                       transaction_type=r.transaction_type)
        for r in plan.rows_to_add], batch_size=2000)
    audit.record("IRR_IMPORTED", "import_batch", batch.pk, user=user,
                 after={"file": filename, "rows_added": len(plan.rows_to_add),
                        **{k: v for k, v in batch.summary.items() if k not in ("review", "issues")}})
    return batch
