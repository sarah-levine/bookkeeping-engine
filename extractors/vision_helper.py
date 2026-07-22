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
1. Transaction amounts (inside "payments"/"credits"/"charges") are always
   POSITIVE numbers — sign is implied by which array they're in, do not use
   negative numbers there.
   EXCEPTION for previous_balance/new_balance specifically: if the statement
   prints "CR" directly after the dollar amount (e.g. "$238.36 CR"), that
   marks a CREDIT balance (the cardholder is owed money, not the other way
   around) — use a NEGATIVE number for that field. Without this, the balance
   equation in rule 5 can never tie for a statement in a credit state, even
   with every transaction read correctly.
2. Use the POST DATE for each transaction (the second date column if there are two), formatted MM/DD/YY with a 2-digit year inferred from the billing period end date.
3. Vendor names: copy them EXACTLY as printed, including reference numbers and city codes (e.g. "AMAZON MKTPL*B51AOOK11 Amzn.com/billWA", "APPLE.COM/BILL 866-712-7753 CA"). Downstream code will normalize them — do not pre-clean.
4. Distinguish transaction types:
   - "payments" array: lines labeled ONLINE PAYMENT, AUTOPAY, THANK YOU, or similar. Description should always be "PAYMENT - THANK YOU".
   - "credits" array: refunds and returns (negative amounts in the statement that aren't payments — e.g. a Home Depot or Lowes refund).
   - "charges" array: all positive purchase charges.
5. The balance equation MUST tie to the penny:
   previous_balance + sum(charges) - total_payments - sum(credits) == new_balance
   (previous_balance and/or new_balance negative if either is a CR/credit balance — see rule 1.)
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


def _parse_json_response(raw: str) -> dict:
    """Parse `raw` as JSON, tolerating a model that second-guesses itself
    mid-response — e.g. outputs one JSON object, then prose like "Wait, let
    me recalculate..." followed by a corrected second JSON object. Plain
    json.loads() chokes on the trailing content ("Extra data") in that case.
    Scans for every top-level JSON object in the text and returns the LAST
    one that parses, since a self-correction is the model's final answer.
    Confirmed live: this exact pattern hit a real BMO statement extraction.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    idx = 0
    last_obj = None
    while idx < len(raw):
        brace = raw.find("{", idx)
        if brace == -1:
            break
        try:
            obj, end = decoder.raw_decode(raw, brace)
            last_obj = obj
            idx = end
        except json.JSONDecodeError:
            idx = brace + 1

    if last_obj is None:
        raise json.JSONDecodeError("No valid JSON object found", raw, 0)
    return last_obj


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
        data = _parse_json_response(raw)
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


# ── Check-image payee extraction ────────────────────────────────────────────
# Separate from the balance-recovery path above: a narrower task (read one
# name off each check image) with its own prompt/response shape, not the
# balance-extraction JSON schema. Reuses is_available()/MAX_PAGES_PER_REQUEST/
# _strip_code_fences — nothing above this section is modified.

CHECK_PAYEE_PROMPT_TEMPLATE = """You are reading {n} images of bank checks, cropped from a "Check images" page in a business checking statement.

For each check image, in the exact order given, find the payee — the name written on the "PAY TO THE ORDER OF" line.

Return a SINGLE valid JSON object — no markdown, no commentary, no code fences. The object must match this schema EXACTLY:

{{
  "payees": ["<payee name>", "<payee name>", ...]
}}

The list must have EXACTLY {n} entries, one per image, in the same order as the images were provided. If a payee name is illegible or a check image is blank/unreadable, use an empty string "" for that entry rather than guessing.

Return ONLY the JSON object."""


def _call_claude_vision_for_check_payees(image_bytes_list: list[bytes]) -> str:
    """Send check images to Claude, return raw response text."""
    import anthropic

    client = anthropic.Anthropic()

    batch = image_bytes_list[:MAX_PAGES_PER_REQUEST]
    content = []
    for img in batch:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(img).decode("ascii"),
            },
        })
    content.append({"type": "text", "text": CHECK_PAYEE_PROMPT_TEMPLATE.format(n=len(batch))})

    response = client.messages.create(
        model=VISION_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(b.text for b in response.content if b.type == "text")


def extract_check_payees(image_bytes_list: list[bytes]) -> list[str]:
    """
    Extract payee names from a list of check images via Claude Vision, in
    the same order the images were given.

    Returns a list of payee strings the same length as image_bytes_list
    (padded with "" if Vision returns fewer than expected, truncated if
    more) — an illegible individual check just becomes "", never a raised
    error for that one check.

    Raises RuntimeError if the API is unavailable, no images were given,
    there are more images than MAX_PAGES_PER_REQUEST, or the response can't
    be parsed as the expected JSON shape.
    """
    ok, reason = is_available()
    if not ok:
        raise RuntimeError(f"Vision helper unavailable: {reason}")

    if not image_bytes_list:
        raise RuntimeError("No check images provided")

    if len(image_bytes_list) > MAX_PAGES_PER_REQUEST:
        raise RuntimeError(
            f"{len(image_bytes_list)} check images exceeds the "
            f"{MAX_PAGES_PER_REQUEST}-image per-request limit — batch into "
            f"smaller groups before calling."
        )

    raw = _call_claude_vision_for_check_payees(image_bytes_list)
    raw = _strip_code_fences(raw)

    try:
        data = _parse_json_response(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Vision returned non-JSON output: {e}\nFirst 500 chars: {raw[:500]}"
        )

    payees = data.get("payees")
    if not isinstance(payees, list):
        raise RuntimeError(f"Vision response missing/invalid 'payees' list: {data!r}")

    n = len(image_bytes_list)
    cleaned = [str(p) if p else "" for p in payees[:n]]
    cleaned += [""] * (n - len(cleaned))
    return cleaned
