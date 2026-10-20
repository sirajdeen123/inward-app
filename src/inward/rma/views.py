"""Material Return screens (thin; rules in services.py)."""
from __future__ import annotations

import tempfile
from pathlib import Path

from django.contrib import messages
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from inward.accounts.services import PermissionDenied, modes_for
from inward.core import documents
from inward.importers.common import ImportFailed
from inward.rma import mails, register, report
from inward.rma import services as svc
from inward.rma.models import Approver, Condition, CustomerSite, Grn, IrrLink, Request, Territory, TransportMode
from inward.web.views import needs, upload_done

ERRORS = (svc.RmaError, ImportFailed, PermissionDenied)
UPLOADS = {"PORTAL": ("RMA Portal Report", svc.import_portal), "CN": ("RMA CN INV Report", svc.import_cn),
           "CUSTOMER": ("Customer Master", svc.import_customer_master), "TSI": ("TSI Master", svc.import_tsi)}
MAX_BYTES = 50 * 1024 * 1024


def _can(user, perm: str) -> bool:
    return "GRANT" in modes_for(user, perm)


@needs("RMA_WORK")
def index(request):
    stage = request.GET.get("stage", "APPROVAL")
    q = request.GET.get("q", "").strip()
    qs = Request.objects.all()
    if stage != "CLOSED":
        qs = qs.filter(closed_at__isnull=True)
    if q:
        qs = qs.filter(Q(request_id__icontains=q) | Q(dealer_name__icontains=q) | Q(dealer_code=q) |
                       Q(invoice_no=q) | Q(rma_no=q) | Q(pending_at__icontains=q) | Q(grn_lines__grn__grn_no__icontains=q)).distinct()
    all_rows = svc.rows(qs)
    counts = {code: 0 for code, _ in svc.STAGES}
    for x in all_rows:
        counts[x.stage] += 1
    shown = [x for x in all_rows if x.stage == stage] if stage != "GRN" else []
    shown.sort(key=lambda x: -(x.days_at_stage or 0))
    unlinked = svc.unlinked_grns()
    unmapped = Approver.objects.filter(active=True, email="", redirect_to__isnull=True).count()
    uploads = [(k, label, svc.last_upload(f"RMA_{k}")) for k, (label, _) in UPLOADS.items()]
    return render(request, "rma/index.html", {
        "stage": stage, "q": q, "stages": [(c, label, counts.get(c, 0)) for c, label in svc.STAGES], "rows": shown[:300],
        "unlinked": unlinked, "unmapped": unmapped, "uploads": uploads, "upload_kinds": UPLOADS,
        "grns": Grn.objects.prefetch_related("lines")[:60] if stage == "GRN" else [],
        "can_master": _can(request.user, "RMA_MASTER"), "cc_rcm": mails.cc_rcm(), "as_on": mails.as_on()})


def _save_upload(upload, suffixes) -> Path:
    if Path(upload.name).suffix.lower() not in suffixes:
        raise svc.RmaError(f"{upload.name}: expected a {' / '.join(suffixes)} file.")
    if upload.size > MAX_BYTES:
        raise svc.RmaError(f"{upload.name} is larger than 50 MB.")
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(upload.name).suffix.lower()) as tmp:
        for chunk in upload.chunks():
            tmp.write(chunk)
    return Path(tmp.name)


@require_POST
@needs("RMA_WORK")
def upload(request):
    kind = request.POST.get("kind", "")
    f = request.FILES.get("file")
    back = reverse("rma:index")
    if f is None:
        messages.error(request, "Choose the file.")
        return upload_done(request, back)
    try:
        if kind == "GRN":
            if f.size > 25 * 1024 * 1024:
                raise svc.RmaError("The PDF is larger than 25 MB.")
            res = svc.upload_grn(request.user, f.read(), f.name)
            for p in res["problems"]:
                messages.error(request, p)
            if res["created"]:
                messages.success(request, f"{len(res['created'])} GRN(s) saved: {', '.join(g.grn_no for g in res['created'])}"
                                          + (f"; {res['linked']} invoice(s) linked to their portal request" if res["linked"] else "")
                                          + ". Pick Good / Damaged stock and the gate entry on each.")
            if res["skipped"]:
                messages.info(request, f"Already saved earlier (skipped): {', '.join(res['skipped'])}.")
            if len(res["created"]) == 1:
                back = reverse("rma:grn", args=[res["created"][0].pk])
            else:
                back = reverse("rma:index") + "?stage=GRN"
        elif kind in UPLOADS:
            label, fn = UPLOADS[kind]
            path = _save_upload(f, (".xls", ".xlsx"))
            try:
                res = fn(request.user, path, f.name)
            finally:
                path.unlink(missing_ok=True)
            if kind == "PORTAL":
                messages.success(request, f"{label}: {res['requests']} request(s) - {res['new']} new, {res['changed']} "
                                          f"moved to another stage / approver; {res['linked']} GRN invoice(s) linked.")
                if res["unmapped"]:
                    messages.warning(request, f"{res['unmapped']} approver name(s) have no e-mail yet - map them on "
                                              "Approvers so the pending mails reach them.")
            elif kind == "CN":
                messages.success(request, f"{label}: {res['lines']} line(s) of {res['credit_notes']} credit note(s)"
                                          + (f"; {res['duplicates']} repeated line(s) counted once" if res["duplicates"] else "") + ".")
            elif kind == "CUSTOMER":
                messages.success(request, f"{label}: {res['sites']} Bill-To site(s) in {res['territories']} territories.")
            else:
                messages.success(request, f"{label}: {res['territories']} territories"
                                          + (f", {res['without_mail']} without a Sales Officer e-mail" if res["without_mail"] else "") + ".")
        else:
            raise svc.RmaError("Choose what is being uploaded.")
    except ERRORS as exc:
        messages.error(request, str(exc))
    return upload_done(request, back)

