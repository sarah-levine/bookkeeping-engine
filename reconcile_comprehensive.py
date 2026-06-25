#!/usr/bin/env python3
"""
Unified Bank Statement Reconciliation Tool

Supported statement formats:
  Credit Cards:  Chase Ink Business, Chase United, American Express, BofA Credit Card,
                 Citi Costco Anywhere Visa, Wells Fargo Signify
  Checking:      Citi Business Checking, BofA Business Checking, Wells Fargo Checking,
                 BMO Premium Business Checking, Northern Trust Checking, US Bank Checking,
                 American Express Business Checking
  Savings:       BofA Business Savings

Usage:
  python reconcile_comprehensive.py <statement.pdf> [output.txt]
  python reconcile_comprehensive.py <statement.pdf> [output.txt] --check-payee 1235='Jane Doe'
"""

import sys
import re
import subprocess
import zipfile
import tempfile
import os
from pathlib import Path
from decimal import Decimal
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

def _now_pst():
    """Return current datetime in US/Pacific (PST/PDT)."""
    return datetime.now(ZoneInfo('America/Los_Angeles'))

try:
    import fitz
    import pytesseract
    from PIL import Image
    import io as _io
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════════════════
# CLIENT REGISTRY — loads all client configs from clients/*.json
# Adding a new client = create a new JSON file, no code changes needed.
# ═══════════════════════════════════════════════════════════════════════════════

import json

# Import all parsers and report helpers from the parsers package
from parsers import (
    StatementParser, ClientRegistry, _registry,
    ChaseParser, AmexStatementParser, AmexCheckingParser,
    ChaseInkParser, ChaseUnitedParser,
    WellsFargoCreditCardParser, WellsFargoCheckingParser,
    BankOfAmericaCreditCardParser, BankOfAmericaCheckingParser, BankOfAmericaSavingsParser,
    CitiCheckingParser, CitiVisaCostcoParser, CitiSavingsParser,
    BMOCheckingParser, BMOCreditCardParser, USBankCheckingParser, NorthernTrustCheckingParser,
    _safe_date_key, _report_header, _summary_block, _balance_check,
    _payments_section, _credits_section, _individual_section,
    _deposits_section, _checks_section, _adp_section,
    _cc_payments_section, _add_missing_row, _charges_section,
)

# Vendor normalization + interactive vendor approval now live in a single
# source of truth (parsers.base / parsers.vendor_normalize). Re-exported here
# for backward compatibility; do not redefine.
from parsers.base import (
    _normalize_vendor_for_client,
    _auto_clean_vendor,
    _collect_unknown_vendors,
    _prompt_approve_new_vendors,
    _VENDOR_PROMPT_BLOCKLIST,
    _US_STATE_CODES,
)


def _check_statement_date(date_str: str, client_name: str, account_type: str) -> str | None:
    """Return a warning string if date_str doesn't match the expected closing day, else None.

    Looks up cc_blocking_rules in digest_config.json. Only fires for CC accounts
    that have a configured closing_day. Allows ±2 days for month-end edge cases.
    """
    try:
        from datetime import datetime
        from log_utils import load_private_json
        cfg = load_private_json("digest_config.json") or {}
        display_names = cfg.get("client_display_names", {})
        rules_map = cfg.get("cc_blocking_rules", {})

        # Resolve display name for this client
        display = display_names.get(client_name.strip().lower(), client_name.strip())
        client_rules = rules_map.get(display)
        if not client_rules:
            return None

        blocker = next(
            (b for b in client_rules.get("cc_blockers", []) if b["key"] == account_type),
            None,
        )
        if not blocker:
            return None

        closing_day = int(blocker["closing_day"])
        parsed = None
        for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(date_str.strip(), fmt).date()
                break
            except ValueError:
                pass
        if parsed is None:
            return None

        if abs(parsed.day - closing_day) > 2:
            return (
                f"⚠️  Statement date {date_str} (day {parsed.day}) doesn't match "
                f"expected closing day {closing_day} for {account_type}. "
                f"Expected a date around the {closing_day}th."
            )
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════
# CLIENT DETECTION
# All vendor normalization is now data-driven via clients/*.json
# ═══════════════════════════════════════════════════════════════════════════════

# Known client names for auto-detection (loaded from registry)
KNOWN_CLIENTS = _registry.KNOWN_CLIENTS

# Canonical name resolution (loaded from registry)
CLIENT_CANONICAL = _registry.CLIENT_CANONICAL

# Empty — kept for any remaining references during transition
CLIENT_NORMALIZERS = {}


def _classify_cc_transaction(vendor, amount):
    """
    Classify a credit card transaction as 'payment', 'credit', or 'charge'.
    Returns one of: 'payment', 'credit', 'charge'
    """
    v = vendor.upper()
    # Actual payments to the card account
    if any(kw in v for kw in [
        'AUTOMATIC PAYMENT', 'PAYMENT - THANK YOU', 'ELECTRONIC PAYMENT',
        'ONLINE PAYMENT', 'AUTOPAY PAYMENT', 'PAYMENT RECEIVED',
    ]):
        return 'payment'
    # Also treat negative amounts with PAYMENT keyword as payments
    if amount < 0 and 'PAYMENT' in v:
        return 'payment'
    # Credits / refunds / returns
    if any(kw in v for kw in [
        'MKTPLACE PMTS', 'MKTPL PMTS', 'AMAZON MKTPLACE',
        'CREDIT', 'RETURN', 'REFUND', 'WIRELESS CREDIT',
        'AMEX CREDIT',
    ]):
        return 'credit'
    # Negative amounts that aren't payments are credits
    if amount < 0:
        return 'credit'
    return 'charge'


# Cardholder names per client (for multi-cardholder AmEx statements)
# NOTE: Loaded dynamically from clients/*.json via _registry.CLIENT_CARDHOLDERS above.
# Do not hardcode here — it would overwrite the registry.


# ═══════════════════════════════════════════════════════════════════════════════
# BASE PARSER
# ═══════════════════════════════════════════════════════════════════════════════

