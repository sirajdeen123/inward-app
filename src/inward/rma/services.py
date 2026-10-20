"""Material Returns - rules (21-Oct-2026). Views are thin; everything that changes data is here and audited.

Flow: GRN (clerk uploads the Oracle Goods Return Note PDF) -> the clerk raises the portal request -> the RMA Portal
Report upload links it to the GRN (same dealer + invoice) and tracks status / Pending At -> approvers are mailed
(mails.py) -> RMA No and Credit Note -> the "Rma Receipt" TRX from the IRR -> per-SKU reconciliation (GRN returned
pieces / portal quantity / CN pieces / IRR pieces) -> the godown checks the material against the handover PDF ->
closed. A rejected request is closed with the reason the material went back (or was re-raised).
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from inward.accounts.models import User
from inward.accounts.services import require
from inward.core import audit, documents
from inward.core.models import DocumentLink, FeatureFlag, ImportBatch
from inward.domain.normalize import clean_text
from inward.importers.common import file_sha256
from inward.rma import importers as imp
from inward.rma.models import (
    Approver, CnLine, Condition, CustomerSite, Grn, GrnLine, IrrLink, Request, RequestLine, StageEvent, Territory,
    TransportMode,
)

ZERO = Decimal(0)


class RmaError(ValueError):
    pass


def flag(key: str, default):
    f = FeatureFlag.objects.filter(key=key).first()
    v = (f.value or {}).get("value") if f else None
    return default if v is None else v


def name_key(name: str) -> str:
    return " ".join(clean_text(name).upper().split())


def pending_names(text: str) -> list[str]:
    """'S Muthusamy , Vignesh Kuttalam' -> two approvers."""
    return [clean_text(n) for n in re.split(r"\s*[,;/&]\s*", clean_text(text)) if clean_text(n)]


def is_pending(status: str) -> bool:
    return "PENDING" in (status or "").upper()


def is_rejected(status: str) -> bool:
    return "REJECT" in (status or "").upper()

# ---------------------------------------------------------------- stages and ageing


STAGES = [("APPROVAL", "Approval pending"), ("RMA", "Waiting for RMA No / Oracle"), ("CN", "Waiting for Credit Note"),
          ("IRR", "Waiting for IRR receipt"), ("CLOSE", "Ready to verify and close"), ("REJECTED", "Rejected - to close"),
          ("CLOSED", "Closed")]
STAGE_LABEL = dict(STAGES)


def stage(r: Request, cn_rmas: set | None = None, irr_reqs: set | None = None) -> str:
    if r.closed_at:
        return "CLOSED"
    if is_rejected(r.status):
        return "REJECTED"
    if is_pending(r.status):
        return "APPROVAL"
    if not r.rma_no:
        return "RMA"
    has_cn = (r.rma_no in cn_rmas) if cn_rmas is not None else CnLine.objects.filter(rma_no=r.rma_no).exists()
    if not has_cn:
        return "CN"
    has_irr = (r.pk in irr_reqs) if irr_reqs is not None else r.irr_links.exists()
    return "CLOSE" if has_irr else "IRR"


def stage_start(r: Request) -> date | None:
    dates = [d for d in (r.requested_date, r.admin_date, r.rsm_date, r.rcm_date, r.stage_since) if d]
    return max(dates) if dates else None


def rag(days: int | None) -> str:
    if days is None:
        return ""
    amber, red = int(flag("rma_amber_days", 7)), int(flag("rma_red_days", 15))
    return "RED" if days >= red else "AMBER" if days >= amber else "GREEN"


@dataclass
class Row:
    """One request as shown on screens and in the mails."""
    r: Request
    stage: str
    days: int | None
    days_at_stage: int | None
    grns: list[str]
    sales_officer: tuple | None

    @property
    def stage_label(self) -> str:
        return STAGE_LABEL[self.stage]

    @property
    def flag(self) -> str:
        return rag(self.days)

    @property
    def stage_flag(self) -> str:
        return rag(self.days_at_stage)


def rows(qs=None, today: date | None = None) -> list[Row]:
    today = today or timezone.localdate()
    qs = (qs if qs is not None else Request.objects.all())
    reqs = list(qs)
    cn_rmas = set(CnLine.objects.filter(rma_no__in=[r.rma_no for r in reqs if r.rma_no]).values_list("rma_no", flat=True))
    irr = set(IrrLink.objects.filter(request__in=reqs).values_list("request_id", flat=True))
    grns = defaultdict(set)
    sites = {}
    for rid, no, site in GrnLine.objects.filter(request__in=reqs).values_list("request_id", "grn__grn_no", "grn__site_code"):
        grns[rid].add(no)
        sites.setdefault(rid, site)
    so = SalesOfficers()
    out = []
    for r in reqs:
        st = stage(r, cn_rmas, irr)
        days = (today - r.requested_date).days if r.requested_date and st != "CLOSED" else None
        start = stage_start(r)
        at = (today - start).days if start and st != "CLOSED" else None
        out.append(Row(r, st, days, at, sorted(grns[r.pk]), so.for_request(r, sites.get(r.pk))))
    return out

# ---------------------------------------------------------------- Sales Officer (Customer Master + TSI Master)


class SalesOfficers:
    """dealer site -> territory -> Sales Officer; cached for one run."""

    def __init__(self):
        self._terr = {t.territory_no: t for t in Territory.objects.all()}
        self._site: dict[str, CustomerSite | None] = {}
        self._acct: dict[str, set] = {}

    def territory(self, no: str):
        t = self._terr.get(no)
        return (t.sales_officer, t.mail, no) if t else None

    def for_site(self, site: str):
        if not site:
            return None
        if site not in self._site:
            self._site[site] = CustomerSite.objects.filter(pk=site).first()
        s = self._site[site]
        return self.territory(s.territory_no) if s else None

    def for_account(self, account: str):
        if account not in self._acct:
            self._acct[account] = set(CustomerSite.objects.filter(account_no=account).values_list("territory_no", flat=True))
        terrs = self._acct[account]
        return self.territory(next(iter(terrs))) if len(terrs) == 1 else None

    def for_request(self, r: Request, grn_site: str | None = None):
        return self.for_site(r.bill_to_site) or self.for_site(grn_site or "") or self.for_account(r.dealer_code)


def territories_of(account: str) -> list[CustomerSite]:
    return list(CustomerSite.objects.filter(account_no=account).order_by("territory_no", "site_code"))

# ---------------------------------------------------------------- approvers


def approver_for(name: str, create: bool = True) -> Approver | None:
    key = name_key(name)
    if not key:
        return None
    a = Approver.objects.filter(key=key).first()
    if a is None and create:
        a = Approver.objects.create(key=key, name=clean_text(name)[:120])
    return a


def final(a: Approver | None) -> Approver | None:
    """Follow 'left - mail goes to' redirects (at most 5 steps)."""
    seen = 0
    while a is not None and a.redirect_to_id and seen < 5:
        a = a.redirect_to
        seen += 1
    return a


@transaction.atomic
def save_approver(user: User, pk: int, email: str, active: bool, redirect_pk: str = "") -> Approver:
    from inward.iw.comms import _clean_emails
    require(user, "RMA_MASTER")
    a = Approver.objects.select_for_update().get(pk=pk)
    addrs = _clean_emails(email)
    if email.strip() and len(addrs) != 1:
        raise RmaError(f"{a.name}: enter one valid e-mail address.")
    redirect = None
    if str(redirect_pk or "").strip():
        redirect = Approver.objects.filter(pk=int(redirect_pk)).first()
        if redirect is None or redirect.pk == a.pk:
            raise RmaError(f"{a.name}: choose another person to receive the mail.")
    before = {"email": a.email, "active": a.active, "redirect": a.redirect_to.name if a.redirect_to else ""}
    a.email, a.active, a.redirect_to, a.updated_by = (addrs[0] if addrs else ""), active, redirect, user
    if not active and redirect is None and not a.email:
        raise RmaError(f"{a.name}: a person who left needs the replacement who gets the mails.")
    a.save()
    after = {"email": a.email, "active": a.active, "redirect": redirect.name if redirect else ""}
    if before != after:
        audit.record("RMA_APPROVER_CHANGED", "rma_approver", a.pk, user=user, before=before, after=after)
    return a


@transaction.atomic
def save_territory_mail(user: User, territory_no: str, email: str) -> Territory:
    from inward.iw.comms import _clean_emails
    require(user, "RMA_MASTER")
    t = Territory.objects.select_for_update().get(pk=territory_no)
    addrs = _clean_emails(email)
    if email.strip() and len(addrs) != 1:
        raise RmaError(f"Territory {territory_no}: enter one valid e-mail address.")
    before = t.email_override
    t.email_override = addrs[0] if addrs else ""
    t.save(update_fields=["email_override", "updated_at"])
    if before != t.email_override:
        audit.record("RMA_SALES_OFFICER_MAIL", "rma_territory", territory_no, user=user,
                     before={"email": before}, after={"email": t.email_override})
    return t

# ---------------------------------------------------------------- imports


def _batch(kind: str, path: Path, filename: str, user: User, content_type="application/vnd.ms-excel"):
    sha = file_sha256(path)
    if ImportBatch.objects.filter(kind=kind, sha256=sha).exists():
        raise RmaError(f"{filename} was already uploaded - nothing changed.")
    doc = documents.store(Path(path).read_bytes(), filename=filename, content_type=content_type, doc_type=kind,
                          user=user)
    return ImportBatch.objects.create(kind=kind, source="MANUAL", filename=filename[:260], sha256=sha, document=doc,
                                      uploaded_by=user)


def last_upload(kind: str):
    return ImportBatch.objects.filter(kind=kind).order_by("-uploaded_at").first()


PORTAL_FIELDS = ["region", "depot", "dealer_code", "dealer_name", "invoice_no", "invoice_date", "total_value",
                 "return_type", "material_receipt_date", "status", "pending_at", "requested_by", "requested_date",
                 "admin_by", "admin_date", "rsm_by", "rsm_date", "rcm_by", "rcm_date", "rma_no", "rma_date", "cn_no",
                 "cn_date", "cn_value", "oracle_status", "oracle_error"]
TEXT_FIELDS = {f.name for f in Request._meta.concrete_fields if f.get_internal_type() == "CharField"}


@transaction.atomic
def import_portal(user: User, path: Path, filename: str) -> dict:
    require(user, "RMA_WORK")
    report = imp.parse_portal(path)
    first_ever = not ImportBatch.objects.filter(kind="RMA_PORTAL").exists()
    batch = _batch("RMA_PORTAL", path, filename, user)
    today = timezone.localdate()
    known = {r.pk: r for r in Request.objects.filter(pk__in=list(report.requests)).select_for_update()}
    new = changed = 0
    for rid, pr in report.requests.items():
        v = dict(pr.values)
        if v.get("cn_value") is None and rid in known:
            v["cn_value"] = known[rid].cn_value
        r = known.get(rid)
        moved = False
        if r is None:
            r = Request(request_id=rid, stage_since=today)
            new += 1
            moved = True
        elif (r.status, r.pending_at) != (v["status"], v["pending_at"]):
            moved = True
            r.stage_since = today
            changed += 1
        for k in PORTAL_FIELDS:
            val = v.get(k)
            if val is None:
                val = ZERO if k == "total_value" else "" if k in TEXT_FIELDS else None
            setattr(r, k, val)
        r.last_seen = batch
        r.save()
        if pr.lines:
            RequestLine.objects.filter(request=r).delete()
            RequestLine.objects.bulk_create([RequestLine(request=r, **ln) for ln in pr.lines])
        if moved:
            StageEvent.objects.create(request=r, status=r.status, pending_at=r.pending_at, batch=batch,
                                      notified=first_ever or not (is_pending(r.status) and r.pending_at))
        for n in pending_names(r.pending_at):
            approver_for(n)
    linked = auto_link(user)
    batch.row_count = batch.accepted_count = len(report.requests)
    batch.summary = {"as_on": str(report.as_on or ""), "new": new, "changed": changed, "linked": linked}
    batch.save(update_fields=["row_count", "accepted_count", "summary"])
    unmapped = Approver.objects.filter(active=True, email="", redirect_to__isnull=True).count()
    audit.record("RMA_PORTAL_IMPORTED", "import_batch", batch.pk, user=user,
                 after={"file": filename, "requests": len(report.requests), "new": new, "changed": changed,
                        "linked_grn_invoices": linked, "as_on": str(report.as_on or "")})
    return {"requests": len(report.requests), "new": new, "changed": changed, "linked": linked, "unmapped": unmapped,
            "as_on": report.as_on}


@transaction.atomic
def import_cn(user: User, path: Path, filename: str) -> dict:
    require(user, "RMA_WORK")
    report = imp.parse_cn(path)
    batch = _batch("RMA_CN", path, filename, user)
    cns = {ln["cn_no"] for ln in report.lines}
    replaced = CnLine.objects.filter(cn_no__in=cns).count()
    CnLine.objects.filter(cn_no__in=cns).delete()
    CnLine.objects.bulk_create([CnLine(batch=batch, **ln) for ln in report.lines])
    batch.row_count, batch.accepted_count = len(report.lines) + report.duplicates, len(report.lines)
    batch.summary = {"credit_notes": len(cns), "duplicates_ignored": report.duplicates, "replaced": replaced}
    batch.save(update_fields=["row_count", "accepted_count", "summary"])
    audit.record("RMA_CN_IMPORTED", "import_batch", batch.pk, user=user,
                 after={"file": filename, "lines": len(report.lines), "credit_notes": len(cns),
                        "duplicates_ignored": report.duplicates})
    return {"lines": len(report.lines), "credit_notes": len(cns), "duplicates": report.duplicates}


@transaction.atomic
def import_customer_master(user: User, path: Path, filename: str) -> dict:
    require(user, "RMA_WORK")
    sites = imp.parse_customer_master(path)
    batch = _batch("RMA_CUSTOMER", path, filename, user)
    CustomerSite.objects.all().delete()
    CustomerSite.objects.bulk_create([CustomerSite(**s) for s in sites])
    batch.row_count = batch.accepted_count = len(sites)
    batch.save(update_fields=["row_count", "accepted_count"])
    audit.record("RMA_CUSTOMER_MASTER_IMPORTED", "import_batch", batch.pk, user=user,
                 after={"file": filename, "sites": len(sites)})
    return {"sites": len(sites), "territories": len({s["territory_no"] for s in sites})}


@transaction.atomic
def import_tsi(user: User, path: Path, filename: str) -> dict:
    require(user, "RMA_WORK")
    terrs = imp.parse_tsi(path)
    batch = _batch("RMA_TSI", path, filename, user)
    for t in terrs:
        Territory.objects.update_or_create(territory_no=t["territory_no"], defaults={
            "sales_officer": t["sales_officer"], "business_line": t["business_line"], "email": t["email"]})
    audit.record("RMA_TSI_IMPORTED", "import_batch", batch.pk, user=user,
                 after={"file": filename, "territories": len(terrs)})
    return {"territories": len(terrs), "without_mail": sum(1 for t in terrs if not t["email"])}

# ---------------------------------------------------------------- GRN


@transaction.atomic
def upload_grn(user: User, data: bytes, filename: str) -> dict:
    require(user, "RMA_WORK")
    if not data.startswith(b"%PDF"):
        raise RmaError("Upload the GRN as the PDF printed from Oracle.")
    found = imp.parse_grn_pdf(data)
    doc = documents.store(data, filename=filename, content_type="application/pdf", doc_type="RMA_GRN", user=user)
    created, skipped, problems = [], [], []
    for g in found:
        if Grn.objects.filter(grn_no=g.grn_no).exists():
            skipped.append(g.grn_no)
            continue
        if not g.lines or not g.dealer_code or g.grn_date is None:
            problems.append(f"{g.grn_no}: the dealer / SKU lines could not be read - enter it again from a clean "
                            "Oracle print.")
            continue
        grn = Grn.objects.create(grn_no=g.grn_no, grn_date=g.grn_date, receipt_time=g.receipt_time,
                                 transporter=g.transporter[:120], vehicle_no=g.vehicle_no[:60],
                                 dealer_code=g.dealer_code, dealer_name=g.dealer_name[:200], site_code=g.site_code,
                                 document=doc, created_by=user)
        GrnLine.objects.bulk_create([GrnLine(grn=grn, invoice_no=ln.invoice_no, invoice_date=ln.invoice_date,
                                             sku=ln.sku, description=ln.description[:200], inv_nop=ln.inv_nop,
                                             ret_nop=ln.ret_nop, drums=ln.drums, cartons=ln.cartons, bags=ln.bags,
                                             loose=ln.loose, weight=ln.weight) for ln in g.lines])
        DocumentLink.objects.create(document=doc, entity_type="rma_grn", entity_id=str(grn.pk), role="grn_pdf")
        audit.record("RMA_GRN_UPLOADED", "rma_grn", grn.pk, user=user,
                     after={"grn": g.grn_no, "dealer": g.dealer_code, "site": g.site_code, "lines": len(g.lines),
                            "invoices": sorted({ln.invoice_no for ln in g.lines}), "file": filename})
        created.append(grn)
    linked = auto_link(user) if created else 0
    return {"created": created, "skipped": skipped, "problems": problems, "linked": linked}


def _dt(value) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        d = date.fromisoformat(text)
    except ValueError as exc:
        raise RmaError(f"Date '{text}' not valid.") from exc
    if d > timezone.localdate():
        raise RmaError("A date cannot be in the future.")
    return d


@transaction.atomic
def update_grn(user: User, pk: int, data: dict, conditions: dict[int, str]) -> Grn:
    """Gate entry, transport, LR, freight and the Good / Damaged pick of every SKU line."""
    require(user, "RMA_WORK")
    g = Grn.objects.select_for_update().get(pk=pk)
    before = {f: str(getattr(g, f) or "") for f in ("gate_entry_no", "gate_entry_date", "transport_mode", "lr_no",
                                                   "lr_date", "freight", "remarks")}
    mode = data.get("transport_mode", "")
    if mode and mode not in TransportMode.values:
        raise RmaError("Choose how the material came.")
    try:
        freight = Decimal(str(data.get("freight") or "").replace(",", "")) if str(data.get("freight") or "").strip() else None
    except InvalidOperation as exc:
        raise RmaError("Freight: enter the amount in rupees.") from exc
    if freight is not None and freight < 0:
        raise RmaError("Freight cannot be negative.")
    g.gate_entry_no = clean_text(data.get("gate_entry_no")).upper()[:40]
    g.gate_entry_date = _dt(data.get("gate_entry_date"))
    g.transport_mode = mode
    g.lr_no = clean_text(data.get("lr_no")).upper()[:40]
    g.lr_date = _dt(data.get("lr_date")) if g.lr_no else None
    g.freight = freight
    g.remarks = clean_text(data.get("remarks"))[:500]
    g.save()
    after = {f: str(getattr(g, f) or "") for f in before}
    picks = {}
    for line in g.lines.select_for_update():
        c = conditions.get(line.pk, line.condition)
        if c and c not in Condition.values:
            raise RmaError("Condition must be Good stock or Damaged stock.")
        if c != line.condition:
            picks[line.sku] = c
            line.condition = c
            line.save(update_fields=["condition"])
    if before != after or picks:
        audit.record("RMA_GRN_UPDATED", "rma_grn", g.pk, user=user, before=before, after={**after, "conditions": picks})
    return g


def grn_missing(g: Grn) -> list[str]:
    out = []
    if g.lines.filter(condition="").exists():
        out.append("Pick Good / Damaged stock for every SKU line.")
    if not g.gate_entry_no:
        out.append("Gate entry number.")
    if not g.transport_mode:
        out.append("How the material came (CFA / dealer vehicle / transport).")
    return out


def auto_link(user: User | None) -> int:
    """GRN invoice -> the portal request raised for it: same dealer account and invoice number, only when exactly one
    request matches and it is not linked to another GRN already."""
    n = 0
    groups = defaultdict(list)
    for ln in GrnLine.objects.filter(request__isnull=True).select_related("grn"):
        groups[(ln.grn.dealer_code, ln.invoice_no)].append(ln)
    for (dealer, inv), lines in groups.items():
        cands = list(Request.objects.filter(dealer_code=dealer, invoice_no=inv).exclude(status__icontains="REJECT")
                     .filter(closed_at__isnull=True))
        if len(cands) != 1 or GrnLine.objects.filter(request=cands[0]).exclude(grn=lines[0].grn).exists():
            continue
        GrnLine.objects.filter(pk__in=[ln.pk for ln in lines]).update(request=cands[0])
        audit.record("RMA_GRN_LINKED", "rma_grn", lines[0].grn_id, user=user,
                     after={"invoice": inv, "request": cands[0].pk, "how": "automatic (dealer + invoice)"})
        n += 1
    return n


def link_candidates(g: Grn, invoice_no: str) -> list[Request]:
    return list(Request.objects.filter(dealer_code=g.dealer_code).filter(closed_at__isnull=True)
                .order_by("-requested_date")[:30]) or list(Request.objects.filter(invoice_no=invoice_no)[:10])


@transaction.atomic
def link_invoice(user: User, grn_pk: int, invoice_no: str, request_id: str) -> None:
    require(user, "RMA_WORK")
    g = Grn.objects.select_for_update().get(pk=grn_pk)
    lines = g.lines.filter(invoice_no=invoice_no)
    if not lines.exists():
        raise RmaError(f"Invoice {invoice_no} is not on {g}.")
    rid = clean_text(request_id)
    if not rid:
        before = sorted({str(x) for x in lines.values_list("request_id", flat=True) if x})
        lines.update(request=None)
        audit.record("RMA_GRN_UNLINKED", "rma_grn", g.pk, user=user, before={"invoice": invoice_no, "request": before})
        return
    r = Request.objects.filter(pk=rid).first()
    if r is None:
        raise RmaError(f"Request {rid} is not in the uploaded portal report yet - upload today's RMA Portal Report.")
    if r.dealer_code and r.dealer_code != g.dealer_code:
        raise RmaError(f"Request {rid} is for dealer {r.dealer_code}, the GRN is for {g.dealer_code}.")
    other = GrnLine.objects.filter(request=r).exclude(grn=g).select_related("grn").first()
    if other:
        raise RmaError(f"Request {rid} is already linked to {other.grn}.")
    lines.update(request=r)
    audit.record("RMA_GRN_LINKED", "rma_grn", g.pk, user=user,
                 after={"invoice": invoice_no, "request": rid, "how": "by the clerk",
                        "invoice_matches": r.invoice_no == invoice_no})


@transaction.atomic
def set_bill_to(user: User, request_id: str, site: str) -> Request:
    require(user, "RMA_WORK")
    r = Request.objects.select_for_update().get(pk=request_id)
    site = clean_text(site)
    if site and not CustomerSite.objects.filter(pk=site, account_no=r.dealer_code).exists():
        raise RmaError("Choose one of the dealer's Bill-To sites.")
    before = r.bill_to_site
    r.bill_to_site = site
    r.save(update_fields=["bill_to_site"])
    audit.record("RMA_BILL_TO_SET", "rma_request", r.pk, user=user, before={"site": before}, after={"site": site})
    return r

# ---------------------------------------------------------------- IRR receipt


def irr_rows(trx_numbers):
    from inward.irr.models import IrrTransaction
    return IrrTransaction.objects.filter(trx_number__in=list(trx_numbers), type_key="RMA RECEIPT")


def irr_candidates(r: Request) -> list[dict]:
    """'Rma Receipt' TRXs in the IRR with this request's SKUs, not used by another request - a suggestion only."""
    from inward.irr.models import IrrTransaction
    skus = set(r.lines.values_list("sku", flat=True))
    if not skus:
        return []
    since = (r.requested_date or timezone.localdate()) - timedelta(days=30)
    used = set(IrrLink.objects.exclude(request=r).values_list("trx_number", flat=True))
    groups = defaultdict(lambda: {"skus": defaultdict(lambda: ZERO), "date": None})
    for x in IrrTransaction.objects.filter(type_key="RMA RECEIPT", sku__in=skus, trx_date__gte=since):
        if x.trx_number in used:
            continue
        g = groups[x.trx_number]
        g["skus"][x.sku] += x.no_of_pack
        g["date"] = x.trx_date
    want = {ln.sku: ln.quantity for ln in r.lines.all()}
    out = []
    for trx, g in groups.items():
        exact = sum(1 for s, q in g["skus"].items() if want.get(s) == q)
        out.append({"trx": trx, "date": g["date"], "skus": dict(g["skus"]), "exact": exact})
    return sorted(out, key=lambda c: (-c["exact"], c["date"] or date.min))[:15]


