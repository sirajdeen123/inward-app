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
