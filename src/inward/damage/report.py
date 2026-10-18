"""Internal Damage Report PDF (18-Oct-2026): one document per report - the list, each line's photos under it, and
the approval / TRX / reconciliation once known. Attached to the approval mail instead of loose photos, refreshed
after the decision and after the reconciliation; every version is kept. The photos themselves stay stored as taken.
"""
from __future__ import annotations

import base64

from django.utils import timezone

from inward.core import branding, documents, pdf
from inward.damage.models import DamageReport

STAGES = {"submitted": "sent for approval", "decided": "after the approval reply", "reconciled": "reconciled",
          "requested": "on request"}


def _data_uri(doc) -> str:
    return f"data:{doc.content_type};base64,{base64.b64encode(documents.read(doc)).decode()}"


def html(report: DamageReport, for_pdf: bool = True) -> str:
    from inward.web.context import depot_info
    lines = []
    for ln in report.lines.select_related("category").prefetch_related("photos__preview", "photos__original"):
        photos = [{"src": _data_uri(p.preview or p.original), "n": i} for i, p in enumerate(ln.photos.all(), start=1)]
        lines.append({"ln": ln, "photos": photos})
    return pdf.render_html("damage/report_pdf.html", {
        "r": report, "lines": lines, "trx": list(report.trx.all()), "depot": depot_info(),
        "logo": branding.logo_data_uri(), "generated": timezone.localtime(), "for_pdf": for_pdf})


def generate(report: DamageReport, stage: str, user=None):
    """Store a new version of the report PDF (None where WeasyPrint is not installed, e.g. a development PC)."""
    data = pdf.html_to_pdf(html(report), "inward/recon.css")
    if data is None:
        return None
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    return documents.store(data, filename=f"{report.number} R{report.revision} {stage} {stamp}.pdf",
                           content_type="application/pdf", doc_type="INTERNAL_DAMAGE_REPORT", user=user,
                           links=[("damage_report", report.pk, "report_pdf")])


def versions(report: DamageReport):
    from inward.core.models import DocumentLink
    return [link.document for link in DocumentLink.objects.filter(entity_type="damage_report", entity_id=str(report.pk),
                                                                  role="report_pdf").select_related("document")
            .order_by("-document__created_at")]
