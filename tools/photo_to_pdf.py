#!/usr/bin/env python3
"""
tools/photo_to_pdf.py
----------------------
Wrap a phone-photo image (JPG/PNG/HEIC, etc.) into a single-page PDF so it
can be run through Mode A (reconcile_comprehensive.py) via the Vision/OCR
fallback, instead of falling back to manual Mode G entry.

HEIC/HEIF input is converted via macOS's built-in `sips` (Pillow can't read
it without the pillow-heif plugin) — this makes HEIC support macOS-only.

Phone photos are also auto-rotated upright if shot sideways/upside-down:
statement bank-type detection (reconcile_comprehensive.detect_statement_type)
and the Vision/OCR parsing fallback both expect right-side-up text, and
phone photos of paper statements commonly aren't.

Multiple photos (e.g. each page of a multi-page statement, photographed
separately) can be merged into one ordered multi-page PDF:

    python3 tools/photo_to_pdf.py <page1> <page2> [<page3> ...] --merge output.pdf

Usage:
    python3 tools/photo_to_pdf.py <photo> [output.pdf]
    python3 tools/photo_to_pdf.py <page1> <page2> ... --merge output.pdf

If output.pdf is omitted (single-photo form), writes <photo-stem>.pdf next
to the input file.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, UnidentifiedImageError

try:
    import pytesseract
    _OCR_AVAILABLE = True
except ImportError:
    _OCR_AVAILABLE = False

# Generic statement vocabulary used only to score candidate orientations —
# not bank-specific, so this works as a pre-processing step for any bank's
# scanned/photographed statement, not just BMO.
_ORIENTATION_TOKENS = ('STATEMENT', 'ACCOUNT', 'BALANCE', 'PAYMENT', 'BANK', 'DATE')


def _upright(img: Image.Image) -> Image.Image:
    """Return `img` rotated so its text reads right-side-up, detected by
    trying all 4 rotations on a downsampled copy and picking whichever OCRs
    the most recognizable statement vocabulary. Best-effort: returns `img`
    unchanged if tesseract isn't installed or no rotation scores a hit."""
    if not _OCR_AVAILABLE:
        return img

    preview = img.copy()
    preview.thumbnail((1000, 1000))

    best_angle, best_score = 0, -1
    for angle in (0, 90, 180, 270):
        candidate = preview.rotate(angle, expand=True)
        try:
            ocr = pytesseract.image_to_string(candidate).upper()
        except Exception:
            continue
        score = sum(ocr.count(tok) for tok in _ORIENTATION_TOKENS)
        if score > best_score:
            best_angle, best_score = angle, score

    if best_score <= 0:
        return img  # no rotation found recognizable text — leave as-is
    return img.rotate(best_angle, expand=True)


def _load_upright_image(photo_path: Path) -> Image.Image:
    """Open `photo_path` (converting HEIC via sips if needed) and return it
    rotated upright. Shared by both the single-photo and multi-photo (merge)
    paths."""
    try:
        img = Image.open(photo_path).convert("RGB")
    except UnidentifiedImageError:
        # Pillow can't read HEIC/HEIF without the pillow-heif plugin. Rather
        # than add that dependency for a macOS-only format, shell out to
        # sips (built into macOS) to get a PNG Pillow can read directly.
        if shutil.which("sips") is None:
            raise
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_png:
            tmp_png_path = tmp_png.name
        subprocess.run(
            ["sips", "-s", "format", "png", str(photo_path), "--out", tmp_png_path],
            check=True, capture_output=True,
        )
        img = Image.open(tmp_png_path).convert("RGB")
        Path(tmp_png_path).unlink()

    return _upright(img)


def _save_with_sane_resolution(img: Image.Image, pdf_path: Path) -> None:
    # Pillow defaults to 72 DPI when saving as PDF if no resolution is given,
    # which treats every pixel as a full point (1/72in). For a real phone
    # photo (thousands of px wide) that claims a PDF page tens of inches
    # across — e.g. a 5712x4284 photo becomes a "59.5x79.3 inch" page. When
    # extractors/vision_helper.py then renders that page at a fixed 200 DPI
    # for the Vision API, the compounded size (~12000x16000px) blows past
    # the API's request-size limit (confirmed live: 413 request_too_large).
    # Assume these are standard US Letter statements and derive a resolution
    # from actual pixel count instead, so the PDF always claims a sane
    # physical page size regardless of source resolution.
    short_side_in = 8.5  # portrait Letter width; used as the short side either way
    resolution = min(img.width, img.height) / short_side_in
    img.save(pdf_path, resolution=resolution)


def photo_to_pdf(photo_path: Path, pdf_path: Path) -> None:
    img = _load_upright_image(photo_path)
    _save_with_sane_resolution(img, pdf_path)


def photos_to_pdf(photo_paths: list[Path], pdf_path: Path) -> None:
    """Merge multiple photos (e.g. each page of a multi-page statement,
    photographed separately) into one ordered multi-page PDF. Each photo
    goes through the same prep as photo_to_pdf() (HEIC conversion, upright
    rotation, sane-resolution PDF encoding) before being appended in the
    order given.

    Uses fitz (PyMuPDF) rather than pypdf — pypdf isn't a guaranteed
    dependency in this environment (not in requirements.txt; reconcile_
    comprehensive.py's own pypdf usage already has an ImportError fallback
    for exactly this reason), while fitz is used throughout this codebase's
    OCR paths and is always available.
    """
    import fitz
    merged = fitz.open()
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, photo_path in enumerate(photo_paths):
            img = _load_upright_image(photo_path)
            page_pdf = Path(tmpdir) / f"page_{i}.pdf"
            _save_with_sane_resolution(img, page_pdf)
            with fitz.open(page_pdf) as page_doc:
                merged.insert_pdf(page_doc)
    merged.save(pdf_path)
    merged.close()


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    if "--merge" in args:
        idx = args.index("--merge")
        if idx + 1 >= len(args) or idx == 0:
            print("error: --merge requires photo(s) before it and an output path after it")
            sys.exit(1)
        photo_paths = [Path(p) for p in args[:idx]]
        pdf_path = Path(args[idx + 1])
        for p in photo_paths:
            if not p.exists():
                print(f"error: {p} not found")
                sys.exit(1)
        photos_to_pdf(photo_paths, pdf_path)
        print(f"wrote {pdf_path} ({len(photo_paths)} pages)")
        return

    photo_path = Path(args[0])
    if not photo_path.exists():
        print(f"error: {photo_path} not found")
        sys.exit(1)

    pdf_path = Path(args[1]) if len(args) > 1 else photo_path.with_suffix(".pdf")

    photo_to_pdf(photo_path, pdf_path)
    print(f"wrote {pdf_path}")


if __name__ == "__main__":
    main()