# ---------------------------------------------------------------- GRN


@needs("RMA_WORK")
def grn(request, pk):
    g = get_object_or_404(Grn, pk=pk)
    if request.method == "POST":
        conds = {}
        for ln in g.lines.all():
            v = request.POST.get(f"cond_{ln.pk}")
            if v is not None:
                conds[ln.pk] = v
        try:
            svc.update_grn(request.user, g.pk, request.POST, conds)
            messages.success(request, f"{g} saved.")
            return redirect("rma:grn", pk=g.pk)
        except ERRORS as exc:
            messages.error(request, str(exc))
    invoices = {}
    for ln in g.lines.select_related("request"):
        inv = invoices.setdefault(ln.invoice_no, {"no": ln.invoice_no, "date": ln.invoice_date, "lines": [], "request": None})
        inv["lines"].append(ln)
        inv["request"] = inv["request"] or ln.request
    for inv in invoices.values():
        inv["candidates"] = [] if inv["request"] else svc.link_candidates(g, inv["no"])
    return render(request, "rma/grn.html", {
        "g": g, "invoices": list(invoices.values()), "conditions": Condition.choices, "modes": TransportMode.choices,
        "missing": svc.grn_missing(g), "so": svc.SalesOfficers().for_site(g.site_code)})


@require_POST
@needs("RMA_WORK")
def grn_link(request, pk):
    g = get_object_or_404(Grn, pk=pk)
    try:
        svc.link_invoice(request.user, g.pk, request.POST.get("invoice_no", ""), request.POST.get("request_id", ""))
        messages.success(request, "Link saved.")
    except ERRORS as exc:
        messages.error(request, str(exc))
    return redirect("rma:grn", pk=g.pk)


