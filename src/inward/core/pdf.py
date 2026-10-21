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


FIT_LEVELS = (1, 2, 3)   # body classes fit1..fit3 in mra.css: progressively tighter spacing / smaller type


def pages_wanted(html: str) -> int:
    return html.count('<section class="page"')


def html_to_pdf(html: str, stylesheet: str = "inward/mra.css", fit: bool = False) -> bytes | None:
    """fit=True (MRA, 22-Oct-2026): when the document runs onto more pages than it has sections (e.g. only the
    signature block falls onto a second page), it is laid out again a little tighter, up to three steps; the first
    layout that keeps every section on its own page is used. If none fits (a long SED list), the normal layout
    stays and simply continues on the next page."""
    if not available():
        return None
    import weasyprint
    css = finders.find(stylesheet)
    sheets = [weasyprint.CSS(filename=css)] if css else None
    doc = weasyprint.HTML(string=html).render(stylesheets=sheets)
    want = pages_wanted(html) if fit else 0
    if want and len(doc.pages) > want and "<body>" in html:
        for level in FIT_LEVELS:
            tight = weasyprint.HTML(string=html.replace("<body>", f'<body class="fit{level}">', 1)).render(stylesheets=sheets)
            if len(tight.pages) <= want:
                doc = tight
                break
    return doc.write_pdf()


class ImageError(ValueError):
    pass


SCAN_PX = 2400   # long side; an A4 page at about 200 dpi - plenty for a signed MRA or an LR


def images_to_pdf(images: list[bytes]) -> bytes:
    """Camera photos / scanned images -> one PDF, one page per image (works without WeasyPrint).
    The photo is turned upright (EXIF), reduced to SCAN_PX on the long side and kept as JPEG inside the PDF."""
    import io

    from PIL import Image, ImageOps, UnidentifiedImageError
    pages = []
    for data in images:
        try:
            with Image.open(io.BytesIO(data)) as img:
                page = ImageOps.exif_transpose(img).convert("RGB")
        except (UnidentifiedImageError, OSError, SyntaxError) as exc:
            raise ImageError("The photo could not be read - take it again or upload a PDF.") from exc
        page.thumbnail((SCAN_PX, SCAN_PX))
        pages.append(page)
    if not pages:
        raise ImageError("No photo.")
    out = io.BytesIO()
    pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=200.0, quality=85)
    return out.getvalue()