def detect_statement_type(pdf_path):
    """
    Returns one of:
      'chase_ink', 'chase_united', 'amex',
      'bofa_credit', 'bofa_checking', 'bofa_savings',
      'citi_checking', 'unknown'
    """
    # AmEx statements are sometimes delivered as zip containers with .txt pages
    try:
        with zipfile.ZipFile(pdf_path, 'r') as z:
            txt_files = [n for n in z.namelist() if n.endswith('.txt')]
            if txt_files:
                with z.open(sorted(txt_files)[0]) as f:
                    sample = f.read().decode('utf-8', errors='replace').upper()
                if 'AMERICAN EXPRESS' in sample or any(c in sample for c in KNOWN_CLIENTS):
                    return 'amex'
    except Exception:
        pass

    # Fall back to pdftotext
    try:
        result = subprocess.run(
            ['pdftotext', '-layout', str(pdf_path), '-'],
            capture_output=True, text=True, check=True
        )
        text = result.stdout.upper()
    except Exception:
        return 'unknown'

    # Wells Fargo checking — must check BEFORE BofA to avoid false positive on 'YOUR CHECKING ACCOUNT'
    if 'INITIATE BUSINESS CHECKING' in text or ('WELLS FARGO' in text and 'TRANSACTION HISTORY' in text and 'DEPOSITS/CREDITS' in text and 'WITHDRAWALS/DEBITS' in text and 'SIGNIFY BUSINESS ESSENTIAL' not in text[:500]):
        return 'wells_fargo_checking'

    # BofA — check savings before checking (savings text contains checking-like phrases)
    if 'YOUR SAVINGS ACCOUNT' in text or 'YOUR BUSINESS INVESTMENT ACCOUNT' in text:
        return 'bofa_savings'
    if ('YOUR CHECKING ACCOUNT' in text or
            'BUSINESS ADVANTAGE FUNDAMENTALS' in text or
            'BUSINESS ADVANTAGE RELATIONSHIP BANKING' in text):
        return 'bofa_checking'
    if ('BUSINESS ADVANTAGE CASH REWARDS' in text or
            'COMPANY STATEMENT' in text or
            ('BUSINESS CARD' in text and 'NEW BALANCE TOTAL' in text)):
        return 'bofa_credit'

    # AmEx — distinguish Business Checking from credit card statements
    if 'AMERICAN EXPRESS' in text:
        # Credit card statements have "Business Platinum Card", "New Balance", "Payment Due Date"
        # and NO "BUSINESS CHECKING ACCOUNT STATEMENT"
        if 'BUSINESS PLATINUM CARD' in text or 'BUSINESS GOLD CARD' in text or 'BUSINESS GREEN CARD' in text:
            return 'amex'  # Credit card
        # Checking account statements explicitly say "BUSINESS CHECKING ACCOUNT STATEMENT"
        if ('BUSINESS CHECKING ACCOUNT STATEMENT' in text or
                'AMERICANEXPRESS.COM/BUSINESSCHECKING' in text):
            return 'amex_checking'
        # If it has "AMERICAN EXPRESS NATIONAL BANK" but also has card indicators, it's a credit card
        if 'AMERICAN EXPRESS NATIONAL BANK' in text and 'New Balance' in text and 'Payment Due Date' in text:
            return 'amex'  # Credit card statement showing national bank as processor
        # Default for AmEx
        if 'AMERICAN EXPRESS NATIONAL BANK' in text:
            return 'amex_checking'
        return 'amex'

    # U.S. Bank Business Checking
    if 'U.S. BANK' in text and 'BUSINESS CHECKING' in text:
        return 'usbank_checking'
    if 'USBANK.COM' in text and ('BUSINESS STATEMENT' in text or 'BUSINESS CHECKING' in text):
        return 'usbank_checking'

    # Chase — gate on Chase's own statement boilerplate, NOT the bare word
    # 'CHASE'. A non-Chase statement (e.g. Citi checking) can contain 'CHASE'
    # as a credit-card-payment payee or inside 'PURCHASE'; those must fall
    # through to their real bank. chase.com / Cardmember Service / JPMorgan Chase
    # appear on every Chase card statement regardless of whether the card name
    # is rendered as a logo image.
    is_chase = ('CHASE.COM' in text or 'JPMORGAN CHASE' in text or
                'CARDMEMBER SERVICE' in text or 'SAPPHIRE' in text or
                'INK BUSINESS' in text or 'CHASE INK' in text or
                'UNITED CLUB' in text or 'MILEAGEPLUS' in text)
    if is_chase:
        # Most-specific product names first. NOTE: 'ULTIMATE REWARDS' is NOT an
        # Ink signal (Sapphire earns it too), so it is not used here.
        if 'SAPPHIRE' in text:
            return 'chase_sapphire'
        if 'INK BUSINESS' in text or 'CHASE INK' in text:
            return 'chase_ink'
        if 'UNITED CLUB' in text or 'MILEAGEPLUS' in text:
            return 'chase_united'
        # Card name only in a logo image — disambiguate by card last-4 via config.
        m = re.search(r'ACCOUNT NUMBER[:\s]+((?:[X\d]{4}\s*){2,})', text)
        if m:
            digits = re.findall(r'\d{4}', m.group(1))
            if digits:
                hit = _registry.lookup_account_ending(digits[-1])
                if hit:
                    return hit[1]
        # Confidently Chase but card type indeterminate — don't guess.
        return 'unknown'

    # Citi — check most-specific first (Costco Visa before generic Citi)
    if ('COSTCO ANYWHERE VISA' in text or
            ('CITI' in text and 'COSTCO' in text and 'CHECKING' not in text)):
        return 'citi_visa_costco'
    if 'CITIBANK' in text and 'CHECKING' in text:
        return 'citi_checking'
    if 'CITI' in text and 'CHECKING' in text:
        return 'citi_checking'
    if ('CITIBANK' in text or 'CITI' in text) and 'SAVINGS' in text:
        return 'citi_savings'

    # Wells Fargo checking
    if 'INITIATE BUSINESS CHECKING' in text or ('TRANSACTION HISTORY' in text and 'DEPOSITS/CREDITS' in text and 'WITHDRAWALS/DEBITS' in text and 'SIGNIFY BUSINESS ESSENTIAL' not in text[:500]):
        return 'wells_fargo_checking'

    # Wells Fargo credit card
    if 'WELLS FARGO' in text and ('SIGNIFY' in text or 'BUSINESS ESSENTIAL' in text or 'BUSINESS CARD' in text):
        return 'wells_fargo_credit'

    # BMO — credit card before checking (credit card has no checking-account keywords)
    if 'BMO' in text and ('BUSINESS PLATINUM' in text or 'PLATINUM REWARDS' in text
                          or 'REWARDS CREDIT CARD' in text):
        if 'MONTHLY ACTIVITY DETAILS' not in text and 'BEGINNING BALANCE' not in text:
            return 'bmo_credit'
    # BMO — detect by BMO logo text or account header
    # Note: OCR sometimes reads 'BMO' as 'pmo' or 'BmoO', so check multiple signals
    if (('BMO' in text or 'PMO' in text) and
            ('MONTHLY ACTIVITY DETAILS' in text or
             'BEGINNING BALANCE' in text or
             'PREMIUM BUSINESS CKG' in text or
             'TELLER DEPOSIT' in text)):
        return 'bmo_checking'
    # Strong secondary signal: BMO-specific layout without logo text
    if ('MONTHLY ACTIVITY DETAILS' in text and
            'BEGINNING BALANCE' in text and
            'TELLER DEPOSIT' in text and
            'ACH DEPOSIT' in text and
            'BOFA' not in text and 'BANK OF AMERICA' not in text and
            'CHASE' not in text and 'WELLS FARGO' not in text):
        return 'bmo_checking'

    # Northern Trust — scanned image PDF. Detect by the bank name in the
    # filename; otherwise the OCR pass below reads the bank name off the
    # scanned statement body (generic, no client-specific tokens).
    fname = str(pdf_path).upper()
    if 'NORTHERN' in fname:
        return 'northern_trust_checking'
    # Try OCR detection for image-only PDFs
    try:
        import fitz
        from PIL import Image
        import pytesseract
        doc = fitz.open(pdf_path)
        page = doc[0]
        mat = fitz.Matrix(1.5, 1.5)
        pix = page.get_pixmap(matrix=mat)
        img = Image.frombytes('RGB', [pix.width, pix.height], pix.samples)
        ocr = pytesseract.image_to_string(img).upper()
        doc.close()
        if 'NORTHERN TRUST' in ocr:
            return 'northern_trust_checking'
    except Exception:
        pass

    # BMO — image-only PDFs return empty pdftotext; try OCR on first page
    try:
        import fitz
        from PIL import Image
        import pytesseract
        doc = fitz.open(pdf_path)
        page = doc[0]
        mat = fitz.Matrix(0.8, 0.8)  # lower res for fast detection
        pix = page.get_pixmap(matrix=mat)
        img = Image.frombytes('RGB', [pix.width, pix.height], pix.samples)
        ocr = pytesseract.image_to_string(img).upper()
        doc.close()
        if (('BMO' in ocr or 'PMO' in ocr) and
                ('BUSINESS PLATINUM' in ocr or 'PLATINUM REWARDS' in ocr
                 or 'REWARDS CREDIT CARD' in ocr or 'INDIVIDUAL BILL ACCOUNT' in ocr)
                and 'MONTHLY ACTIVITY DETAILS' not in ocr
                and 'BEGINNING BALANCE' not in ocr):
            return 'bmo_credit'
        if (('BMO' in ocr or 'PMO' in ocr) and
                ('MONTHLY ACTIVITY DETAILS' in ocr or
                 'BEGINNING BALANCE' in ocr)):
            return 'bmo_checking'
        if ('MONTHLY ACTIVITY DETAILS' in ocr and
                'BEGINNING BALANCE' in ocr and
                'TELLER DEPOSIT' in ocr):
            return 'bmo_checking'
    except Exception:
        pass

    # Subprocess fallback: pdftoppm + tesseract (no Python packages needed)
    # Works wherever 'brew install poppler tesseract' has been run.
    try:
        import tempfile, os as _os
        with tempfile.TemporaryDirectory() as _tmpdir:
            _prefix = _os.path.join(_tmpdir, 'p')
            subprocess.run(
                ['pdftoppm', '-png', '-r', '120', '-l', '1', str(pdf_path), _prefix],
                check=True, capture_output=True, timeout=30,
            )
            _pages = sorted(Path(_tmpdir).glob('p-*.png')) or sorted(Path(_tmpdir).glob('p*.png'))
            if _pages:
                _r = subprocess.run(
                    ['tesseract', str(_pages[0]), 'stdout'],
                    capture_output=True, text=True, check=True, timeout=30,
                )
                ocr = _r.stdout.upper()
                if (('BMO' in ocr or 'PMO' in ocr) and
                        ('BUSINESS PLATINUM' in ocr or 'PLATINUM REWARDS' in ocr
                         or 'REWARDS CREDIT CARD' in ocr or 'INDIVIDUAL BILL ACCOUNT' in ocr)
                        and 'MONTHLY ACTIVITY DETAILS' not in ocr
                        and 'BEGINNING BALANCE' not in ocr):
                    return 'bmo_credit'
                if (('BMO' in ocr or 'PMO' in ocr) and
                        ('MONTHLY ACTIVITY DETAILS' in ocr or 'BEGINNING BALANCE' in ocr)):
                    return 'bmo_checking'
    except Exception:
        pass

    return 'unknown'


