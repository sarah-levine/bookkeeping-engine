import sys
import re
import os
import json
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

from parsers.base import StatementParser, _registry, KNOWN_CLIENTS, CLIENT_CANONICAL
from parsers.report import *

class NorthernTrustCheckingParser(StatementParser):
    """
    Northern Trust Basic Business Checking.
    Note: Statements are scanned images — requires PyMuPDF + pytesseract for OCR.
    """
    statement_type = "Northern Trust Basic Business Checking"

    def __init__(self, pdf_path, client_name=None):
        self.pdf_path = pdf_path
        self.client_name = client_name
        self.beginning_balance = None
        self.ending_balance = None
        self.credits = []
        self.debits = []
        self.checks = []
        self.service_fees = Decimal('0')
        self._ocr_text = None
        self.text = self._extract_text()
        if not self.client_name:
            self.client_name = self._detect_client()

    def _extract_text(self):
        """OCR the scanned PDF pages."""
        try:
            import fitz
            from PIL import Image
            import pytesseract
            import io as _io
            doc = fitz.open(self.pdf_path)
            pages = []
            for page in doc:
                mat = fitz.Matrix(2, 2)
                pix = page.get_pixmap(matrix=mat)
                img = Image.frombytes('RGB', [pix.width, pix.height], pix.samples)
                pages.append(pytesseract.image_to_string(img))
            doc.close()
            self._ocr_text = '\n'.join(pages)
            return self._ocr_text
        except Exception as e:
            return ''

    def _detect_client(self):
        # For bank statements, match the account holder name from the top
        # of the statement (first 800 chars) to avoid false positives from
        # payees/vendors mentioned in the transaction list.
        header = self.text[:2500].upper()
        for name in KNOWN_CLIENTS:
            if name.upper() in header:
                return CLIENT_CANONICAL.get(name, name)
        # Fallback: full text search
        text = self.text.upper()
        for name in KNOWN_CLIENTS:
            if name.upper() in text:
                return CLIENT_CANONICAL.get(name, name)
        return None

    def normalize_vendor(self, description):
        result = _registry.normalize_vendor(self.client_name or '', description)
        if result != description:
            return result
        return description.strip()

    def parse(self):
        lines = self.text.split('\n')

        for line in lines:
            if 'Beginning Balance on' in line and self.beginning_balance is None:
                m = re.search(r'([\d,]+\.\d{2})', line)
                if m:
                    self.beginning_balance = Decimal(m.group(1).replace(',', ''))
            if 'Ending Balance on' in line and self.ending_balance is None:
                m = re.search(r'([\d,]+\.\d{2})', line)
                if m:
                    self.ending_balance = Decimal(m.group(1).replace(',', ''))

        # Parse transactions — format:
        #   "ACH Debit ACH DEBIT Square Inc SQ250303 T3QXZF 55.00"
        #   "C74FOYMZZ 03/03 8797583 CCD"   <- continuation has the date
        in_transactions = False
        pending = None  # {'desc': str, 'amount': Decimal}
        year = self._get_statement_year()

        # Load client config for Square line position mapping
        config = _registry.get_config(self.client_name) or {}
        square_order = {entry['position']: entry for entry in config.get('square_line_order', [])}
        square_counter = 0  # tracks which Square transaction we're on

        for line in lines:
            stripped = line.strip()

            if 'Other Items Paid' in stripped:
                in_transactions = True
                continue
            if 'Daily Ledger' in stripped or 'Balance Balance' in stripped:
                in_transactions = False
                continue
            if not in_transactions or not stripped:
                continue
            if stripped in ('Description', 'Amount', 'Description Amount'):
                continue

            # Primary transaction line: starts with "ACH Debit" and ends with amount
            txn_m = re.match(r'^(ACH\s+Debit|ACH\s+Credit|Deposit|Withdrawal)\s+(.+?)\s+([\d,]+\.\d{2})\s*$',
                              stripped, re.IGNORECASE)
            if txn_m:
                txn_type = txn_m.group(1).lower()
                desc = txn_m.group(2).strip()
                amount = Decimal(txn_m.group(3).replace(',', ''))
                pending = {'desc': desc, 'amount': amount, 'is_credit': 'credit' in txn_type or 'deposit' in txn_type}
                continue

            # Continuation line — extract date MM/DD
            if pending:
                date_m = re.search(r'(\d{2}/\d{2})', stripped)
                if date_m:
                    month, day = date_m.group(1).split('/')
                    date_str = f"{month}/{day}/{str(year)[2:]}"
                    vendor = self.normalize_vendor(pending['desc'])
                    # Apply position-based Square QB account mapping
                    memo = ''
                    if 'Square' in vendor and square_order:
                        square_counter += 1
                        mapping = square_order.get(square_counter)
                        if mapping:
                            vendor = mapping['account']
                            memo = mapping.get('memo', '')
                    if pending['is_credit']:
                        self.credits.append({'date': date_str, 'vendor': vendor, 'amount': pending['amount'], 'memo': memo})
                    else:
                        self.debits.append({'date': date_str, 'vendor': vendor, 'amount': -pending['amount'], 'memo': memo})
                    pending = None

    def _get_statement_year(self):
        # Look for 4-digit year in statement period line
        m = re.search(r'(?:Statement Period|through|03/\d{2}/)(\d{4})', self.text)
        if m:
            return int(m.group(1))
        # Fallback: find any 4-digit year >= 2020
        for y in re.findall(r'\b(20\d{2})\b', self.text):
            return int(y)
        return 2025

    def generate_report(self, check_payee_map=None, check_date_map=None):
        total_debits  = sum(t['amount'] for t in self.debits)
        total_credits = sum(t['amount'] for t in self.credits)

        calc = self.beginning_balance + total_credits + total_debits
        ok = abs(calc - self.ending_balance) < Decimal('0.01')

        period = ''
        m = re.search(r'(\d{2}/\d{2}/\d{2,4})\s+through\s+(\d{2}/\d{2}/\d{2,4})', self.text)
        if m:
            period = f"{m.group(1)} - {m.group(2)}"

        report = _report_header(self.statement_type, self.client_name, statement_date=period)
        report += _summary_block([
            ('Beginning Balance',      self.beginning_balance),
            ('Deposits and Credits',   total_credits),
            ('Withdrawals and Debits', total_debits),
            ('Ending Balance',         self.ending_balance),
        ])
        report += _balance_check(ok, calc)

        if self.credits:
            credit_rows = [{'vendor': c['vendor'], 'date': c['date'], 'amount': c['amount'], 'count': 1}
                           for c in self.credits]
            report += _deposits_section(credit_rows, total_credits)

        debit_rows = [{'vendor': t['vendor'], 'date': t['date'], 'amount': t['amount'], 'count': 1}
                      for t in sorted(self.debits, key=lambda x: _safe_date_key(x['date']))]
        report += _individual_section(debit_rows, total_debits, 'WITHDRAWALS AND DEBITS')

        return report


# ═══════════════════════════════════════════════════════════════════════════════
# SHARED REPORT HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