@transaction.atomic
def add_irr(user: User, request_id: str, trx_no: str) -> IrrLink:
    require(user, "RMA_WORK")
    r = Request.objects.select_for_update().get(pk=request_id)
    if r.closed_at:
        raise RmaError("The request is closed.")
    no = clean_text(trx_no).replace(" ", "")
    if not re.match(r"^\d{1,20}$", no):
        raise RmaError("IRR TRX No: the number of the Rma Receipt in Oracle.")
    clash = IrrLink.objects.filter(trx_number=no).first()
    if clash:
        raise RmaError(f"TRX {no} is already linked to request {clash.request_id}.")
    if not irr_rows([no]).exists():
        raise RmaError(f"TRX {no} is not an 'Rma Receipt' in the imported IRR - import the latest IRR first.")
    link = IrrLink.objects.create(request=r, trx_number=no, entered_by=user)
    audit.record("RMA_IRR_LINKED", "rma_request", r.pk, user=user, after={"trx": no})
    return link


@transaction.atomic
def remove_irr(user: User, link_pk: int) -> None:
    require(user, "RMA_WORK")
    link = IrrLink.objects.select_related("request").get(pk=link_pk)
    if link.request.closed_at:
        raise RmaError("The request is closed.")
    audit.record("RMA_IRR_UNLINKED", "rma_request", link.request_id, user=user, before={"trx": link.trx_number})
    link.delete()

