"""The matching rules, from the analysis of the IRR (01-May to 12-Sep-2026, 18,261 rows):

SUBINV   Subinventory Transfer: Issue TRX N <-> Receipt TRX N+1 (sometimes up to N+10), same SKU, pieces and lot, the
         receipt's source = the issue's destination. A transfer INTO the DMG sub-inventory must be explained by an
         approved internal damage report or an IW damage TRX (from the cut-off date).
PICK     Internal Order Pick: the same pairing (stock moved to 035OUT for an outbound order).
SHIP     Int Order Intr Ship: what was picked into 035OUT must leave in the shipments of the same day (per SKU).
CONVERT  Miscellaneous Issue / Receipt of a conversion (31 decanting, 32 restencilling, 33 mixing / tinting, 0801):
         per day and reason, the litres / kilos issued = the litres / kilos received (0.5 % tolerance).
LSCCR    Miscellaneous Issue / Receipt reason 08 (LSCCR request set): explained by the IW whose SED TRX it is.
INBOUND  Int Req Intr Rcpt / Po Receipt / Rma Receipt: receipts from outside - information (linked to the IW when
         its receipt number is known). RESIDUAL: zero-quantity rows - information.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from inward.core.models import DepotOperator, FeatureFlag
from inward.irr.models import IrrTransaction
from inward.irrecon.models import IrrMark, IrrMatch, IrrReconRun

S = IrrMatch.Status
ZERO = Decimal(0)
TOLERANCE = Decimal("0.005")
CONVERSION_CODES = {"31", "32", "33", "0801"}
# the receipt half is usually TRX N+1; Oracle sometimes numbers it a few further away (seen up to N+6)
OFFSETS = [d for k in range(1, 11) for d in (k, -k)]


def _flag_date(key: str, default: date) -> date:
    f = FeatureFlag.objects.filter(key=key).first()
    v = (f.value or {}).get("value") if f else None
    try:
        return date.fromisoformat(str(v)) if v else default
    except ValueError:
        return default


def _go_live() -> date:
    op = DepotOperator.objects.order_by("effective_from").first()
    return op.effective_from if op else date(2026, 4, 1)


def _code(text: str) -> str:
    return (text or "").split("/", 1)[0].strip()


def _num(trx: str):
    return int(trx) if trx.isdigit() else None


def _measure(r) -> Decimal:
    return (r.volume_ltr or ZERO) + (r.weight_kg or ZERO)


class Engine:
    def __init__(self):
        self.out: dict[int, tuple] = {}   # row id -> (family, status, group, note, link)
        self.damage_from = _flag_date("irr_recon_damage_from", date(2026, 10, 18))
        self.go_live = _go_live()
        from inward.damage.models import DamageTrx
        from inward.iw.models import ErpReceipt, SedTrx
        self.idr = dict(DamageTrx.objects.exclude(report__status="CANCELLED")
                        .values_list("trx_no", "report__number"))
        self.sed = {t: (no, kind) for t, no, kind in SedTrx.objects.values_list("trx_no", "iw__iw_no", "kind")}
        self.receipts = dict(ErpReceipt.objects.values_list("receipt_no", "iw__iw_no"))

    def set(self, r, family, status, group="", note="", link=""):
        self.out[r.pk] = (family, status, group, note[:300], link[:40])

    # ---------------------------------------------------------------- issue / receipt pairs
    def pairs(self, rows, family: str, prefix: str):
        issues = [r for r in rows if r.transaction_type.upper() == "ISSUE"]
        by_key = defaultdict(list)
        for r in rows:
            if r.transaction_type.upper() == "RECEIPT":
                by_key[(_num(r.trx_number), r.sku)].append(r)
        used = set()
        matched = []
        for i in issues:
            n = _num(i.trx_number)
            hit = None
            for d in OFFSETS:
                for c in by_key.get((n + d, i.sku) if n is not None else (None, None), []):
                    if c.pk in used or c.no_of_pack != i.no_of_pack:
                        continue
                    if i.lot_number and c.lot_number and i.lot_number != c.lot_number:
                        continue
                    if c.source_subinv and i.dest_subinv and c.source_subinv != i.dest_subinv:
                        continue
                    hit = c
                    break
                if hit:
                    break
            if hit is None:
                self.set(i, family, S.OPEN, f"{prefix}:{i.trx_number}",
                         f"No matching receipt half (TRX {i.trx_number} +1..10, same SKU / pieces / lot)")
                continue
            used.add(hit.pk)
            matched.append((i, hit))
        for r in rows:
            if r.transaction_type.upper() == "RECEIPT" and r.pk not in used:
                self.set(r, family, S.OPEN, f"{prefix}:{r.trx_number}",
                         f"No matching issue half (TRX {r.trx_number} -1..10, same SKU / pieces / lot)")
        return matched

    def subinventory(self, rows):
        for i, rc in self.pairs(rows, "SUBINV", "ST"):
            group = f"ST:{i.trx_number}"
            note, status, link = f"{i.source_subinv} -> {i.dest_subinv}", S.MATCHED, ""
            if i.dest_subinv.endswith("DMG") and not i.source_subinv.endswith("DMG"):
                idr = self.idr.get(i.trx_number) or self.idr.get(rc.trx_number)
                sed = self.sed.get(i.trx_number) or self.sed.get(rc.trx_number)
                if idr:
                    status, link, note = S.EXPLAINED, idr, f"Moved to DMG - internal damage {idr}"
                elif sed:
                    status, link, note = S.EXPLAINED, sed[0], f"Moved to DMG - {sed[0]} {sed[1].lower()}"
                elif i.trx_date < self.damage_from:
                    note = f"Moved to DMG ({i.reason_description}) - before internal damage approvals"
                else:
                    status = S.OPEN
                    note = (f"Moved to DMG ({i.reason_description}) without an approved internal damage report "
                            "or IW damage TRX")
            self.set(i, "SUBINV", status, group, note, link)
            self.set(rc, "SUBINV", status, group, note, link)

    def pick_and_ship(self, picks, ships):
        picked = defaultdict(lambda: ZERO)
        for i, rc in self.pairs(picks, "PICK", "PK"):
            key = (i.trx_date, i.sku)
            picked[key] += i.no_of_pack
            for r in (i, rc):
                self.set(r, "PICK", S.MATCHED, f"PS:{i.trx_date}:{i.sku}", f"{i.source_subinv} -> {i.dest_subinv}")
        shipped, ship_rows = defaultdict(lambda: ZERO), defaultdict(list)
        for s in ships:
            shipped[(s.trx_date, s.sku)] += s.no_of_pack
            ship_rows[(s.trx_date, s.sku)].append(s)
        for key in set(picked) | set(shipped):
            p, s = picked.get(key, ZERO), shipped.get(key, ZERO)
            group = f"PS:{key[0]}:{key[1]}"
            if p == s:
                for r in ship_rows.get(key, []):
                    self.set(r, "SHIP", S.MATCHED, group, f"Shipped = picked ({s})", r.shipping_id)
                continue
            note = f"Picked into 035OUT {p}, shipped {s} on {key[0]:%d-%b-%Y}"
            for r in ship_rows.get(key, []):
                self.set(r, "SHIP", S.OPEN, group, note, r.shipping_id)
            for r in picks:
                if (r.trx_date, r.sku) == key and self.out.get(r.pk, ("", ""))[1] == S.MATCHED:
                    self.set(r, "PICK", S.OPEN, group, note)

    def conversions(self, rows):
        groups = defaultdict(list)
        for r in rows:
            groups[(r.trx_date, _code(r.reason_description))].append(r)
        for (day, code), grp in groups.items():
            out = sum((_measure(r) for r in grp if r.transaction_type.upper() == "ISSUE"), ZERO)
            inn = sum((_measure(r) for r in grp if r.transaction_type.upper() == "RECEIPT"), ZERO)
            group = f"CV:{day}:{code}"
            ok = out and inn and abs(out - inn) <= max(Decimal("0.01"), out * TOLERANCE)
            reason = grp[0].reason_description
            note = (f"{reason}: issued {out.normalize():f}, received {inn.normalize():f} (LTR + KG) on {day:%d-%b-%Y}")
            for r in grp:
                self.set(r, "CONVERT", S.MATCHED if ok else S.OPEN, group, note)

    def lsccr(self, rows):
        for r in rows:
            sed = self.sed.get(r.trx_number)
            if sed:
                self.set(r, "LSCCR", S.EXPLAINED, f"LS:{r.trx_number}", f"LSCCR request set - {sed[0]} {sed[1].lower()}",
                         sed[0])
            elif r.trx_date < self.go_live:
                self.set(r, "LSCCR", S.INFO, f"LS:{r.trx_number}", "LSCCR posting before the inward app went live")
            else:
                self.set(r, "LSCCR", S.OPEN, f"LS:{r.trx_number}",
                         "LSCCR posting not linked to an IW - enter this TRX on the IW's SED memo")

    def one_sided(self, r):
        t = r.type_key
        if t == "INT REQ INTR RCPT":
            iw = self.receipts.get(r.receipt_no)
            self.set(r, "INBOUND", S.INFO, f"RC:{r.receipt_no}", "Stock receipt" + (f" - {iw}" if iw else
                     " (reconciled in the inward app)"), iw or "")
        elif t == "PO RECEIPT":
            iw = self.receipts.get(r.receipt_no)
            self.set(r, "INBOUND", S.INFO, f"PO:{r.receipt_no or r.trx_number}", "Vendor PO receipt", iw or "")
        elif t == "RMA RECEIPT":
            self.set(r, "RETURN", S.INFO, f"RM:{r.trx_number}", f"Customer return - {r.reason_description}"
                     + (" (into DMG)" if r.source_subinv.endswith("DMG") else ""))
        elif t.startswith("RESIDUAL"):
            self.set(r, "RESIDUAL", S.INFO, "", "Zero-quantity residual row")
        else:
            self.set(r, "OTHER", S.OPEN, "", f"Transaction type '{r.type}' is not known to the engine - check it")

    def run(self, rows):
        buckets = defaultdict(list)
        for r in rows:
            t = r.type_key
            if t == "SUBINVENTORY TRANSFER":
                buckets["SUBINV"].append(r)
            elif t == "INTERNAL ORDER PICK":
                buckets["PICK"].append(r)
            elif t == "INT ORDER INTR SHIP":
                buckets["SHIP"].append(r)
            elif t in ("MISCELLANEOUS ISSUE", "MISCELLANEOUS RECEIPT"):
                code = _code(r.reason_description)
                buckets["LSCCR" if code == "08" else "CONVERT" if code in CONVERSION_CODES else "MISC"].append(r)
            else:
                self.one_sided(r)
        self.subinventory(buckets["SUBINV"])
        self.pick_and_ship(buckets["PICK"], buckets["SHIP"])
        self.conversions(buckets["CONVERT"])
        self.lsccr(buckets["LSCCR"])
        for r in buckets["MISC"]:
            self.set(r, "MISC", S.OPEN, f"MI:{r.trx_number}", f"Miscellaneous {r.transaction_type.lower()} with reason "
                     f"'{r.reason_description or '-'}' - not a known pattern")
        return self.out


@transaction.atomic
def run(user=None) -> IrrReconRun:
    rows = list(IrrTransaction.objects.order_by("trx_date", "id"))
    out = Engine().run(rows)
    marks = {m.row_id: m for m in IrrMark.objects.select_related("marked_by")}
    IrrMatch.objects.all().delete()
    objs, summary = [], defaultdict(lambda: defaultdict(int))
    for r in rows:
        family, status, group, note, link = out.get(r.pk, ("OTHER", S.OPEN, "", "Not classified", ""))
        if status == S.OPEN and r.pk in marks:
            m = marks[r.pk]
            status, note = S.EXPLAINED, f"Marked by {m.marked_by.username} {timezone.localtime(m.marked_at):%d-%b-%Y}: {m.reason}"[:300]
        objs.append(IrrMatch(row=r, family=family, status=status, group=group, note=note, link=link))
        summary[family][status] += 1
    IrrMatch.objects.bulk_create(objs, batch_size=2000)
    run = IrrReconRun.objects.create(run_by=user if getattr(user, "is_authenticated", False) else None,
                                     rows=len(rows), summary={f: dict(v) for f, v in summary.items()})
    from inward.core import audit
    audit.record("IRR_RECON_ENGINE_RUN", "irr_recon_run", run.pk, user=run.run_by,
                 after={"rows": len(rows), "open": sum(v.get(S.OPEN, 0) for v in summary.values())})
    return run
