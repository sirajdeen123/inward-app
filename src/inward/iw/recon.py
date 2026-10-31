"""Screen 05 part B - IRR reconciliation (SoT 8.3 - 8.6, table 10). Started by the clerk; never closes anything.

Ship (normal receipts): IRR 'Int Req Intr Rcpt' rows whose TRX Number is '<Receipt No> / <Ship ID>' (Shipping ID
Number checked when present), aggregated by full SKU, against the full document quantity - also for a Direct
Factory Ship, whose short / excess / damage the LSCCR request set posts separately (decision 17-Oct-2026).
Vendor: only through the PO Receipt No ('Po Receipt' rows '<no> / <challan text>'); challan text differences are
information only. SED memos: by TRX No - Short = Miscellaneous Issue, Excess = Miscellaneous Receipt, Damage =
Subinventory Transfer into DMG counted once from the Issue side (either number of the Issue / Receipt pair may be
entered); Direct Factory damage may also be a Miscellaneous Issue of the LSCCR request set. A request-set row whose
reason is not '08 / Depot-Short Stock Receipt' is noted for information. Results: FULL, PARTIAL, REVIEW, NOT_FOUND
(or PENDING = nothing entered). LSCRR_COVERED appears only in runs saved before 17-Oct-2026.
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
from inward.domain.normalize import floor_sku
from inward.irr.models import IrrTransaction
from inward.iw import erp
from inward.iw.models import ErpReceipt, Iw, ReconRun

FULL, PARTIAL, REVIEW, NOT_FOUND, PENDING, COVERED = "FULL", "PARTIAL", "REVIEW", "NOT_FOUND", "PENDING", "LSCRR_COVERED"
STOCK, PO, MISC_ISSUE, MISC_RECEIPT, SUBINV = ("INT REQ INTR RCPT", "PO RECEIPT", "MISCELLANEOUS ISSUE",
                                               "MISCELLANEOUS RECEIPT", "SUBINVENTORY TRANSFER")
MEMO_TYPE = {"SHORT": MISC_ISSUE, "EXCESS": MISC_RECEIPT, "DAMAGE": SUBINV}
LSCCR_REASON = "08"   # reason of the rows the LSCCR request set creates: '08 / Depot-Short Stock Receipt'
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
    floor: str = ""
    # where the IRR rows put the stock: (sub-inventory, locator, lot, receipt / TRX no) -> pieces
    places: dict = field(default_factory=lambda: defaultdict(lambda: ZERO))

    def as_dict(self) -> dict:
        return {"sku": self.sku, "floor": self.floor or floor_sku(self.sku), "description": self.description,
                "expected": _f(self.expected), "actual": _f(self.actual), "diff": _f(self.actual - self.expected),
                "family": self.family,
                "exp_measure": None if self.exp_measure is None else _f(self.exp_measure.quantize(Decimal("0.001"))),
                "act_measure": _f(self.act_measure.quantize(Decimal("0.001"))),
                "places": [{"subinv": s, "locator": loc, "lot": lot, "no": no, "qty": _f(q)}
                           for (s, loc, lot, no), q in sorted(self.places.items())]}


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
        ln.places[(r.source_subinv, r.source_locator.strip("' "), r.lot_number, r.receipt_no or r.trx_number)] += r.no_of_pack
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


def _ship_expected(link) -> dict[str, SkuLine]:
    out = {}
    for ln in link.lines.all():
        fam = "" if ln.is_empty_packing else ln.measure_family
        out[ln.sku] = SkuLine(ln.sku, ln.quantity, family=fam, exp_measure=ln.measure if fam else None,
                              description=ln.description, floor=ln.floor_sku)
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
            if r.trx_reference and r.trx_reference != ship_id:
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
    for link in iw.ship_links.prefetch_related("lines", "receipts").order_by("linked_at"):
        normal = [r.receipt_no for r in link.receipts.all() if r.kind == ErpReceipt.Kind.NORMAL]
        res = Result(f"Ship {link.ship_id}", "SHIP", normal, PENDING)
        if not normal:
            res.notes.append("Stock Receipt No not entered.")
        else:
            rows, review = _receipt_rows(normal, link.ship_id, res.notes)
            if not rows and not review:
                res.status = NOT_FOUND
            else:
                _classify(res, _ship_expected(link), rows, tol)
                if review:
                    res.status = REVIEW
        out.append(res)
    return out


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
            if any(not r.trx_reference for r in found):
                res.notes.append(f"Info: receipt {no} has no challan text in the IRR.")
            refs = {r.trx_reference for r in found if r.trx_reference}
            if refs and not any(cons.challan_key.replace("/", "") in x.upper().replace(" ", "").replace("/", "") for x in refs):
                res.notes.append(f"Info: challan text in the IRR ({', '.join(sorted(refs))}) differs from {cons.challan_no}.")
            rows += found
        if not rows and not review:
            res.status = NOT_FOUND
        else:
            expected = {}
            for ln in cons.lines.all():
                e = expected.setdefault(ln.sku, SkuLine(ln.sku, family=ln.measure_family, exp_measure=ZERO,
                                                        description=ln.description, floor=ln.floor_sku))
                e.expected += ln.nop
                e.exp_measure += ln.ltr if ln.measure_family == "LTR" else ln.kg
            _classify(res, expected, rows, tol)
            if review:
                res.status = REVIEW
        out.append(res)
    return out


def _memo_rows(trx_numbers: list[str], kind: str, notes: list[str], direct: bool = False):
    allowed = {MEMO_TYPE[kind]} | ({MISC_ISSUE} if direct and kind == "DAMAGE" else set())
    rows, seen, review = [], set(), False
    for no in trx_numbers:
        found = list(IrrTransaction.objects.filter(trx_number=no))
        if not found:
            notes.append(f"TRX {no} not found in the IRR yet.")
            continue
        if kind == "DAMAGE" and not (direct and all(r.type_key == MISC_ISSUE for r in found)):
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
        if types - allowed:
            review = True
            notes.append(f"TRX {no} is {', '.join(sorted(r.type for r in found))[:80]} - expected "
                         f"{' or '.join(sorted(t.title() for t in allowed))}.")
        if direct:
            other = next((r for r in found if r.type_key in (MISC_ISSUE, MISC_RECEIPT)
                          and not r.reason_description.startswith(LSCCR_REASON)), None)
            if other:
                notes.append(f"Info: TRX {no} reason '{other.reason_description or '-'}' - not the LSCCR request set "
                             "(08 / Depot-Short Stock Receipt).")
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
        nums = [t.trx_no for t in m.trx]
        res = Result(label, "SED", nums, PENDING)
        if not nums:
            res.notes.append(f"TRX No not entered (LSCCR {m.lsccr_no or 'No not recorded'} request set)." if m.direct
                             else "TRX No not entered.")
            out.append(res)
            continue
        rows, review = _memo_rows(nums, m.kind, res.notes, m.direct)
        if not rows and not review:
            res.status = NOT_FOUND
        else:
            expected = {}
            for ln, qty in m.lines:
                e = expected.setdefault(ln.sku, SkuLine(ln.sku, description=ln.description, floor=ln.floor_sku))
                e.expected += qty
            _classify(res, expected, rows, tol)
            if review:
                res.status = REVIEW
        out.append(res)
    return out


def detail(iw: Iw) -> dict:
    """The detailed reconciliation report (screen / PDF): every track with every SKU and where the IRR rows put
    the stock (sub-inventory + source locator = the put-away place on the floor, lot, receipt / TRX no)."""
    results = [r.as_dict() for r in reconcile(iw)]
    for r in results:
        r["ok"] = r["status"] in (FULL, COVERED)
        r["expected"] = _f(sum((Decimal(ln["expected"]) for ln in r["lines"]), ZERO))
        r["actual"] = _f(sum((Decimal(ln["actual"]) for ln in r["lines"]), ZERO))
        for fam in ("LTR", "KG"):
            r[fam.lower()] = _f(sum((Decimal(ln["act_measure"]) for ln in r["lines"] if ln["family"] == fam), ZERO))
        dates = []
        for no in r["numbers"]:
            day = (IrrTransaction.objects.filter(Q(receipt_no=no) | Q(trx_number=no)).order_by("trx_date")
                   .values_list("trx_date", flat=True).first())
            dates.append({"no": no, "date": day})
        r["dates"] = dates
        r["many"] = len(r["numbers"]) > 1   # several receipts: show the number against each put-away row
    # what came into stock = the Ship and PO receipts; the SED memo TRX are adjustments and are not added again
    stock = [r for r in results if r["kind"] in ("SHIP", "PO")]
    total = {k: _f(sum((Decimal(r[k]) for r in stock), ZERO)) for k in ("actual", "ltr", "kg")}
    total["skus"] = len({ln["sku"] for r in stock for ln in r["lines"]})
    sed = [r for r in results if r["kind"] == "SED"]
    for r in sed:   # "SED Ship 53481142 short" -> "Ship 53481142 short"
        r["kind_label"] = r["track"][4:] if r["track"].startswith("SED ") else r["track"]
    return {"results": results, "sed": sed, "all_ok": all(r["ok"] for r in results),
            "skus": sum(len(r["lines"]) for r in results), "total": total, "receipts": len(stock)}


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