# ═══════════════════════════════════════════════════════════════════════════════
# COMBINED PDF SPLITTING
# ═══════════════════════════════════════════════════════════════════════════════

# Keywords that identify each statement type by page content
_PAGE_TYPE_SIGNATURES = {
    'meevo_register':   [['MR080'], ['REGISTER SUMMARY']],
    'meevo_inventory':  [['MI210'], ['INVENTORY VALUE']],
    'citi_visa_costco': [['COSTCO ANYWHERE VISA', 'CITICARDS', 'CITI.COM/COSTCO',
                          'COSTCO CASH BACK REWARDS', 'CITIBANK']],
    'bmo_credit':       [['BMO', 'BUSINESS PLATINUM'], ['REWARDS CREDIT CARD']],
    'bmo_checking':     [['BMO', 'MONTHLY ACTIVITY DETAILS', 'BEGINNING BALANCE'],
                         ['BMO', 'PREMIUM BUSINESS CKG']],
    'bofa_checking':    [['BUSINESS ADVANTAGE FUNDAMENTALS'], ['YOUR CHECKING ACCOUNT'], ['BUSINESS ADVANTAGE RELATIONSHIP BANKING']],
    'bofa_credit':      [['BUSINESS ADVANTAGE CASH REWARDS'], ['NEW BALANCE TOTAL']],
    'bofa_savings':     [['YOUR SAVINGS ACCOUNT'], ['BUSINESS INVESTMENT ACCOUNT']],
    'amex':             [['AMERICAN EXPRESS']],
    'amex_checking':    [['AMERICAN EXPRESS', 'BUSINESS CHECKING ACCOUNT STATEMENT']],
    'usbank_checking':  [['U.S. BANK', 'BUSINESS CHECKING'], ['USBANK.COM', 'BUSINESS CHECKING'], ['U.S. BANK SILVER']],
    'chase_ink':        [['CHASE INK', 'INK BUSINESS CASH', 'INK BUSINESS PREFERRED',
                          'INK BUSINESS UNLIMITED']],
    'chase_sapphire':   [['CHASE SAPPHIRE'], ['SAPPHIRE PREFERRED'], ['SAPPHIRE RESERVE']],
    'chase_united':     [['CHASE UNITED', 'UNITED CLUB', 'MILEAGEPLUS']],
}

# Statement types we should skip (not financial statements)
_SKIP_TYPES = {'meevo_register', 'meevo_inventory'}


def _classify_page(page_text):
    """Return the statement type for a single page of text.
    Strips spaces to handle OCR-spaced characters like 'J o J o'."""
    upper = page_text.upper()
    # Also check a space-stripped version for OCR-spaced text
    nospace = re.sub(r'\s+', '', upper)

    for stmt_type, sig_groups in _PAGE_TYPE_SIGNATURES.items():
        # Each sig_group is a list of alternatives — any one must match
        # sig_groups itself is a list of groups; ALL groups must have at least one match
        all_groups_match = True
        for group in sig_groups:
            group_match = any(
                (s in upper) or (s.replace(' ', '') in nospace)
                for s in group
            )
            if not group_match:
                all_groups_match = False
                break
        if all_groups_match:
            return stmt_type
    return 'unknown'


def _citi_account_types(pdf_path):
    """Return the Citi deposit account types present in a CitiBusiness statement.

    One PDF can bundle both a checking and a savings/IMMA account, each of which
    must be reconciled separately. Returns ['citi_checking'], ['citi_savings'],
    or both. The activity-section/account-name markers (not the relationship
    summary, which always lists every account) decide what's actually present.
    """
    try:
        text = subprocess.run(
            ['pdftotext', '-layout', str(pdf_path), '-'],
            capture_output=True, text=True, check=True
        ).stdout.upper()
    except Exception:
        return ['citi_checking']
    types = []
    if 'CHECKING ACTIVITY' in text or 'STREAMLINED CHECKING' in text:
        types.append('citi_checking')
    if 'SAVINGS ACTIVITY' in text or 'IMMA' in text or 'MONEY MARKET' in text:
        types.append('citi_savings')
    return types or ['citi_checking']