# ---------------------------------------------------------------- reconciliation and closure


def reconcile(r: Request) -> dict:
    """Per SKU: GRN returned pieces, portal requested quantity, CN pieces, IRR received pieces."""
    want = defaultdict(lambda: ZERO)
    names, value = {}, defaultdict(lambda: ZERO)
    for ln in r.lines.all():
        want[ln.sku] += ln.quantity
        value[ln.sku] += ln.value
        names[ln.sku] = ln.description
    grn = defaultdict(lambda: ZERO)
    cond = defaultdict(set)
    for ln in GrnLine.objects.filter(request=r):
        grn[ln.sku] += ln.ret_nop
        names.setdefault(ln.sku, ln.description)
        if ln.condition:
            cond[ln.sku].add(ln.get_condition_display())
    cn, credited = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
    cn_lines = list(CnLine.objects.filter(rma_no=r.rma_no)) if r.rma_no else []
    for ln in cn_lines:
        cn[ln.sku] += ln.cn_nop
        credited[ln.sku] += ln.cn_nop * (ln.cn_rate or ZERO)
    trx = [x.trx_number for x in r.irr_links.all()]
    irr, places = defaultdict(lambda: ZERO), defaultdict(set)
    for x in irr_rows(trx):
        irr[x.sku] += x.no_of_pack
        names.setdefault(x.sku, x.item_description)
        places[x.sku].add(f"{x.source_subinv} {x.source_locator}".strip())
    lines = []
    for sku in sorted(set(want) | set(grn) | set(cn) | set(irr)):
        vals = {"grn": grn.get(sku), "portal": want.get(sku), "cn": cn.get(sku) if cn_lines else None,
                "irr": irr.get(sku) if trx else None}
        known = [v for v in vals.values() if v is not None]
        st = ("MATCH" if len(known) == 4 and len(set(known)) == 1 else
              "PENDING" if len(set(known)) <= 1 else "MISMATCH")
        lines.append({"sku": sku, "description": names.get(sku, ""), **{k: (format(v.normalize(), "f") if v is not None
                                                                           else "") for k, v in vals.items()},
                      "status": st, "condition": ", ".join(sorted(cond[sku])),
                      "requested_value": str(value.get(sku, ZERO)), "credited_value": str(round(credited.get(sku, ZERO), 2)),
                      "locator": ", ".join(sorted(p for p in places[sku] if p))})
    if not GrnLine.objects.filter(request=r).exists():
        status = "NO_GRN"
    elif not cn_lines:
        status = "WAIT_CN"
    elif not trx:
        status = "WAIT_IRR"
    elif any(ln["status"] != "MATCH" for ln in lines):
        status = "MISMATCH"
    else:
        status = "MATCH"
    return {"status": status, "lines": lines, "trx": trx, "cn": sorted({ln.cn_no for ln in cn_lines}),
            "requested_value": str(sum(value.values(), ZERO)), "credited_value": str(round(sum(credited.values(), ZERO), 2)),
            "at": timezone.now().isoformat()}


