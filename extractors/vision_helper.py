"""
Vision-based fallback for scanned/image-quality PDFs.

When the standard pdftotext-based parse fails to tie out (balance equation
doesn't balance to the penny), the parser invokes this helper to re-extract
transactions from PDF page images using Claude Vision.

The helper returns data in the parser's NATIVE internal shape:

    {
        "previous_balance": Decimal,
        "new_balance":      Decimal,
        "total_payments":   Decimal,
        "payments":  [{"date": "MM/DD/YY", "description": str, "amount": Decimal}, ...],
        "credits":   [{"date": "MM/DD/YY", "description": str, "amount": Decimal}, ...],
        "charges":   [{"date": "MM/DD/YY", "vendor":      str, "amount": Decimal}, ...],
    }

The calling parser assigns these directly onto self, then continues into
generate_report() which runs the existing aggregator + vendor normalization
+ report formatter. No new schema, no new report format.

REQUIRES:
  - ANTHROPIC_API_KEY environment variable
  - anthropic Python SDK   (pip install anthropic)
  - PyMuPDF (fitz)         (pip install pymupdf)
"""

import base64
import json
import os
import re
from decimal import Decimal


VISION_MODEL = "claude-sonnet-4-5"
MAX_PAGES_PER_REQUEST = 8
EPS = Decimal("0.01")


def is_available() -> tuple[bool, str]:
    """Return (available, reason). Cheap probe — does not call the API."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False, "ANTHROPIC_API_KEY not set in environment"
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, "anthropic SDK not installed (pip install anthropic)"
    try:
        import fitz  # noqa: F401
    except ImportError:
        return False, "PyMuPDF not installed (pip install pymupdf)"
    return True, "ready"


def tied_out(previous_balance, new_balance, total_payments, credits, charges) -> bool:
    """
    The self-check: prev + charges - payments - credits == new_balance.

    Pure function — takes parser fields, returns bool. Used both to decide
    whether to invoke vision AND to validate vision's response.
    """
    if previous_balance is None or new_balance is None:
        return False
    if not new_balance and not previous_balance:
        return False
    total_credits = sum((Decimal(str(c["amount"])) for c in credits), Decimal("0"))
    total_charges = sum((Decimal(str(c["amount"])) for c in charges), Decimal("0"))
    computed = (Decimal(str(previous_balance))
                + total_charges
                - Decimal(str(total_payments))
                - total_credits)
    return abs(computed - Decimal(str(new_balance))) < EPS


def _render_pdf_to_images(pdf_path: str, dpi: int = 200) -> list[bytes]:
    """Render each PDF page to PNG bytes."""
    import fitz
    images = []
    doc = fitz.open(pdf_path)
    try:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            images.append(pix.tobytes("png"))
    finally:
        doc.close()
    return images


# The prompt asks for JSON that maps DIRECTLY onto parser internals.
# Description strings should be the RAW vendor name as printed on the statement —
# the parser's own normalize_vendor() + _aggregate_by_vendor() will clean them up
# downstream. Do NOT pre-aggregate or pre-normalize here.
EXTRACTION_PROMPT = """You are reading a scanned credit-card or bank statement.

Return a SINGLE valid JSON object — no markdown, no commentary, no code fences. The object must match this schema EXACTLY:

{
  "previous_balance": <number>,
  "new_balance":      <number>,
  "total_payments":   <number, positive>,
  "payments": [
    {"date": "MM/DD/YY", "description": "PAYMENT - THANK YOU", "amount": <positive number>}
  ],
  "credits": [
    {"date": "MM/DD/YY", "description": "<vendor name as printed>", "amount": <positive number>}
  ],
  "charges": [
    {"date": "MM/DD/YY", "vendor": "<vendor name as printed>", "amount": <positive number>}
  ]
}

