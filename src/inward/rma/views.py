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
from inward.rma import autofetch, mails, register, report
from inward.rma import services as svc
from inward.rma.models import Approver, ApproverMail, Condition, CustomerSite, Grn, IrrLink, Request, Territory, TransportMode
from inward.web.views import needs, upload_done

ERRORS = (svc.RmaError, ImportFailed, PermissionDenied)
UPLOADS = {"PORTAL": ("RMA Portal Report", svc.import_portal), "CN": ("RMA CN INV Report", svc.import_cn),
           "CUSTOMER": ("Dealer master (Customer Master)", svc.import_customer_master),
           "TSI": ("TSI Master", svc.import_tsi)}
UPLOAD_HELP = [
    ("GRN", "GRN - Goods Return Note (PDF)", "When the material arrives", "Oracle GRN print; one GRN or a bulk file"),
    ("PORTAL", None, "Every day", "Both sheets: RMA Summary and RMA Details"),
    ("CN", None, "When credit notes are raised", "Oracle XXGST BPIL RMA CN INV Report"),
    ("CUSTOMER", None, "Monthly, or when dealers / territories change",
     "BPIL Customer Master Summary - dealer Bill-To site -> territory"),
    ("TSI", None, "When a Sales Officer changes", "Territory, Sales rep, BL, Email"),
]


@require_POST
@needs("RMA_WORK")
def cn_fetch_now(request):
    from inward.mail.gmail import from_settings
    try:
        out = autofetch.import_from_mail(from_settings(), request.user)
        (messages.success if out.status == "imported" else messages.warning)(request, out.message)
    except ERRORS as exc:
        messages.error(request, str(exc))
    except Exception as exc:  # noqa: BLE001 - mailbox not reachable etc.
        messages.error(request, f"Gmail could not be read: {exc}")
    return redirect("rma:index")


@require_POST
@needs("RMA_MASTER")
def cn_fetch_settings(request):
    from inward.core import audit
    from inward.core.models import FeatureFlag
    vals = {"rma_cn_mail_subject": request.POST.get("subject", "").strip(),
            "rma_cn_fetch_time": request.POST.get("fetch_time", "").strip(),
            "rma_cn_fetch_deadline": request.POST.get("deadline", "").strip()}
    import re
    if len(vals["rma_cn_mail_subject"]) < 5 or not all(re.match(r"^\d{1,2}:\d{2}$", vals[k])
                                                       for k in ("rma_cn_fetch_time", "rma_cn_fetch_deadline")):
        messages.error(request, "Enter the subject text and the times as HH:MM.")
        return redirect("rma:index")
    for k, v in vals.items():
        f = FeatureFlag.objects.filter(key=k).first()
        old = (f.value or {}).get("value") if f else None
        if old != v:
            FeatureFlag.objects.update_or_create(key=k, defaults={"value": {"value": v}})
            audit.record("FEATURE_FLAG_CHANGED", "feature_flag", k, user=request.user, before={"value": old},
                         after={"value": v})
    messages.success(request, "CN report mail settings saved.")
    return redirect("rma:index")


