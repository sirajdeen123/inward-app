"""Screen 05 part B - IRR reconciliation (SoT 8.3 - 8.6, table 10). Started by the clerk; never closes anything.

Ship (normal receipts): IRR 'Int Req Intr Rcpt' rows whose TRX Number is '<Receipt No> / <Ship ID>' (Shipping ID
Number checked when present), aggregated by full SKU, against the expected quantity: the full document quantity,
or document - short - damage for a Direct Factory Ship with LSCRR applied. LSCRR excess: the Excess Receipt No
as 'Int Req Intr Rcpt <no> / <Ship ID>' or as a Miscellaneous Receipt with that TRX Number (decision 16-Oct-2026),
against the SED excess. Vendor: only through the PO Receipt No ('Po Receipt' rows '<no> / <challan text>');
challan text differences are information only. SED memos: by TRX No - Short = Miscellaneous Issue, Excess =
Miscellaneous Receipt, Damage = Subinventory Transfer into DMG counted once from the Issue side (either number of
the Issue / Receipt pair may be entered). Results: FULL, PARTIAL, REVIEW, NOT_FOUND (or PENDING = nothing entered).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from inward.accounts.models import User
from inward.accounts.services import require
from inward.core import audit
from inward.core.models import FeatureFlag
from inward.irr.models import IrrTransaction
from inward.iw import erp
from inward.iw.models import ErpReceipt, Iw, ReconRun

FULL, PARTIAL, REVIEW, NOT_FOUND, PENDING, COVERED = "FULL", "PARTIAL", "REVIEW", "NOT_FOUND", "PENDING", "LSCRR_COVERED"
STOCK, PO, MISC_ISSUE, MISC_RECEIPT, SUBINV = ("INT REQ INTR RCPT", "PO RECEIPT", "MISCELLANEOUS ISSUE",
                                               "MISCELLANEOUS RECEIPT", "SUBINVENTORY TRANSFER")
MEMO_TYPE = {"SHORT": MISC_ISSUE, "EXCESS": MISC_RECEIPT, "DAMAGE": SUBINV}
ZERO = Decimal(0)


def _f(v: Decimal) -> str:
    return format(Decimal(v).normalize(), "f")


def _tolerance() -> Decimal:
    f = FeatureFlag.objects.filter(key="measure_tolerance").first()
    return Decimal(str((f.value or {}).get("value") or "0.001")) if f else Decimal("0.001")


@dataclass
class SkuLine:
    sku: str
    expected: Decimal = ZERO
    actual: Decimal = ZERO
    family: str = ""
    exp_measure: Decimal | None = None
    act_measure: Decimal = ZERO
    description: str = ""

    def as_dict(self) -> dict:
        return {"sku": self.sku, "description": self.description, "expected": _f(self.expected),
                "actual": _f(self.actual), "family": self.family,
                "exp_measure": None if self.exp_measure is None else _f(self.exp_measure.quantize(Decimal("0.001"))),
                "act_measure": _f(self.act_measure.quantize(Decimal("0.001")))}


@dataclass
class Result:
    track: str            # "Ship 52497520" / "Ship 52497520 excess" / "Vendor V54 challan ..." / "SED Ship ... short"
    kind: str             # SHIP / EXCESS / PO / SED
    numbers: list[str]
    status: str
    notes: list[str] = field(default_factory=list)
    lines: list[SkuLine] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in (FULL, COVERED)

    def as_dict(self) -> dict:
        return {"track": self.track, "kind": self.kind, "numbers": self.numbers, "status": self.status,
                "notes": self.notes, "lines": [ln.as_dict() for ln in self.lines]}


def _measure(row: IrrTransaction, family: str) -> Decimal:
    v = row.volume_ltr if family == "LTR" else row.weight_kg if family == "KG" else None
    return Decimal(v or 0)


def _classify(res: Result, expected: dict[str, SkuLine], rows, tol: Decimal) -> Result:
    for r in rows:
        ln = expected.get(r.sku)
        if ln is None:
            ln = expected[r.sku] = SkuLine(r.sku, description=r.item_description)
        ln.actual += r.no_of_pack
        ln.act_measure += _measure(r, ln.family)
    res.lines = sorted(expected.values(), key=lambda x: x.sku)
    over = [ln for ln in res.lines if ln.actual > ln.expected]
    under = [ln for ln in res.lines if ln.actual < ln.expected]
    measure_off = [ln for ln in res.lines if ln.exp_measure is not None and ln.actual == ln.expected
                   and abs(ln.act_measure - ln.exp_measure) > tol]
    if over:
        res.status = REVIEW
        res.notes += [f"{ln.sku}: IRR {_f(ln.actual)} vs expected {_f(ln.expected)}" + (" (SKU not expected)" if not ln.expected else "")
                      for ln in over]
    if measure_off:
        res.status = REVIEW
        res.notes += [f"{ln.sku}: {ln.family} {_f(ln.act_measure)} vs {_f(ln.exp_measure)}" for ln in measure_off]
    if res.status != REVIEW:
        res.status = PARTIAL if under else FULL
    return res

# ---------------------------------------------------------------- tracks


def _ship_expected(link, lscrr: bool) -> dict[str, SkuLine]:
    sed = defaultdict(lambda: ZERO)
    if lscrr:
        for s in link.sed_lines.all():
            sed[s.sku] += s.short_qty + s.damage_qty
    out = {}
    for ln in link.lines.all():
        qty = ln.quantity - sed[ln.sku]
        per = (ln.measure / ln.quantity) if ln.quantity else ZERO
        fam = "" if ln.is_empty_packing else ln.measure_family
        out[ln.sku] = SkuLine(ln.sku, qty, family=fam, exp_measure=(per * qty) if fam else None,
                              description=ln.description)
    return out


def _receipt_rows(numbers: list[str], ship_id: str, notes: list[str], also_misc: bool = False):
    """Stock-receipt rows for these receipt numbers and this Ship; anomalies go to notes (=> REVIEW)."""
    rows, review = [], False
    for no in numbers:
        found = list(IrrTransaction.objects.filter(receipt_no=no, type_key=STOCK))
        misc = list(IrrTransaction.objects.filter(trx_number=no, type_key=MISC_RECEIPT)) if also_misc else []
        other = IrrTransaction.objects.filter(Q(receipt_no=no) | Q(trx_number=no)).exclude(type_key__in=[STOCK, MISC_RECEIPT] if also_misc else [STOCK])
        if other.exists():
            review = True
            notes.append(f"Receipt {no} exists in the IRR as {other.first().type} - not a Ship receipt.")
        for r in found:
            if r.trx_reference != ship_id:
                review = True
                notes.append(f"Receipt {no} belongs to Ship {r.trx_reference} in the IRR, not {ship_id}.")
                continue
            if r.shipping_id and r.shipping_id != ship_id:
                review = True
                notes.append(f"Receipt {no}: Shipping ID Number {r.shipping_id} differs from {ship_id}.")
                continue
            rows.append(r)
        rows += misc
        if not found and not misc:
            notes.append(f"Receipt {no} not found in the IRR yet.")
    return rows, review


def ship_results(iw: Iw, tol: Decimal) -> list[Result]:
    out = []
    for link in iw.ship_links.prefetch_related("lines", "sed_lines", "receipts").order_by("linked_at"):
        lscrr = bool(iw.load_type.is_direct_factory and link.lscrr_applied)
        normal = [r.receipt_no for r in link.receipts.all() if r.kind == ErpReceipt.Kind.NORMAL]
        res = Result(f"Ship {link.ship_id}", "SHIP", normal, PENDING)
        if not normal:
            res.notes.append("Stock Receipt No not entered.")
            out.append(res)
        else:
            rows, review = _receipt_rows(normal, link.ship_id, res.notes)
            if not rows and not review:
                res.status = NOT_FOUND
                out.append(res)
            else:
                _classify(res, _ship_expected(link, lscrr), rows, tol)
                if review:
                    res.status = REVIEW
                if res.status == REVIEW and iw.load_type.is_direct_factory:
                    hint = _lscrr_hint(link, lscrr, rows, tol)
                    if hint:
                        res.notes.append(hint)
                out.append(res)
        if lscrr:
            excess_expected = {}
            for s in link.sed_lines.all():
                if s.excess_qty:
                    excess_expected[s.sku] = SkuLine(s.sku, s.excess_qty, description=s.description)
            nums = [r.receipt_no for r in link.receipts.all() if r.kind == ErpReceipt.Kind.EXCESS]
            if excess_expected or nums:
                ex = Result(f"Ship {link.ship_id} excess", "EXCESS", nums, PENDING)
                if not nums:
                    ex.notes.append("Excess Receipt No not entered.")
                else:
                    rows, review = _receipt_rows(nums, link.ship_id, ex.notes, also_misc=True)
                    if not rows and not review:
                        ex.status = NOT_FOUND
                    else:
                        _classify(ex, excess_expected, rows, tol)
                        if review:
                            ex.status = REVIEW
                out.append(ex)
    return out


def _lscrr_hint(link, lscrr: bool, rows, tol: Decimal) -> str:
    """If the IRR fits the other LSCRR pattern than declared, say so; the engine never switches mode."""
    other = _classify(Result("", "", [], PENDING), _ship_expected(link, not lscrr), rows, tol)
    if other.status == FULL:
        return "IRR matches non-LSCRR pattern" if lscrr else "IRR matches LSCRR pattern"
    return ""


def vendor_results(iw: Iw, tol: Decimal) -> list[Result]:
    out = []
    for cons in iw.vendor_consignments.prefetch_related("lines", "receipts"):
        nums = [r.receipt_no for r in cons.receipts.all()]
        res = Result(f"Vendor {cons.vendor_id} challan {cons.challan_no}", "PO", nums, PENDING)
        if not nums:
            res.notes.append("PO Receipt No not entered.")
            out.append(res)
            continue
        rows, review = [], False
        for no in nums:
            found = list(IrrTransaction.objects.filter(receipt_no=no, type_key=PO))
            if IrrTransaction.objects.filter(receipt_no=no).exclude(type_key=PO).exists():
                review = True
                res.notes.append(f"Receipt {no} exists in the IRR with another type - review.")
            if not found:
                res.notes.append(f"PO Receipt {no} not found in the IRR yet.")
            refs = {r.trx_reference for r in found}
            if found and not any(cons.challan_key.replace("/", "") in x.upper().replace(" ", "").replace("/", "") for x in refs):
                res.notes.append(f"Info: challan text in the IRR ({', '.join(sorted(refs))}) differs from {cons.challan_no}.")
            rows += found
        if not rows and not review:
            res.status = NOT_FOUND
        else:
            expected = {}
            for ln in cons.lines.all():
                e = expected.setdefault(ln.sku, SkuLine(ln.sku, family=ln.measure_family, exp_measure=ZERO,
                                                        description=ln.description))
                e.expected += ln.nop
                e.exp_measure += ln.ltr if ln.measure_family == "LTR" else ln.kg
            _classify(res, expected, rows, tol)
            if review:
                res.status = REVIEW
        out.append(res)
    return out


def _memo_rows(trx_numbers: list[str], kind: str, notes: list[str]):
    rows, seen, review = [], set(), False
    for no in trx_numbers:
        found = list(IrrTransaction.objects.filter(trx_number=no))
        if not found:
            notes.append(f"TRX {no} not found in the IRR yet.")
            continue
        if kind == "DAMAGE":
            issue_side = [r for r in found if r.transaction_type.upper() == "ISSUE"]
            if not issue_side and no.isdigit():  # the Receipt half was entered: take its Issue partner
                for partner in (str(int(no) - 1), str(int(no) + 1)):
                    cand = [r for r in IrrTransaction.objects.filter(trx_number=partner, type_key=SUBINV)
                            if r.transaction_type.upper() == "ISSUE"]
                    if cand:
                        issue_side = cand
                        break
            found = issue_side
            if any(not r.dest_subinv.endswith("DMG") for r in found):
                review = True
                notes.append(f"TRX {no} is not a transfer into the DMG sub-inventory.")
        types = {r.type_key for r in found}
        if types - {MEMO_TYPE[kind]}:
            review = True
            notes.append(f"TRX {no} is {', '.join(sorted(r.type for r in found))[:80]} - expected {MEMO_TYPE[kind].title()}.")
        for r in found:
            key = (r.trx_number, r.trx_date, r.pk)
            if key not in seen:
                seen.add(key)
                rows.append(r)
    return rows, review


def sed_results(iw: Iw, tol: Decimal) -> list[Result]:
    out = []
    for m in erp.memos(iw):
        label = f"SED {m.source_label} {m.kind.lower()}"
        if m.lscrr_covered:
            out.append(Result(label, "SED", [], COVERED, ["LSCRR-covered - no separate TRX expected."]))
            continue
        nums = [t.trx_no for t in m.trx]
        res = Result(label, "SED", nums, PENDING)
        if not nums:
            res.notes.append("TRX No not entered.")
            out.append(res)
            continue
        rows, review = _memo_rows(nums, m.kind, res.notes)
        if not rows and not review:
            res.status = NOT_FOUND
        else:
            expected = {}
            for ln, qty in m.lines:
                e = expected.setdefault(ln.sku, SkuLine(ln.sku, description=ln.description))
                e.expected += qty
            _classify(res, expected, rows, tol)
            if review:
                res.status = REVIEW
        out.append(res)
    return out


def reconcile(iw: Iw) -> list[Result]:
    tol = _tolerance()
    return ship_results(iw, tol) + vendor_results(iw, tol) + sed_results(iw, tol)


@transaction.atomic
def run(user: User, iw_pk: int) -> ReconRun:
    require(user, "IMPORT_RECONCILE")
    iw = Iw.objects.select_for_update(of=("self",)).select_related("load_type").get(pk=iw_pk)
    erp._require_erp(iw)
    results = reconcile(iw)
    ok = bool(results) and all(r.ok for r in results)
    if not results:  # outbound / Colour Bank / Non-Stock only: nothing to reconcile
        ok = True
    last_import = IrrTransaction.objects.order_by("-batch_id").values_list("batch_id", flat=True).first()
    rr = ReconRun.objects.create(iw=iw, run_by=user, all_ok=ok, results=[r.as_dict() for r in results],
                                 irr_batch_id=last_import)
    if ok and iw.status in (Iw.Status.MRA_ISSUED, Iw.Status.ERP_IN_PROGRESS):
        iw.status = Iw.Status.RECON_COMPLETE
    elif not ok and iw.status == Iw.Status.RECON_COMPLETE:
        iw.status = Iw.Status.ERP_IN_PROGRESS
    elif iw.status == Iw.Status.MRA_ISSUED:
        iw.status = Iw.Status.ERP_IN_PROGRESS
    iw.version += 1
    iw.save(update_fields=["status", "version", "updated_at"])
    audit.record("RECONCILIATION_RUN", "iw", iw.pk, user=user, iw_id=iw.pk,
                 after={"all_ok": ok, "results": {r.track: r.status for r in results}, "at": timezone.now().isoformat()})
    return rr