@needs("RMA_WORK")
def grn_pdf(request, pk):
    g = get_object_or_404(Grn.objects.select_related("document"), pk=pk)
    if g.document is None:
        raise Http404
    resp = HttpResponse(documents.read(g.document), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{g.grn_no.replace("/", "-")}.pdf"'
    return resp

# ---------------------------------------------------------------- request


@needs("RMA_WORK")
def detail(request, request_id):
    r = get_object_or_404(Request, pk=request_id)
    row = svc.rows(Request.objects.filter(pk=r.pk))[0]
    recon = r.recon if r.closed_at and r.recon else svc.reconcile(r)
    approvers = []
    for n in svc.pending_names(r.pending_at) if svc.is_pending(r.status) else []:
        a = svc.approver_for(n, create=False)
        approvers.append({"name": n, "a": a, "final": svc.final(a)})
    from inward.core.models import DocumentLink
    pdfs = [x.document for x in DocumentLink.objects.filter(entity_type="rma_request", entity_id=r.pk, role="handover_pdf")
            .select_related("document").order_by("-document__created_at")]
    return render(request, "rma/request.html", {
        "r": r, "row": row, "recon": recon, "recon_text": svc.RECON_TEXT.get(recon["status"], recon["status"]),
        "approvers": approvers, "lines": r.lines.all(), "grn_lines": r.grn_lines.select_related("grn"),
        "cn_lines": svc.CnLine.objects.filter(rma_no=r.rma_no) if r.rma_no else [],
        "irr_links": r.irr_links.select_related("entered_by"),
        "candidates": svc.irr_candidates(r) if not r.closed_at and r.rma_no else [],
        "sites": svc.territories_of(r.dealer_code) if not r.closed_at else [],
        "blockers": svc.close_blockers(r, recon) if not r.closed_at else [],
        "can_close": _can(request.user, "RMA_CLOSE"), "events": r.events.all(), "pdfs": pdfs})


def _act(request, request_id, fn, ok):
    try:
        fn()
        messages.success(request, ok)
    except ERRORS as exc:
        messages.error(request, str(exc))
    return redirect("rma:detail", request_id=request_id)


@require_POST
@needs("RMA_WORK")
def irr_add(request, request_id):
    trx = request.POST.get("trx", "")
    return _act(request, request_id, lambda: svc.add_irr(request.user, request_id, trx), f"IRR TRX {trx} linked.")


@require_POST
@needs("RMA_WORK")
def irr_remove(request, request_id, link_pk):
    get_object_or_404(IrrLink, pk=link_pk, request_id=request_id)
    return _act(request, request_id, lambda: svc.remove_irr(request.user, link_pk), "IRR TRX removed.")


@require_POST
@needs("RMA_WORK")
def bill_to(request, request_id):
    return _act(request, request_id, lambda: svc.set_bill_to(request.user, request_id, request.POST.get("site", "")),
                "Bill-To site saved - the Sales Officer follows it.")


@require_POST
@needs("RMA_WORK")
def close(request, request_id):
    p = request.POST
    if p.get("rejected"):
        return _act(request, request_id, lambda: svc.close_rejected(request.user, request_id, p.get("note", "")),
                    f"Request {request_id} closed (rejected).")
    return _act(request, request_id,
                lambda: svc.close(request.user, request_id, p.get("verified_by", ""), p.get("verified_on", ""),
                                  p.get("note", ""), bool(p.get("accept"))),
                f"Request {request_id} closed.")


@needs("RMA_WORK")
def handover(request, request_id):
    r = get_object_or_404(Request, pk=request_id)
    doc_id = request.GET.get("doc")
    if doc_id:
        from inward.core.models import DocumentLink
        link = get_object_or_404(DocumentLink, entity_type="rma_request", entity_id=r.pk, role="handover_pdf",
                                 document_id=doc_id)
        resp = HttpResponse(documents.read(link.document), content_type="application/pdf")
        resp["Content-Disposition"] = f'inline; filename="{link.document.original_filename}"'
        return resp
    doc = report.generate(r, request.user)
    if doc is None:     # no WeasyPrint on this PC: the same sheet as a page
        return HttpResponse(report.html(r))
    resp = HttpResponse(documents.read(doc), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{doc.original_filename}"'
    return resp

# ---------------------------------------------------------------- masters, register, report preview


@needs("RMA_MASTER")
def approvers(request):
    if request.method == "POST":
        done = 0
        for a in Approver.objects.all():
            if f"email_{a.pk}" not in request.POST:
                continue
            try:
                before = (a.email, a.active, a.redirect_to_id)
                a2 = svc.save_approver(request.user, a.pk, request.POST.get(f"email_{a.pk}", ""),
                                       request.POST.get(f"left_{a.pk}") != "1", request.POST.get(f"redirect_{a.pk}", ""))
                done += before != (a2.email, a2.active, a2.redirect_to_id)
            except ERRORS as exc:
                messages.error(request, str(exc))
        for t in Territory.objects.all():
            key = f"so_{t.territory_no}"
            if key in request.POST and request.POST[key].strip().lower() != (t.email_override or ""):
                try:
                    svc.save_territory_mail(request.user, t.territory_no, request.POST[key])
                    done += 1
                except ERRORS as exc:
                    messages.error(request, str(exc))
        messages.success(request, f"Saved - {done} change(s).")
        return redirect("rma:approvers")
    rws = svc.rows(Request.objects.filter(closed_at__isnull=True))
    groups, unmapped = svc.pending_by_approver(rws)
    counts = {g["approver"].pk: len(g["rows"]) for g in groups}
    waiting = {svc.name_key(u["name"]): len(u["rows"]) for u in unmapped}
    people = list(Approver.objects.select_related("redirect_to"))
    for a in people:
        a.pending = counts.get(a.pk, 0) or waiting.get(a.key, 0)
    people.sort(key=lambda a: (bool(a.email) or bool(a.redirect_to_id), a.name))
    return render(request, "rma/approvers.html", {
        "people": people, "territories": Territory.objects.all(),
        "sites": CustomerSite.objects.count(), "cc_rcm": mails.cc_rcm()})


@needs("RMA_WORK")
def register_xlsx(request):
    resp = HttpResponse(register.build(),
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    from django.utils import timezone
    resp["Content-Disposition"] = f'attachment; filename="Materials Return Register {timezone.localdate():%d-%m-%Y}.xlsx"'
    return resp


@needs("RMA_WORK")
def daily_preview(request):
    to, cc, missing = mails.daily_recipients()
    return render(request, "rma/daily_preview.html", {"r": mails.daily_report(), "to": to, "cc": cc, "missing": missing})