def upload_rows() -> list[dict]:
    from inward.rma.models import CustomerSite, Grn, Territory
    counts = {"GRN": f"{Grn.objects.count()} GRN(s) saved" if Grn.objects.exists() else "",
              "CUSTOMER": f"{CustomerSite.objects.count()} Bill-To sites" if CustomerSite.objects.exists() else "",
              "TSI": f"{Territory.objects.count()} territories" if Territory.objects.exists() else ""}
    out = []
    auto = autofetch.settings_now()
    for kind, label, when, note in UPLOAD_HELP:
        if kind == "CN":
            when = f"Fetched from Gmail every morning from {auto['fetch_time']:%I:%M %p} (upload by hand only if needed)"
        out.append({"kind": kind, "label": label or UPLOADS[kind][0], "when": when, "note": note,
                    "batch": None if kind == "GRN" else svc.last_upload(f"RMA_{kind}"), "count": counts.get(kind, ""),
                    "accept": "application/pdf,.pdf" if kind == "GRN" else ".xls,.xlsx"})
    return out
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
    if stage == "BEFORE":
        shown = svc.rows(qs.filter(tracked=False), untracked=True)
    else:
        shown = [x for x in all_rows if x.stage == stage] if stage != "GRN" else []
    shown.sort(key=lambda x: -(x.days_at_stage or 0))
    shown = shown[:300]
    if stage == "IRR":
        for x in shown:
            x.suggestions = len(svc.irr_candidates(x.r))
    if stage == "CLOSE":
        for x in shown:
            x.recon = svc.reconcile(x.r)
            x.recon_text = svc.RECON_TEXT.get(x.recon["status"], x.recon["status"])
            x.blockers = svc.close_blockers(x.r, x.recon)
    unlinked = svc.unlinked_grns()
    unmapped = Approver.objects.filter(active=True, email="", redirect_to__isnull=True).count()
    uploads = upload_rows()
    return render(request, "rma/index.html", {
        "stage": stage, "q": q, "stages": [(c, label, counts.get(c, 0)) for c, label in svc.STAGES], "rows": shown,
        "before": Request.objects.filter(tracked=False).count(), "can_close": _can(request.user, "RMA_CLOSE"),
        "go_live": svc.go_live(),
        "unlinked": unlinked, "unmapped": unmapped, "uploads": uploads,
        "grns": Grn.objects.prefetch_related("lines")[:60] if stage == "GRN" else [],
        "can_master": _can(request.user, "RMA_MASTER"), "cc_rcm": mails.cc_rcm(), "as_on": mails.as_on(),
        "cn_auto": autofetch.settings_now(), "cn_today": autofetch.today_status()})


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
                                          f"moved to another stage / approver; {res['linked']} GRN invoice(s) linked"
                                          + (f"; {res['irr_linked']} IRR receipt(s) linked automatically" if res["irr_linked"] else "")
                                          + (f"; {res['before_go_live']} already processed / rejected before go-live kept "
                                             "for reference only" if res["before_go_live"] else "") + ".")
                if res["unmapped"]:
                    messages.warning(request, f"{res['unmapped']} approver name(s) have no e-mail yet - map them on "
                                              "Approvers so the pending mails reach them.")
            elif kind == "CN":
                messages.success(request, f"{label}: {res['lines']} line(s) of {res['credit_notes']} credit note(s)"
                                          + (f"; {res['duplicates']} repeated line(s) counted once" if res["duplicates"] else "")
                                          + (f"; {res['irr_linked']} IRR receipt(s) linked automatically" if res["irr_linked"] else "") + ".")
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
    row = svc.rows(Request.objects.filter(pk=r.pk), untracked=True)[0]
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
        "can_close": _can(request.user, "RMA_CLOSE"), "events": r.events.all(), "pdfs": pdfs,
        "steps": svc.steps(r, recon),
        "urgent_sent": ApproverMail.objects.filter(kind="URGENT", requests__contains=[r.pk]).select_related("sent_by")
        .order_by("-created_at")[:5], "can_urgent": _can(request.user, "RMA_URGENT_MAIL")})


@require_POST
@needs("RMA_WORK")
def track(request, request_id):
    on = request.POST.get("on") == "1"
    return _act(request, request_id, lambda: svc.set_tracked(request.user, request_id, on),
                "Tracked again - it is chased like any open return." if on else "Kept for reference only.")


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


@needs("RMA_URGENT_MAIL")
def urgent(request):
    """Urgent approval reminder: tick the approver group(s) and the request(s), give the reason, preview, send."""
    only = request.GET.get("request", "") or request.POST.get("only", "")
    groups = mails.urgent_groups(only)
    picks = {g["key"]: {x.r.request_id for x in g["rows"] if request.POST.get(f"r_{g['key']}_{x.r.request_id}")}
             for g in groups if request.POST.get(f"g_{g['key']}")} if request.method == "POST" else {}
    reason_kind, reason_text = request.POST.get("reason_kind", ""), request.POST.get("reason_text", "")
    for g in groups:
        g["picked"] = bool(only) if request.method != "POST" else g["key"] in picks
        for x in g["rows"]:
            x.picked = request.method != "POST" or x.r.request_id in picks.get(g["key"], set())
    preview = []
    if request.method == "POST":
        try:
            if request.POST.get("action") == "send":
                sent = mails.send_urgent(request.user, picks, reason_kind, reason_text, bool(request.POST.get("force")))
                messages.success(request, f"Urgent reminder sent ({len(sent)} mail(s)): " + "; ".join(
                    f"{', '.join(m.mail.to)} - request(s) {', '.join(m.requests)}" for m in sent) + ".")
                return redirect(reverse("rma:detail", args=[only]) if only else reverse("rma:index"))
            if not any(picks.values()):
                raise svc.RmaError("Tick at least one approver and request.")
            if reason_kind not in mails.URGENT_REASONS:
                raise svc.RmaError("Choose why it is urgent.")
            cc_base = mails.approval_cc()
            for g in groups:
                ids = picks.get(g["key"])
                if ids:
                    rows = [x for x in g["rows"] if x.r.request_id in ids]
                    sos = sorted({x.sales_officer[1] for x in rows if x.sales_officer and x.sales_officer[1]})
                    preview.append({"g": g, "rows": rows, "to": [a.email for a in g["approvers"]],
                                    "cc": [c for c in cc_base + sos if c not in [a.email for a in g["approvers"]]]})
        except (svc.RmaError, PermissionDenied) as exc:
            messages.error(request, str(exc))
    return render(request, "rma/urgent.html", {
        "groups": groups, "only": only, "reasons": mails.URGENT_REASONS, "picks": picks, "preview": preview,
        "reason_kind": reason_kind, "reason_text": reason_text, "posted": request.method == "POST"})


@needs("RMA_WORK")
def daily_preview(request):
    to, cc, missing = mails.daily_recipients()
    return render(request, "rma/daily_preview.html", {"r": mails.daily_report(), "to": to, "cc": cc, "missing": missing})