RECON_TEXT = {"NO_GRN": "No GRN linked", "WAIT_CN": "Waiting for the Credit Note", "WAIT_IRR": "Waiting for the IRR receipt",
              "MISMATCH": "Quantities differ", "MATCH": "All quantities agree"}


def close_blockers(r: Request, recon: dict | None = None) -> list[str]:
    recon = recon or reconcile(r)
    out = []
    if r.closed_at:
        return ["Already closed."]
    if is_pending(r.status):
        out.append("The portal approval is still pending.")
    if recon["status"] in ("NO_GRN", "WAIT_CN", "WAIT_IRR"):
        out.append(RECON_TEXT[recon["status"]] + ".")
    for g in Grn.objects.filter(lines__request=r).distinct():
        if g.lines.filter(condition="").exists():
            out.append(f"{g}: pick Good / Damaged stock for every SKU line.")
    return out


@transaction.atomic
def close(user: User, request_id: str, verified_by: str, verified_on, note: str = "", accept: bool = False) -> Request:
    require(user, "RMA_CLOSE")
    r = Request.objects.select_for_update().get(pk=request_id)
    recon = reconcile(r)
    problems = close_blockers(r, recon)
    if problems:
        raise RmaError(" ".join(problems))
    if recon["status"] == "MISMATCH" and not accept:
        raise RmaError("Quantities differ - tick 'accept the difference' and write why, or correct the source first.")
    if recon["status"] == "MISMATCH" and len(clean_text(note)) < 5:
        raise RmaError("Write why the difference is accepted.")
    if len(clean_text(verified_by)) < 2:
        raise RmaError("Name of the godown person who checked the material.")
    r.verified_by, r.verified_on = clean_text(verified_by)[:120], _dt(verified_on) or timezone.localdate()
    r.closed_at, r.closed_by, r.close_note = timezone.now(), user, clean_text(note)[:500]
    r.close_kind = "RECONCILED" if recon["status"] == "MATCH" else "ACCEPTED"
    r.recon = recon
    r.save()
    audit.record("RMA_REQUEST_CLOSED", "rma_request", r.pk, user=user,
                 after={"kind": r.close_kind, "verified_by": r.verified_by, "note": r.close_note,
                        "recon": recon["status"], "trx": recon["trx"], "cn": recon["cn"]})
    from inward.rma import report
    report.generate(r, user)
    return r