def split_combined_pdf(pdf_path):
    """
    Detect whether a PDF contains multiple statement types (e.g. combined files
    that bundle a Meevo Register Summary + Citi Costco statement).

    Returns a list of (stmt_type, tmp_pdf_path) tuples for each distinct statement
    found, skipping non-financial pages (Meevo reports).  If the PDF is a single
    statement, returns [(detected_type, original_path)].

    Caller is responsible for deleting any temp files created here.
    """
    try:
        import pypdf
    except ImportError:
        # pypdf not available — fall back to full-PDF detection
        full_type = detect_statement_type(pdf_path)
        if full_type in ('citi_checking', 'citi_savings'):
            citi_types = _citi_account_types(pdf_path)
            if len(citi_types) > 1:
                return [(t, str(pdf_path)) for t in citi_types]
        return [(full_type, str(pdf_path))]

    try:
        reader = pypdf.PdfReader(pdf_path)
    except Exception:
        full_type = detect_statement_type(pdf_path)
        return [(full_type, str(pdf_path))]

    if len(reader.pages) <= 1:
        full_type = detect_statement_type(pdf_path)
        return [(full_type, str(pdf_path))]

    # Classify pages FIRST — before calling detect_statement_type on the full
    # PDF — so that Meevo/inventory pages cannot pollute detection.
    # Bug fixed: a Meevo Register Summary lists "AMERICAN EXPRESS" as a payment
    # type; running detect_statement_type on the full PDF would return 'amex'
    # and the early-exit below would bypass the Meevo-strip logic entirely.
    # We now only early-exit when there are NO skip-type pages at all.
    page_types = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ''
        except Exception:
            text = ''
        page_types.append(_classify_page(text))

    # Check if there's more than one distinct non-skip type
    unique_types = set(t for t in page_types if t not in _SKIP_TYPES and t != 'unknown')
    all_types = set(page_types)
    has_skip = bool(_SKIP_TYPES & all_types)

    # Only use full-PDF detect_statement_type for the early-exit when there are
    # NO skip-type (Meevo/inventory) pages — those pages contain false signals
    # (e.g. "AMERICAN EXPRESS" in the payment breakdown) that corrupt detection.
    if not has_skip and len(unique_types) <= 1:
        full_type = detect_statement_type(pdf_path)
        if full_type in ('citi_checking', 'citi_savings'):
            citi_types = _citi_account_types(pdf_path)
            if len(citi_types) > 1:
                return [(t, str(pdf_path)) for t in citi_types]
        return [(full_type, str(pdf_path))]

    # Group pages by type, merging all pages of the same financial type together
    # (even if non-consecutive, e.g. Citi pages scattered across the PDF)
    type_to_pages = defaultdict(list)
    for i, pt in enumerate(page_types):
        if pt not in _SKIP_TYPES:
            type_to_pages[pt].append(i)

    financial_types = [t for t in type_to_pages if t != 'unknown']

    if not financial_types:
        # No recognisable financial pages at all
        full_type = detect_statement_type(pdf_path)
        return [(full_type, str(pdf_path))]

    # If only one financial type but skip pages exist, strip them and return a
    # cleaned PDF — do NOT call detect_statement_type on the full PDF here,
    # as skip pages may contain false signals.
    if len(financial_types) == 1:
        stmt_type = financial_types[0]
        if not has_skip:
            # No skip pages — safe to return original path
            return [(stmt_type, str(pdf_path))]
        # Strip Meevo/inventory pages and return cleaned PDF
        page_indices = sorted(type_to_pages[stmt_type])
        writer = pypdf.PdfWriter()
        for pi in page_indices:
            writer.add_page(reader.pages[pi])
        tmp_dir = tempfile.mkdtemp()
        tmp_path = os.path.join(tmp_dir, f'segment_{stmt_type}_clean.pdf')
        with open(tmp_path, 'wb') as f:
            writer.write(f)
        return [(stmt_type, tmp_path)]

    # Write each financial type's pages to a temp PDF
    results = []
    tmp_dir = tempfile.mkdtemp()
    for stmt_type in financial_types:
        page_indices = sorted(type_to_pages[stmt_type])
        writer = pypdf.PdfWriter()
        for pi in page_indices:
            writer.add_page(reader.pages[pi])
        tmp_path = os.path.join(tmp_dir, f'segment_{stmt_type}_{page_indices[0]}.pdf')
        with open(tmp_path, 'wb') as f:
            writer.write(f)
        # Re-detect on the extracted pages for accuracy
        detected = detect_statement_type(tmp_path)
        if detected == 'unknown':
            detected = stmt_type
        results.append((detected, tmp_path))

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

STATEMENT_TYPE_LABELS = {
    'chase_ink':               'Chase Ink Business Credit Card',
    'chase_sapphire':          'Chase Sapphire Credit Card',
    'chase_united':            'Chase United Credit Card',
    'amex':                    'American Express Business',
    'amex_checking':           'American Express Business Checking',
    'bofa_credit':             'Bank of America Business Credit Card',
    'bofa_checking':           'Bank of America Business Checking',
    'bofa_savings':            'Bank of America Business Savings',
    'citi_checking':           'Citi Business Checking',
    'citi_savings':            'Citi Business Savings',
    'citi_visa_costco':        'Citi Costco Anywhere Visa',
    'bmo_credit':               'BMO Business Platinum Rewards Credit Card',
    'bmo_checking':             'BMO Premium Business Checking',
    'northern_trust_checking': 'Northern Trust Business Checking',
    'wells_fargo_checking':    'Wells Fargo Business Checking',
    'wells_fargo_credit':      'Wells Fargo Business Credit Card',
}

def _ask(prompt, required=True, default=None):
    """Prompt user for input, return Decimal for amounts or string for text."""
    while True:
        suffix = f" [{default}]" if default else ""
        val = input(f"  {prompt}{suffix}: ").strip()
        if not val and default is not None:
            return default
        if not val and not required:
            return None
        if not val:
            print("    (required)")
            continue
        return val

def _ask_amount(prompt, required=True):
    """Prompt for a dollar amount, return Decimal."""
    while True:
        val = _ask(prompt, required=required)
        if val is None:
            return Decimal('0')
        try:
            clean = val.replace('$', '').replace(',', '').strip()
            return Decimal(clean)
        except Exception:
            print(f"    Invalid amount: {val!r} — enter a number like 1234.56")

def manual_entry_for_parser(stmt_type_code, client_name):
    """Called automatically when a PDF can't be parsed — pre-fills type and client."""
    stmt_type_label = STATEMENT_TYPE_LABELS.get(stmt_type_code, stmt_type_code)
    print()
    print(f"  Statement type : {stmt_type_label}")
    print(f"  Client         : {client_name or '(unknown)'}")
    if not client_name:
        client_name = input("  Enter client name: ").strip()
    period = _ask("Statement period (e.g. December 15, 2025)", required=False) or ''

    print()
    is_cc = stmt_type_code in ('chase_ink', 'chase_united', 'amex', 'bofa_credit',
                                'citi_visa_costco', 'wells_fargo_credit')

    if is_cc:
        print("Enter amounts from the Account Summary:")
        prev_bal       = _ask_amount("Previous Balance")
        payments       = _ask_amount("Payments")
        credits        = _ask_amount("Credits / Returns", required=False)
        new_charges    = _ask_amount("New Charges (Purchases)")
        finance_charge = _ask_amount("Finance Charge", required=False)
        new_bal        = _ask_amount("New Balance")

        calc = (prev_bal - payments - (credits or Decimal('0'))
                + new_charges + (finance_charge or Decimal('0')))
        ok = abs(calc - new_bal) < Decimal('0.01')

        report = _report_header(stmt_type_label, client_name, statement_date=period)
        report += _summary_block([
            ('Previous Balance',  prev_bal),
            ('Payments',          payments),
            ('Credits / Returns', credits if credits else None),
            ('Purchases',       new_charges),
            ('Finance Charges',    finance_charge if finance_charge else None),
            ('New Balance',       new_bal),
        ])
        report += _balance_check(ok, calc)
    else:
        print("Enter amounts from the Account Summary:")
        beg_bal     = _ask_amount("Beginning Balance")
        deposits    = _ask_amount("Deposits and Credits")
        withdrawals = _ask_amount("Total Withdrawals and Debits")
        checks      = _ask_amount("  Checks", required=False)
        payroll     = _ask_amount("  Payroll", required=False)
        cc_pmts     = _ask_amount("  Credit Card Payments", required=False)
        bank_fees   = _ask_amount("  Bank Fees", required=False)
        end_bal     = _ask_amount("Ending Balance")

        calc = beg_bal + deposits - withdrawals
        ok = abs(calc - end_bal) < Decimal('0.01')

        report = _report_header(stmt_type_label, client_name, statement_date=period)
        report += _summary_block([
            ('Beginning Balance',       beg_bal),
            ('Deposits and Credits',    deposits),
            ('Withdrawals and Debits',  -withdrawals),
            ('  Checks',                -checks if checks else None, 'indent'),
            ('  Payroll',               -payroll if payroll else None, 'indent'),
            ('  Credit Card Payments',  -cc_pmts if cc_pmts else None, 'indent'),
            ('  Bank Fees',             bank_fees if bank_fees else None, 'indent'),
            ('Ending Balance',          end_bal),
        ])
        report += _balance_check(ok, calc)

    report += "\n[Manually entered — no transaction detail available]\n"
    return report


