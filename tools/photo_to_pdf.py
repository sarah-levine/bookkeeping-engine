#!/usr/bin/env python3
"""
tools/photo_to_pdf.py
----------------------
Wrap a phone-photo image (JPG/PNG/HEIC, etc.) into a single-page PDF so it
can be run through Mode A (reconcile_comprehensive.py) via the Vision/OCR
fallback, instead of falling back to manual Mode G entry.

Usage:
    python3 tools/photo_to_pdf.py <photo> [output.pdf]

If output.pdf is omitted, writes <photo-stem>.pdf next to the input file.
"""
import sys
from pathlib import Path

from PIL import Image


def photo_to_pdf(photo_path: Path, pdf_path: Path) -> None:
    img = Image.open(photo_path).convert("RGB")
    img.save(pdf_path)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    photo_path = Path(sys.argv[1])
    if not photo_path.exists():
        print(f"error: {photo_path} not found")
        sys.exit(1)

    pdf_path = Path(sys.argv[2]) if len(sys.argv) > 2 else photo_path.with_suffix(".pdf")

    photo_to_pdf(photo_path, pdf_path)
    print(f"wrote {pdf_path}")


if __name__ == "__main__":
    main()
