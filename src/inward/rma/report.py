"""Material return handover sheet (PDF): the per-SKU reconciliation the godown checks the material against, with the
GRN, RMA, CN and IRR references. Printed before the check; a final version is stored when the request is closed."""
from __future__ import annotations

from django.utils import timezone

from inward.core import branding, documents, pdf
from inward.rma import services as svc
from inward.rma.models import Grn, Request


def html(r: Request) -> str:
    from inward.web.context import depot_info
    recon = r.recon if r.closed_at and r.recon else svc.reconcile(r)
    grns = list(Grn.objects.filter(lines__request=r).distinct())
    return pdf.render_html("rma/handover_pdf.html", {
        "r": r, "recon": recon, "recon_text": svc.RECON_TEXT.get(recon["status"], recon["status"]), "grns": grns,
        "trail": svc.trail(r), "depot": depot_info(), "logo": branding.logo_data_uri(), "generated": timezone.localtime()})


def generate(r: Request, user=None):
    """Store the sheet as PDF (None where WeasyPrint is not installed, e.g. a development PC)."""
    data = pdf.html_to_pdf(html(r), "inward/recon.css")
    if data is None:
        return None
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    kind = "closed" if r.closed_at else "for check"
    return documents.store(data, filename=f"Return {r.request_id} handover {kind} {stamp}.pdf",
                           content_type="application/pdf", doc_type="RMA_HANDOVER", user=user,
                           links=[("rma_request", r.pk, "handover_pdf")])
