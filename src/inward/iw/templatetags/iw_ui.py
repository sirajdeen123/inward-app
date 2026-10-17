"""IW status colours and the 'next step' guide shown on every IW screen (UAT 17-Oct-2026)."""
from __future__ import annotations

from django import template
from django.utils.html import format_html

register = template.Library()


@register.simple_tag
def iw_status(iw):
    """<span class="chip st st-CLOSED">Closed</span> - the colour follows the stage (app.css .st-*)."""
    return format_html('<span class="chip st st-{}">{}</span>', iw.status, iw.get_status_display())


@register.inclusion_tag("iw/_next.html", takes_context=True)
def next_step(context, iw):
    """The first missing action of the IW with a button to the screen that does it, plus the progress strip."""
    from inward.iw import ops
    request = context.get("request")
    acts = [] if iw.status in ("CLOSED", "VOID") else ops.actions(iw)
    here = request.path if request else ""
    return {"iw": iw, "first": acts[0] if acts else None, "more": acts[1:4], "here": here,
            "progress": ops.progress(iw) if iw.status not in ("DRAFT", "VOID") else []}
