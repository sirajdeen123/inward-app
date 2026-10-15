"""Server-side PDF from Django HTML templates (WeasyPrint, SoT 7.4).

WeasyPrint needs the Pango libraries, installed on the server (Ubuntu packages). On a development PC without
them `available()` is False; callers then keep the HTML version and say that the PDF is made on the server.
The page's stylesheet is passed to WeasyPrint directly (the browser preview links the same static file).
"""
from __future__ import annotations

from functools import lru_cache

from django.contrib.staticfiles import finders
from django.template.loader import render_to_string


@lru_cache(maxsize=1)
def available() -> bool:
    try:
        import weasyprint  # noqa: F401
    except (ImportError, OSError):
        return False
    return True


def render_html(template: str, context: dict) -> str:
    return render_to_string(template, context)


def html_to_pdf(html: str, stylesheet: str = "inward/mra.css") -> bytes | None:
    if not available():
        return None
    import weasyprint
    css = finders.find(stylesheet)
    return weasyprint.HTML(string=html).write_pdf(stylesheets=[weasyprint.CSS(filename=css)] if css else None)