@transaction.atomic
def close_rejected(user: User, request_id: str, note: str) -> Request:
    require(user, "RMA_CLOSE")
    r = Request.objects.select_for_update().get(pk=request_id)
    if r.closed_at:
        raise RmaError("Already closed.")
    if not is_rejected(r.status):
        raise RmaError("Only a request the portal shows as rejected is closed this way.")
    if len(clean_text(note)) < 5:
        raise RmaError("Write what happened to the material (sent back to the dealer / raised again as request ...).")
    r.closed_at, r.closed_by, r.close_kind, r.close_note = timezone.now(), user, "REJECTED", clean_text(note)[:500]
    r.recon = reconcile(r)
    r.save()
    audit.record("RMA_REJECTED_CLOSED", "rma_request", r.pk, user=user, after={"note": r.close_note, "status": r.status})
    return r

# ---------------------------------------------------------------- lists for screens and reports


def unlinked_grns(today: date | None = None) -> list[dict]:
    """GRN invoices with no portal request yet (the portal entry is still to be raised)."""
    today = today or timezone.localdate()
    out = []
    groups = defaultdict(list)
    for ln in GrnLine.objects.filter(request__isnull=True).select_related("grn").order_by("grn__grn_date", "grn_id"):
        groups[(ln.grn, ln.invoice_no)].append(ln)
    for (g, inv), lines in groups.items():
        days = (today - g.grn_date).days
        out.append({"grn": g, "invoice_no": inv, "invoice_date": lines[0].invoice_date, "lines": len(lines),
                    "pieces": sum((ln.ret_nop for ln in lines), ZERO), "days": days, "flag": rag(days)})
    return out


def pending_by_approver(rws: list[Row]) -> list[dict]:
    """Requests pending approval grouped by the person who must act (left -> replacement)."""
    groups: dict[int, dict] = {}
    unmapped: dict[str, dict] = {}
    approvers = {a.key: a for a in Approver.objects.select_related("redirect_to")}
    for row in rws:
        if row.stage != "APPROVAL":
            continue
        for n in pending_names(row.r.pending_at):
            a = final(approvers.get(name_key(n)))
            if a is None or not a.email:
                u = unmapped.setdefault(name_key(n), {"name": n, "rows": []})
                u["rows"].append(row)
                continue
            g = groups.setdefault(a.pk, {"approver": a, "rows": []})
            if row not in g["rows"]:
                g["rows"].append(row)
    out = []
    for g in groups.values():
        g["value"] = sum((x.r.total_value for x in g["rows"]), ZERO)
        g["overdue"] = sum(1 for x in g["rows"] if x.stage_flag == "RED")
        g["oldest"] = max((x.days_at_stage or 0) for x in g["rows"])
        out.append(g)
    return sorted(out, key=lambda g: -g["oldest"]), list(unmapped.values())