CRITICAL RULES:
1. All amounts are POSITIVE numbers. Payments and credits are tracked in their own arrays — do not use negative numbers.
2. Use the POST DATE for each transaction (the second date column if there are two), formatted MM/DD/YY with a 2-digit year inferred from the billing period end date.
3. Vendor names: copy them EXACTLY as printed, including reference numbers and city codes (e.g. "AMAZON MKTPL*B51AOOK11 Amzn.com/billWA", "APPLE.COM/BILL 866-712-7753 CA"). Downstream code will normalize them — do not pre-clean.
4. Distinguish transaction types:
   - "payments" array: lines labeled ONLINE PAYMENT, AUTOPAY, THANK YOU, or similar. Description should always be "PAYMENT - THANK YOU".
   - "credits" array: refunds and returns (negative amounts in the statement that aren't payments — e.g. a Home Depot or Lowes refund).
   - "charges" array: all positive purchase charges.
5. The balance equation MUST tie to the penny:
   previous_balance + sum(charges) - total_payments - sum(credits) == new_balance
6. Ignore handwritten margin notes — they are NOT part of the statement data.
7. Do NOT invent transactions. Only include what you can clearly read.

VERIFY YOUR OUTPUT before returning: compute the balance equation and confirm it ties. If it doesn't, re-read the statement and fix the errors before responding.

Return ONLY the JSON object."""


def _call_claude_vision(image_bytes_list: list[bytes]) -> str:
    """Send images to Claude, return raw response text."""
    import anthropic

    client = anthropic.Anthropic()

    content = []
    for img in image_bytes_list[:MAX_PAGES_PER_REQUEST]:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(img).decode("ascii"),
            },
        })
    content.append({"type": "text", "text": EXTRACTION_PROMPT})

    response = client.messages.create(
        model=VISION_MODEL,
        max_tokens=8000,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(b.text for b in response.content if b.type == "text")


def _strip_code_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    return s


def extract(pdf_path: str) -> dict:
    """
    Extract parser-native data from a scanned PDF via Claude Vision.

    Returns a dict with keys: previous_balance, new_balance, total_payments,
    payments, credits, charges.

    Raises RuntimeError if the API is unavailable or the response can't be
    parsed. Raises ValueError if the extracted data fails the balance check.
    """
    ok, reason = is_available()
    if not ok:
        raise RuntimeError(f"Vision helper unavailable: {reason}")

    images = _render_pdf_to_images(str(pdf_path))
    if not images:
        raise RuntimeError("Could not render any pages from PDF")

    raw = _call_claude_vision(images)
    raw = _strip_code_fences(raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Vision returned non-JSON output: {e}\nFirst 500 chars: {raw[:500]}"
        )

    # Coerce numeric fields to Decimal — matches what existing parsers store
    result = {
        "previous_balance": Decimal(str(data.get("previous_balance", 0))),
        "new_balance":      Decimal(str(data.get("new_balance", 0))),
        "total_payments":   Decimal(str(data.get("total_payments", 0))),
        "payments":  [],
        "credits":   [],
        "charges":   [],
    }
    for p in data.get("payments", []):
        result["payments"].append({
            "date": p["date"],
            "description": p.get("description", "PAYMENT - THANK YOU"),
            "amount": Decimal(str(p["amount"])),
        })
    for c in data.get("credits", []):
        result["credits"].append({
            "date": c["date"],
            "description": c.get("description", ""),
            "amount": Decimal(str(c["amount"])),
        })
    for ch in data.get("charges", []):
        result["charges"].append({
            "date": ch["date"],
            "vendor": ch.get("vendor", ""),
            "amount": Decimal(str(ch["amount"])),
        })

    # Validate. If vision returned data that doesn't tie out, raise — the
    # caller will fall through to manual entry rather than silently using
    # bad data.
    if not tied_out(
        result["previous_balance"], result["new_balance"],
        result["total_payments"], result["credits"], result["charges"],
    ):
        total_credits = sum(c["amount"] for c in result["credits"])
        total_charges = sum(c["amount"] for c in result["charges"])
        computed = (result["previous_balance"] + total_charges
                    - result["total_payments"] - total_credits)
        raise ValueError(
            f"Vision extraction did not tie out: computed new balance "
            f"{computed}, statement says {result['new_balance']} "
            f"(off by {computed - result['new_balance']})"
        )

    return result
