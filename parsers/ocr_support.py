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