def manual_entry():
    """Interactive manual entry mode for scanned/unreadable statements."""
    print()
    print("=" * 60)
    print("MANUAL ENTRY MODE")
    print("=" * 60)
    print()

    # Choose statement type
    types = list(STATEMENT_TYPE_LABELS.items())
    print("Statement type:")
    for i, (code, label) in enumerate(types, 1):
        print(f"  {i:2}. {label}")
    while True:
        choice = input("  Enter number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(types):
            stmt_type_code, stmt_type_label = types[int(choice) - 1]
            break
        print("  Invalid choice")

    # Client name
    print()
    print("Known clients:")
    clients = sorted(set(
        cfg.get('client_name', '')
        for cfg in _registry._configs.values()
        if cfg.get('client_name')
    ))
    for i, c in enumerate(clients, 1):
        print(f"  {i:2}. {c}")
    print(f"  {len(clients)+1:2}. Other (type manually)")
    while True:
        choice = input("  Enter number or client name: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(clients):
            client_name = clients[int(choice) - 1]
            break
        elif choice.isdigit() and int(choice) == len(clients) + 1:
            client_name = input("  Client name: ").strip()
            break
        elif choice:
            client_name = choice
            break

    # Statement period
    print()
    period = _ask("Statement period (e.g. December 15, 2025 or 12/01/25-12/31/25)", required=False) or ''

    print()
    is_cc = stmt_type_code in ('chase_ink', 'chase_united', 'amex', 'bofa_credit',
                                'citi_visa_costco', 'wells_fargo_credit')

    if is_cc:
        # Credit card fields
        print("Enter amounts from the Account Summary (enter 0 if not applicable):")
        prev_bal      = _ask_amount("Previous Balance")
        payments      = _ask_amount("Payments")
        credits       = _ask_amount("Credits / Returns", required=False)
        new_charges   = _ask_amount("New Charges (Purchases)")
        finance_charge= _ask_amount("Finance Charge", required=False)
        new_bal       = _ask_amount("New Balance")

        # Verify
        calc = prev_bal - payments - (credits or Decimal('0')) + new_charges + (finance_charge or Decimal('0'))
        ok = abs(calc - new_bal) < Decimal('0.01')

        report = _report_header(stmt_type_label, client_name, statement_date=period)
        summary_rows = [
            ('Previous Balance',  prev_bal),
            ('Payments',          payments),
            ('Credits / Returns', credits if credits else None),
            ('Purchases',       new_charges),
            ('Finance Charges',    finance_charge if finance_charge else None),
            ('New Balance',       new_bal),
        ]
        report += _summary_block(summary_rows)
        report += _balance_check(ok, calc)
        report += "\n[Manually entered — no transaction detail available]\n"

    else:
        # Checking account fields
        print("Enter amounts from the Account Summary (enter 0 if not applicable):")
        beg_bal     = _ask_amount("Beginning Balance")
        deposits    = _ask_amount("Deposits and Credits")
        withdrawals = _ask_amount("Withdrawals and Debits (total)")
        checks      = _ask_amount("  Checks", required=False)
        payroll     = _ask_amount("  Payroll", required=False)
        cc_pmts     = _ask_amount("  Credit Card Payments", required=False)
        bank_fees   = _ask_amount("  Bank Fees", required=False)
        end_bal     = _ask_amount("Ending Balance")

        total_deb = withdrawals
        calc = beg_bal + deposits - total_deb
        ok = abs(calc - end_bal) < Decimal('0.01')

        report = _report_header(stmt_type_label, client_name, statement_date=period)
        summary_rows = [
            ('Beginning Balance',        beg_bal),
            ('Deposits and Credits',      deposits),
            ('Withdrawals and Debits',    -total_deb),
            ('  Checks',                  -checks if checks else None, 'indent'),
            ('  Payroll',                 -payroll if payroll else None, 'indent'),
            ('  Credit Card Payments',    -cc_pmts if cc_pmts else None, 'indent'),
            ('  Bank Fees',               bank_fees if bank_fees else None, 'indent'),
            ('Ending Balance',            end_bal),
        ]
        report += _summary_block(summary_rows)
        report += _balance_check(ok, calc)
        report += "\n[Manually entered — no transaction detail available]\n"

    print()
    print(report)

    # Optionally save
    save = input("Save to file? Enter filename or press Enter to skip: ").strip()
    if save:
        if not save.endswith('.txt'):
            save += '.txt'
        with open(save, 'w') as f:
            f.write(report)
        print(f"Saved to {save}")

    print("\n✓ Manual entry complete")


def _suggest_client(text):
    """
    Scan statement text for known client names or similar matches.
    Returns (suggested_name, is_exact) or (None, False).
    """
    text_upper = text.upper()

    # 1. Exact match — known client name appears in text
    for canonical, cfg in _registry._configs.items():
        client_name = cfg.get('client_name', canonical)
        if client_name.upper() in text_upper:
            return client_name, True
        for alias in cfg.get('aliases', []):
            if alias.upper() in text_upper:
                return client_name, True

    # 2. Word-based fuzzy match — 2+ significant words from a known client appear
    stop = {'LLC', 'INC', 'CORP', 'CO', 'THE', 'AND', '&', 'DDS', 'MD', 'STUDIO'}
    best_match = None
    best_score = 0
    for canonical, cfg in _registry._configs.items():
        client_name = cfg.get('client_name', canonical)
        client_words = set(re.sub(r'[^A-Z0-9 ]', '', client_name.upper()).split()) - stop
        matches = sum(1 for w in client_words if len(w) > 2 and w in text_upper)
        if matches >= 2 and matches > best_score:
            best_score = matches
            best_match = client_name

    if best_match:
        return best_match, False

    return None, False
    """Return existing client names that are similar to the given name."""
    name_upper = name.upper()
    results = []
    for canonical, cfg in _registry._configs.items():
        existing = cfg.get('client_name', canonical)
        existing_upper = existing.upper()
        # Check substring match in either direction
        if (name_upper in existing_upper or existing_upper in name_upper):
            results.append(existing)
            continue
        # Check word overlap — if 2+ words match
        name_words = set(re.sub(r'[^A-Z0-9 ]', '', name_upper).split())
        exist_words = set(re.sub(r'[^A-Z0-9 ]', '', existing_upper).split())
        # Ignore common words
        stop = {'LLC', 'INC', 'CORP', 'CO', 'THE', 'AND', '&', 'DDS', 'MD'}
        name_words -= stop
        exist_words -= stop
        if len(name_words & exist_words) >= 2:
            results.append(existing)
    return results


def build_new_client_config(stmt_type, detected_text=''):
    """
    Interactively builds a new client JSON config file.
    Called when a statement is uploaded from an unknown client.
    """
    print()
    print("=" * 60)
    print("NEW CLIENT SETUP")
    print("=" * 60)
    print("This statement belongs to an unrecognized client.")
    print("Let's build the config — it only takes a minute.")
    print()

    # Try to suggest a client from the statement text
    suggested, is_exact = _suggest_client(detected_text) if detected_text else (None, False)

    if suggested and is_exact:
        # High confidence — name found verbatim in statement
        print(f"  Looks like: {suggested}")
        confirm = input("  Is this correct? (y/n): ").strip().lower()
        if confirm == 'y':
            return suggested
        # User says no — fall through to manual entry
        print()

    elif suggested and not is_exact:
        # Lower confidence — fuzzy word match
        print(f"  Possible match: {suggested}")
        confirm = input("  Is this the same client? (y/n): ").strip().lower()
        if confirm == 'y':
            return suggested
        print()

    # Manual entry — pre-fill with suggestion if available
    prompt = f"  Client name"
    if suggested:
        prompt += f" [{suggested}]"
    prompt += ": "
    client_name = (input(prompt).strip() or suggested or '').strip()
    if not client_name:
        print("  Skipping new client setup.")
        return None

    # Final check for similar existing clients
    similar = _similar_clients(client_name)
    if similar:
        print()
        print(f"  ⚠ Similar client already exists: {similar[0]}")
        confirm = input("  Same client? (y/n): ").strip().lower()
        if confirm != 'y':
            pass  # continue creating new
        else:
            existing_cfg = _registry.get_config(similar[0])
            if existing_cfg and client_name not in existing_cfg.get('aliases', []):
                existing_cfg.setdefault('aliases', []).append(client_name)
                import json
                from log_utils import get_clients_dir
                clients_dir = get_clients_dir()
                for p in clients_dir.glob('*.json'):
                    cfg = json.load(open(p))
                    if cfg.get('client_name') == similar[0]:
                        cfg['aliases'] = existing_cfg['aliases']
                        json.dump(cfg, open(p, 'w'), indent=2)
                        _registry._load(clients_dir)
                        print(f"  ✓ Added '{client_name}' as alias for '{similar[0]}'")
                        break
            return similar[0]

    canonical = client_name.upper()

    # Statement type — pre-filled if detected
    print()
    if stmt_type and stmt_type != 'unknown':
        print(f"  Statement type detected: {stmt_type}")
        confirm = input("  Is this correct? (y/n): ").strip().lower()
        if confirm == 'y':
            stmt_types = [stmt_type]
        else:
            stmt_types = _ask_statement_types()
    else:
        stmt_types = _ask_statement_types()

    # Payroll and CC — only relevant for checking accounts
    is_checking = any(t in stmt_types for t in [
        'bofa_checking', 'wells_fargo_checking', 'citi_checking',
        'amex_checking', 'northern_trust_checking'
    ])

    payroll_vendors = []
    if is_checking:
        print()
        print("  Does this client use payroll? (ADP, Square, etc.)")
        has_payroll = input("  (y/n): ").strip().lower() == 'y'
        if has_payroll:
            print("  Common payroll keywords to route to Payroll section:")
            print("  ADP: 'ADP WAGE PAY', 'ADP TAX', 'ADP PAY-BY-PAY'")
            print("  Square: 'Square Inc Payr Tax', 'Square Inc Payr DD', 'IRS Usataxpymt'")
            default = 'ADP WAGE PAY, ADP TAX, ADP PAY-BY-PAY'
            raw = input(f"  Payroll keywords (comma-separated) [{default}]: ").strip()
            payroll_vendors = [v.strip() for v in (raw or default).split(',')]
    if is_checking:
        print()
        print("  Does this client pay credit cards from this account?")
        has_cc = input("  (y/n): ").strip().lower() == 'y'
        if has_cc:
            print("  Keyword that appears in credit card payment transactions")
            print("  (e.g. 'BANK OF AMERICA CREDIT CARD', 'CITI CARD', 'CHASE CREDIT')")
            raw = input("  CC payment keyword(s) (comma-separated): ").strip()
            if raw:
                cc_keywords = [v.strip() for v in raw.split(',')]

    # Vendor rules are added later as you work through statements
    vendor_rules = []

    # Build config
    config = {
        "client_name": client_name,
        "aliases": aliases,
        "canonical_name": canonical,
        "statement_types": stmt_types,
        "cardholders": [],
        "payroll_vendors": payroll_vendors,
        "cc_keywords": cc_keywords,
        "vendor_rules": vendor_rules,
    }

    # Save to clients/
    import json
    from log_utils import get_clients_dir
    clients_dir = get_clients_dir()
    filename = re.sub(r'[^a-z0-9]+', '_', client_name.lower()).strip('_') + '.json'
    filepath = clients_dir / filename

    print()
    print(f"  Saving to: clients/{filename}")
    with open(filepath, 'w') as f:
        json.dump(config, f, indent=2)

    # Reload registry
    _registry._load(clients_dir)

    print(f"  ✓ Client '{client_name}' added successfully!")
    print(f"    Add vendor_rules to clients/{filename} as you work through statements.")
    print()
    return client_name


def _ask_statement_types():
    """Ask user to select statement types from the supported list."""
    types = list(STATEMENT_TYPE_LABELS.items())
    print()
    print("  Select statement type(s):")
    for i, (code, label) in enumerate(types, 1):
        print(f"  {i:2}. {label}")
    selected = []
    while True:
        raw = input("  Enter number(s) comma-separated: ").strip()
        if not raw:
            break
        for part in raw.split(','):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= len(types):
                selected.append(types[int(part)-1][0])
        if selected:
            break
        print("  Invalid selection — try again")
    return selected


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == '--sync-from-sheet':
        import importlib
        _su = importlib.import_module('sheets_updater')
        print("Sync complete: " + str(added) + " new rows added to reconciliation_log.csv")
        return
        return

    if len(sys.argv) < 2 or sys.argv[1] in ('--manual', '-m'):
        if len(sys.argv) >= 2 and sys.argv[1] in ('--manual', '-m'):
            manual_entry()
            return
        print("Usage: python reconcile_comprehensive.py <statement.pdf> [output.txt]")
        print("       python reconcile_comprehensive.py --manual")
        print("       python reconcile_comprehensive.py <statement.pdf> --check-payee 1235='Jane Doe'")
        print()
        print("Supported statement types:")
        print("  Chase Ink Business Credit Card")
        print("  Chase United Credit Card")
        print("  Citi Business Checking")
        print("  Citi Costco Anywhere Visa")
        print("  American Express Business")
        print("  American Express Business Checking")
        print("  BofA Business Checking")
        print("  BofA Business Credit Card")
        print("  BofA Business Savings                (any client)")
        sys.exit(1)

    pdf_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith('--') else None

    # Parse --no-prompt flag — auto-answers 'later' at the QB confirmation prompt
    # so the script can run non-interactively (e.g. from Claude's environment).
    no_prompt = '--no-prompt' in sys.argv

    # Parse --check-payee and --check-date flags
    # Usage: --check-payee 1239='Jane Doe'  --check-date 1239=02/28/26
    # For unnumbered checks use post date as key: --check-payee 03/18/26=Franchise Tax Board
    check_payee_map = {}
    check_date_map = {}
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ('--check-payee', '--check-date') and i + 1 < len(args):
            flag = arg
            next_arg = args[i + 1]
            if '=' in next_arg:
                key, val = next_arg.split('=', 1)
                key = key.strip(); val = val.strip().strip("\'\"")
                if flag == '--check-payee':
                    check_payee_map[key] = val
                else:
                    check_date_map[key] = val
            i += 2
        elif arg.startswith('--check-payee') or arg.startswith('--check-date'):
            flag = '--check-payee' if arg.startswith('--check-payee') else '--check-date'
            rest = arg[len(flag):].lstrip('= ')
            if '=' in rest:
                key, val = rest.split('=', 1)
                key = key.strip(); val = val.strip().strip("\'\"")
                if flag == '--check-payee':
                    check_payee_map[key] = val
                else:
                    check_date_map[key] = val
            i += 1
        else:
            i += 1

    if not Path(pdf_path).exists():
        print(f"Error: File not found: {pdf_path}")
        sys.exit(1)

    print(f"Processing: {pdf_path}")

    parser_map = {
        'chase_ink':                ChaseInkParser,
        'chase_sapphire':           ChaseParser,
        'chase_united':             ChaseUnitedParser,
        'amex':                     AmexStatementParser,
        'amex_checking':            AmexCheckingParser,
        'bofa_credit':              BankOfAmericaCreditCardParser,
        'bofa_checking':            BankOfAmericaCheckingParser,
        'bofa_savings':             BankOfAmericaSavingsParser,
        'citi_checking':            CitiCheckingParser,
        'citi_savings':             CitiSavingsParser,
        'citi_visa_costco':         CitiVisaCostcoParser,
        'bmo_credit':                BMOCreditCardParser,
        'bmo_checking':              BMOCheckingParser,
        'northern_trust_checking':  NorthernTrustCheckingParser,
        'usbank_checking':          USBankCheckingParser,
        'wells_fargo_credit':       WellsFargoCreditCardParser,
        'wells_fargo_checking':     WellsFargoCheckingParser,
    }

    # Split combined PDFs (e.g. files that bundle Meevo + Citi Costco)
    print(f"[Step 2] Splitting PDF into segments...")
    segments = split_combined_pdf(pdf_path)
    tmp_files = [p for (_, p) in segments if p != str(pdf_path)]

    all_reports = []
    _session_stmt_types = []  # tracks (stmt_type, client_name) for each statement processed
    try:
        for stmt_type, seg_path in segments:
            print(f"[Step 4] Detected: {stmt_type}  ({Path(seg_path).name})")

            if stmt_type not in parser_map:
                print(f"  ⚠ Unrecognised statement type: {stmt_type}")
                print(f"    This may be a new bank format that needs a new parser.")
                print(f"    Skipping this segment.")
                continue

            parser = parser_map[stmt_type](seg_path)
            # If client not detected in the segment (e.g. Meevo pages stripped),
            # try to detect from the original full PDF
            if not parser.client_name and seg_path != str(pdf_path):
                original_parser = StatementParser(pdf_path)
                parser.client_name = original_parser.client_name
            if parser.client_name:
                print(f"[Step 6] Client: {parser.client_name}")
            else:
                print(f"[Step 6] ⚠ Client not recognized in this statement.")
                new_name = build_new_client_config(stmt_type, detected_text=parser.text)
                if new_name:
                    parser.client_name = new_name
                    print(f"Client:     {parser.client_name}")
            print(f"[Step 7] Parsing transactions...")
            parser.parse()

            # Vision fallback: if the pdftotext parse didn't tie to the penny
            # (typical for scans / photos / faxed statements), try Claude Vision.
            # Only applies to credit-card statements where the balance equation
            # is well-defined. Checking accounts have a more complex equation
            # involving deposits and would need their own fallback shape.
            _CC_STATEMENT_TYPES = {
                'citi_visa_costco', 'chase_ink', 'chase_united',
                'amex', 'bofa_credit', 'wells_fargo_credit', 'bmo_credit',
            }
            if stmt_type in _CC_STATEMENT_TYPES:
                print(f"[Step 7b] Verifying balance (Vision fallback if needed)...")
                parser._try_vision_fallback()

            # Check if parser extracted usable balance data
            has_data = any([
                getattr(parser, 'previous_balance', None),
                getattr(parser, 'new_balance', None),
                getattr(parser, 'beginning_balance', None),
                getattr(parser, 'ending_balance', None),
            ])

            if not has_data:
                print()
                print(f"⚠ Could not extract balance data from this PDF (likely a scanned image).")
                print(f"  Switching to manual entry mode...")
                report = manual_entry_for_parser(stmt_type, parser.client_name or '')
            elif stmt_type in ('bofa_checking', 'amex_checking', 'northern_trust_checking', 'wells_fargo_checking', 'bmo_checking', 'usbank_checking', 'citi_checking', 'citi_savings'):
                # Merge client-config check_payee_overrides into check_payee_map
                # (CLI args take precedence over config overrides)
                if parser.client_name:
                    cfg = _registry.get_config(parser.client_name) or {}
                    for k, v in cfg.get('check_payee_overrides', {}).items():
                        if k not in check_payee_map:
                            check_payee_map[k] = v
                report = parser.generate_report(check_payee_map, check_date_map)

                # ── Flag unrecognized CC payments ──────────────────────────
                # Any CC payment debit whose issuer has no CC statement in
                # this session gets flagged (ASK CLIENT) and logged as an
                # open manual issue in recon_log.json.
                try:
                    from log_utils import append_manual_issue as _flag
                    cc_stmts_this_session = {
                        s for s in [t[0] for t in _session_stmt_types]
                        if s in ('amex', 'bofa_credit', 'chase_ink',
                                 'chase_united', 'chase_sapphire', 'citi_costco',
                                 'bmo_credit')
                    }
                    cc_payments = getattr(parser, 'credit_card_payments', [])
                    for pmt in cc_payments:
                        vendor = pmt.get('vendor', '').upper()
                        matched = any(
                            kw.upper() in vendor
                            for kw in (cfg.get('cc_keywords', []) if parser.client_name else [])
                        )
                        # Flag if vendor contains a known card issuer but no
                        # statement for that issuer was reconciled this session
                        if 'AMERICAN EXPRESS' in vendor and 'amex' not in cc_stmts_this_session:
                            issue = f"Unrecognized Amex payment ${pmt['amount']:,.2f} on {pmt['date']} — no Amex statement on file (ASK CLIENT)"
                            print(f"  ⚠ {issue}")
                            if parser.client_name:
                                _flag(client=parser.client_name, issue=issue)
                        elif 'CHASE' in vendor and not any(
                            s in cc_stmts_this_session for s in ('chase_ink', 'chase_united', 'chase_sapphire')
                        ):
                            issue = f"Unrecognized Chase payment ${pmt['amount']:,.2f} on {pmt['date']} — no Chase statement on file (ASK CLIENT)"
                            print(f"  ⚠ {issue}")
                            if parser.client_name:
                                _flag(client=parser.client_name, issue=issue)
                except Exception as _e:
                    print(f"  ⚠ CC flag check failed: {_e}")
            else:
                report = parser.generate_report()

            all_reports.append(report)
            _session_stmt_types.append((stmt_type, getattr(parser, 'client_name', '')))

            # ── Balance verification gate — NO SILENT FAILURES ────────────
            print(f"[Step 7c] Checking balance verification...")
            # Every report must pass balance check. If it contains a FAILED
            # line, halt immediately with a clear error rather than continuing.
            # Pass --force to bypass this gate (e.g. when called from qa_reconciliation.py)
            force = '--force' in sys.argv
            if '✗ Balance verification: FAILED' in report and not force:
                print()
                print('!' * 80)
                print('  BALANCE CHECK FAILED — halting.')
                print(f'  Statement: {pdf_path}')
                print(f'  Client:    {getattr(parser, "client_name", "unknown")}')
                print(f'  Type:      {stmt_type}')
                print()
                print('  The report has been printed above. Do NOT enter this in QuickBooks.')
                print('  Investigate the missing transactions before proceeding.')
                print('!' * 80)
                raise SystemExit(1)

            # ── Digest log: IN_PROGRESS (written immediately after parse) ───
            if has_data and parser.client_name:
                try:
                    from log_utils import upsert_recon_log as _upsert_log
                    _beg = getattr(parser, 'beginning_balance',
                           getattr(parser, 'previous_balance', None))
                    _end = getattr(parser, 'ending_balance',
                           getattr(parser, 'new_balance', None))
                    _date = getattr(parser, 'closing_date',
                            getattr(parser, 'statement_date', ''))
                    _beg_f = f"{float(_beg):,.2f}" if _beg is not None else '—'
                    _end_f = f"{float(_end):,.2f}" if _end is not None else '—'
                    _upsert_log(
                        client             = parser.client_name,
                        account_type       = stmt_type,
                        statement_end_date = str(_date) if _date else '',
                        statement          = Path(pdf_path).name,
                        beginning_balance  = _beg_f,
                        ending_balance     = _end_f,
                        difference         = "0.00",
                        status             = "IN_PROGRESS",
                    )
                    print(f"[Step 12a] 📝 Digest log → recon_log.json (IN_PROGRESS)")
                    import subprocess as _sp
                    from log_utils import get_logs_dir as _gld
                    _ld = str(_gld())  # logs live in the private logs dir, not the public repo
                    # Stash any unstaged changes (e.g. the log file just written) so
                    # pull --rebase doesn't refuse to run, then pop them back after.
                    _sp.run(['git', '-C', _ld, 'stash'], capture_output=True)
                    _pr = _sp.run(['git', '-C', _ld, 'pull', '--rebase', 'origin', 'main'],
                                  capture_output=True, text=True)
                    if _pr.returncode != 0:
                        print(f"  ⚠ Git pull failed: {_pr.stderr.strip()}")
                    _sp.run(['git', '-C', _ld, 'stash', 'pop'], capture_output=True)
                    _sp.run(['git', '-C', _ld, 'add', 'recon_log.json'], capture_output=True)
                    _sp.run(['git', '-C', _ld, 'commit', '-m',
                             f'digest: {parser.client_name} {stmt_type} IN_PROGRESS'], capture_output=True)
                    _r = _sp.run(['git', '-C', _ld, 'push'], capture_output=True, text=True)
                    if _r.returncode == 0:
                        print(f"  ✅ Pushed to GitHub")
                    else:
                        print(f"  ⚠ Git push failed: {_r.stderr.strip()}")
                except Exception as _e:
                    print(f"  ⚠ Digest log not updated: {_e}")
            # ────────────────────────────────────────────────────────────────

            # ── Client reconciliation notes ────────────────────────────────
            if has_data and parser.client_name:
                try:
                    from log_utils import get_client_notes
                    _notes = get_client_notes(parser.client_name, stmt_type)
                    if _notes:
                        print('─' * 80)
                        print('  📋 Client notes:')
                        for _note in _notes:
                            print(f'     • {_note}')
                except Exception:
                    pass
            # ───────────────────────────────────────────────────────────────

            # ── QB confirmation prompt ──────────────────────────────────────
            print(report)
            print()
            print('─' * 80)
            if no_prompt:
                answer = 'later'
                print('  [--no-prompt] Auto-answered: later — log written, sheet update deferred.')
            else:
                while True:
                    answer = input('  Have you entered this into QuickBooks? (done / later): ').strip().lower()
                    if answer in ('done', 'later'):
                        break
                    print('  Please type "done" when finished, or "later" to log now and update the sheet when done.')
                if answer == 'later':
                    print('  📋 Logging now — sheet will update next time you run with "done".')
            print('─' * 80)
            print()
            # ───────────────────────────────────────────────────────────────

            # ── Closing-day validation ──────────────────────────────────────
            if has_data and parser.client_name:
                _raw_date = getattr(parser, 'closing_date',
                            getattr(parser, 'statement_date', ''))
                _date_warn = _check_statement_date(
                    str(_raw_date), parser.client_name, stmt_type)
                if _date_warn:
                    print(f"\n  {_date_warn}")
                    if no_prompt:
                        print("  Writing anyway (--no-prompt mode).")
                    else:
                        _cont = input("  Continue writing to logs? [y/N] ").strip().lower()
                        if _cont != 'y':
                            print("  Skipped. Fix the statement date and re-run.")
                            continue
            # ───────────────────────────────────────────────────────────────

            # ── Reconciliation log ──────────────────────────────────────────
            print(f"[Step 12] Writing logs...")
            # Always write to BOTH reconciliation_log.csv AND recon_log.json.
            # "later" logs with IN_PROGRESS status; "done" logs with DONE status.
            # Sheet update only fires on "done".
            if has_data and parser.client_name:
                try:
                    from log_utils import write_both_logs as _write_logs
                    _beg = getattr(parser, 'beginning_balance',
                           getattr(parser, 'previous_balance', None))
                    _end = getattr(parser, 'ending_balance',
                           getattr(parser, 'new_balance', None))
                    _pay = getattr(parser, 'total_payments', None)
                    _date = getattr(parser, 'closing_date',
                            getattr(parser, 'statement_date', ''))
                    _write_logs(
                        client             = parser.client_name,
                        client_name        = parser.client_name,
                        account_type       = stmt_type,
                        statement_end_date = str(_date) if _date else '',
                        statement          = Path(pdf_path).name,
                        beginning_balance  = f"{float(_beg):,.2f}" if _beg is not None else '—',
                        ending_balance     = f"{float(_end):,.2f}" if _end is not None else '—',
                        total_payments     = f"{float(_pay):.2f}" if _pay is not None else '',
                        status             = "DONE" if answer == 'done' else "IN_PROGRESS",
                    )
                    import subprocess as _sp
                    from log_utils import get_logs_dir as _gld
                    _ld = str(_gld())  # logs live in the private logs dir, not the public repo
                    # Stash any unstaged changes (e.g. log files just written) so
                    # pull --rebase doesn't refuse to run, then pop them back after.
                    _sp.run(['git', '-C', _ld, 'stash'], capture_output=True)
                    _pr = _sp.run(['git', '-C', _ld, 'pull', '--rebase', 'origin', 'main'],
                                  capture_output=True, text=True)
                    if _pr.returncode != 0:
                        print(f"  ⚠ Git pull failed: {_pr.stderr.strip()}")
                    _sp.run(['git', '-C', _ld, 'stash', 'pop'], capture_output=True)
                    _sp.run(['git', '-C', _ld, 'add',
                             'reconciliation_log.csv', 'recon_log.json'], capture_output=True)
                    _sp.run(['git', '-C', _ld, 'commit', '-m',
                             f'recon: {parser.client_name} {stmt_type} {"DONE" if answer == "done" else "IN_PROGRESS"}'], capture_output=True)
                    _r = _sp.run(['git', '-C', _ld, 'push'],
                                 capture_output=True, text=True)
                    if _r.returncode == 0:
                        print(f"  ✅ Both logs pushed to GitHub")
                    else:
                        print(f"  ⚠ Git push failed: {_r.stderr.strip()}")
                except Exception as _e:
                    print(f"  ✗ LOG WRITE FAILED — {_e}")
                    print(f"  ✗ reconciliation_log.csv and recon_log.json may be out of sync.")
                    print(f"  ✗ Do not proceed until this is resolved.")

            # ── Google Sheet update ─────────────────────────────────────────
            if has_data and parser.client_name and answer == "done":
                print(f"[Step 14] Updating Google Sheet...")
                try:
                    import sys as _sys, importlib as _il
                    _su = _il.import_module("sheets_updater") if "sheets_updater" in _sys.modules \
                          else _il.import_module("sheets_updater")
                    from log_utils import _normalize_client_key as _nck
                    _client_key = _nck(parser.client_name)
                    _date = getattr(parser, 'closing_date',
                            getattr(parser, 'statement_date', ''))
                    if _date:
                        # Fixed-cell grid update
                        _su.update_sheet(_client_key, stmt_type, str(_date))
                        # Append-only Recon Log tab (robust: never silently skips)
                        _su.append_recon_row(parser.client_name, stmt_type, str(_date))
                except Exception as _e:
                    print(f"  ⚠ Sheet update skipped: {_e}")
            # ───────────────────────────────────────────────────────────────

    finally:
        # Clean up any temp segment files
        for tmp in tmp_files:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    full_output = ('\n\n' + '=' * 80 + '\n\n').join(all_reports)

    if output_path:
        Path(output_path).write_text(full_output)
        print(f"Report saved to: {output_path}")
    else:
        pass  # Reports already printed inline before QB confirmation prompt

    print("\n✓ Reconciliation complete")

    # ── Auto-trigger Google Sheet update ────────────────────────────────────
    # Trigger the update_sheet GitHub Actions workflow so the Reconciliation
    # Tracker always reflects the latest dates without manual intervention.
    if any(s for s, _ in _session_stmt_types):
        try:
            import urllib.request, json as _json, os as _os, time as _time
            pat = _os.environ.get("GITHUB_PAT_BOOKKEEPING", "").strip()
            if not pat:
                with open("/etc/environment") as _f:
                    for _line in _f:
                        if _line.startswith("GITHUB_PAT_BOOKKEEPING="):
                            pat = _line.split("=", 1)[1].strip().strip('"')
                            break
            if pat:
                _headers = {
                    "Authorization": f"token {pat}",
                    "Accept": "application/vnd.github.v3+json",
                    "Content-Type": "application/json",
                }
                # Fire repository_dispatch to trigger sync_tracker.yml
                # in the private Bookkeeping-clients repo
                _dispatch_req = urllib.request.Request(
                    "https://api.github.com/repos/sarah-levine/Bookkeeping-clients"
                    "/dispatches",
                    data=_json.dumps({"event_type": "logs-updated"}).encode(),
                    headers=_headers,
                    method="POST",
                )
                with urllib.request.urlopen(_dispatch_req, timeout=10) as _r:
                    pass
                print("[Step 15] 📊 Sheet update triggered — Reconciliation Tracker will update shortly")
            else:
                print("  ⚠ GITHUB_PAT_BOOKKEEPING not set — sheet not auto-updated")
        except Exception as _e:
            print(f"  ⚠ Sheet update trigger failed: {_e}")
            print(f"  ⚠ Run manually: GitHub → Bookkeeping → Actions → Update Reconciliation Tracker Sheet")


if __name__ == "__main__":
    main()
