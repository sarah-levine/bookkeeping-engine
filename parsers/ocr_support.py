"""Shared OCR-library availability check for parsers with a scanned-PDF OCR
fallback path (pdftotext extracts nothing usable, so fitz renders each page
to an image and pytesseract reads it). Previously this exact try/except was
copy-pasted into every parser file; most never actually used fitz/pytesseract
at all (dead code left over from a shared template) — only
NorthernTrustCheckingParser and BMOCreditCardParser/BMOCheckingParser have a
real OCR fallback, so those two import from here instead of redefining it.
"""
try:
    import fitz
    import pytesseract
    from PIL import Image
    import io as _io
    OCR_AVAILABLE = True
except ImportError:
    fitz = None
    pytesseract = None
    Image = None
    _io = None
    OCR_AVAILABLE = False


def zoom_for_target_width(page, target_width=1600, min_zoom=0.5, max_zoom=4.0):
    """Zoom factor to render `page` at ~target_width px, not a fixed
    multiplier. A fixed multiplier (e.g. matrix=(1.0, 1.0) or (2, 2)) render
    is only reliable for OCR when the source PDF's page size (in points)
    happens to already be in the right range — for a page whose size was
    naively derived from source pixel count (e.g. a phone photo saved as a
    PDF at a wrong DPI, before tools/photo_to_pdf.py started setting
    resolution explicitly), a fixed multiplier can silently render far too
    small (illegible) or far too large (slow, or over an API request-size
    limit) depending on the source. Targeting an absolute pixel width keeps
    OCR quality consistent regardless of the page's point dimensions.
    """
    native_width = page.rect.width  # points, i.e. px at zoom=1.0
    if native_width <= 0:
        return 1.0
    return max(min_zoom, min(max_zoom, target_width / native_width))
